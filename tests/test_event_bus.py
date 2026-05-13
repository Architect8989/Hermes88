#!/usr/bin/env python3
"""
Tests for rhodawk_core.event_bus module.

Tests Event data model creation, JSON serialization/deserialization,
channel constants, priority values, and event ID generation.

Copyright (c) 2024-2025 Rhodawk AI. All rights reserved.
"""
import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    import pytest
except ImportError:
    class _FakePytest:
        @staticmethod
        def fixture(*args, **kwargs):
            def decorator(fn):
                return fn
            if args and callable(args[0]):
                return args[0]
            return decorator
    pytest = _FakePytest()

from rhodawk_core.event_bus import Event, EventBus


# -- Event Creation Tests ------------------------------------------------------


class TestEventCreation:
    """Tests for Event data model creation."""

    def test_event_creation(self):
        """Test creating an Event with default values."""
        event = Event(type="test.event")
        assert event.type == "test.event"
        assert event.priority == "normal"
        assert event.channel == "hermes:events"
        assert event.payload == {}
        assert event.source == ""
        assert event.id.startswith("evt_")
        assert event.timestamp  # Should be auto-generated ISO timestamp

    def test_event_creation_with_params(self):
        """Test creating an Event with explicit values."""
        event = Event(
            id="evt_custom_12345678",
            type="github.push",
            priority="high",
            payload={"repo": "hermes88", "branch": "main", "commits": 3},
            source="webhook-receiver",
            channel="hermes:alerts",
            timestamp="2024-01-01T00:00:00+00:00",
        )
        assert event.id == "evt_custom_12345678"
        assert event.type == "github.push"
        assert event.priority == "high"
        assert event.payload == {"repo": "hermes88", "branch": "main", "commits": 3}
        assert event.source == "webhook-receiver"
        assert event.channel == "hermes:alerts"

    def test_event_default_id_format(self):
        """Test that auto-generated event IDs follow the expected format."""
        event = Event(type="test")
        # Format: evt_{timestamp_ms}_{uuid_hex[:8]}
        assert event.id.startswith("evt_")
        parts = event.id.split("_")
        assert len(parts) >= 3  # evt, timestamp, uuid


# -- Event Serialization Tests -------------------------------------------------


class TestEventSerialization:
    """Tests for Event JSON serialization and deserialization."""

    def test_event_serialization(self):
        """Test to_json produces valid JSON."""
        event = Event(
            id="evt_serial_001",
            type="task.completed",
            priority="normal",
            payload={"task_id": "task_123", "result": "success"},
            source="task-worker",
            channel="hermes:tasks",
        )
        json_str = event.to_json()
        data = json.loads(json_str)

        assert data["id"] == "evt_serial_001"
        assert data["type"] == "task.completed"
        assert data["priority"] == "normal"
        assert data["payload"]["task_id"] == "task_123"
        assert data["source"] == "task-worker"
        assert data["channel"] == "hermes:tasks"

    def test_event_from_json_roundtrip(self):
        """Test that to_json -> from_json produces equivalent event."""
        original = Event(
            id="evt_roundtrip",
            type="system.health",
            priority="low",
            payload={"component": "redis", "status": "healthy"},
            source="watchdog",
            channel="hermes:health",
        )
        json_str = original.to_json()
        restored = Event.from_json(json_str)

        assert restored.id == original.id
        assert restored.type == original.type
        assert restored.priority == original.priority
        assert restored.payload == original.payload
        assert restored.source == original.source
        assert restored.channel == original.channel

    def test_event_to_dict(self):
        """Test to_dict conversion."""
        event = Event(
            id="evt_dict_001",
            type="alert.warning",
            payload={"message": "High memory usage"},
        )
        d = event.to_dict()
        assert isinstance(d, dict)
        assert d["id"] == "evt_dict_001"
        assert d["type"] == "alert.warning"
        assert d["payload"]["message"] == "High memory usage"

    def test_event_from_json_ignores_extra_fields(self):
        """Test that from_json handles extra fields gracefully."""
        data = {
            "id": "evt_extra",
            "type": "test",
            "timestamp": "2024-01-01T00:00:00Z",
            "priority": "normal",
            "payload": {},
            "source": "",
            "channel": "hermes:events",
            "unknown_field": "should be ignored",
        }
        event = Event.from_json(json.dumps(data))
        assert event.id == "evt_extra"
        assert event.type == "test"


