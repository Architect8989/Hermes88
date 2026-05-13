"""
Rhodawk Core - The Brain of Hermes88.

A JARVIS-grade AI assistant framework by Rhodawk AI.
Provides memory engine, LLM orchestration, task queue, event bus,
tool registry, synthesis layer, proactive intelligence, and audit logging.

Copyright (c) 2024-2025 Rhodawk AI. All rights reserved.
"""

__version__ = "1.0.0"
__author__ = "Rhodawk AI"
__package_name__ = "rhodawk_core"

from rhodawk_core.memory import (
    MemoryEngine,
    MemoryEntry,
    MemoryQueryResult,
    EmbeddingProvider,
    StructuredMemoryStore,
    VectorMemoryStore,
)
from rhodawk_core.orchestrator import Orchestrator
from rhodawk_core.task_engine import TaskQueue, WorkerPool, Task, StatusStreamer
from rhodawk_core.event_bus import EventBus
from rhodawk_core.tools import ToolRegistry
from rhodawk_core.synthesis import SynthesisEngine
from rhodawk_core.proactive import ProactiveEngine
from rhodawk_core.audit import AuditLogger

__all__ = [
    # Memory
    "MemoryEngine",
    "MemoryEntry",
    "MemoryQueryResult",
    "EmbeddingProvider",
    "StructuredMemoryStore",
    "VectorMemoryStore",
    # Orchestrator
    "Orchestrator",
    # Task Engine
    "TaskQueue",
    "WorkerPool",
    "Task",
    "StatusStreamer",
    # Event Bus
    "EventBus",
    # Tools
    "ToolRegistry",
    # Synthesis
    "SynthesisEngine",
    # Proactive
    "ProactiveEngine",
    # Audit
    "AuditLogger",
]
