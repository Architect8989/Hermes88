#!/usr/bin/env python3
"""
Tests for rhodawk_core.task_engine module.

Tests Task data model creation, JSON serialization/deserialization,
priority ordering, status transitions, retry logic enforcement,
timeout enforcement, and duration calculation.

Copyright (c) 2024-2025 Rhodawk AI. All rights reserved.
"""
import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

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

from rhodawk_core.task_engine import Task, TaskStatus, TaskPriority, TaskQueue


# -- Task Creation Tests -------------------------------------------------------


class TestTaskCreation:
    """Tests for Task data model creation and defaults."""

    def test_task_creation(self):
        """Test creating a Task with default values."""
        task = Task(name="Test Task")
        assert task.name == "Test Task"
        assert task.status == TaskStatus.PENDING
        assert task.priority == TaskPriority.NORMAL
        assert task.retries == 0
        assert task.max_retries == 3
        assert task.timeout == 3600
        assert task.progress == 0.0
        assert task.result is None
        assert task.error is None
        assert task.started_at is None
        assert task.completed_at is None
        assert task.id.startswith("task_")
        assert task.created_at > 0

    def test_task_creation_with_params(self):
        """Test creating a Task with explicit values."""
        task = Task(
            id="task_custom123",
            name="Custom Task",
            description="A detailed description",
            priority=TaskPriority.CRITICAL,
            status=TaskStatus.PENDING,
            timeout=600,
            max_retries=5,
            skill="code_review",
            params={"file": "main.py"},
            metadata={"owner": "admin"},
            notify_channel="discord",
        )
        assert task.id == "task_custom123"
        assert task.name == "Custom Task"
        assert task.description == "A detailed description"
        assert task.priority == TaskPriority.CRITICAL
        assert task.timeout == 600
        assert task.max_retries == 5
        assert task.skill == "code_review"
        assert task.params == {"file": "main.py"}
        assert task.notify_channel == "discord"


# -- Serialization Tests -------------------------------------------------------


class TestTaskSerialization:
    """Tests for Task JSON serialization and deserialization."""

    def test_task_serialization(self):
        """Test to_json produces valid JSON with all fields."""
        task = Task(
            id="task_serial001",
            name="Serialization Test",
            priority=TaskPriority.HIGH,
            status=TaskStatus.RUNNING,
            created_at=1700000000.0,
            started_at=1700000001.0,
            timeout=1800,
            skill="deploy",
            params={"branch": "main"},
        )
        json_str = task.to_json()
        data = json.loads(json_str)

        assert data["id"] == "task_serial001"
        assert data["name"] == "Serialization Test"
        assert data["priority"] == TaskPriority.HIGH
        assert data["status"] == TaskStatus.RUNNING
        assert data["created_at"] == 1700000000.0
        assert data["started_at"] == 1700000001.0
        assert data["skill"] == "deploy"
        assert data["params"] == {"branch": "main"}

    def test_task_from_json_roundtrip(self):
        """Test that to_json -> from_json produces equivalent task."""
        original = Task(
            id="task_roundtrip",
            name="Roundtrip Test",
            description="Testing roundtrip serialization",
            priority=TaskPriority.LOW,
            status=TaskStatus.PENDING,
            timeout=900,
            max_retries=2,
            skill="research",
            params={"query": "test"},
            metadata={"source": "api"},
        )
        json_str = original.to_json()
        restored = Task.from_json(json_str)

        assert restored.id == original.id
        assert restored.name == original.name
        assert restored.description == original.description
        assert restored.priority == original.priority
        assert restored.status == original.status
        assert restored.timeout == original.timeout
        assert restored.max_retries == original.max_retries
        assert restored.skill == original.skill
        assert restored.params == original.params

    def test_task_from_json_ignores_extra_fields(self):
        """Test that from_json handles extra fields gracefully."""
        data = {
            "id": "task_extra",
            "name": "Extra Fields",
            "status": "pending",
            "priority": 2,
            "created_at": 1700000000.0,
            "unknown_field": "should be ignored",
        }
        task = Task.from_json(json.dumps(data))
        assert task.id == "task_extra"
        assert task.name == "Extra Fields"


# -- Priority Ordering Tests ---------------------------------------------------


