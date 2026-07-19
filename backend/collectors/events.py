# -*- coding: utf-8 -*-
"""进化事件采集器 — 读取 evolution-events.jsonl 并合并 state.db 反向重建事件

事件源：
  1. 当前 profile 的 <hermes_home>/logs/evolution-events.jsonl（hook 产出）
  2. 所有兄弟 profile 的 logs/evolution-events.jsonl（跨 profile 混排）
  3. hermes root 的 logs/evolution-events.jsonl（default profile）
  4. state.db 反向重建的工具调用事件（origin='state_db_backfill'）——
     覆盖 hermes-webui 场景下 hook 无法生效的空白期

如 all_profiles=True，采集器合并所有 profile 的 jsonl；每个 profile 都会额外
读一遍其 state.db 抽取事件。合并后按 (tool, session, timestamp) 去重（防止
未来 hook 恢复工作时与 state_db_backfill 事件重复），再按时间倒序返回。
"""
import json
from pathlib import Path
from datetime import datetime, timezone

from .base import BaseCollector, CollectorResult
from .state_db_events import StateDbEventsCollector
from .fs_change import FsChangeCollector


class EventsCollector(BaseCollector):
    name = "events"
    path_pattern = "logs/evolution-events.jsonl"
    known_schema_version = "v1"

    # 是否跨 profile 混排（前端可通过 /api/timeline?all_profiles=true 触发）
    def collect(self, all_profiles: bool = False) -> CollectorResult:
        ts = datetime.now(timezone.utc).isoformat() + "Z"
        result = CollectorResult(
            source=self.name, data={}, schema_version=self.known_schema_version, timestamp=ts
        )

        files = self._enumerate_event_files(all_profiles=all_profiles)
        if not files:
            result.data = {"events": [], "total": 0, "sources": []}
            return result

        try:
            events = []
            sources = []
            # ─── 1. jsonl 事件（hook 产出，历史遗留数据）───
            for pf_name, fp in files:
                exists = fp.exists()
                sources.append({
                    "profile": pf_name,
                    "file": str(fp),
                    "exists": exists,
                    "size": fp.stat().st_size if exists else 0,
                })
                if not exists:
                    continue
                for line in fp.read_text(encoding="utf-8").strip().split("\n"):
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                        ev.setdefault("profile", pf_name)
                        events.append(ev)
                    except json.JSONDecodeError:
                        continue

            # ─── 2. state.db 反向重建事件（补齐 hook 未生效期间的空白）───
            for pf_name, pf_home in self._enumerate_profile_homes(all_profiles=all_profiles):
                try:
                    sc = StateDbEventsCollector(pf_home)
                    sr = sc.collect(limit=500)
                    db_path = pf_home / "state.db"
                    sources.append({
                        "profile": pf_name,
                        "file": str(db_path),
                        "exists": db_path.exists(),
                        "size": db_path.stat().st_size if db_path.exists() else 0,
                        "kind": "state_db",
                    })
                    if sr.status == "ok":
                        for ev in sr.data.get("events", []):
                            ev.setdefault("profile", pf_name)
                            events.append(ev)
                except Exception:
                    # 单 profile 抽取失败不影响整体，继续下一个
                    continue

            # ─── 3. 文件系统变更事件（识别绕过工具的手动编辑）───
            for pf_name, pf_home in self._enumerate_profile_homes(all_profiles=all_profiles):
                try:
                    fc = FsChangeCollector(pf_home)
                    fr = fc.collect()
                    if fr.status == "ok":
                        for ev in fr.data.get("events", []):
                            ev.setdefault("profile", pf_name)
                            events.append(ev)
                except Exception:
                    continue

            # ─── 4. 去重：以 (tool, session, timestamp 精确到秒) 为 key ───
            # hook 事件与 state_db_backfill 事件在同一次工具调用可能同时存在，
            # 优先保留 hook 事件（origin != state_db_backfill）。
            events = self._dedupe(events)
            events.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
            result.data = {"events": events, "total": len(events), "sources": sources}
        except Exception as e:
            result.status = "error"
            result.error = str(e)
        return result

    def _dedupe(self, events: list) -> list:
        seen: dict[tuple, dict] = {}
        for ev in events:
            ts = str(ev.get("timestamp", ""))[:19]  # 精确到秒
            key = (ev.get("tool", ""), ev.get("session", ""), ts, ev.get("action", ""))
            existing = seen.get(key)
            if existing is None:
                seen[key] = ev
                continue
            # 冲突：优先保留非 state_db_backfill（即真正的 hook 事件）
            if existing.get("origin") == "state_db_backfill" and ev.get("origin") != "state_db_backfill":
                seen[key] = ev
        return list(seen.values())

    def _enumerate_profile_homes(self, all_profiles: bool = False):
        """列出 (profile_name, hermes_home_path) 对——供 state.db 采集用。"""
        hh = self.hermes_home
        if not all_profiles:
            yield (self._infer_profile_name(hh), hh)
            return

        # 跨 profile 模式
        if hh.parent.name == "profiles":
            root = hh.parent.parent
        else:
            root = hh
        yield ("default", root)
        profiles_dir = root / "profiles"
        if profiles_dir.exists():
            for pf_dir in sorted(profiles_dir.iterdir()):
                if pf_dir.is_dir():
                    yield (pf_dir.name, pf_dir)

    def _enumerate_event_files(self, all_profiles: bool = False):
        """列出所有 (profile_name, path) 对。
        - all_profiles=False：只当前 hermes_home
        - all_profiles=True：hermes root + 所有 profiles/<name>/
        """
        out = []
        hh = self.hermes_home

        if not all_profiles:
            # 单 profile 模式：hh 就是要读的
            pf = self._infer_profile_name(hh)
            out.append((pf, hh / "logs" / "evolution-events.jsonl"))
            return out

        # 跨 profile 模式：找 hermes root
        # hh 可能是 ~/.hermes（default）也可能是 ~/.hermes/profiles/<name>
        if hh.parent.name == "profiles":
            root = hh.parent.parent  # ~/.hermes
        else:
            root = hh
        # default = 根目录
        out.append(("default", root / "logs" / "evolution-events.jsonl"))
        # 其他 profiles
        profiles_dir = root / "profiles"
        if profiles_dir.exists():
            for pf_dir in sorted(profiles_dir.iterdir()):
                if pf_dir.is_dir():
                    out.append((pf_dir.name, pf_dir / "logs" / "evolution-events.jsonl"))
        return out

    def _infer_profile_name(self, hh: Path) -> str:
        if hh.parent.name == "profiles":
            return hh.name
        return "default"

    def append_event(self, event: dict) -> bool:
        """追加一条进化事件到 jsonl 文件（供 Plugin Hook 调用；写入本 profile）"""
        events_file = self.hermes_home / "logs" / "evolution-events.jsonl"
        try:
            events_file.parent.mkdir(parents=True, exist_ok=True)
            with open(events_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
            return True
        except Exception:
            return False
