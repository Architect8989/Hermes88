#!/usr/bin/env python3
"""
gateway/event_consumer.py — DEAD CODE — NOT CALLED BY ANY RUNNING PROCESS.

The claim in earlier versions ("runs as a background task within the gateway
process") is FALSE. gateway/run.py does os.execvpe(hermes | openclaw) which
replaces the Python process. This file is never imported at runtime.

If you want event-driven behavior, you have two options:
  A) Use hermes-agent's built-in event bus (configured via gateway.yaml:
     agent.event_bus_url). hermes-agent subscribes to Redis internally.
  B) Run this file as a standalone process (separate supervisord program)
     that consumes events and routes them to the hermes/openclaw gateway
     via their HTTP/WebSocket control APIs.

Option B requires writing the control-plane adapter — that code does not exist.
Until it does, this module has no callers and no effect on the running system.

Rhodawk AI -- Peak Architecture v10.0
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Callable, Optional, Any
from dataclasses import dataclass, field

# Ensure rhodawk_core is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from rhodawk_core.event_bus import EventBus
    EVENT_BUS_AVAILABLE = True
except ImportError:
    EVENT_BUS_AVAILABLE = False

try:
    from rhodawk_core.audit import AuditLogger
    AUDIT_AVAILABLE = True
except ImportError:
    AUDIT_AVAILABLE = False

try:
    import redis.asyncio as aioredis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False


@dataclass
class EventFilter:
    """Defines which events a handler wants to receive."""
    event_types: list = field(default_factory=list)
    priority_min: str = "low"
    channels: list = field(default_factory=list)

    PRIORITY_ORDER = {"critical": 0, "high": 1, "normal": 2, "low": 3, "background": 4}

    def matches(self, event: dict) -> bool:
        """Check if an event matches this filter."""
        event_type = event.get("type", "")
        priority = event.get("priority", "normal")

        # Check event type filter
        if self.event_types and event_type not in self.event_types:
            # Allow prefix matching (e.g., "github." matches "github.push")
            prefix_match = any(
                event_type.startswith(et) for et in self.event_types
                if et.endswith(".")
            )
            if not prefix_match:
                return False

        # Check priority filter
        event_priority = self.PRIORITY_ORDER.get(priority, 3)
        min_priority = self.PRIORITY_ORDER.get(self.priority_min, 3)
        if event_priority > min_priority:
            return False

        return True


@dataclass
class EventHandler:
    """A registered event handler with its filter."""
    name: str
    callback: Callable
    filter: EventFilter
    enabled: bool = True


class EventConsumer:
    """
    Background event consumer that bridges the Redis event bus to the gateway.

    Subscribes to multiple Redis PubSub channels and routes events to
    registered handlers. Runs as an asyncio task within the gateway.

    Usage:
        consumer = EventConsumer()
        consumer.register_handler("ci_failure", handle_ci_failure,
                                  EventFilter(event_types=["github.ci_failure"]))
        await consumer.start()

    The consumer handles:
    - GitHub webhook events (CI failures, security advisories, PR reviews)
    - System health alerts (disk, memory, process crashes)
    - Task queue events (completion, failure)
    - Financial events (payment failures, subscription changes)
    - Proactive intelligence triggers
    """

    def __init__(self, config: Optional[dict] = None):
        """
        Initialize the event consumer.

        Args:
            config: Optional configuration dict with Redis URL, channels, etc.
        """
        self.config = config or {}
        self._handlers: list[EventHandler] = []
        self._running: bool = False
        self._task: Optional[asyncio.Task] = None
        self._reconnect_delay: float = 5.0
        self._max_reconnect_delay: float = 60.0
        self._events_processed: int = 0
        self._events_errors: int = 0
        self._start_time: float = 0.0

        # Redis configuration
        self._redis_url: str = self.config.get(
            "redis_url",
            os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        )
        self._channels: list = self.config.get("channels", [
            "hermes:events",
            "hermes:alerts",
            "hermes:tasks:events",
            "hermes:health",
        ])

        # Audit logging
        self._audit: Optional[Any] = None
        if AUDIT_AVAILABLE:
            try:
                self._audit = AuditLogger()
            except Exception:
                pass

        # Notification callback (for sending messages to operator)
        self._notify_callback: Optional[Callable] = None

    def register_handler(self, name: str, callback: Callable,
                         event_filter: Optional[EventFilter] = None):
        """
        Register an event handler.

        Args:
            name: Unique name for this handler.
            callback: Async callable that processes matching events.
                     Signature: async def handler(event: dict) -> None
            event_filter: Filter defining which events this handler receives.
                         If None, handler receives all events.
        """
        handler = EventHandler(
            name=name,
            callback=callback,
            filter=event_filter or EventFilter(),
        )
        self._handlers.append(handler)

    def set_notify_callback(self, callback: Callable):
        """
        Set the callback for sending notifications to the operator.

        Args:
            callback: Async callable with signature:
                     async def notify(message: str) -> None
        """
        self._notify_callback = callback

    async def start(self):
        """
        Start the event consumer as a background task.
        Connects to Redis and begins listening for events.
        """
        if self._running:
            return

        if not REDIS_AVAILABLE:
            print("[event_consumer] Redis not available, consumer disabled", flush=True)
            return

        self._running = True
        self._start_time = time.time()
        self._task = asyncio.create_task(self._consume_loop())
        print(
            f"[event_consumer] Started, subscribing to: {', '.join(self._channels)}",
            flush=True,
        )

    async def stop(self):
        """Stop the event consumer gracefully."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        duration = time.time() - self._start_time if self._start_time else 0
        print(
            f"[event_consumer] Stopped. Processed {self._events_processed} events "
            f"({self._events_errors} errors) over {duration:.0f}s",
            flush=True,
        )

    async def _consume_loop(self):
        """Main consumption loop with automatic reconnection."""
        reconnect_delay = self._reconnect_delay

        while self._running:
            try:
                client = aioredis.from_url(
                    self._redis_url, decode_responses=True
                )
                pubsub = client.pubsub()

                # Subscribe to all configured channels
                await pubsub.subscribe(*self._channels)
                print("[event_consumer] Connected to Redis PubSub", flush=True)
                reconnect_delay = self._reconnect_delay  # Reset on success

                # Process messages
                while self._running:
                    try:
                        message = await pubsub.get_message(
                            ignore_subscribe_messages=True, timeout=1.0
                        )
                        if message and message["type"] == "message":
                            await self._process_message(message)
                    except asyncio.CancelledError:
                        break
                    except Exception as e:
                        print(f"[event_consumer] Message error: {e}", flush=True)
                        self._events_errors += 1
                        await asyncio.sleep(0.1)

                # Cleanup
                await pubsub.unsubscribe()
                await client.close()

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(
                    f"[event_consumer] Connection error: {e}, "
                    f"reconnecting in {reconnect_delay:.0f}s",
                    flush=True,
                )
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(
                    reconnect_delay * 2, self._max_reconnect_delay
                )

    async def _process_message(self, message: dict):
        """Process a single PubSub message."""
        try:
            data = message.get("data", "")
            if isinstance(data, bytes):
                data = data.decode("utf-8")

            event = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            print(f"[event_consumer] Invalid message format: {e}", flush=True)
            self._events_errors += 1
            return

        self._events_processed += 1

        # Route to matching handlers
        for handler in self._handlers:
            if not handler.enabled:
                continue
            if handler.filter.matches(event):
                try:
                    await handler.callback(event)
                except Exception as e:
                    print(
                        f"[event_consumer] Handler '{handler.name}' error: {e}",
                        flush=True,
                    )
                    self._events_errors += 1

        # Audit log for high-priority events
        if self._audit and event.get("priority") in ("critical", "high"):
            self._audit.log("event_consumed", {
                "event_type": event.get("type", "unknown"),
                "priority": event.get("priority", "normal"),
                "event_id": event.get("id", ""),
            })

    async def notify_operator(self, message: str):
        """Send a notification to the operator via the registered callback."""
        if self._notify_callback:
            try:
                await self._notify_callback(message)
            except Exception as e:
                print(f"[event_consumer] Notify error: {e}", flush=True)

    @property
    def is_running(self) -> bool:
        """Whether the consumer is currently active."""
        return self._running

    @property
    def stats(self) -> dict:
        """Get consumer statistics."""
        return {
            "running": self._running,
            "events_processed": self._events_processed,
            "events_errors": self._events_errors,
            "handlers_registered": len(self._handlers),
            "channels": self._channels,
            "uptime_seconds": time.time() - self._start_time if self._start_time else 0,
        }