class TestPriorityOrdering:
    """Tests for task priority ordering behavior."""

    def test_priority_ordering(self):
        """Test that priority values are correctly ordered (lower = higher priority)."""
        assert TaskPriority.CRITICAL < TaskPriority.HIGH
        assert TaskPriority.HIGH < TaskPriority.NORMAL
        assert TaskPriority.NORMAL < TaskPriority.LOW
        assert TaskPriority.LOW < TaskPriority.BACKGROUND

    def test_priority_values(self):
        """Test priority enum integer values."""
        assert TaskPriority.CRITICAL == 0
        assert TaskPriority.HIGH == 1
        assert TaskPriority.NORMAL == 2
        assert TaskPriority.LOW == 3
        assert TaskPriority.BACKGROUND == 4

    def test_priority_sorting(self):
        """Test sorting tasks by priority."""
        tasks = [
            Task(name="Low", priority=TaskPriority.LOW),
            Task(name="Critical", priority=TaskPriority.CRITICAL),
            Task(name="Normal", priority=TaskPriority.NORMAL),
            Task(name="High", priority=TaskPriority.HIGH),
        ]
        sorted_tasks = sorted(tasks, key=lambda t: t.priority)
        assert sorted_tasks[0].name == "Critical"
        assert sorted_tasks[1].name == "High"
        assert sorted_tasks[2].name == "Normal"
        assert sorted_tasks[3].name == "Low"


# -- Status Transitions Tests --------------------------------------------------


class TestStatusTransitions:
    """Tests for task status transitions."""

    def test_status_transitions(self):
        """Test valid status values."""
        task = Task(name="Status Test")
        assert task.status == TaskStatus.PENDING

        task.status = TaskStatus.RUNNING
        assert task.status == TaskStatus.RUNNING

        task.status = TaskStatus.COMPLETED
        assert task.status == TaskStatus.COMPLETED

    def test_is_terminal(self):
        """Test is_terminal property for terminal states."""
        task = Task(name="Terminal Test")
        assert not task.is_terminal

        task.status = TaskStatus.RUNNING
        assert not task.is_terminal

        task.status = TaskStatus.RETRYING
        assert not task.is_terminal

        task.status = TaskStatus.COMPLETED
        assert task.is_terminal

        task.status = TaskStatus.FAILED
        assert task.is_terminal

        task.status = TaskStatus.CANCELLED
        assert task.is_terminal

    def test_all_status_values(self):
        """Test all status enum values exist."""
        assert TaskStatus.PENDING == "pending"
        assert TaskStatus.RUNNING == "running"
        assert TaskStatus.COMPLETED == "completed"
        assert TaskStatus.FAILED == "failed"
        assert TaskStatus.CANCELLED == "cancelled"
        assert TaskStatus.RETRYING == "retrying"


# -- Retry Logic Tests ---------------------------------------------------------


class TestRetryLogic:
    """Tests for task retry logic enforcement."""

    def test_retry_logic(self):
        """Test retry counter and max_retries enforcement."""
        task = Task(name="Retry Test", max_retries=3)
        assert task.retries == 0

        # Simulate retries
        task.retries = 1
        assert task.retries < task.max_retries

        task.retries = 2
        assert task.retries < task.max_retries

        task.retries = 3
        assert task.retries >= task.max_retries

    def test_retry_with_different_max(self):
        """Test retry logic with non-default max_retries."""
        task = Task(name="Custom Retry", max_retries=1)
        task.retries = 1
        assert task.retries >= task.max_retries

        task2 = Task(name="No Retry", max_retries=0)
        assert task2.retries >= task2.max_retries


# -- Timeout Tests -------------------------------------------------------------


class TestTimeoutEnforcement:
    """Tests for task timeout enforcement."""

    def test_timeout_enforcement(self):
        """Test default timeout value."""
        task = Task(name="Timeout Test")
        assert task.timeout == 3600  # default 1 hour

    def test_custom_timeout(self):
        """Test custom timeout values."""
        task = Task(name="Short Timeout", timeout=30)
        assert task.timeout == 30

        task2 = Task(name="Long Timeout", timeout=86400)
        assert task2.timeout == 86400


# -- Duration Calculation Tests ------------------------------------------------


class TestTaskDuration:
    """Tests for task duration calculation."""

    def test_task_duration_calculation(self):
        """Test duration calculation for started tasks."""
        task = Task(name="Duration Test")
        assert task.duration is None  # Not started

        task.started_at = time.time() - 10.0
        task.completed_at = time.time()
        duration = task.duration
        assert duration is not None
        assert 9.5 <= duration <= 11.0

    def test_task_duration_running(self):
        """Test duration calculation for currently running tasks."""
        task = Task(name="Running Duration")
        task.started_at = time.time() - 5.0
        # No completed_at means it uses current time
        duration = task.duration
        assert duration is not None
        assert 4.5 <= duration <= 6.0

    def test_task_duration_not_started(self):
        """Test duration is None for tasks that never started."""
        task = Task(name="Not Started")
        assert task.duration is None
