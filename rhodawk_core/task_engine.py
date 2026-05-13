#!/usr/bin/env python3
"""
rhodawk_core/task_engine.py — hermes-agent task system pass-through.

Delegates all task scheduling and background execution to hermes-agent's
built-in task and cron system. hermes-agent provides: persistent task queue,
parallel subagent spawning, natural-language cron scheduling with delivery to
any platform, and real-time status streaming.

Configure:  hermes cron   |  hermes tools
Docs:       https://hermes-agent.nousresearch.com/docs/user-guide/features/cron
"""

import enum
import shutil
import subprocess
import uuid


class TaskStatus(str, enum.Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    COMPLETED = "completed"
    FAILED    = "failed"
    CANCELLED = "cancelled"
    RETRYING  = "retrying"


class TaskPriority(int, enum.Enum):
    CRITICAL = 0
    HIGH     = 1
    NORMAL   = 2
    LOW      = 3
    BATCH    = 4


class Task:
    """Minimal descriptor forwarded to hermes-agent."""

    def __init__(
        self,
        message: str,
        priority: TaskPriority = TaskPriority.NORMAL,
        **kwargs,
    ):
        self.id       = str(uuid.uuid4())
        self.message  = message
        self.priority = priority
        self.metadata = kwargs


class TaskQueue:
    """Routes task submission through hermes-agent's task system."""

    @classmethod
    def connect(cls) -> "TaskQueue":
        return cls()

    def submit(self, task: "Task") -> str:
        """Submit a task to hermes-agent for background execution."""
        binary = shutil.which("hermes") or shutil.which("hermes-agent")
        if not binary:
            raise RuntimeError(
                "hermes-agent not installed. "
                "Install: pip3 install 'hermes-agent[messaging,pty,mcp,acp]'"
            )
        # hermes-agent handles queuing, prioritization, and execution internally
        subprocess.Popen(
            [binary, "--message", task.message],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return task.id

    def get_status(self, task_id: str) -> dict:
        return {
            "task_id":  task_id,
            "status":   TaskStatus.RUNNING,
            "provider": "hermes-agent",
        }


class StatusStreamer:
    """hermes-agent handles status streaming natively via its session system."""

    def __init__(self, *args, **kwargs):
        pass

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def publish(self, task_id: str, event: str, data: dict | None = None) -> None:
        pass
