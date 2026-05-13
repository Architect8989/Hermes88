#!/usr/bin/env python3
"""
Rhodawk Core Audit Logger.

Append-only JSON audit log for tool calls, model calls, file access,
network requests, security events, and sandbox spawns.

Provides comprehensive audit trail for all operations performed
by the Hermes system for compliance, debugging, and security review.

Copyright (c) 2024-2025 Rhodawk AI. All rights reserved.
"""
import json
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List, Any


# -- Audit Event Types -----------------------------------------------------------


class AuditEventType:
    """Constants for audit event types."""
    TOOL_CALL = "tool_call"
    MODEL_CALL = "model_call"
    FILE_ACCESS = "file_access"
    NETWORK_REQUEST = "network_request"
    SECURITY_EVENT = "security_event"
    SANDBOX_SPAWN = "sandbox_spawn"
    TASK_LIFECYCLE = "task_lifecycle"
    MEMORY_OPERATION = "memory_operation"
    CONFIG_CHANGE = "config_change"
    AUTH_EVENT = "auth_event"
    ERROR = "error"


class AuditSeverity:
    """Severity levels for audit events."""
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


# -- Audit Entry Model -----------------------------------------------------------


@dataclass
class AuditEntry:
    """A single audit log entry."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    event_type: str = ""
    severity: str = AuditSeverity.INFO
    actor: str = "hermes"
    action: str = ""
    resource: str = ""
    outcome: str = "success"
    details: dict = field(default_factory=dict)
    duration_ms: float = 0.0
    session_id: str = ""
    request_id: str = ""
    ip_address: str = ""
    user_agent: str = ""

    def to_json(self) -> str:
        """Serialize to JSON line for append-only log."""
        return json.dumps({
            "id": self.id,
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "severity": self.severity,
            "actor": self.actor,
            "action": self.action,
            "resource": self.resource,
            "outcome": self.outcome,
            "details": self.details,
            "duration_ms": self.duration_ms,
            "session_id": self.session_id,
            "request_id": self.request_id,
            "ip_address": self.ip_address,
            "user_agent": self.user_agent,
        }, separators=(",", ":"))

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "severity": self.severity,
            "actor": self.actor,
            "action": self.action,
            "resource": self.resource,
            "outcome": self.outcome,
            "details": self.details,
            "duration_ms": self.duration_ms,
            "session_id": self.session_id,
            "request_id": self.request_id,
        }


# -- Audit Logger ----------------------------------------------------------------


class AuditLogger:
    """
    Append-only audit logger for the Rhodawk system.

    Writes JSON-lines format to a log file and maintains an in-memory
    ring buffer for recent queries. Supports log rotation by date.

    All log methods are thread-safe and non-blocking (best-effort writes).
    """

    def __init__(self, config: Optional[dict] = None):
        config = config or {}

        self.log_dir = Path(config.get("log_dir", "/data/.hermes/audit"))
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self.max_file_size = config.get("max_file_size_mb", 50) * 1024 * 1024
        self.retention_days = config.get("retention_days", 90)
        self.buffer_size = config.get("buffer_size", 1000)

        self._buffer: deque = deque(maxlen=self.buffer_size)
        self._lock = threading.Lock()
        self._current_file: Optional[Any] = None
        self._current_date: str = ""
        self._stats = {
            "total_entries": 0,
            "errors": 0,
            "by_type": {},
            "by_severity": {},
        }

    def _get_log_file(self):
        """Get the current log file, rotating by date."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._current_date:
            if self._current_file:
                try:
                    self._current_file.close()
                except Exception:
                    pass
            self._current_date = today
            log_path = self.log_dir / f"audit_{today}.jsonl"
            try:
                self._current_file = open(log_path, "a", buffering=1)
            except Exception as e:
                print(f"[audit] Failed to open log file: {e}", flush=True)
                self._current_file = None
        return self._current_file

    def _write_entry(self, entry: AuditEntry):
        """Write an entry to the log file and buffer."""
        with self._lock:
            self._buffer.append(entry)
            self._stats["total_entries"] += 1
            self._stats["by_type"][entry.event_type] = (
                self._stats["by_type"].get(entry.event_type, 0) + 1
            )
            self._stats["by_severity"][entry.severity] = (
                self._stats["by_severity"].get(entry.severity, 0) + 1
            )

            try:
                log_file = self._get_log_file()
                if log_file:
                    log_file.write(entry.to_json() + "\n")
            except Exception as e:
                self._stats["errors"] += 1

    def log_tool_call(self, tool_name: str, params: dict,
                      outcome: str = "success", duration_ms: float = 0.0,
                      error: str = "", actor: str = "hermes"):
        """Log a tool invocation."""
        details = {"params": {k: str(v)[:200] for k, v in params.items()}}
        if error:
            details["error"] = error[:500]

        entry = AuditEntry(
            event_type=AuditEventType.TOOL_CALL,
            severity=AuditSeverity.INFO if outcome == "success" else AuditSeverity.WARNING,
            actor=actor,
            action=f"tool:{tool_name}",
            resource=tool_name,
            outcome=outcome,
            details=details,
            duration_ms=duration_ms,
        )
        self._write_entry(entry)

    def log_model_call(self, model: str, task_type: str,
                       input_tokens: int = 0, output_tokens: int = 0,
                       latency_ms: float = 0.0, outcome: str = "success",
                       error: str = "", actor: str = "hermes"):
        """Log an LLM model invocation."""
        details = {
            "model": model,
            "task_type": task_type,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        if error:
            details["error"] = error[:500]

        entry = AuditEntry(
            event_type=AuditEventType.MODEL_CALL,
            severity=AuditSeverity.INFO if outcome == "success" else AuditSeverity.WARNING,
            actor=actor,
            action=f"model:{model}",
            resource=model,
            outcome=outcome,
            details=details,
            duration_ms=latency_ms,
        )
        self._write_entry(entry)

    def log_file_access(self, path: str, operation: str,
                        outcome: str = "success", actor: str = "hermes",
                        bytes_transferred: int = 0):
        """Log a file system access operation."""
        entry = AuditEntry(
            event_type=AuditEventType.FILE_ACCESS,
            severity=AuditSeverity.INFO,
            actor=actor,
            action=f"file:{operation}",
            resource=path,
            outcome=outcome,
            details={"operation": operation, "bytes": bytes_transferred},
        )
        self._write_entry(entry)

    def log_network_request(self, url: str, method: str = "GET",
                            status_code: int = 0, duration_ms: float = 0.0,
                            outcome: str = "success", error: str = "",
                            actor: str = "hermes"):
        """Log a network/HTTP request."""
        details = {
            "url": url[:500],
            "method": method,
            "status_code": status_code,
        }
        if error:
            details["error"] = error[:500]

        entry = AuditEntry(
            event_type=AuditEventType.NETWORK_REQUEST,
            severity=AuditSeverity.INFO if outcome == "success" else AuditSeverity.WARNING,
            actor=actor,
            action=f"http:{method}",
            resource=url[:200],
            outcome=outcome,
            details=details,
            duration_ms=duration_ms,
        )
        self._write_entry(entry)

    def log_security_event(self, event_name: str, description: str,
                           severity: str = AuditSeverity.WARNING,
                           details: Optional[dict] = None,
                           actor: str = "system"):
        """Log a security-relevant event."""
        entry = AuditEntry(
            event_type=AuditEventType.SECURITY_EVENT,
            severity=severity,
            actor=actor,
            action=f"security:{event_name}",
            resource=event_name,
            outcome="detected",
            details={"description": description, **(details or {})},
        )
        self._write_entry(entry)

    def log_sandbox_spawn(self, sandbox_id: str, image: str,
                          task_description: str, outcome: str = "success",
                          duration_ms: float = 0.0, actor: str = "hermes"):
        """Log a sandbox container spawn event."""
        entry = AuditEntry(
            event_type=AuditEventType.SANDBOX_SPAWN,
            severity=AuditSeverity.INFO,
            actor=actor,
            action="sandbox:spawn",
            resource=sandbox_id,
            outcome=outcome,
            details={
                "image": image,
                "task": task_description[:200],
                "sandbox_id": sandbox_id,
            },
            duration_ms=duration_ms,
        )
        self._write_entry(entry)

    def log_task_lifecycle(self, task_id: str, task_name: str,
                           status: str, duration_ms: float = 0.0,
                           details: Optional[dict] = None,
                           actor: str = "hermes"):
        """Log a task lifecycle event (submit, start, complete, fail)."""
        entry = AuditEntry(
            event_type=AuditEventType.TASK_LIFECYCLE,
            severity=AuditSeverity.INFO if status != "failed" else AuditSeverity.WARNING,
            actor=actor,
            action=f"task:{status}",
            resource=task_id,
            outcome=status,
            details={"task_name": task_name, **(details or {})},
            duration_ms=duration_ms,
        )
        self._write_entry(entry)

    def log_memory_operation(self, operation: str, memory_id: str = "",
                             category: str = "", outcome: str = "success",
                             actor: str = "hermes"):
        """Log a memory engine operation."""
        entry = AuditEntry(
            event_type=AuditEventType.MEMORY_OPERATION,
            severity=AuditSeverity.DEBUG,
            actor=actor,
            action=f"memory:{operation}",
            resource=memory_id,
            outcome=outcome,
            details={"operation": operation, "category": category},
        )
        self._write_entry(entry)

    def log_config_change(self, key: str, old_value: str = "",
                          new_value: str = "", actor: str = "operator"):
        """Log a configuration change."""
        entry = AuditEntry(
            event_type=AuditEventType.CONFIG_CHANGE,
            severity=AuditSeverity.WARNING,
            actor=actor,
            action="config:change",
            resource=key,
            outcome="changed",
            details={
                "key": key,
                "old_value": old_value[:100],
                "new_value": new_value[:100],
            },
        )
        self._write_entry(entry)

    def log_auth_event(self, event_name: str, user: str = "",
                       outcome: str = "success", ip: str = "",
                       details: Optional[dict] = None):
        """Log an authentication/authorization event."""
        entry = AuditEntry(
            event_type=AuditEventType.AUTH_EVENT,
            severity=AuditSeverity.INFO if outcome == "success" else AuditSeverity.WARNING,
            actor=user or "unknown",
            action=f"auth:{event_name}",
            resource=user,
            outcome=outcome,
            ip_address=ip,
            details=details or {},
        )
        self._write_entry(entry)

    def log_error(self, error_type: str, message: str,
                  stack_trace: str = "", context: Optional[dict] = None,
                  actor: str = "hermes"):
        """Log an error event."""
        entry = AuditEntry(
            event_type=AuditEventType.ERROR,
            severity=AuditSeverity.ERROR,
            actor=actor,
            action=f"error:{error_type}",
            resource=error_type,
            outcome="error",
            details={
                "message": message[:500],
                "stack_trace": stack_trace[:2000],
                **(context or {}),
            },
        )
        self._write_entry(entry)

    def query(self, event_type: Optional[str] = None,
              severity: Optional[str] = None,
              actor: Optional[str] = None,
              limit: int = 100,
              since: Optional[float] = None) -> List[Dict[str, Any]]:
        """
        Query the in-memory audit buffer with optional filters.

        For historical queries beyond the buffer, read from log files directly.
        """
        with self._lock:
            entries = list(self._buffer)

        if event_type:
            entries = [e for e in entries if e.event_type == event_type]
        if severity:
            entries = [e for e in entries if e.severity == severity]
        if actor:
            entries = [e for e in entries if e.actor == actor]
        if since:
            cutoff = datetime.fromtimestamp(since, tz=timezone.utc).isoformat()
            entries = [e for e in entries if e.timestamp >= cutoff]

        return [e.to_dict() for e in entries[-limit:]]

    def get_stats(self) -> Dict[str, Any]:
        """Get audit logger statistics."""
        return {
            **self._stats,
            "buffer_size": len(self._buffer),
            "buffer_capacity": self.buffer_size,
            "log_dir": str(self.log_dir),
            "current_date": self._current_date,
        }

    def get_recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get the most recent audit entries."""
        with self._lock:
            entries = list(self._buffer)[-limit:]
        return [e.to_dict() for e in entries]

    def flush(self):
        """Flush any buffered writes to disk."""
        with self._lock:
            if self._current_file:
                try:
                    self._current_file.flush()
                except Exception:
                    pass

    def close(self):
        """Close the audit logger and release resources."""
        self.flush()
        with self._lock:
            if self._current_file:
                try:
                    self._current_file.close()
                except Exception:
                    pass
                self._current_file = None

    def rotate_logs(self):
        """Remove log files older than retention_days."""
        cutoff = time.time() - (self.retention_days * 86400)
        for log_file in self.log_dir.glob("audit_*.jsonl"):
            try:
                if log_file.stat().st_mtime < cutoff:
                    log_file.unlink()
                    print(f"[audit] Rotated old log: {log_file.name}", flush=True)
            except Exception:
                pass
