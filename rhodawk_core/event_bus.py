#!/usr/bin/env python3
"""
Rhodawk Core Event Bus.

Redis PubSub-based event system for publishing, subscribing, and routing
events across Hermes components. Provides channel management, event filtering,
priority routing, and background consumer coroutines.

Channels:
  - hermes:events   - General event stream
  - hermes:tasks    - Task lifecycle events
  - hermes:alerts   - High-priority alerts
  - hermes:health   - System health events

Copyright (c) 2024-2025 Rhodawk AI. All rights reserved.
"""
import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional, Callable, Dict, List, Any, Set

try:
    import redis.asyncio as aioredis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False


# -- Event Models ----------------------------------------------------------------


@dataclass
class Event:
    """A single event in the Rhodawk event system."""
    id: str = field(default_factory=lambda: f"evt_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}")
    type: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    priority: str = "normal"
    payload: dict = field(default_factory=dict)
    source: str = ""
    channel: str = "hermes:events"

    def to_json(self) -> str:
        """Serialize event to JSON."""
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str) -> "Event":
        """Deserialize event from JSON."""
        d = json.loads(data)
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return asdict(self)


# -- Event Bus -------------------------------------------------------------------


class EventBus:
    """
    Redis PubSub event bus for the Rhodawk system.

    Provides:
    - Publishing events to specific channels
    - Subscribing to channels with handler callbacks
    - Event filtering by type and priority
    - Background consumer coroutine for gateway integration
    - Event history for debugging
    - Dead letter queue for failed event processing
    """

    # Standard channels
    CHANNEL_EVENTS = "hermes:events"
    CHANNEL_TASKS = "hermes:tasks"
    CHANNEL_ALERTS = "hermes:alerts"
    CHANNEL_HEALTH = "hermes:health"

    ALL_CHANNELS = [CHANNEL_EVENTS, CHANNEL_TASKS, CHANNEL_ALERTS, CHANNEL_HEALTH]

    def __init__(self, redis_url: str = None):
        self.redis_url = redis_url or os.environ.get(
            "REDIS_URL", "redis://localhost:6379/1"
        )
        self._client: Optional[Any] = None
        self._pubsub: Optional[Any] = None
        self._running = False
        self._handlers: Dict[str, List[Callable]] = {}
        self._type_handlers: Dict[str, List[Callable]] = {}
        self._subscribed_channels: Set[str] = set()
        self._event_history: List[Event] = []
        self._history_limit = 500
        self._dead_letter: List[Dict[str, Any]] = []
        self._dead_letter_limit = 100
        self._stats = {
            "published": 0,
            "received": 0,
            "processed": 0,
            "errors": 0,
        }

    async def connect(self):
        """Establish connection to Redis."""
        if not REDIS_AVAILABLE:
            print("[event_bus] Redis not available - running in local-only mode", flush=True)
            return

        try:
            self._client = aioredis.from_url(self.redis_url, decode_responses=True)
            await self._client.ping()
            print("[event_bus] Connected to Redis", flush=True)
        except Exception as e:
            print(f"[event_bus] Redis connection failed: {e}", flush=True)
            self._client = None

    async def close(self):
        """Close Redis connection and clean up."""
        self._running = False
        if self._pubsub:
            await self._pubsub.unsubscribe()
            await self._pubsub.close()
            self._pubsub = None
        if self._client:
            await self._client.close()
            self._client = None

    async def publish(self, event_type: str, payload: dict,
                      priority: str = "normal",
                      channel: Optional[str] = None,
                      source: str = "") -> str:
        """
        Publish an event to the event bus.

        Args:
            event_type: Type identifier (e.g., "github.push", "system.health")
            payload: Event data dictionary
            priority: Event priority ("critical", "high", "normal", "low")
            channel: Target channel (defaults to hermes:events)
            source: Source identifier

        Returns:
            The event ID.
        """
        if channel is None:
            if priority in ("critical", "high"):
                channel = self.CHANNEL_ALERTS
            else:
                channel = self.CHANNEL_EVENTS

        event = Event(
            type=event_type,
            priority=priority,
            payload=payload,
            source=source,
            channel=channel,
        )

        # Store in history
        self._event_history.append(event)
        if len(self._event_history) > self._history_limit:
            self._event_history = self._event_history[-self._history_limit:]

        self._stats["published"] += 1

        # Publish to Redis
        if self._client:
            try:
                await self._client.publish(channel, event.to_json())
            except Exception as e:
                print(f"[event_bus] Publish failed: {e}", flush=True)

        # Also dispatch to local handlers
        await self._dispatch_local(event)

        return event.id

    async def subscribe(self, channel: str, handler: Callable):
        """
        Subscribe to a channel with a handler callback.

        The handler should be an async callable that accepts an Event.
        """
        if channel not in self._handlers:
            self._handlers[channel] = []
        self._handlers[channel].append(handler)
        self._subscribed_channels.add(channel)

    async def subscribe_type(self, event_type: str, handler: Callable):
        """
        Subscribe to a specific event type regardless of channel.

        The handler should be an async callable that accepts an Event.
        """
        if event_type not in self._type_handlers:
            self._type_handlers[event_type] = []
        self._type_handlers[event_type].append(handler)

    async def unsubscribe(self, channel: str, handler: Optional[Callable] = None):
        """
        Unsubscribe from a channel. If handler is None, remove all handlers.
        """
        if handler is None:
            self._handlers.pop(channel, None)
            self._subscribed_channels.discard(channel)
        elif channel in self._handlers:
            self._handlers[channel] = [
                h for h in self._handlers[channel] if h != handler
            ]

    async def consume(self, channels: Optional[List[str]] = None):
        """
        Start consuming events from Redis PubSub channels.

        This is the main background consumer coroutine that should be
        started as an asyncio task in the gateway process.
        """
        if not self._client:
            await self.connect()

        if not self._client:
            print("[event_bus] Cannot consume - no Redis connection", flush=True)
            return

        channels = channels or self.ALL_CHANNELS
        self._pubsub = self._client.pubsub()
        await self._pubsub.subscribe(*channels)
        self._running = True

        print(f"[event_bus] Consuming from channels: {channels}", flush=True)

        while self._running:
            try:
                message = await self._pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if message and message["type"] == "message":
                    self._stats["received"] += 1
                    try:
                        event = Event.from_json(message["data"])
                        await self._dispatch_local(event)
                        self._stats["processed"] += 1
                    except Exception as e:
                        self._stats["errors"] += 1
                        self._add_dead_letter(message["data"], str(e))
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[event_bus] Consumer error: {e}", flush=True)
                self._stats["errors"] += 1
                await asyncio.sleep(5)

        print("[event_bus] Consumer stopped", flush=True)

    async def stop(self):
        """Stop the consumer loop."""
        self._running = False

    async def _dispatch_local(self, event: Event):
        """Dispatch an event to registered local handlers."""
        # Channel-based handlers
        handlers = self._handlers.get(event.channel, [])
        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(event)
                else:
                    handler(event)
            except Exception as e:
                print(
                    f"[event_bus] Handler error for {event.type}: {e}",
                    flush=True,
                )
                self._stats["errors"] += 1

        # Type-based handlers
        type_handlers = self._type_handlers.get(event.type, [])
        for handler in type_handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(event)
                else:
                    handler(event)
            except Exception as e:
                print(
                    f"[event_bus] Type handler error for {event.type}: {e}",
                    flush=True,
                )
                self._stats["errors"] += 1

    def _add_dead_letter(self, raw_data: str, error: str):
        """Add a failed event to the dead letter queue."""
        self._dead_letter.append({
            "data": raw_data[:1000],
            "error": error,
            "timestamp": time.time(),
        })
        if len(self._dead_letter) > self._dead_letter_limit:
            self._dead_letter = self._dead_letter[-self._dead_letter_limit:]

    def get_history(self, limit: int = 50,
                    event_type: Optional[str] = None,
                    channel: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Get recent event history with optional filtering.

        Args:
            limit: Maximum events to return
            event_type: Filter by event type
            channel: Filter by channel
        """
        events = self._event_history
        if event_type:
            events = [e for e in events if e.type == event_type]
        if channel:
            events = [e for e in events if e.channel == channel]
        return [e.to_dict() for e in events[-limit:]]

    def get_dead_letters(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Get recent dead letter entries for debugging."""
        return self._dead_letter[-limit:]

    def get_stats(self) -> Dict[str, Any]:
        """Get event bus statistics."""
        return {
            **self._stats,
            "subscribed_channels": list(self._subscribed_channels),
            "handler_count": sum(len(h) for h in self._handlers.values()),
            "type_handler_count": sum(len(h) for h in self._type_handlers.values()),
            "history_size": len(self._event_history),
            "dead_letter_size": len(self._dead_letter),
            "connected": self._client is not None,
            "consuming": self._running,
        }

    async def emit_alert(self, title: str, message: str,
                         severity: str = "warning", source: str = ""):
        """Convenience method to emit an alert event."""
        return await self.publish(
            event_type=f"alert.{severity}",
            payload={"title": title, "message": message},
            priority="high" if severity in ("critical", "error") else "normal",
            channel=self.CHANNEL_ALERTS,
            source=source,
        )

    async def emit_health(self, component: str, status: str,
                          details: Optional[dict] = None):
        """Convenience method to emit a health check event."""
        return await self.publish(
            event_type="health.check",
            payload={
                "component": component,
                "status": status,
                "details": details or {},
            },
            priority="normal",
            channel=self.CHANNEL_HEALTH,
            source=component,
        )
