#!/usr/bin/env python3
"""
Tests for rhodawk_core.memory module.

Tests MemoryEntry data model, StructuredMemoryStore CRUD operations,
importance scoring, temporal decay calculations, knowledge graph relations,
flat file append, full-text search, and category retrieval.

Copyright (c) 2024-2025 Rhodawk AI. All rights reserved.
"""
import json
import math
import os
import sys
import time
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

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

from rhodawk_core.memory import MemoryEntry, StructuredMemoryStore


# -- MemoryEntry Tests ---------------------------------------------------------


class TestMemoryEntry:
    """Tests for MemoryEntry data model."""

    def test_memory_entry_creation(self):
        """Test creating a MemoryEntry with default values."""
        entry = MemoryEntry(content="Test memory content")
        assert entry.content == "Test memory content"
        assert entry.category == "general"
        assert entry.importance == 0.5
        assert entry.access_count == 0
        assert entry.tags == []
        assert entry.related_ids == []
        assert entry.metadata == {}
        assert entry.id  # Should have auto-generated UUID
        assert entry.created_at > 0
        assert entry.last_accessed > 0

    def test_memory_entry_creation_with_params(self):
        """Test creating a MemoryEntry with explicit values."""
        entry = MemoryEntry(
            id="custom-id-123",
            content="User preference: dark mode",
            category="preferences",
            tags=["user", "ui"],
            importance=0.9,
            created_at=1700000000.0,
            last_accessed=1700000000.0,
            access_count=10,
            source="telegram",
            related_ids=["other-id"],
            metadata={"verified": True},
        )
        assert entry.id == "custom-id-123"
        assert entry.content == "User preference: dark mode"
        assert entry.category == "preferences"
        assert entry.tags == ["user", "ui"]
        assert entry.importance == 0.9
        assert entry.access_count == 10
        assert entry.source == "telegram"
        assert entry.metadata == {"verified": True}

    def test_memory_entry_to_dict(self):
        """Test serialization to dictionary (excludes embedding)."""
        entry = MemoryEntry(
            id="dict-test-001",
            content="Test content",
            category="general",
            tags=["test"],
            importance=0.7,
            created_at=1700000000.0,
            last_accessed=1700000000.0,
            embedding=[0.1, 0.2, 0.3],
        )
        d = entry.to_dict()
        assert d["id"] == "dict-test-001"
        assert d["content"] == "Test content"
        assert d["category"] == "general"
        assert d["tags"] == ["test"]
        assert d["importance"] == 0.7
        # embedding should be excluded from to_dict
        assert "embedding" not in d

    def test_memory_entry_from_dict(self):
        """Test deserialization from dictionary."""
        data = {
            "id": "from-dict-001",
            "content": "Restored memory",
            "category": "facts",
            "tags": ["important"],
            "importance": 0.6,
            "created_at": 1700000000.0,
            "last_accessed": 1700000000.0,
            "access_count": 3,
            "source": "manual",
            "related_ids": [],
            "metadata": {},
        }
        entry = MemoryEntry.from_dict(data)
        assert entry.id == "from-dict-001"
        assert entry.content == "Restored memory"
        assert entry.category == "facts"
        assert entry.importance == 0.6

    def test_memory_entry_from_dict_ignores_unknown_fields(self):
        """Test that from_dict ignores fields not in the dataclass."""
        data = {
            "id": "ignore-test",
            "content": "Test",
            "unknown_field": "should be ignored",
            "another_random": 42,
        }
        entry = MemoryEntry.from_dict(data)
        assert entry.id == "ignore-test"
        assert entry.content == "Test"
        assert not hasattr(entry, "unknown_field")


# -- StructuredMemoryStore Tests -----------------------------------------------


