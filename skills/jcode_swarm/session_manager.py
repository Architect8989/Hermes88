#!/usr/bin/env python3
"""
skills/jcode_swarm/session_manager.py — Persistent jcode Session Manager (Layer B)

Maintains long-lived jcode sessions per project so jcode's semantic memory
system accumulates across tasks. jcode's primary architectural differentiator
is its memory system (~10MB/session vs 213MB for alternatives) — but only
activates when sessions persist across turns.

Current Hermes88 approach (broken):
  jcode run --message "..." --non-interactive   ← fresh session every time
  → Memory never accumulates, defeating jcode's primary advantage

Fixed approach (this module):
  JcodeSessionManager.send_task(project_id, task)
  → Maps project_id to persistent session
  → jcode automatically retrieves context from prior turns in the session

Usage:
    from skills.jcode_swarm.session_manager import get_session_manager

    manager = get_session_manager()
    result = manager.send_task("Architect8989/Hermes88", "Add error handling to gateway/run.py")
    result = manager.send_task("Architect8989/Hermes88", "Write tests for the new error handler")
    # ↑ Second call benefits from jcode's memory of the first

Copyright (c) 2024-2025 Rhodawk AI. All rights reserved.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, List


# ── Configuration ─────────────────────────────────────────────────────────────

JCODE_BIN          = "jcode"
MAX_SESSIONS       = 20        # jcode ~10MB/session → 200MB max
SESSION_TTL_HOURS  = 8         # Idle sessions pruned after N hours
DEFAULT_TIMEOUT    = 300       # 5 minutes per task
DO_BASE_URL        = os.environ.get("DO_INFERENCE_BASE_URL", "https://inference.do-ai.run/v1")
DO_API_KEY         = os.environ.get("DO_INFERENCE_API_KEY", "")
JCODE_MODEL        = os.environ.get("JCODE_MODEL", "kimi-k2.6")
SESSION_DIR        = Path(os.environ.get("HERMES_HOME", "/data/.hermes")) / "jcode_sessions"


# ── Data Models ───────────────────────────────────────────────────────────────


@dataclass
class JcodeSession:
    """A persistent jcode session for a project."""
    project_id: str           # e.g. "Architect8989/Hermes88" or "local:/tmp/repos/myapp"
    workdir: str              # Local filesystem path for jcode to operate on
    session_key: str          # Unique session identifier used with --session flag
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    task_count: int = 0
    total_duration_s: float = 0.0

    @property
    def idle_hours(self) -> float:
        return (time.time() - self.last_used) / 3600

    @property
    def is_stale(self) -> bool:
        return self.idle_hours > SESSION_TTL_HOURS

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "workdir": self.workdir,
            "session_key": self.session_key,
            "created_at": self.created_at,
            "last_used": self.last_used,
            "task_count": self.task_count,
            "total_duration_s": self.total_duration_s,
        }


@dataclass
class TaskResult:
    """Result from a jcode task execution."""
    project_id: str
    task: str
    output: str
    succeeded: bool
    duration_s: float
    session_key: str
    error: str = ""


# ── Session Persistence ───────────────────────────────────────────────────────


class SessionStore:
    """File-backed session store with thread safety."""

    def __init__(self) -> None:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._sessions: Dict[str, JcodeSession] = self._load()

    def _store_path(self) -> Path:
        return SESSION_DIR / "sessions.json"

    def _load(self) -> Dict[str, JcodeSession]:
        path = self._store_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text())
            return {
                k: JcodeSession(**v)
                for k, v in data.items()
                if isinstance(v, dict) and "project_id" in v
            }
        except Exception:
            return {}

    def _save(self) -> None:
        self._store_path().write_text(
            json.dumps({k: v.to_dict() for k, v in self._sessions.items()}, indent=2)
        )

    def get(self, project_id: str) -> Optional[JcodeSession]:
        with self._lock:
            return self._sessions.get(project_id)

    def put(self, session: JcodeSession) -> None:
        with self._lock:
            self._sessions[session.project_id] = session
            self._save()

    def remove(self, project_id: str) -> None:
        with self._lock:
            self._sessions.pop(project_id, None)
            self._save()

    def all(self) -> List[JcodeSession]:
        with self._lock:
            return list(self._sessions.values())

    def prune_stale(self) -> int:
        """Remove stale sessions. Returns count removed."""
        with self._lock:
            stale = [k for k, v in self._sessions.items() if v.is_stale]
            for k in stale:
                del self._sessions[k]
            if stale:
                self._save()
            return len(stale)


# ── Session Manager ───────────────────────────────────────────────────────────


class JcodeSessionManager:
    """
    Manages persistent jcode sessions mapped to project IDs.

    Key behaviors:
    - get_or_create_session(): returns existing session if alive, else creates new
    - send_task(): routes task to correct persistent session
    - jcode's memory accumulates across tasks within the same session
    - Stale sessions (idle > SESSION_TTL_HOURS) are pruned automatically
    """

    def __init__(self) -> None:
        self._store = SessionStore()
        self._jcode_available = self._check_jcode()

    def _check_jcode(self) -> bool:
        """Check if jcode binary is available."""
        try:
            result = subprocess.run(
                [JCODE_BIN, "--version"],
                capture_output=True, text=True, timeout=5,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def _make_session_key(self, project_id: str) -> str:
        """Generate a stable session key from project ID."""
        import hashlib
        return "hermes-" + hashlib.md5(project_id.encode()).hexdigest()[:12]

    def get_or_create_session(self, project_id: str, workdir: Optional[str] = None) -> JcodeSession:
        """
        Return an existing session for this project, or create a new one.
        Prunes stale sessions to keep memory bounded.
        """
        # Prune stale sessions periodically
        self._store.prune_stale()

        existing = self._store.get(project_id)
        if existing and not existing.is_stale:
            return existing

        # Determine workdir
        if not workdir:
            # Default: clone/use repo under /tmp/repos
            repo_name = project_id.split("/")[-1]
            workdir = f"/tmp/repos/{repo_name}"

        # Enforce session cap
        sessions = self._store.all()
        if len(sessions) >= MAX_SESSIONS:
            # Remove oldest session
            oldest = sorted(sessions, key=lambda s: s.last_used)[0]
            self._store.remove(oldest.project_id)

        session = JcodeSession(
            project_id=project_id,
            workdir=workdir,
            session_key=self._make_session_key(project_id),
        )
        self._store.put(session)
        return session

    def send_task(
        self,
        project_id: str,
        task: str,
        workdir: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> TaskResult:
        """
        Send a task to the persistent jcode session for this project.

        jcode's semantic memory will automatically retrieve relevant context
        from previous tasks in the same session.

        Args:
            project_id: Project identifier (e.g. "owner/repo" or "local:/path")
            task: Task description for jcode
            workdir: Override working directory (optional)
            timeout: Max seconds to wait for jcode response
        """
        if not self._jcode_available:
            return TaskResult(
                project_id=project_id,
                task=task,
                output="",
                succeeded=False,
                duration_s=0.0,
                session_key="",
                error="jcode not installed — run: curl -fsSL https://raw.githubusercontent.com/1jehuang/jcode/master/scripts/install.sh | bash",
            )

        session = self.get_or_create_session(project_id, workdir)
        effective_workdir = workdir or session.workdir

        # Ensure workdir exists
        Path(effective_workdir).mkdir(parents=True, exist_ok=True)

        # Build jcode command with session ID for memory persistence
        env = {
            **os.environ,
            "OPENAI_API_KEY":  DO_API_KEY,
            "OPENAI_BASE_URL": DO_BASE_URL,
            "OPENAI_MODEL":    JCODE_MODEL,
            "JCODE_API_KEY":   DO_API_KEY,
            "JCODE_BASE_URL":  DO_BASE_URL,
            "JCODE_MODEL":     JCODE_MODEL,
        }

        cmd = [
            JCODE_BIN,
            "run",
            "--message", task,
            "--non-interactive",
            "--session", session.session_key,  # Persist memory across invocations
        ]

        start_time = time.time()
        try:
            result = subprocess.run(
                cmd,
                cwd=effective_workdir,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            duration_s = time.time() - start_time
            output = (result.stdout + result.stderr).strip()
            succeeded = result.returncode == 0

        except subprocess.TimeoutExpired:
            duration_s = time.time() - start_time
            return TaskResult(
                project_id=project_id,
                task=task,
                output="",
                succeeded=False,
                duration_s=duration_s,
                session_key=session.session_key,
                error=f"jcode timed out after {timeout}s",
            )
        except Exception as exc:
            duration_s = time.time() - start_time
            return TaskResult(
                project_id=project_id,
                task=task,
                output="",
                succeeded=False,
                duration_s=duration_s,
                session_key=session.session_key,
                error=str(exc),
            )

        # Update session stats
        session.last_used = time.time()
        session.task_count += 1
        session.total_duration_s += duration_s
        self._store.put(session)

        return TaskResult(
            project_id=project_id,
            task=task,
            output=output[:8000],
            succeeded=succeeded,
            duration_s=duration_s,
            session_key=session.session_key,
        )

    def list_sessions(self) -> List[dict]:
        """List all active sessions with stats."""
        return [
            {
                "project_id": s.project_id,
                "session_key": s.session_key,
                "workdir": s.workdir,
                "task_count": s.task_count,
                "idle_hours": round(s.idle_hours, 1),
                "total_duration_s": round(s.total_duration_s, 1),
                "is_stale": s.is_stale,
            }
            for s in self._store.all()
        ]

    def close_session(self, project_id: str) -> bool:
        """Explicitly close a session (e.g. project complete)."""
        session = self._store.get(project_id)
        if not session:
            return False
        self._store.remove(project_id)
        return True

    def prewarm_sessions(self, project_ids: List[str]) -> None:
        """
        Pre-warm sessions for known projects on startup.
        Called from init_and_start.sh for the operator's active repos.
        """
        for project_id in project_ids:
            self.get_or_create_session(project_id)
            print(f"[jcode-session] Pre-warmed session for: {project_id}", flush=True)


# ── Singleton ─────────────────────────────────────────────────────────────────

_manager: Optional[JcodeSessionManager] = None


def get_session_manager() -> JcodeSessionManager:
    """Get the singleton JcodeSessionManager."""
    global _manager
    if _manager is None:
        _manager = JcodeSessionManager()
    return _manager


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="jcode Persistent Session Manager")
    parser.add_argument("--task", help="Task to send to jcode")
    parser.add_argument("--project", default="local", help="Project ID")
    parser.add_argument("--workdir", default="/tmp/repos", help="Working directory")
    parser.add_argument("--list", action="store_true", help="List active sessions")
    parser.add_argument("--close", help="Close a session by project ID")
    args = parser.parse_args()

    manager = get_session_manager()

    if args.list:
        sessions = manager.list_sessions()
        print(json.dumps(sessions, indent=2))
    elif args.close:
        closed = manager.close_session(args.close)
        print("Closed" if closed else "Session not found")
    elif args.task:
        result = manager.send_task(args.project, args.task, workdir=args.workdir)
        print(f"Project:  {result.project_id}")
        print(f"Session:  {result.session_key}")
        print(f"Duration: {result.duration_s:.1f}s")
        print(f"Success:  {result.succeeded}")
        if result.error:
            print(f"Error: {result.error}", file=sys.stderr)
        print(f"\nOutput:\n{result.output}")
    else:
        parser.print_help()
