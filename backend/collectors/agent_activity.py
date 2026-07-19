# -*- coding: utf-8 -*-
"""Agent 活动采集器 — 全局监控所有 Hermes 进程和会话

数据来源：
1. psutil 扫描所有 hermes 相关进程（gateway/worker/cron/CLI）
2. 遍历所有 profile 的 state.db 查询活跃会话
3. 读取各 profile 的 gateway.pid / cron.pid 判断进程状态
4. 读取 kanban.db 的 task_claims 获取 Worker 进程信息
"""
import os
import sqlite3
import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

from .base import BaseCollector, CollectorResult


class AgentActivityCollector(BaseCollector):
    """全局 Agent 活动采集器

    不绑定单个 profile，而是扫描整个 ~/.hermes/ 目录树，
    发现所有 profile 和它们的活跃进程/会话。
    """
    name = "agent_activity"
    path_pattern = ""  # 不绑定单个文件
    known_schema_version = "v1"

    def __init__(self, hermes_home: Path):
        # hermes_home 这里是 ~/.hermes 根目录
        super().__init__(hermes_home)

    def collect(self) -> CollectorResult:
        ts = datetime.now(timezone.utc).isoformat() + "Z"
        result = CollectorResult(
            source=self.name, data={}, schema_version=self.known_schema_version, timestamp=ts
        )

        try:
            # 1. 扫描所有 hermes 进程
            processes = self._scan_processes()

            # 2. 扫描所有 profile 目录
            profiles = self._scan_profiles()

            # 3. 遍历每个 profile 的 state.db 获取活跃会话
            sessions = self._scan_all_sessions(profiles)

            # 4. 汇总统计
            stats = self._compute_stats(processes, sessions)

            result.data = {
                "processes": processes,
                "profiles": profiles,
                "sessions": sessions,
                "stats": stats,
            }
        except Exception as e:
            result.status = "error"
            result.error = str(e)
        return result

    def _scan_processes(self) -> list:
        """用 psutil 扫描所有 hermes 相关进程"""
        if not HAS_PSUTIL:
            return []

        processes = []
        for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'create_time', 'cpu_percent', 'memory_info']):
            try:
                info = proc.info
                cmdline = ' '.join(info.get('cmdline', []))
                if not cmdline:
                    continue

                # 识别 hermes 相关进程
                is_hermes = False
                proc_type = "unknown"
                profile = "default"

                if 'hermes' in cmdline.lower() or 'python' in (info.get('name', '') or '').lower():
                    if 'gateway' in cmdline:
                        is_hermes = True
                        proc_type = "gateway"
                    elif 'kanban' in cmdline or 'HERMES_KANBAN_TASK' in cmdline:
                        is_hermes = True
                        proc_type = "kanban_worker"
                    elif 'cron' in cmdline:
                        is_hermes = True
                        proc_type = "cron"
                    elif 'hermes' in cmdline and ('chat' in cmdline or 'agent' in cmdline):
                        is_hermes = True
                        proc_type = "cli"
                    elif 'delegate_task' in cmdline or 'subagent' in cmdline:
                        is_hermes = True
                        proc_type = "subagent"

                if not is_hermes:
                    continue

                # 尝试提取 profile
                if '--profile' in cmdline:
                    parts = cmdline.split('--profile')
                    if len(parts) > 1:
                        profile = parts[1].strip().split()[0].strip('-')
                elif 'HERMES_HOME' in cmdline:
                    # 从环境变量提取（如果 cmdline 中可见）
                    pass

                # 尝试获取环境变量中的 HERMES_HOME
                try:
                    env = proc.environ()
                    hh = env.get('HERMES_HOME', '')
                    if hh and 'profiles/' in hh:
                        profile = hh.split('profiles/')[-1].strip('/')
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    pass

                mem = info.get('memory_info')
                mem_mb = mem.rss / 1024 / 1024 if mem else 0

                processes.append({
                    "pid": info['pid'],
                    "type": proc_type,
                    "profile": profile,
                    "cmdline": cmdline[:200],
                    "started_at": datetime.fromtimestamp(info.get('create_time', 0), timezone.utc).isoformat() + "Z" if info.get('create_time') else "",
                    "cpu_percent": info.get('cpu_percent', 0),
                    "memory_mb": round(mem_mb, 1),
                    "status": "running",
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

        return processes

    def _scan_profiles(self) -> list:
        """扫描所有 profile 目录"""
        profiles = []

        # default profile
        root = self.hermes_home
        if root.exists():
            profiles.append({
                "name": "default",
                "path": str(root),
                "has_state_db": (root / "state.db").exists(),
                "has_gateway_pid": (root / "gateway.pid").exists(),
                "gateway_running": self._check_pid_file(root / "gateway.pid"),
            })

        # named profiles
        profiles_dir = root / "profiles"
        if profiles_dir.exists():
            for p in sorted(profiles_dir.iterdir()):
                if p.is_dir() and not p.name.startswith('.'):
                    profiles.append({
                        "name": p.name,
                        "path": str(p),
                        "has_state_db": (p / "state.db").exists(),
                        "has_gateway_pid": (p / "gateway.pid").exists(),
                        "gateway_running": self._check_pid_file(p / "gateway.pid"),
                    })

        return profiles

    def _check_pid_file(self, pid_file: Path) -> bool:
        """检查 PID 文件对应的进程是否存活"""
        if not pid_file.exists():
            return False
        try:
            pid = int(pid_file.read_text().strip())
            if HAS_PSUTIL:
                return psutil.pid_exists(pid)
            else:
                os.kill(pid, 0)
                return True
        except (ValueError, OSError, ProcessLookupError):
            return False

    def _scan_all_sessions(self, profiles: list) -> list:
        """遍历所有 profile 的 state.db 查询活跃会话"""
        sessions = []

        for profile in profiles:
            if not profile.get("has_state_db"):
                continue

            db_path = Path(profile["path"]) / "state.db"
            try:
                conn = sqlite3.connect(str(db_path))
                conn.row_factory = sqlite3.Row

                # 查询活跃会话（ended_at IS NULL 或最近 5 分钟有活动）
                cursor = conn.execute(
                    "SELECT s.id, s.source, s.model, s.title, s.started_at, s.ended_at, "
                    "s.message_count, s.api_call_count, s.input_tokens, s.output_tokens, "
                    "COALESCE((SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id = s.id), "
                    "s.started_at) as last_active "
                    "FROM sessions s "
                    "WHERE s.ended_at IS NULL "
                    "OR (SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id = s.id) > strftime('%s','now') - 300 "
                    "ORDER BY last_active DESC LIMIT 50"
                )

                for row in cursor.fetchall():
                    d = dict(row)
                    # 转换时间戳
                    started = d.get("started_at", 0)
                    last_active = d.get("last_active", 0)
                    if started:
                        d["started_at"] = datetime.fromtimestamp(started, timezone.utc).isoformat() + "Z"
                    if last_active:
                        d["last_active"] = datetime.fromtimestamp(last_active, timezone.utc).isoformat() + "Z"
                    d["profile"] = profile["name"]
                    d["is_active"] = d.get("ended_at") is None
                    sessions.append(d)

                conn.close()
            except Exception:
                continue

        # 按最后活跃时间排序
        sessions.sort(key=lambda s: s.get("last_active", ""), reverse=True)
        return sessions

    def _compute_stats(self, processes: list, sessions: list) -> dict:
        """汇总统计"""
        # 进程统计
        proc_by_type = {}
        for p in processes:
            t = p.get("type", "unknown")
            proc_by_type[t] = proc_by_type.get(t, 0) + 1

        # 会话统计
        active_sessions = [s for s in sessions if s.get("is_active")]
        sessions_by_profile = {}
        sessions_by_source = {}
        for s in sessions:
            prof = s.get("profile", "unknown")
            sessions_by_profile[prof] = sessions_by_profile.get(prof, 0) + 1
            src = s.get("source", "unknown")
            sessions_by_source[src] = sessions_by_source.get(src, 0) + 1

        # Agent 状态汇总
        agent_status = {}
        for p in processes:
            prof = p.get("profile", "default")
            ptype = p.get("type", "unknown")
            key = f"{prof} ({ptype})"
            agent_status[key] = "working"

        # 检查空闲 profile（有进程但无活跃会话）
        for prof in set(p.get("profile", "default") for p in processes):
            has_active = any(s.get("profile") == prof and s.get("is_active") for s in sessions)
            if not has_active:
                agent_status[f"{prof} (idle)"] = "idle"

        return {
            "total_processes": len(processes),
            "processes_by_type": proc_by_type,
            "total_sessions": len(sessions),
            "active_sessions": len(active_sessions),
            "sessions_by_profile": sessions_by_profile,
            "sessions_by_source": sessions_by_source,
            "agent_status": agent_status,
        }