class TestStructuredMemoryStore:
    """Tests for StructuredMemoryStore CRUD and search operations."""

    def _make_store(self, tmp_path):
        """Helper to create a temp store."""
        db_path = str(tmp_path / "test.db")
        return StructuredMemoryStore(db_path=db_path)

    def test_structured_store_crud(self, tmp_path):
        """Test create, read, update, delete cycle."""
        store = self._make_store(tmp_path)

        # Create
        entry = MemoryEntry(
            id="crud-001",
            content="CRUD test memory",
            category="test",
            importance=0.7,
        )
        stored_id = store.store(entry)
        assert stored_id == "crud-001"

        # Read
        retrieved = store.get("crud-001")
        assert retrieved is not None
        assert retrieved.content == "CRUD test memory"
        assert retrieved.category == "test"
        assert retrieved.importance == 0.7

        # Update (store again with same ID, different content)
        entry.content = "Updated CRUD content"
        entry.importance = 0.9
        store.store(entry)
        retrieved = store.get("crud-001")
        assert retrieved.content == "Updated CRUD content"
        assert retrieved.importance == 0.9

        # Delete (via prune - not directly supported, test get returns None for missing)
        missing = store.get("nonexistent-id")
        assert missing is None

    def test_importance_scoring(self, tmp_path):
        """Test that importance values are stored and retrievable correctly."""
        store = self._make_store(tmp_path)

        # Store entries with different importance levels
        for i, imp in enumerate([0.1, 0.5, 0.9, 0.3, 0.7]):
            entry = MemoryEntry(
                id=f"imp-{i}",
                content=f"Memory with importance {imp}",
                category="scored",
                importance=imp,
            )
            store.store(entry)

        # Retrieve by category (should be ordered by importance DESC)
        results = store.get_by_category("scored", limit=5)
        assert len(results) == 5
        importances = [r.importance for r in results]
        assert importances == sorted(importances, reverse=True)

    def test_temporal_decay_calculation(self, tmp_path):
        """Test that temporal decay reduces importance over time."""
        store = self._make_store(tmp_path)

        old_time = time.time() - (60 * 86400)  # 60 days ago
        entry = MemoryEntry(
            id="decay-001",
            content="Old memory for decay test",
            category="decay",
            importance=0.8,
            last_accessed=old_time,
        )
        store.store(entry)

        # Apply decay
        store.decay_importance(decay_rate=0.02, half_life_days=30.0)

        # Retrieve and check importance decreased
        result = store.get("decay-001")
        assert result is not None
        assert result.importance < 0.8
        assert result.importance >= 0.1  # minimum floor

    def test_knowledge_graph_add_relation(self, tmp_path):
        """Test adding and retrieving knowledge graph relations."""
        store = self._make_store(tmp_path)

        # Store two memories
        store.store(MemoryEntry(id="kg-a", content="Memory A", category="test"))
        store.store(MemoryEntry(id="kg-b", content="Memory B", category="test"))

        # Add relation
        store.add_relation("kg-a", "kg-b", "related_to", weight=0.85)

        # Retrieve relations
        relations = store.get_related("kg-a")
        assert len(relations) == 1
        target_id, relation, weight = relations[0]
        assert target_id == "kg-b"
        assert relation == "related_to"
        assert weight == 0.85

        # Bidirectional - also findable from kg-b
        relations_b = store.get_related("kg-b")
        assert len(relations_b) == 1
        assert relations_b[0][0] == "kg-a"

    def test_flat_file_append(self, tmp_path):
        """Test that _append_flat creates proper markdown entries."""
        from rhodawk_core.memory import MemoryEngine

        memory_file = tmp_path / "MEMORY.md"
        engine = MemoryEngine(config={
            "db_path": str(tmp_path / "test.db"),
            "memory_path": str(memory_file),
            "vault_path": str(tmp_path / "vault"),
            "redis_url": "redis://localhost:6379/0",
        })

        entry = MemoryEntry(
            id="flat-001",
            content="Flat file test content",
            category="test",
            importance=0.6,
            created_at=1700000000.0,
        )
        engine._append_flat(entry)

        assert memory_file.exists()
        content = memory_file.read_text()
        assert "Flat file test content" in content
        assert "Category: test" in content
        assert "Importance: 0.6" in content

    def test_fulltext_search(self, tmp_path):
        """Test full-text search across memory content."""
        store = self._make_store(tmp_path)

        store.store(MemoryEntry(id="ft-1", content="Python programming language", category="tech"))
        store.store(MemoryEntry(id="ft-2", content="JavaScript framework React", category="tech"))
        store.store(MemoryEntry(id="ft-3", content="Python data science numpy", category="tech"))

        results = store.search_fulltext("Python", limit=10)
        assert len(results) >= 1
        contents = [r.content for r in results]
        assert any("Python" in c for c in contents)

    def test_category_retrieval(self, tmp_path):
        """Test retrieving memories by category."""
        store = self._make_store(tmp_path)

        store.store(MemoryEntry(id="cat-1", content="Memory A", category="work", importance=0.5))
        store.store(MemoryEntry(id="cat-2", content="Memory B", category="personal", importance=0.7))
        store.store(MemoryEntry(id="cat-3", content="Memory C", category="work", importance=0.9))

        work_memories = store.get_by_category("work", limit=10)
        assert len(work_memories) == 2
        assert all(m.category == "work" for m in work_memories)

        personal_memories = store.get_by_category("personal", limit=10)
        assert len(personal_memories) == 1
        assert personal_memories[0].content == "Memory B"

    def test_prune_low_importance(self, tmp_path):
        """Test pruning low-importance memories beyond max count."""
        store = self._make_store(tmp_path)

        # Store 5 memories with varying importance
        for i in range(5):
            store.store(MemoryEntry(
                id=f"prune-{i}",
                content=f"Prune test {i}",
                category="test",
                importance=0.05 * (i + 1),  # 0.05, 0.10, 0.15, 0.20, 0.25
            ))

        # Prune with max_entries=3
        pruned = store.prune(min_importance=0.12, max_entries=3)
        assert pruned == 2  # Should remove 2 entries (importance < 0.12)

        # Verify remaining count
        stats = store.get_stats()
        assert stats["total_memories"] == 3
