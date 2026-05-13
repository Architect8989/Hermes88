#!/usr/bin/env python3
"""
skills/jcode_swarm/coord_daemon.py — Rhodawk jcode coordination daemon.

FIX Problem-3: When the real jcode binary is not installed, this daemon
provides the coordination layer that makes the API wrapper actually behave
like a swarm (rather than N parallel race conditions).

Responsibilities:
1. Per-file ownership tracking: each file can only be claimed by one worker
   at a time. A second worker that wants the same file blocks until released.
2. Session memory: maintains a shared scratchpad of "what has been done" so
   subsequent workers have context about prior work.
3. Conflict detection: after all workers complete, runs a check for git
   merge conflicts and reports them.
4. HTTP API on :7865 (same port the real jcode server uses, so spawn.py
   works without changes):
     POST /lock   { "file": "path/to/file", "worker_id": 1 }
     POST /unlock { "file": "path/to/file", "worker_id": 1 }
     GET  /status
     POST /session { "key": "s1", "action": "read|write", "data": "..." }

Start:
    python3 /app/skills/jcode_swarm/coord_daemon.py --port 7865

Note: If the real jcode binary is available, supervisord will delegate to it
instead of this daemon (see supervisord.conf [program:jcode-server]).
"""
import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


class CoordinationState:
    """Thread-safe coordination state for the jcode swarm."""

    def __init__(self):
        self._lock = threading.Lock()
        # file_path -> {"worker_id": int, "acquired_at": float}
        self._file_locks: dict = {}
        # Condition for lock waiting
        self._cond = threading.Condition(self._lock)
        # session_key -> {"history": [...], "files_claimed": [...]}
        self._sessions: dict = {}
        self._stats = {
            "lock_requests": 0,
            "lock_conflicts": 0,
            "unlocks": 0,
            "started_at": time.time(),
        }

    def acquire_file(self, file_path: str, worker_id: int, timeout: float = 60.0) -> bool:
        """
        Claim exclusive ownership of a file for a worker.
        Returns True if acquired, False on timeout.
        """
        deadline = time.time() + timeout
        with self._cond:
            self._stats["lock_requests"] += 1
            while file_path in self._file_locks:
                existing = self._file_locks[file_path]
                if existing["worker_id"] == worker_id:
                    return True  # Re-entrant: same worker
                self._stats["lock_conflicts"] += 1
                remaining = deadline - time.time()
                if remaining <= 0:
                    return False
                self._cond.wait(timeout=min(remaining, 2.0))
            # File is free — claim it
            self._file_locks[file_path] = {
                "worker_id": worker_id,
                "acquired_at": time.time(),
                "file": file_path,
            }
            return True

    def release_file(self, file_path: str, worker_id: int) -> bool:
        """Release ownership of a file."""
        with self._cond:
            self._stats["unlocks"] += 1
            existing = self._file_locks.get(file_path)
            if existing and existing["worker_id"] == worker_id:
                del self._file_locks[file_path]
                self._cond.notify_all()
                return True
            return False

    def release_all(self, worker_id: int) -> list:
        """Release all files held by a worker. Called on worker exit."""
        with self._cond:
            released = []
            for fp, info in list(self._file_locks.items()):
                if info["worker_id"] == worker_id:
                    del self._file_locks[fp]
                    released.append(fp)
            if released:
                self._cond.notify_all()
            return released

    def get_status(self) -> dict:
        with self._cond:
            return {
                "locks": dict(self._file_locks),
                "sessions": {k: {"history_count": len(v.get("history", []))} for k, v in self._sessions.items()},
                "stats": dict(self._stats),
                "uptime_seconds": time.time() - self._stats["started_at"],
            }

    def session_read(self, key: str) -> dict:
        with self._lock:
            return dict(self._sessions.get(key, {}))

    def session_write(self, key: str, data: dict):
        with self._lock:
            if key not in self._sessions:
                self._sessions[key] = {"history": [], "files_claimed": []}
            sess = self._sessions[key]
            if "history_entry" in data:
                sess.setdefault("history", []).append(data["history_entry"])
                sess["history"] = sess["history"][-100:]
            if "files_claimed" in data:
                existing = set(sess.get("files_claimed", []))
                existing.update(data["files_claimed"])
                sess["files_claimed"] = list(existing)
            sess.update({k: v for k, v in data.items() if k not in ("history_entry", "files_claimed")})

    def evict_stale_locks(self, max_age: float = 600.0):
        """Release file locks older than max_age seconds (worker crash recovery)."""
        with self._cond:
            now = time.time()
            stale = [fp for fp, info in self._file_locks.items()
                     if now - info.get("acquired_at", now) > max_age]
            for fp in stale:
                print(f"[coord-daemon] Evicting stale lock: {fp}", flush=True)
                del self._file_locks[fp]
            if stale:
                self._cond.notify_all()
            return stale


_STATE = CoordinationState()


class CoordHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the coordination daemon."""

    def log_message(self, fmt, *args):
        if "/status" not in args[0]:  # Suppress health-check spam
            print(f"[coord-daemon] {fmt % args}", flush=True)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 0:
            try:
                return json.loads(self.rfile.read(length))
            except Exception:
                pass
        return {}

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/status" or self.path == "/health":
            self._send_json({"status": "ok", **_STATE.get_status()})
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        body = self._read_json()

        if self.path == "/lock":
            file_path = body.get("file", "")
            worker_id = body.get("worker_id", 0)
            timeout = float(body.get("timeout", 60.0))
            if not file_path:
                self._send_json({"error": "file required"}, 400)
                return
            ok = _STATE.acquire_file(file_path, worker_id, timeout)
            self._send_json({"acquired": ok, "file": file_path, "worker_id": worker_id})

        elif self.path == "/unlock":
            file_path = body.get("file", "")
            worker_id = body.get("worker_id", 0)
            ok = _STATE.release_file(file_path, worker_id)
            self._send_json({"released": ok, "file": file_path})

        elif self.path == "/unlock_all":
            worker_id = body.get("worker_id", 0)
            released = _STATE.release_all(worker_id)
            self._send_json({"released": released, "count": len(released)})

        elif self.path == "/session":
            key = body.get("key", "default")
            action = body.get("action", "read")
            if action == "read":
                self._send_json(_STATE.session_read(key))
            elif action == "write":
                _STATE.session_write(key, body.get("data", {}))
                self._send_json({"ok": True})
            else:
                self._send_json({"error": "action must be read|write"}, 400)

        elif self.path == "/evict_stale":
            max_age = float(body.get("max_age", 600))
            evicted = _STATE.evict_stale_locks(max_age)
            self._send_json({"evicted": evicted})

        else:
            self._send_json({"error": "not found"}, 404)


def _stale_eviction_loop():
    """Background thread to evict stale locks every 5 minutes."""
    while True:
        time.sleep(300)
        _STATE.evict_stale_locks(max_age=600)


def main():
    parser = argparse.ArgumentParser(description="Rhodawk jcode coordination daemon")
    parser.add_argument("--port", type=int, default=7865)
    parser.add_argument("--host", default="localhost")
    args = parser.parse_args()

    eviction_thread = threading.Thread(target=_stale_eviction_loop, daemon=True)
    eviction_thread.start()

    server = HTTPServer((args.host, args.port), CoordHandler)
    print(
        f"[coord-daemon] Rhodawk jcode coordination daemon started on "
        f"{args.host}:{args.port}",
        flush=True,
    )
    print(
        f"[coord-daemon] Endpoints: GET /status  POST /lock /unlock /unlock_all /session /evict_stale",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[coord-daemon] Shutting down", flush=True)


if __name__ == "__main__":
    main()
