#!/usr/bin/env python3
"""
Rhodawk AI Hermes88 - Test Fixtures.

Provides shared pytest fixtures for the test suite including mock Redis,
mock OpenAI, in-memory SQLite for StructuredMemoryStore, temporary directories,
and sample data objects for memory and task testing.

Copyright (c) 2024-2025 Rhodawk AI. All rights reserved.
"""
import os
import sys
import json
import time
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

# Ensure rhodawk_core is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    import pytest
except ImportError:
    # Allow ast.parse to succeed without pytest installed
    class _FakePytest:
        @staticmethod
        def fixture(*args, **kwargs):
            def decorator(fn):
                return fn
            if args and callable(args[0]):
                return args[0]
            return decorator
    pytest = _FakePytest()

from rhodawk_core.memory import MemoryEntry, StructuredMemoryStore
from rhodawk_core.task_engine import Task, TaskStatus, TaskPriority
from rhodawk_core.event_bus import Event, EventBus
from rhodawk_core.orchestrator import Orchestrator, ModelConfig, RateLimiter
from rhodawk_core.synthesis import SynthesisEngine, CHANNELS


# -- Mock Redis Fixture --------------------------------------------------------


@pytest.fixture
def mock_redis():
    """Provide a MagicMock Redis client for testing without a real Redis server."""
    client = MagicMock()
    client.ping = MagicMock(return_value=True)
    client.hset = MagicMock(return_value=1)
    client.hget = MagicMock(return_value=None)
    client.hgetall = MagicMock(return_value={})
    client.publish = MagicMock(return_value=1)
    client.zadd = MagicMock(return_value=1)
    client.zpopmin = MagicMock(return_value=[])
    client.zcard = MagicMock(return_value=0)
    client.zrange = MagicMock(return_value=[])
    client.setex = MagicMock(return_value=True)
    client.delete = MagicMock(return_value=1)
    client.zrem = MagicMock(return_value=1)
    return client


# -- Mock OpenAI Fixture -------------------------------------------------------


@pytest.fixture
def mock_openai():
    """Provide a MagicMock AsyncOpenAI client for testing LLM calls."""
    client = MagicMock()
    client.chat = MagicMock()
    client.chat.completions = MagicMock()

    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "Test response from LLM"
    mock_response.usage.prompt_tokens = 10
    mock_response.usage.completion_tokens = 20
    mock_response.usage.total_tokens = 30

    client.chat.completions.create = AsyncMock(return_value=mock_response)
    return client


# -- In-Memory SQLite Fixture --------------------------------------------------


@pytest.fixture
def memory_db(tmp_path):
    """Provide an in-memory StructuredMemoryStore backed by a temp SQLite DB."""
    db_path = str(tmp_path / "test_memory.db")
    store = StructuredMemoryStore(db_path=db_path)
    yield store
    # Cleanup
    store.conn.close()


# -- Temporary Directory Fixture -----------------------------------------------


@pytest.fixture
def temp_dir(tmp_path):
    """Provide a temporary directory for file operations."""
    return tmp_path


# -- Sample Memory Entry Fixture -----------------------------------------------


@pytest.fixture
def sample_memory_entry():
    """Provide a sample MemoryEntry with test data."""
    return MemoryEntry(
        id="test-memory-001",
        content="The user prefers dark mode and uses VS Code as their primary editor.",
        category="preferences",
        tags=["user", "editor", "settings"],
        importance=0.8,
        created_at=1700000000.0,
        last_accessed=1700000000.0,
        access_count=5,
        source="telegram",
        related_ids=["test-memory-002"],
        metadata={"confidence": 0.95, "verified": True},
    )


# -- Sample Task Fixture -------------------------------------------------------


@pytest.fixture
def sample_task():
    """Provide a sample Task with test data."""
    return Task(
        id="task_test12345678",
        name="Test Code Review",
        description="Review the authentication module for security issues",
        priority=TaskPriority.HIGH,
        status=TaskStatus.PENDING,
        created_at=1700000000.0,
        timeout=1800,
        max_retries=3,
        skill="code_review",
        params={"repo": "rhodawk/hermes88", "path": "gateway/run.py"},
        metadata={"requester": "admin", "urgency": "high"},
    )


# -- Mock Telegram Fixture -----------------------------------------------------


@pytest.fixture
def mock_telegram():
    """Provide a MagicMock Telegram bot for testing message sending."""
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=12345))
    bot.edit_message_text = AsyncMock(return_value=MagicMock(message_id=12345))
    bot.send_document = AsyncMock(return_value=MagicMock(message_id=12346))
    bot.token = "fake-bot-token"
    return bot


# -- Event Bus Fixture ---------------------------------------------------------


@pytest.fixture
def event_bus():
    """Provide a fresh EventBus instance (no Redis connection)."""
    bus = EventBus(redis_url="redis://localhost:6379/1")
    return bus


# -- Orchestrator Fixture ------------------------------------------------------


@pytest.fixture
def orchestrator():
    """Provide an Orchestrator instance for testing model routing."""
    return Orchestrator(config={
        "base_url": "http://localhost:8080/v1",
        "api_key": "test-key",
    })


# -- Synthesis Engine Fixture --------------------------------------------------


@pytest.fixture
def synthesis_engine():
    """Provide a SynthesisEngine instance for testing formatting."""
    return SynthesisEngine(default_channel="telegram")
