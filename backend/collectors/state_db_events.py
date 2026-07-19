# -*- coding: utf-8 -*-
"""state.db 事件采集器 — 从 Hermes 会话数据库反向抽取工具调用事件。

背景：hermes-observatory-hook 只在 Hermes CLI/gateway 进程里生效；hermes-webui
自己没触发 hermes_cli.plugins.discover_plugins()，所以 WebUI 里的所有工具调用
都不会走 hook，evolution-events.jsonl 从此不再增长。

本采集器直接读 state.db 的 messages 表，把 role='assistant' 消息里的 tool_calls
JSON 反解出来，过滤出 skill_manage/skill_view/memory 三类进化相关工具，并把下一条
role='tool' 消息里的 result 关联进去，产出与原 hook 相同 schema 的事件。

架构原则：只读——不写 state.db 也不写 evolution-events.jsonl。
"""
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .base import BaseCollector, CollectorResult

# 与 hook (handler.py) 保持一致：只关心这三类工具
EVOLUTION_TOOLS = {"memory", "skill_manage", "skill_view"}

# 简单进程内 TTL 缓存，避免每次 /api/timeline 都全表扫
_CACHE: dict[str, tuple[float, list]] = {}
_CACHE_TTL_SEC = 5.0


class StateDbEventsCollector(BaseCollector):
    """从 state.db 反向抽取进化事件。"""

    name = "state_db_events"
    path_pattern = "state.db"
    known_schema_version = "v1"

    def collect(self, limit: int = 500) -> CollectorResult:
        """返回 events 列表，格式与 evolution-events.jsonl 每行一致。

        limit: 最多返回多少条最新事件（默认 500，够时间线视图用了）。
        """
        ts = datetime.now(timezone.utc).isoformat() + "Z"
        result = CollectorResult(
            source=self.name, data={}, schema_version=self.known_schema_version, timestamp=ts
        )

        db_path = self.hermes_home / "state.db"
        if not db_path.exists():
            result.status = "unavailable"
            result.error = "state.db not found"
            result.data = {"events": []}
            return result

        cache_key = f"{db_path}:{limit}"
        now = time.monotonic()
        cached = _CACHE.get(cache_key)
        if cached and now - cached[0] < _CACHE_TTL_SEC:
            result.data = {"events": cached[1]}
            return result

        try:
            events = self._extract_events(db_path, limit=limit)
            _CACHE[cache_key] = (now, events)
            result.data = {"events": events}
        except Exception as e:
            result.status = "error"
            result.error = str(e)
            result.data = {"events": []}
        return result

    # ─────────────────────────────────────────────────────────────
    # 抽取核心
    # ─────────────────────────────────────────────────────────────

    def _extract_events(self, db_path: Path, limit: int) -> List[dict]:
        # 每次都用短连接（避免持有锁）
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            # 抓最近 N 条 assistant 消息含 tool_calls
            # 用 id DESC + LIMIT 是最经济的方式（id 是主键，天然索引）
            # 500 * 2 = 1000 已经足够覆盖数千次工具调用
            rows = conn.execute(
                """
                SELECT id, session_id, timestamp, tool_calls
                FROM messages
                WHERE role='assistant'
                  AND tool_calls IS NOT NULL
                  AND tool_calls != ''
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit * 4,),
            ).fetchall()

            if not rows:
                return []

            # 一次性把这些 assistant 消息之后的 tool 结果全捞出来（按 tool_call_id 索引）
            # tool_call_id 在 role='tool' 消息里
            min_id = rows[-1]["id"]
            tool_results = self._load_tool_results(conn, min_id)

            # 会话级 turn 计数：同一 session 内按时间顺序 +1
            session_turns: dict[str, int] = {}
            events: List[dict] = []

            # 从最老到最新遍历，方便正确累计 turn
            for row in reversed(rows):
                try:
                    tc = json.loads(row["tool_calls"])
                except Exception:
                    continue
                if isinstance(tc, dict):
                    tc = [tc]
                if not isinstance(tc, list):
                    continue

                session_id = row["session_id"] or ""
                session_turns[session_id] = session_turns.get(session_id, 0) + 1
                turn_no = session_turns[session_id]

                for call in tc:
                    ev = self._call_to_event(
                        call=call,
                        session_id=session_id,
                        timestamp=row["timestamp"],
                        turn=turn_no,
                        tool_results=tool_results,
                    )
                    if ev is not None:
                        events.append(ev)

            # 按时间倒序取 limit 条
            events.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
            return events[:limit]
        finally:
            conn.close()

    def _load_tool_results(self, conn, min_msg_id: int) -> dict:
        """把 tool_call_id → tool result 建成 dict。"""
        try:
            rows = conn.execute(
                """
                SELECT tool_call_id, tool_name, content
                FROM messages
                WHERE role='tool'
                  AND id >= ?
                  AND tool_call_id IS NOT NULL
                  AND tool_call_id != ''
                """,
                (min_msg_id,),
            ).fetchall()
            return {
                r["tool_call_id"]: {"tool_name": r["tool_name"], "content": r["content"]}
                for r in rows
            }
        except Exception:
            return {}

    def _call_to_event(
        self,
        call: dict,
        session_id: str,
        timestamp: float,
        turn: int,
        tool_results: dict,
    ) -> Optional[dict]:
        """把一条 tool_call 转成事件。返回 None 表示跳过。"""
        try:
            fn = call.get("function") or {}
            tool_name = fn.get("name") or call.get("name")
            if tool_name not in EVOLUTION_TOOLS:
                return None

            # arguments 可能是 str 也可能是 dict
            raw_args = fn.get("arguments") if isinstance(fn, dict) else call.get("arguments")
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args)
                except Exception:
                    args = {"_raw": raw_args[:200]}
            elif isinstance(raw_args, dict):
                args = raw_args
            else:
                args = {}

            action = args.get("action", "")

            # 结果关联（可选，找不到就空）
            call_id = call.get("id")
            result_hint = ""
            if call_id and call_id in tool_results:
                result_content = tool_results[call_id].get("content", "")
                # 截断，避免时间线载荷过大
                result_hint = str(result_content)[:200]

            # 事件类型 + 描述（与 handler.py 同规则）
            if tool_name == "memory":
                event_type = "memory"
                desc = self._describe_memory(action, args)
            elif tool_name == "skill_manage":
                event_type = "skill"
                desc = self._describe_skill(action, args)
            elif tool_name == "skill_view":
                event_type = "skill"
                desc = f"查看技能: {args.get('name', 'unknown')}"
            else:
                event_type = "unknown"
                desc = f"{tool_name}: {action}"

            iso_ts = datetime.fromtimestamp(timestamp, timezone.utc).isoformat() + "Z"

            return {
                "timestamp": iso_ts,
                "type": event_type,
                "tool": tool_name,
                "action": action,
                "desc": desc,
                "origin": "state_db_backfill",  # 标明来自反向重建
                "session": session_id,
                "turn": turn,
                "args": self._safe_args(args),
                "result_hint": result_hint,
                "profile": self._infer_profile_name(),
            }
        except Exception:
            return None

    # ─────────────────────────────────────────────────────────────
    # 与 handler.py 保持描述文本一致
    # ─────────────────────────────────────────────────────────────

    def _describe_memory(self, action: str, args: dict) -> str:
        target = args.get("target", "memory")
        if action == "add":
            content = str(args.get("content", ""))[:80]
            return f"添加 {target} 条目: {content}..."
        if action == "replace":
            return f"替换 {target} 条目"
        if action == "remove":
            return f"移除 {target} 条目"
        # operations 批量模式
        if args.get("operations"):
            n = len(args["operations"])
            return f"批量 memory 操作 ({n} 条)"
        return f"memory {action}" if action else "memory 操作"

    def _describe_skill(self, action: str, args: dict) -> str:
        name = args.get("name", "unknown")
        mapping = {
            "create": f"创建新技能: {name}",
            "patch": f"修补技能: {name}",
            "edit": f"编辑技能: {name}",
            "delete": f"删除技能: {name}",
            "write_file": f"写入技能辅助文件: {name}",
            "remove_file": f"删除技能辅助文件: {name}",
        }
        return mapping.get(action, f"skill_manage {action}: {name}")

    def _safe_args(self, args: dict) -> dict:
        """截断长字段避免时间线载荷过大（与 hook 逻辑一致）。"""
        safe = {}
        for k, v in args.items():
            if k in ("content", "new_string", "file_content"):
                safe[k] = str(v)[:200]
            elif k in ("old_string", "old_text"):
                safe[k] = str(v)[:100]
            else:
                safe[k] = v
        return safe

    def _infer_profile_name(self) -> str:
        hh = self.hermes_home
        if hh.parent.name == "profiles":
            return hh.name
        return "default"
