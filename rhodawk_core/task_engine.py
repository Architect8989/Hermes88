#!/usr/bin/env python3
"""
Rhodawk Core Task Engine.

Background execution with Redis-backed queue, priority scheduling,
parallel async workers, real-time Telegram status streaming,
retry policies, and task lifecycle management.

FIX Problem-9: TaskQueue.connect() now gracefully degrades to an in-memory
queue when Redis is unavailable (package not installed, or connection refused).
The RuntimeError that previously crashed the gateway on startup has been replaced
with a warning + automatic fallback to InMemoryTaskQueue.

Copyright (c) 2024-2025 Rhodawk AI. All rights reserved.
"""
import asyncio
import enum
import json
import os
import time
import traceback
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional, Callable, Any, Dict, List

try:
    import redis.asyncio as aioredis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False


# -- Task Models -----------------------------------------------------------------


class TaskStatus(str, enum.Enum):
    """Lifecycle states for a task."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RETRYING = "retrying"


class TaskPriority(int, enum.Enum):
    """Priority levels for task scheduling (lower = higher priority)."""
    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3
    BACKGROUND = 4


@dataclass
class Task:
    """A unit of work to be executed by the worker pool."""
    id: str = field(default_factory=lambda: f"task_{uuid.uuid4().hex[:12]}")
    name: str = ""
    description: str = ""
    priority: int = TaskPriority.NORMAL
    status: str = TaskStatus.PENDING
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    timeout: int = 3600
    retries: int = 0
    max_retries: int = 3
    result: Optional[str] = None
    error: Optional[str] = None
    progress: float = 0.0
    progress_message: str = ""
    metadata: dict = field(default_factory=dict)
    skill: str = ""
    params: dict = field(default_factory=dict)
    source_event: Optional[str] = None
    notify_channel: str = "telegram"

    def to_json(self) -> str:
        """Serialize task to JSON string."""
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str) -> "Task":
        """Deserialize task from JSON string."""
        d = json.loads(data)
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    @property
    def duration(self) -> Optional[float]:
        """Calculate task duration in seconds."""
        if self.started_at:
            end = self.completed_at or time.time()
            return end - self.started_at
        return None

    @property
    def is_terminal(self) -> bool:
        """Check if the task is in a terminal state."""
        return self.status in (
            TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED
        )


# -- In-Memory Fallback Queue (Problem-9 fix) ------------------------------------


class InMemoryTaskQueue:
    """
    In-memory priority queue fallback when Redis is unavailable.

    FIX Problem-9: Provides a degraded but functional task queue that
    does NOT require Redis. Tasks are not persisted across restarts,
    PubSub events are no-ops, and result TTLs are not enforced.
    Functionally equivalent for single-process usage.
    """

    def __init__(self, queue_name: str = "hermes:tasks"):
        self.queue_name = queue_name
        self._queue: list = []  # List of (score, task_id)
        self._tasks: dict = {}  # task_id -> Task
        self._results: dict = {}  # task_id -> Task
        self._lock = asyncio.Lock()
        print(
            f"[task_engine] WARNING: Using in-memory queue (Redis unavailable). "
            f"Tasks will not survive restarts.",
            flush=True,
        )

    async def connect(self):
        pass  # No connection needed

    async def close(self):
        pass

    async def submit(self, task: Task) -> str:
        async with self._lock:
            score = task.priority * 1e10 + task.created_at
            self._queue.append((score, task.id))
            self._queue.sort(key=lambda x: x[0])
            self._tasks[task.id] = task
        return task.id

    async def dequeue(self, timeout: int = 5) -> Optional[Task]:
        async with self._lock:
            if not self._queue:
                return None
            _, task_id = self._queue.pop(0)
            task = self._tasks.get(task_id)
            if not task:
                return None
            task.status = TaskStatus.RUNNING
            task.started_at = time.time()
            return task

    async def complete(self, task: Task, result: str = ""):
        task.status = TaskStatus.COMPLETED
        task.completed_at = time.time()
        task.result = result
        task.progress = 1.0
        async with self._lock:
            self._results[task.id] = task

    async def fail(self, task: Task, error: str):
        task.retries += 1
        if task.retries < task.max_retries:
            task.status = TaskStatus.RETRYING
            task.error = error
            await asyncio.sleep(min(60, task.retries * 5))
            task.status = TaskStatus.PENDING
            await self.submit(task)
        else:
            task.status = TaskStatus.FAILED
            task.completed_at = time.time()
            task.error = error
            async with self._lock:
                self._results[task.id] = task

    async def cancel(self, task_id: str) -> bool:
        async with self._lock:
            self._queue = [(s, tid) for s, tid in self._queue if tid != task_id]
            task = self._tasks.get(task_id)
            if task:
                task.status = TaskStatus.CANCELLED
                return True
            return False

    async def get_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        task = self._tasks.get(task_id) or self._results.get(task_id)
        if not task:
            return None
        return {"status": task.status, "progress": str(task.progress)}

    async def update_progress(self, task: Task, progress: float, message: str = ""):
        task.progress = min(1.0, max(0.0, progress))
        task.progress_message = message

    async def get_queue_stats(self) -> Dict[str, Any]:
        async with self._lock:
            return {
                "pending": len(self._queue),
                "queue_name": self.queue_name + " (in-memory)",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

    async def get_pending_tasks(self, limit: int = 20) -> List[Dict[str, Any]]:
        async with self._lock:
            return [
                {"task_id": tid, "score": s, "status": "pending"}
                for s, tid in self._queue[:limit]
            ]

    async def _update_status(self, task: Task):
        pass  # State is already in-memory

    async def _store_result(self, task: Task):
        self._results[task.id] = task

    async def _publish_event(self, event_type: str, task: Task):
        pass  # No PubSub without Redis


# -- Task Queue (Redis-backed with graceful degradation) -------------------------


class TaskQueue:
    """
    Redis-backed priority task queue with status tracking.

    FIX Problem-9: connect() no longer raises RuntimeError when Redis is
    unavailable. Instead it transparently switches to InMemoryTaskQueue and
    logs a WARNING. All public methods delegate to whichever backend is active.

    Uses sorted sets for priority ordering and hashes for task state.
    Supports submission, dequeue, completion, failure with retry,
    cancellation, progress tracking, and statistics.
    """

    def __init__(self, redis_url: str = None, queue_name: str = "hermes:tasks"):
        self.redis_url = redis_url or os.environ.get(
            "REDIS_URL", "redis://localhost:6379/0"
        )
        self.queue_name = queue_name
        self.status_prefix = "hermes:task:status:"
        self.result_prefix = "hermes:task:result:"
        self.result_ttl = 86400  # 24 hours
        self._client: Optional[Any] = None
        self._degraded = False  # True = using InMemoryTaskQueue fallback
        self._fallback: Optional[InMemoryTaskQueue] = None

    async def connect(self):
        """
        Establish connection to Redis.

        FIX Problem-9: gracefully degrades to InMemoryTaskQueue instead of
        raising RuntimeError when Redis is unavailable.
        """
        if not REDIS_AVAILABLE:
            print(
                "[task_engine] WARNING: redis package not installed — "
                "falling back to in-memory task queue (pip install redis to enable)",
                flush=True,
            )
            self._degraded = True
            self._fallback = InMemoryTaskQueue(self.queue_name)
            return

        try:
            client = aioredis.from_url(self.redis_url, decode_responses=True)
            await client.ping()
            self._client = client
            self._degraded = False
            print(
                f"[task_engine] Redis connected at {self.redis_url}",
                flush=True,
            )
        except Exception as exc:
            print(
                f"[task_engine] WARNING: Redis connection failed ({exc}) — "
                f"falling back to in-memory task queue",
                flush=True,
            )
            self._degraded = True
            self._fallback = InMemoryTaskQueue(self.queue_name)

    async def close(self):
        """Close the Redis connection."""
        if self._degraded and self._fallback:
            await self._fallback.close()
        elif self._client:
            await self._client.close()
            self._client = None

    async def submit(self, task: Task) -> str:
        """Submit a task to the queue. Returns the task ID."""
        if self._degraded:
            return await self._fallback.submit(task)

        if not self._client:
            await self.connect()

        await self._client.hset(
            f"{self.status_prefix}{task.id}",
            mapping={
                "task_json": task.to_json(),
                "status": task.status,
                "progress": str(task.progress),
                "progress_message": task.progress_message,
                "submitted_at": str(task.created_at),
            },
        )

        score = task.priority * 1e10 + task.created_at
        await self._client.zadd(self.queue_name, {task.id: score})

        await self._client.publish("hermes:tasks:events", json.dumps({
            "event": "task_submitted",
            "task_id": task.id,
            "name": task.name,
            "priority": task.priority,
        }))

        return task.id

    async def dequeue(self, timeout: int = 5) -> Optional[Task]:
        """Pop the highest-priority task from the queue."""
        if self._degraded:
            return await self._fallback.dequeue(timeout)

        if not self._client:
            await self.connect()

        result = await self._client.zpopmin(self.queue_name, count=1)
        if not result:
            return None

        task_id, _ = result[0]
        task_data = await self._client.hget(
            f"{self.status_prefix}{task_id}", "task_json"
        )
        if not task_data:
            return None

        task = Task.from_json(task_data)
        task.status = TaskStatus.RUNNING
        task.started_at = time.time()

        await self._update_status(task)
        return task

    async def complete(self, task: Task, result: str = ""):
        """Mark a task as completed with an optional result string."""
        if self._degraded:
            return await self._fallback.complete(task, result)

        task.status = TaskStatus.COMPLETED
        task.completed_at = time.time()
        task.result = result
        task.progress = 1.0
        await self._update_status(task)
        await self._store_result(task)
        await self._publish_event("task_completed", task)

    async def fail(self, task: Task, error: str):
        """Mark a task as failed. Retries if retries remain."""
        if self._degraded:
            return await self._fallback.fail(task, error)

        task.retries += 1
        if task.retries < task.max_retries:
            task.status = TaskStatus.RETRYING
            task.error = error
            await self._update_status(task)
            await self._publish_event("task_retrying", task)
            await asyncio.sleep(min(60, task.retries * 5))
            task.status = TaskStatus.PENDING
            await self.submit(task)
        else:
            task.status = TaskStatus.FAILED
            task.completed_at = time.time()
            task.error = error
            await self._update_status(task)
            await self._store_result(task)
            await self._publish_event("task_failed", task)

    async def cancel(self, task_id: str) -> bool:
        """Cancel a pending task by removing it from the queue."""
        if self._degraded:
            return await self._fallback.cancel(task_id)

        if not self._client:
            return False
        removed = await self._client.zrem(self.queue_name, task_id)
        if removed:
            await self._client.hset(
                f"{self.status_prefix}{task_id}", "status", TaskStatus.CANCELLED
            )
            return True
        return False

    async def get_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Get current status of a task."""
        if self._degraded:
            return await self._fallback.get_status(task_id)

        if not self._client:
            await self.connect()
        data = await self._client.hgetall(f"{self.status_prefix}{task_id}")
        return data if data else None

    async def update_progress(self, task: Task, progress: float, message: str = ""):
        """Update task progress (0.0 to 1.0) with optional message."""
        if self._degraded:
            return await self._fallback.update_progress(task, progress, message)

        task.progress = min(1.0, max(0.0, progress))
        task.progress_message = message
        if self._client:
            await self._client.hset(
                f"{self.status_prefix}{task.id}",
                mapping={
                    "progress": str(task.progress),
                    "progress_message": message,
                },
            )
            await self._publish_event("task_progress", task)

    async def get_queue_stats(self) -> Dict[str, Any]:
        """Get queue statistics."""
        if self._degraded:
            return await self._fallback.get_queue_stats()

        if not self._client:
            await self.connect()
        pending = await self._client.zcard(self.queue_name)
        return {
            "pending": pending,
            "queue_name": self.queue_name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    async def get_pending_tasks(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Get list of pending tasks with their priorities."""
        if self._degraded:
            return await self._fallback.get_pending_tasks(limit)

        if not self._client:
            await self.connect()
        items = await self._client.zrange(
            self.queue_name, 0, limit - 1, withscores=True
        )
        tasks = []
        for task_id, score in items:
            status = await self.get_status(task_id)
            if status:
                tasks.append({
                    "task_id": task_id,
                    "score": score,
                    "status": status.get("status", "unknown"),
                    "progress": status.get("progress", "0"),
                })
        return tasks

    @property
    def is_degraded(self) -> bool:
        """True when running without Redis (in-memory fallback active)."""
        return self._degraded

    async def _update_status(self, task: Task):
        """Update task status in Redis."""
        if self._client:
            await self._client.hset(
                f"{self.status_prefix}{task.id}",
                mapping={
                    "task_json": task.to_json(),
                    "status": task.status,
                    "progress": str(task.progress),
                    "progress_message": task.progress_message,
                },
            )

    async def _store_result(self, task: Task):
        """Store task result with TTL for later retrieval."""
        if self._client:
            await self._client.setex(
                f"{self.result_prefix}{task.id}",
                self.result_ttl,
                task.to_json(),
            )

    async def _publish_event(self, event_type: str, task: Task):
        """Publish task lifecycle event to Redis PubSub."""
        if self._client:
            await self._client.publish("hermes:tasks:events", json.dumps({
                "event": event_type,
                "task_id": task.id,
                "name": task.name,
                "status": task.status,
                "progress": task.progress,
                "progress_message": task.progress_message,
                "duration": task.duration,
            }))


# -- Worker Pool -----------------------------------------------------------------


class WorkerPool:
    """
    Async worker pool that processes tasks from the queue.

    Each worker is an asyncio task that continuously dequeues work,
    executes it via registered skill handlers, and reports results.
    """

    def __init__(self, queue: TaskQueue, num_workers: int = 5):
        self.queue = queue
        self.num_workers = num_workers
        self._workers: List[asyncio.Task] = []
        self._running = False
        self._skill_handlers: Dict[str, Callable] = {}
        self._active_tasks: Dict[str, Task] = {}

    def register_skill(self, skill_name: str, handler: Callable):
        """Register a skill handler function."""
        self._skill_handlers[skill_name] = handler

    async def start(self):
        """Start the worker pool with configured number of workers."""
        self._running = True
        await self.queue.connect()
        self._workers = [
            asyncio.create_task(self._worker_loop(i))
            for i in range(self.num_workers)
        ]
        degraded = " [in-memory fallback]" if self.queue.is_degraded else ""
        print(
            f"[task_engine] Worker pool started: {self.num_workers} workers{degraded}",
            flush=True,
        )

    async def stop(self):
        """Stop all workers gracefully."""
        self._running = False
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        await self.queue.close()
        print("[task_engine] Worker pool stopped", flush=True)

    @property
    def active_count(self) -> int:
        """Number of currently active tasks."""
        return len(self._active_tasks)

    async def _worker_loop(self, worker_id: int):
        """Main worker loop: dequeue -> execute -> repeat."""
        while self._running:
            try:
                task = await self.queue.dequeue(timeout=5)
                if not task:
                    await asyncio.sleep(1)
                    continue

                self._active_tasks[task.id] = task
                print(
                    f"[worker-{worker_id}] Processing: {task.name} "
                    f"(priority={task.priority}, id={task.id})",
                    flush=True,
                )

                try:
                    result = await asyncio.wait_for(
                        self._execute_task(task),
                        timeout=task.timeout,
                    )
                    await self.queue.complete(task, result or "completed")
                    print(
                        f"[worker-{worker_id}] Completed: {task.name} "
                        f"({task.duration:.1f}s)",
                        flush=True,
                    )

                except asyncio.TimeoutError:
                    await self.queue.fail(task, f"Timeout after {task.timeout}s")
                    print(
                        f"[worker-{worker_id}] Timeout: {task.name}",
                        flush=True,
                    )

                except Exception as e:
                    error_detail = f"{type(e).__name__}: {str(e)}"
                    await self.queue.fail(task, error_detail)
                    print(
                        f"[worker-{worker_id}] Failed: {task.name}: {error_detail}",
                        flush=True,
                    )

                finally:
                    self._active_tasks.pop(task.id, None)

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[worker-{worker_id}] Loop error: {e}", flush=True)
                await asyncio.sleep(5)

    async def _execute_task(self, task: Task) -> str:
        """Execute a task using the registered skill handler."""
        handler = self._skill_handlers.get(task.skill)
        if not handler:
            if "command" in task.params:
                return await self._execute_shell(task)
            raise ValueError(f"No handler registered for skill: {task.skill}")
        return await handler(task)

    async def _execute_shell(self, task: Task) -> str:
        """Execute a shell command task as fallback."""
        cmd = task.params.get("command", "")
        workdir = task.params.get("workdir", "/tmp")

        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir,
        )

        stdout, stderr = await proc.communicate()
        output = (stdout or b"").decode() + (stderr or b"").decode()

        if proc.returncode != 0:
            raise RuntimeError(
                f"Command failed (rc={proc.returncode}): {output[-500:]}"
            )

        return output[-2000:]


# -- Status Streamer (Telegram Integration) --------------------------------------


class StatusStreamer:
    """
    Streams task status updates to Telegram in real-time.

    Subscribes to task events via Redis PubSub and sends
    formatted status messages to the operator.
    """

    def __init__(self, redis_url: str = None):
        self.redis_url = redis_url or os.environ.get(
            "REDIS_URL", "redis://localhost:6379/0"
        )
        self.bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        self._running = False
        self._message_ids: Dict[str, int] = {}

    async def start(self):
        """Start streaming task status to Telegram via Redis PubSub."""
        if not self.bot_token or not self.chat_id:
            print(
                "[streamer] No Telegram config - status streaming disabled",
                flush=True,
            )
            return

        if not REDIS_AVAILABLE:
            print("[streamer] Redis not available - status streaming disabled", flush=True)
            return

        self._running = True
        try:
            client = aioredis.from_url(self.redis_url, decode_responses=True)
            await client.ping()
        except Exception as exc:
            print(f"[streamer] Redis unavailable ({exc}) - status streaming disabled", flush=True)
            return

        pubsub = client.pubsub()
        await pubsub.subscribe("hermes:tasks:events")

        while self._running:
            try:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if message and message["type"] == "message":
                    event = json.loads(message["data"])
                    await self._handle_event(event)
            except Exception as e:
                print(f"[streamer] Error: {e}", flush=True)
                await asyncio.sleep(5)

        await pubsub.unsubscribe()
        await client.close()

    async def stop(self):
        """Stop the status streamer."""
        self._running = False

    async def _handle_event(self, event: dict):
        """Format and send task event to Telegram."""
        event_type = event.get("event", "")
        task_name = event.get("name", "unknown")

        if event_type == "task_submitted":
            msg = f"[QUEUED] {task_name}"
        elif event_type == "task_completed":
            duration = event.get("duration", 0)
            msg = f"[DONE] {task_name} ({duration:.0f}s)"
        elif event_type == "task_failed":
            msg = f"[FAILED] {task_name}"
        elif event_type == "task_retrying":
            msg = f"[RETRY] {task_name}"
        elif event_type == "task_progress":
            progress = event.get("progress", 0)
            progress_msg = event.get("progress_message", "")
            pct = int(progress * 100)
            if pct % 25 == 0 and pct > 0:
                msg = f"[PROGRESS] {task_name}: {pct}% {progress_msg}"
            else:
                return
        else:
            return

        await self._send_telegram(msg)

    async def _send_telegram(self, text: str):
        """Send a message via Telegram Bot API."""
        import urllib.request

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = json.dumps({
            "chat_id": self.chat_id,
            "text": text,
        }).encode()
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print(f"[streamer] Telegram error: {e}", flush=True)


# -- Convenience Functions -------------------------------------------------------


async def submit_task(name: str, skill: str, params: dict,
                      priority: int = TaskPriority.NORMAL,
                      timeout: int = 3600,
                      max_retries: int = 3) -> str:
    """Convenience function to submit a task from anywhere in Hermes."""
    queue = TaskQueue()
    await queue.connect()
    task = Task(
        name=name,
        skill=skill,
        params=params,
        priority=priority,
        timeout=timeout,
        max_retries=max_retries,
    )
    task_id = await queue.submit(task)
    await queue.close()
    return task_id
