#!/usr/bin/env python3
"""
gateway/memory_injector.py — DEAD CODE — NOT CALLED BY ANY RUNNING PROCESS.

The claim "This module is imported by gateway/run.py" is FALSE.
gateway/run.py does os.execvpe(hermes | openclaw). The Python process is
replaced before any import of this file could occur. This module has zero
callers in the current architecture.

Memory injection in the running system is handled natively by whichever
gateway you chose (hermes-agent or openclaw) using their own memory config:
  - hermes-agent: config.yaml → memory.* section
  - openclaw: SOUL.md + its own session memory

rhodawk_core.MemoryEngine (which this file wraps) is also unreachable.
See rhodawk_core/__init__.py for the full dead-code explanation.

To make this useful: implement it as a standalone memory-writer process
that reads from the Redis event bus and writes to the SQLite/Redis store,
then configure hermes-agent to query that same store via its semantic
memory backend.

Rhodawk AI -- Peak Architecture v10.0
"""
import os
import sys
import time
import hashlib
from pathlib import Path
from typing import Optional

# Ensure rhodawk_core is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from rhodawk_core.memory import MemoryEngine
    MEMORY_AVAILABLE = True
except ImportError:
    MEMORY_AVAILABLE = False

try:
    from rhodawk_core.audit import AuditLogger
    AUDIT_AVAILABLE = True
except ImportError:
    AUDIT_AVAILABLE = False


