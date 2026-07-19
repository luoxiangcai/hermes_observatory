# -*- coding: utf-8 -*-
"""Kanban 采集器 — 读取 ~/.hermes/kanban.db 多 Agent 协作看板"""
import sqlite3
import json
from pathlib import Path
from datetime import datetime, timezone

from .base import BaseCollector, CollectorResult


class KanbanCollector(BaseCollector):
    name = "kanban"
    path_pattern = "kanban.db"
    known_schema_version = "v1"

    def collect(self) -> CollectorResult:
        ts = datetime.now(timezone.utc).isoformat() + "Z"
        result = CollectorResult(
            source=self.name, data={}, schema_version=self.known_schema_version, timestamp=ts
        )

        db_path = self.hermes_home / "kanban.db"
        if not db_path.exists():
            result.status = "unavailable"
            result.error = "kanban.db not found"
            return result

        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row

            tasks = self._get_tasks(conn)
            stats = self._get_stats(conn, tasks)
            events = self._get_recent_events(conn)
            workers = self._get_active_workers(conn)

            conn.close()

            result.data = {
                "tasks": tasks,
                "stats": stats,
                "recent_events": events,
                "active_workers": workers,
            }
        except Exception as e:
            result.status = "error"
            result.error = str(e)
        return result

    def _get_tasks(self, conn) -> list:
        try:
            cursor = conn.execute(
                "SELECT id, title, body, assignee, status, priority, tenant, "
                "parent_task_id, created_at, updated_at, claimed_at, completed_at "
                "FROM tasks ORDER BY updated_at DESC LIMIT 200"
            )
            return [dict(row) for row in cursor.fetchall()]
        except Exception:
            return []

    def _get_stats(self, conn, tasks: list) -> dict:
        status_counts = {}
        assignee_counts = {}
        for t in tasks:
            s = t.get("status", "unknown")
            status_counts[s] = status_counts.get(s, 0) + 1
            a = t.get("assignee", "unassigned")
            assignee_counts[a] = assignee_counts.get(a, 0) + 1

        return {
            "total_tasks": len(tasks),
            "by_status": status_counts,
            "by_assignee": assignee_counts,
        }

    def _get_recent_events(self, conn, limit=100) -> list:
        try:
            cursor = conn.execute(
                "SELECT id, task_id, kind, payload, created_at "
                "FROM task_events ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            events = []
            for row in cursor.fetchall():
                d = dict(row)
                if d.get("payload"):
                    try:
                        d["payload"] = json.loads(d["payload"])
                    except Exception:
                        pass
                events.append(d)
            return events
        except Exception:
            return []

    def _get_active_workers(self, conn) -> list:
        try:
            cursor = conn.execute(
                "SELECT task_id, assignee, pid, started_at, last_heartbeat "
                "FROM task_claims WHERE expires_at > datetime('now') "
                "AND pid IS NOT NULL ORDER BY started_at DESC"
            )
            return [dict(row) for row in cursor.fetchall()]
        except Exception:
            return []
