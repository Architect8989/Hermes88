"""
rhodawk_core — DEAD CODE WARNING
=================================
This package is NOT called by any running process in the current architecture.

The production path is:
  supervisord → gateway/run.py → os.execvpe(hermes | openclaw)

os.execvpe replaces the Python process entirely. After exec, no Python code
in this repo runs. rhodawk_core is therefore unreachable at runtime.

Specifically, the following are FALSE claims left over from an earlier design:
  - gateway/memory_injector.py says "This module is imported by gateway/run.py"  → FALSE
  - gateway/response_formatter.py says "called by gateway/run.py"                → FALSE
  - gateway/event_consumer.py says "runs as a background task within the gateway"→ FALSE

None of those files are imported by gateway/run.py.

What this package WOULD do (if wired in):
  - MemoryEngine: SQLite structured store + Redis vector embeddings
  - Orchestrator: model routing with token-bucket rate limiting and caching
  - TaskQueue / WorkerPool: Redis-backed task queue with priority levels
  - EventBus: Redis pub/sub for webhook and system events
  - ToolRegistry: callable tool registry with permission checks
  - SynthesisEngine: channel-aware response formatting
  - ProactiveEngine: scheduled scans (GitHub, system health, finance)
  - AuditLogger: structured audit trail

To use this code you would need to wire it into the gateway BEFORE the execvpe
call, or run it in a separate long-lived process that reads from the Redis event
bus. Neither integration has been written.

Until that integration exists, do not add dependencies on this package and do
not assume any of its functionality is active.
"""

__version__ = "1.0.0"
__author__ = "Rhodawk AI"
__package_name__ = "rhodawk_core"

# Imports are intentionally lazy (not at module level) so that this package
# can be imported without requiring redis, grpc, or other heavy dependencies
# that are not installed in the production container.

def _lazy_imports():
    from rhodawk_core.memory import (
        MemoryEngine, MemoryEntry, MemoryQueryResult,
        EmbeddingProvider, StructuredMemoryStore, VectorMemoryStore,
    )
    from rhodawk_core.orchestrator import Orchestrator
    from rhodawk_core.task_engine import TaskQueue, WorkerPool, Task, StatusStreamer
    from rhodawk_core.event_bus import EventBus
    from rhodawk_core.tools import ToolRegistry
    from rhodawk_core.synthesis import SynthesisEngine
    from rhodawk_core.proactive import ProactiveEngine
    from rhodawk_core.audit import AuditLogger
    return locals()

__all__ = [
    "MemoryEngine", "MemoryEntry", "MemoryQueryResult",
    "EmbeddingProvider", "StructuredMemoryStore", "VectorMemoryStore",
    "Orchestrator",
    "TaskQueue", "WorkerPool", "Task", "StatusStreamer",
    "EventBus",
    "ToolRegistry",
    "SynthesisEngine",
    "ProactiveEngine",
    "AuditLogger",
]