# -- Default Event Handlers -------------------------------------------------------

async def handle_ci_failure(event: dict):
    """Handle CI pipeline failure events."""
    payload = event.get("payload", {})
    repo = payload.get("repo", "unknown")
    workflow = payload.get("workflow", "unknown")
    branch = payload.get("branch", "main")
    url = payload.get("url", "")

    print(
        f"[event_consumer] CI FAILURE: {repo}/{workflow} on {branch}",
        flush=True,
    )
    # The gateway should pick this up and initiate auto-fix


async def handle_security_advisory(event: dict):
    """Handle security advisory events."""
    payload = event.get("payload", {})
    severity = payload.get("severity", "unknown")
    summary = payload.get("summary", "")

    print(
        f"[event_consumer] SECURITY [{severity.upper()}]: {summary[:100]}",
        flush=True,
    )


async def handle_task_completed(event: dict):
    """Handle task completion events."""
    payload = event.get("payload", {})
    task_name = payload.get("name", "unknown")
    duration = payload.get("duration", 0)

    print(
        f"[event_consumer] TASK DONE: {task_name} ({duration:.0f}s)",
        flush=True,
    )


async def handle_payment_failed(event: dict):
    """Handle payment failure events from Stripe."""
    payload = event.get("payload", {})
    amount = payload.get("amount", 0)
    customer = payload.get("customer", "unknown")

    print(
        f"[event_consumer] PAYMENT FAILED: ${amount/100:.2f} from {customer}",
        flush=True,
    )