# -- Channel Constants Tests ---------------------------------------------------


class TestChannelConstants:
    """Tests for EventBus channel constants."""

    def test_channel_constants(self):
        """Test that all standard channels are defined."""
        assert EventBus.CHANNEL_EVENTS == "hermes:events"
        assert EventBus.CHANNEL_TASKS == "hermes:tasks"
        assert EventBus.CHANNEL_ALERTS == "hermes:alerts"
        assert EventBus.CHANNEL_HEALTH == "hermes:health"

    def test_all_channels_list(self):
        """Test ALL_CHANNELS contains all defined channels."""
        assert len(EventBus.ALL_CHANNELS) == 4
        assert EventBus.CHANNEL_EVENTS in EventBus.ALL_CHANNELS
        assert EventBus.CHANNEL_TASKS in EventBus.ALL_CHANNELS
        assert EventBus.CHANNEL_ALERTS in EventBus.ALL_CHANNELS
        assert EventBus.CHANNEL_HEALTH in EventBus.ALL_CHANNELS


# -- Priority Values Tests -----------------------------------------------------


class TestPriorityValues:
    """Tests for event priority values and routing."""

    def test_priority_values(self):
        """Test that priority values are valid strings."""
        valid_priorities = ["critical", "high", "normal", "low"]
        for p in valid_priorities:
            event = Event(type="test", priority=p)
            assert event.priority == p

    def test_default_priority(self):
        """Test that default priority is normal."""
        event = Event(type="test")
        assert event.priority == "normal"


# -- Event ID Generation Tests -------------------------------------------------


class TestEventIdGeneration:
    """Tests for event ID generation uniqueness."""

    def test_event_id_generation(self):
        """Test that generated event IDs are unique."""
        ids = set()
        for _ in range(100):
            event = Event(type="test")
            ids.add(event.id)
        # All 100 should be unique
        assert len(ids) == 100

    def test_event_id_prefix(self):
        """Test that event IDs start with expected prefix."""
        event = Event(type="test")
        assert event.id.startswith("evt_")

    def test_event_id_contains_timestamp(self):
        """Test that event ID contains a timestamp component."""
        before = int(time.time() * 1000)
        event = Event(type="test")
        after = int(time.time() * 1000)
        # Extract timestamp from ID (format: evt_{timestamp}_{uuid})
        parts = event.id.split("_")
        # parts[1] should be the timestamp in milliseconds
        ts = int(parts[1])
        assert before <= ts <= after


# -- EventBus Initialization Tests ---------------------------------------------


class TestEventBusInit:
    """Tests for EventBus initialization."""

    def test_event_bus_defaults(self):
        """Test EventBus default state."""
        bus = EventBus()
        stats = bus.get_stats()
        assert stats["published"] == 0
        assert stats["received"] == 0
        assert stats["processed"] == 0
        assert stats["errors"] == 0
        assert stats["connected"] is False
        assert stats["consuming"] is False

    def test_event_bus_custom_url(self):
        """Test EventBus with custom Redis URL."""
        bus = EventBus(redis_url="redis://custom:6380/2")
        assert bus.redis_url == "redis://custom:6380/2"

    def test_event_bus_history(self):
        """Test event history storage (local mode, no Redis)."""
        bus = EventBus()
        history = bus.get_history(limit=10)
        assert history == []

    def test_event_bus_dead_letters(self):
        """Test dead letter queue access."""
        bus = EventBus()
        dead = bus.get_dead_letters(limit=10)
        assert dead == []