class MemoryInjector:
    """
    Injects relevant memory context into every gateway prompt.

    Flow:
    1. Operator message arrives at gateway
    2. MemoryInjector extracts intent/topic from the message
    3. Queries semantic memory for relevant context
    4. Formats context as a prefix block before SOUL.md content
    5. Returns enriched system prompt for the LLM call

    Configuration is loaded from environment variables and config files.
    """

    def __init__(self, config: Optional[dict] = None):
        """
        Initialize the memory injector.

        Args:
            config: Optional configuration dict. If not provided, reads from
                    environment variables and default paths.
        """
        self.config = config or {}
        self._engine: Optional["MemoryEngine"] = None
        self._audit: Optional["AuditLogger"] = None
        self._cache: dict = {}
        self._cache_ttl: float = 30.0  # seconds
        self._max_context_tokens: int = self.config.get("max_context_tokens", 2000)
        self._similarity_threshold: float = self.config.get("similarity_threshold", 0.72)
        self._enabled: bool = self.config.get("enabled", True) and MEMORY_AVAILABLE

        # Soul path for loading the SOUL.md content
        self._soul_path: str = self.config.get(
            "soul_path",
            os.environ.get("SOUL_PATH", "/data/.hermes/SOUL.md")
        )

        # Initialize components
        self._init_engine()
        self._init_audit()

    def _init_engine(self):
        """Initialize the memory engine with configuration."""
        if not MEMORY_AVAILABLE:
            return

        try:
            engine_config = {
                "redis_url": os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
                "index_name": self.config.get("index_name", "hermes:memory:vectors"),
                "db_path": self.config.get("db_path", "/data/.hermes/memory.db"),
                "similarity_threshold": self._similarity_threshold,
                "temporal_decay_rate": self.config.get("temporal_decay_rate", 0.02),
                "decay_half_life_days": self.config.get("decay_half_life_days", 30.0),
                "max_memories": self.config.get("max_memories", 10000),
                "memory_path": self.config.get(
                    "memory_path", "/data/.hermes/memories/MEMORY.md"
                ),
                "user_path": self.config.get(
                    "user_path", "/data/.hermes/memories/USER.md"
                ),
            }
            self._engine = MemoryEngine(engine_config)
        except Exception as e:
            print(f"[memory_injector] Engine init failed: {e}", flush=True)
            self._engine = None

    def _init_audit(self):
        """Initialize audit logger."""
        if not AUDIT_AVAILABLE:
            return
        try:
            self._audit = AuditLogger()
        except Exception:
            self._audit = None

    def get_context_for_prompt(self, message: str, session_id: str = "",
                               channel: str = "telegram") -> str:
        """
        Get enriched context to inject into the system prompt.

        This is the main entry point called by gateway/run.py for every
        incoming operator message.

        Args:
            message: The operator's message text.
            session_id: Current session identifier for cache keying.
            channel: The source channel (telegram, discord, slack, etc.)

        Returns:
            A formatted context string to prepend to the system prompt.
            Returns empty string if no relevant memories found.
        """
        if not self._enabled or not self._engine:
            return ""

        # Check cache to avoid repeated queries for rapid messages
        cache_key = self._compute_cache_key(message, session_id)
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        try:
            context = self._retrieve_and_format(message, channel)

            # Cache the result
            self._set_cached(cache_key, context)

            # Audit log
            if self._audit and context:
                self._audit.log("memory_injection", {
                    "message_length": len(message),
                    "context_length": len(context),
                    "channel": channel,
                })

            return context

        except Exception as e:
            print(f"[memory_injector] Context retrieval error: {e}", flush=True)
            return ""

    def build_system_prompt(self, message: str, session_id: str = "",
                            channel: str = "telegram",
                            additional_context: str = "") -> str:
        """
        Build the complete system prompt with memory context and SOUL.md.

        This composes the full system prompt in the correct order:
        1. Memory context (relevant past information)
        2. Additional context (task-specific instructions)
        3. SOUL.md content (persona and behavior rules)

        Args:
            message: The operator's message (used for memory retrieval).
            session_id: Session identifier.
            channel: Source channel.
            additional_context: Any extra context to inject (e.g., skill context).

        Returns:
            The complete system prompt string.
        """
        parts = []

        # 1. Memory context
        memory_context = self.get_context_for_prompt(message, session_id, channel)
        if memory_context:
            parts.append(memory_context)

        # 2. Additional context (skill-specific, task-specific)
        if additional_context:
            parts.append(additional_context)

        # 3. SOUL.md content
        soul_content = self._load_soul()
        if soul_content:
            parts.append(soul_content)

        return "\n\n".join(parts)

    def record_interaction(self, message: str, response: str,
                           category: str = "task_outcome",
                           importance: float = 0.5,
                           tags: Optional[list] = None):
        """
        Record the outcome of an interaction to memory.

        Called by the gateway after a task is completed to persist
        the outcome for future context retrieval.

        Args:
            message: The operator's original message.
            response: The response/outcome produced.
            category: Memory category for classification.
            importance: How important this memory is (0.0-1.0).
            tags: Optional tags for categorization.
        """
        if not self._enabled or not self._engine:
            return

        try:
            # Compose a concise memory entry from the interaction
            content = self._compose_memory_entry(message, response)
            self._engine.remember(
                content=content,
                category=category,
                importance=importance,
                tags=tags or [],
                source="gateway_interaction",
            )
        except Exception as e:
            print(f"[memory_injector] Record error: {e}", flush=True)

    def _retrieve_and_format(self, message: str, channel: str) -> str:
        """Retrieve relevant memories and format them as context."""
        # Use the engine's built-in context generation
        context = self._engine.get_context_for_task(
            task_description=message,
            max_tokens=self._max_context_tokens,
        )

        if not context:
            return ""

        # Add channel-specific header
        header = "## Relevant Memory Context (auto-injected)\n"
        header += f"Channel: {channel} | Retrieved: {int(time.time())}\n\n"

        return header + context

    def _compose_memory_entry(self, message: str, response: str) -> str:
        """Compose a memory entry from an interaction pair."""
        # Truncate to keep memory entries concise
        msg_summary = message[:200]
        resp_summary = response[:500]

        return (
            f"Operator request: {msg_summary}\n"
            f"Outcome: {resp_summary}"
        )

    def _load_soul(self) -> str:
        """Load SOUL.md content from disk."""
        try:
            soul_path = Path(self._soul_path)
            if soul_path.exists():
                return soul_path.read_text(encoding="utf-8")
        except Exception as e:
            print(f"[memory_injector] Failed to load SOUL.md: {e}", flush=True)

        # Fallback: try the local config path
        fallback = Path(__file__).resolve().parent.parent / "hermes_config" / "SOUL.md"
        try:
            if fallback.exists():
                return fallback.read_text(encoding="utf-8")
        except Exception:
            pass

        return ""

    def _compute_cache_key(self, message: str, session_id: str) -> str:
        """Compute a cache key for deduplication."""
        raw = f"{session_id}:{message[:100]}"
        return hashlib.md5(raw.encode()).hexdigest()

    def _get_cached(self, key: str) -> Optional[str]:
        """Get a cached context result if still valid."""
        if key not in self._cache:
            return None
        entry = self._cache[key]
        if time.time() - entry["time"] > self._cache_ttl:
            del self._cache[key]
            return None
        return entry["value"]

    def _set_cached(self, key: str, value: str):
        """Cache a context result."""
        # Evict old entries if cache is too large
        if len(self._cache) > 100:
            oldest_key = min(self._cache, key=lambda k: self._cache[k]["time"])
            del self._cache[oldest_key]

        self._cache[key] = {"value": value, "time": time.time()}

    def clear_cache(self):
        """Clear the context cache."""
        self._cache.clear()

    @property
    def is_enabled(self) -> bool:
        """Whether memory injection is active."""
        return self._enabled and self._engine is not None


# -- Module-level convenience function -------------------------------------------

_default_injector: Optional[MemoryInjector] = None


def get_injector(config: Optional[dict] = None) -> MemoryInjector:
    """Get or create the default MemoryInjector instance."""
    global _default_injector
    if _default_injector is None:
        _default_injector = MemoryInjector(config)
    return _default_injector


def get_context_for_prompt(message: str, session_id: str = "",
                           channel: str = "telegram") -> str:
    """
    Convenience function: get memory context for a prompt.

    This is the primary function called by gateway/run.py.

    Args:
        message: The operator's message.
        session_id: Current session ID.
        channel: Source channel.

    Returns:
        Formatted memory context string (empty if nothing relevant).
    """
    injector = get_injector()
    return injector.get_context_for_prompt(message, session_id, channel)