async def handle_system_alert(event: dict):
    """Handle system health alerts."""
    payload = event.get("payload", {})
    alert_type = payload.get("alert_type", "unknown")
    message = payload.get("message", "")

    print(
        f"[event_consumer] SYSTEM ALERT [{alert_type}]: {message}",
        flush=True,
    )


# -- Factory Function -------------------------------------------------------------

def create_default_consumer(config: Optional[dict] = None) -> EventConsumer:
    """
    Create an EventConsumer with default handlers registered.

    This is the standard way to create a consumer for the gateway.

    Args:
        config: Optional configuration dict.

    Returns:
        Configured EventConsumer ready to start.
    """
    consumer = EventConsumer(config)

    # Register default handlers
    consumer.register_handler(
        "ci_failure",
        handle_ci_failure,
        EventFilter(event_types=["github.ci_failure"], priority_min="high"),
    )
    consumer.register_handler(
        "security_advisory",
        handle_security_advisory,
        EventFilter(event_types=["github.security_advisory"], priority_min="high"),
    )
    consumer.register_handler(
        "task_completed",
        handle_task_completed,
        EventFilter(event_types=["task_completed"]),
    )
    consumer.register_handler(
        "payment_failed",
        handle_payment_failed,
        EventFilter(event_types=["stripe.payment_failed"], priority_min="high"),
    )
    consumer.register_handler(
        "system_alert",
        handle_system_alert,
        EventFilter(event_types=["system."], priority_min="normal"),
    )

    return consumer
