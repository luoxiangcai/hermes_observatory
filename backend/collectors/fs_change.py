# -*- coding: utf-8 -*-
"""文件系统变更采集器 — 监听 memory / user_profile / skills 的手动编辑。

针对场景：用户直接编辑 memories/MEMORY.md, memories/USER.md 或 skills/**/SKILL.md
（不经过 memory / skill_manage 工具），从 state.db 是抓不到的。本采集器在文件系统
层面对这些文件做增量 hash 快照对比，识别未经 tool_call 的手动修改，产出事件。

## 监听清单（可扩展）
- `memories/MEMORY.md`  → 类型 `memory`，块分隔 `\\n§\\n`
- `memories/USER.md`    → 类型 `user_profile`，块分隔 `\\n§\\n`
- `skills/**/SKILL.md`  → 类型 `skill`，unified diff 前 N 行

## Baseline 存放
`<hermes_home>/logs/observatory-snapshots/`
  - `<sanitized_path>.snapshot.json` — 上一次的内容 hash + 完整内容
    （首次运行时建立 baseline，不产事件）

## Tool_call 去重
读 state.db 最近 60 秒的 memory / skill_manage 工具调用，用 ±5s 窗口关联：
  - 匹配上 → 不产事件（state_db_events 已经报了）
  - 匹配不上 → 产事件，`origin='manual_edit'` 入时间线

架构原则：只读 Hermes 数据文件（对比 baseline 时读），只写 observatory 自己的
snapshot 目录（`logs/observatory-snapshots/`——logs 目录已经是 observatory 事件
数据的家，不算越界）。
"""
import difflib
import hashlib
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .base import BaseCollector, CollectorResult

# 监听清单：(相对 hermes_home 的 glob, 事件类型, diff 模式)
# diff 模式：
#   - "sectioned"：以 `§` 分块的 markdown（用于 MEMORY.md / USER.md）
#   - "textual"：unified diff（用于 SKILL.md）
WATCH_LIST = [
    ("memories/MEMORY.md", "memory", "sectioned"),
    ("memories/USER.md", "user_profile", "sectioned"),
    ("skills/**/SKILL.md", "skill", "textual"),
]

TOOL_CALL_MATCH_WINDOW_SEC = 5.0   # 文件变更与 tool_call 时间戳的匹配窗口
TOOL_CALL_LOOKBACK_SEC = 60.0      # 从 state.db 抓过去多久的 tool_call

# 进程内 5 秒 TTL 缓存
_CACHE: dict[str, tuple[float, list]] = {}
_CACHE_TTL_SEC = 5.0


