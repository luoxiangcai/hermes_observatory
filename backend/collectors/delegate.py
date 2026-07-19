# -*- coding: utf-8 -*-
"""Delegate 采集器 — 读取 delegate_task 子代理委派日志

数据来源：
- ~/.hermes/logs/agent.log 中的 delegate_task 相关行
- ~/.hermes/state.db 中的 background delegation 完成事件
"""
import sqlite3
import re
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

from .base import BaseCollector, CollectorResult


class DelegateCollector(BaseCollector):
    name = "delegate"
    path_pattern = "logs/agent.log"
    known_schema_version = "v1"

    # 匹配 agent.log 中的 delegate_task 相关行
    DELEGATE_PATTERN = re.compile(
        r"(\d{4}-\d{2}-\d{2}.*?)(?:INFO|DEBUG|WARNING).*?"
        r"(delegate_task|subagent|delegation|child.*agent|spawn.*worker|"
        r"kanban.*spawn|kanban.*claim|kanban.*complete|kanban.*block)"
        r"[:\s]*(.*)",
        re.IGNORECASE,
    )

    def collect(self) -> CollectorResult:
        ts = datetime.now(timezone.utc).isoformat() + "Z"
        result = CollectorResult(
            source=self.name, data={}, schema_version=self.known_schema_version, timestamp=ts
        )

        log_events = self._parse_agent_log()
        bg_delegations = self._get_background_delegations()

        if not log_events and not bg_delegations:
            result.status = "unavailable"
            result.error = "no delegation data found"
            return result

        # 合并并按时间排序
        all_events = log_events + bg_delegations
        all_events.sort(key=lambda e: e.get("timestamp", ""), reverse=True)

        result.data = {
            "events": all_events[:200],  # 最近 200 条
            "total": len(all_events),
            "stats": self._compute_stats(all_events),
        }
        return result

    def _parse_agent_log(self) -> list:
        log_path = self.hermes_home / "logs" / "agent.log"
        if not log_path.exists():
            return []

        events = []
        try:
            # 只读最后 5000 行避免内存爆炸
            lines = log_path.read_text(encoding="utf-8", errors="replace").split("\n")[-5000:]
            for line in lines:
                m = self.DELEGATE_PATTERN.search(line)
                if m:
                    timestamp_str = m.group(1).strip()
                    event_type = m.group(2).lower().strip()
                    detail = m.group(3).strip()

                    events.append({
                        "timestamp": timestamp_str,
                        "type": event_type,
                        "detail": detail[:300],
                        "source": "agent.log",
                    })
        except Exception:
            pass
        return events

    def _get_background_delegations(self) -> list:
        db_path = self.hermes_home / "state.db"
        if not db_path.exists():
            return []

        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            # 查找 background delegation 完成事件
            cursor = conn.execute(
                "SELECT id, session_id, created_at, metadata "
                "FROM messages WHERE role='tool' AND content LIKE '%delegate%' "
                "ORDER BY created_at DESC LIMIT 100"
            )
            events = []
            for row in cursor.fetchall():
                d = dict(row)
                events.append({
                    "timestamp": d.get("created_at", ""),
                    "type": "background_delegation",
                    "detail": f"session={d.get('session_id','')}",
                    "source": "state.db",
                })
            conn.close()
            return events
        except Exception:
            return []

    def _compute_stats(self, events: list) -> dict:
        type_counts = defaultdict(int)
        for e in events:
            type_counts[e.get("type", "unknown")] += 1
        return {
            "total_events": len(events),
            "by_type": dict(type_counts),
        }