class FsChangeCollector(BaseCollector):
    """文件系统层面的手动编辑侦测器。"""

    name = "fs_change"
    path_pattern = ""  # 多个文件，用 WATCH_LIST 管理
    known_schema_version = "v1"

    def collect(self) -> CollectorResult:
        ts = datetime.now(timezone.utc).isoformat() + "Z"
        result = CollectorResult(
            source=self.name, data={}, schema_version=self.known_schema_version, timestamp=ts
        )

        cache_key = str(self.hermes_home)
        now = time.monotonic()
        cached = _CACHE.get(cache_key)
        if cached and now - cached[0] < _CACHE_TTL_SEC:
            result.data = {"events": cached[1]}
            return result

        try:
            events = self._scan_and_diff()
            _CACHE[cache_key] = (now, events)
            result.data = {"events": events}
        except Exception as e:
            result.status = "error"
            result.error = str(e)
            result.data = {"events": []}
        return result

    # ─────────────────────────────────────────────────────────────
    # 核心扫描
    # ─────────────────────────────────────────────────────────────

    def _scan_and_diff(self) -> list:
        snapshot_dir = self.hermes_home / "logs" / "observatory-snapshots"
        snapshot_dir.mkdir(parents=True, exist_ok=True)

        # 收集所有变更（先不判断是否手动，全收，最后再和 tool_call 去重）
        raw_changes = []
        for pattern, ev_type, diff_mode in WATCH_LIST:
            for file_path in self._resolve_pattern(pattern):
                try:
                    change = self._detect_change(file_path, snapshot_dir, ev_type, diff_mode)
                    if change is not None:
                        raw_changes.append(change)
                except Exception:
                    continue

        if not raw_changes:
            return []

        # 拉最近的 memory / skill_manage tool_call，用于去重
        recent_calls = self._load_recent_tool_calls()

        events = []
        profile_name = self._infer_profile_name()
        for ch in raw_changes:
            if self._matches_tool_call(ch, recent_calls):
                # tool 调用产生的变更 → 让 state_db_events 采集器负责，这里不重复
                continue
            events.append(self._to_event(ch, profile_name))

        return events

    def _resolve_pattern(self, pattern: str) -> list[Path]:
        """把 glob pattern 展开成实际文件列表。"""
        # Path.glob() 处理 ** 需要用 rglob 或者传绝对路径 glob
        if "**" in pattern:
            # 拆成 base + relative_glob
            parts = pattern.split("/**/")
            if len(parts) == 2:
                base = self.hermes_home / parts[0]
                if not base.exists():
                    return []
                return list(base.rglob(parts[1]))
        # 普通路径
        path = self.hermes_home / pattern
        if path.exists() and path.is_file():
            return [path]
        return []

    def _detect_change(
        self, file_path: Path, snapshot_dir: Path, ev_type: str, diff_mode: str
    ) -> Optional[dict]:
        """检测单个文件是否变化。首次运行 → 建 baseline 但不返回事件。"""
        content = file_path.read_text(encoding="utf-8", errors="replace")
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        mtime = file_path.stat().st_mtime

        snap_path = snapshot_dir / (self._sanitize(file_path) + ".snapshot.json")
        old_snap = None
        if snap_path.exists():
            try:
                old_snap = json.loads(snap_path.read_text(encoding="utf-8"))
            except Exception:
                old_snap = None

        # 更新 baseline（无论是否首次）
        new_snap = {
            "path": str(file_path.relative_to(self.hermes_home)),
            "hash": content_hash,
            "mtime": mtime,
            "content": content,
            "updated_at": datetime.now(timezone.utc).isoformat() + "Z",
        }
        snap_path.write_text(json.dumps(new_snap, ensure_ascii=False), encoding="utf-8")

        # 首次 → baseline 已建，返回 None
        if old_snap is None:
            return None
        # hash 没变 → 无事件
        if old_snap.get("hash") == content_hash:
            return None

        old_content = old_snap.get("content", "")
        diff = self._compute_diff(old_content, content, diff_mode)

        return {
            "file": str(file_path.relative_to(self.hermes_home)),
            "ev_type": ev_type,
            "diff_mode": diff_mode,
            "diff": diff,
            "mtime": mtime,
            "timestamp": datetime.fromtimestamp(mtime, timezone.utc).isoformat() + "Z",
        }

    def _sanitize(self, path: Path) -> str:
        """把绝对/相对路径转成 snapshot 文件名安全字符串。"""
        rel = str(path.relative_to(self.hermes_home))
        return re.sub(r"[^\w.-]", "_", rel)

    # ─────────────────────────────────────────────────────────────
    # Diff 计算
    # ─────────────────────────────────────────────────────────────

    def _compute_diff(self, old: str, new: str, mode: str) -> dict:
        if mode == "sectioned":
            return self._diff_sectioned(old, new)
        else:
            return self._diff_textual(old, new)

    def _diff_sectioned(self, old: str, new: str) -> dict:
        """按 `§` 分块，做 set 级 diff（每条完整规则视为一个不可拆分的原子）。"""
        old_blocks = [b.strip() for b in old.split("\n§\n") if b.strip()]
        new_blocks = [b.strip() for b in new.split("\n§\n") if b.strip()]
        old_set = set(old_blocks)
        new_set = set(new_blocks)
        added = [self._truncate(b, 200) for b in new_blocks if b not in old_set]
        removed = [self._truncate(b, 200) for b in old_blocks if b not in new_set]
        return {
            "added": added,
            "removed": removed,
            "added_count": len(added),
            "removed_count": len(removed),
            "old_total": len(old_blocks),
            "new_total": len(new_blocks),
        }

    def _diff_textual(self, old: str, new: str) -> dict:
        """unified diff，截前 40 行。"""
        diff_lines = list(
            difflib.unified_diff(
                old.splitlines(keepends=False),
                new.splitlines(keepends=False),
                lineterm="",
                n=2,
            )
        )
        truncated = diff_lines[:40]
        old_lines = old.count("\n") + 1
        new_lines = new.count("\n") + 1
        return {
            "unified_diff": "\n".join(truncated),
            "diff_truncated": len(diff_lines) > 40,
            "old_lines": old_lines,
            "new_lines": new_lines,
        }

    def _truncate(self, s: str, limit: int) -> str:
        s = s.strip()
        if len(s) <= limit:
            return s
        return s[:limit] + "…"

    # ─────────────────────────────────────────────────────────────
    # Tool_call 去重
    # ─────────────────────────────────────────────────────────────

    def _load_recent_tool_calls(self) -> list:
        """从 state.db 抓最近 60 秒内的 memory / skill_manage 工具调用时间戳列表。"""
        db_path = self.hermes_home / "state.db"
        if not db_path.exists():
            return []

        since_ts = time.time() - TOOL_CALL_LOOKBACK_SEC
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                rows = conn.execute(
                    """
                    SELECT timestamp, tool_calls FROM messages
                    WHERE role='assistant'
                      AND tool_calls IS NOT NULL
                      AND tool_calls != ''
                      AND timestamp >= ?
                    ORDER BY timestamp DESC
                    """,
                    (since_ts,),
                ).fetchall()
            finally:
                conn.close()

            calls = []
            for ts, tc_raw in rows:
                try:
                    tc = json.loads(tc_raw)
                    if isinstance(tc, dict):
                        tc = [tc]
                    for c in tc:
                        fn = c.get("function") or {}
                        name = fn.get("name") or c.get("name")
                        if name in ("memory", "skill_manage"):
                            calls.append({"tool": name, "timestamp": ts})
                except Exception:
                    continue
            return calls
        except Exception:
            return []

    def _matches_tool_call(self, change: dict, calls: list) -> bool:
        """在 tool_call 时间戳列表里找匹配。"""
        # memory/user_profile 类型 → 匹配 memory tool_call
        # skill 类型 → 匹配 skill_manage tool_call
        ev_type = change["ev_type"]
        if ev_type in ("memory", "user_profile"):
            expected_tool = "memory"
        elif ev_type == "skill":
            expected_tool = "skill_manage"
        else:
            return False

        target_ts = change["mtime"]
        for c in calls:
            if c["tool"] != expected_tool:
                continue
            if abs(c["timestamp"] - target_ts) <= TOOL_CALL_MATCH_WINDOW_SEC:
                return True
        return False

    # ─────────────────────────────────────────────────────────────
    # 事件生成
    # ─────────────────────────────────────────────────────────────

    def _to_event(self, change: dict, profile_name: str) -> dict:
        ev_type = change["ev_type"]
        diff = change["diff"]
        diff_mode = change["diff_mode"]

        # 描述文本
        if diff_mode == "sectioned":
            added = diff.get("added_count", 0)
            removed = diff.get("removed_count", 0)
            parts = []
            if added:
                parts.append(f"新增 {added} 条")
            if removed:
                parts.append(f"删除 {removed} 条")
            action_word = " / ".join(parts) if parts else "变更"
            file_label = "MEMORY.md" if ev_type == "memory" else "USER.md"
            desc = f"手动编辑 {file_label}：{action_word}"
        else:
            skill_name = self._extract_skill_name(change["file"])
            desc = f"手动编辑技能: {skill_name}"

        return {
            "timestamp": change["timestamp"],
            "type": ev_type,
            "tool": "manual_edit",
            "action": "edit",
            "desc": desc,
            "origin": "manual_edit",
            "session": "",
            "turn": 0,
            "args": {"file": change["file"]},
            "diff": diff,
            "profile": profile_name,
        }

    def _extract_skill_name(self, rel_path: str) -> str:
        # skills/<category>/<name>/SKILL.md 或 skills/<name>/SKILL.md
        parts = rel_path.split("/")
        if len(parts) >= 2:
            return parts[-2]  # 倒数第二段
        return rel_path

    def _infer_profile_name(self) -> str:
        hh = self.hermes_home
        if hh.parent.name == "profiles":
            return hh.name
        return "default"
