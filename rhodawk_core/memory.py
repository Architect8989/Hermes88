#!/usr/bin/env python3
"""
Rhodawk Core Memory Engine.

Provides semantic retrieval with vector embeddings, importance weighting,
temporal decay, automatic knowledge graph construction, and Obsidian vault sync.

Wraps patterns from mem0ai/mem0 (importance scoring, temporal decay),
chroma-core/chroma (vector search), and Obsidian vault (markdown with frontmatter).

Copyright (c) 2024-2025 Rhodawk AI. All rights reserved.
"""
import hashlib
import json
import math
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False

try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

try:
    from redis.commands.search.field import VectorField, TextField, NumericField, TagField
    from redis.commands.search.indexDefinition import IndexDefinition, IndexType
    from redis.commands.search.query import Query
    REDIS_SEARCH_AVAILABLE = True
except ImportError:
    REDIS_SEARCH_AVAILABLE = False


# -- Data Models ----------------------------------------------------------------


@dataclass
class MemoryEntry:
    """A single memory entry with metadata for the Rhodawk memory system."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    content: str = ""
    category: str = "general"
    tags: list = field(default_factory=list)
    importance: float = 0.5
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    access_count: int = 0
    source: str = ""
    related_ids: list = field(default_factory=list)
    embedding: Optional[list] = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Serialize to dictionary, excluding embedding for storage efficiency."""
        d = asdict(self)
        d.pop("embedding", None)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryEntry":
        """Deserialize from dictionary."""
        valid_fields = cls.__dataclass_fields__
        return cls(**{k: v for k, v in d.items() if k in valid_fields})


@dataclass
class MemoryQueryResult:
    """Result from a memory query with relevance score."""
    entry: MemoryEntry
    relevance_score: float
    decay_factor: float
    final_score: float


# -- Embedding Provider ---------------------------------------------------------


class EmbeddingProvider:
    """
    Generate embeddings via DO Inference (OpenAI-compatible endpoint).
    Falls back to deterministic hash-based pseudo-embeddings when API is unavailable.
    """

    def __init__(self, api_key: str = "", base_url: str = "",
                 model: str = "text-embedding-3-small", dimensions: int = 1536):
        self.api_key = api_key or os.environ.get("DO_INFERENCE_API_KEY", "")
        self.base_url = base_url or os.environ.get(
            "DO_INFERENCE_BASE_URL", "https://inference.do-ai.run/v1"
        )
        self.model = model
        self.dimensions = dimensions

    def embed(self, text: str) -> List[float]:
        """Generate embedding vector for text via API with fallback."""
        import urllib.request
        import urllib.error

        if not self.api_key:
            return self._fallback_embed(text)

        payload = json.dumps({
            "model": self.model,
            "input": text[:8000],
        }).encode()

        req = urllib.request.Request(
            f"{self.base_url.rstrip(chr(47))}/embeddings",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            return data["data"][0]["embedding"]
        except Exception as e:
            print(f"[memory] Embedding API failed: {e}", flush=True)
            return self._fallback_embed(text)

    def _fallback_embed(self, text: str) -> List[float]:
        """Deterministic pseudo-embedding when API is unavailable."""
        h = hashlib.sha256(text.encode()).digest()
        seed = int.from_bytes(h[:4], "big")
        if NUMPY_AVAILABLE:
            rng = np.random.RandomState(seed)
            vec = rng.randn(self.dimensions).tolist()
        else:
            import random
            rng = random.Random(seed)
            vec = [rng.gauss(0, 1) for _ in range(self.dimensions)]
        return vec

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """Batch embedding for efficiency."""
        return [self.embed(t) for t in texts]


# -- SQLite Structured Store ----------------------------------------------------


class StructuredMemoryStore:
    """SQLite-backed structured memory with full-text search."""

    def __init__(self, db_path: str = "/data/.hermes/memory.db"):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        """Initialize database schema with tables and indexes."""
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                category TEXT DEFAULT "general",
                tags TEXT DEFAULT "[]",
                importance REAL DEFAULT 0.5,
                created_at REAL NOT NULL,
                last_accessed REAL NOT NULL,
                access_count INTEGER DEFAULT 0,
                source TEXT DEFAULT "",
                related_ids TEXT DEFAULT "[]",
                metadata TEXT DEFAULT "{}"
            );

            CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category);
            CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(importance DESC);
            CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at DESC);

            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                content, category, tags,
                content="memories",
                content_rowid="rowid"
            );

            CREATE TABLE IF NOT EXISTS knowledge_graph (
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                relation TEXT NOT NULL,
                weight REAL DEFAULT 1.0,
                created_at REAL NOT NULL,
                PRIMARY KEY (source_id, target_id, relation)
            );

            CREATE TABLE IF NOT EXISTS memory_stats (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
        """)
        self.conn.commit()

    def store(self, entry: MemoryEntry) -> str:
        """Store or update a memory entry in SQLite."""
        self.conn.execute("""
            INSERT OR REPLACE INTO memories
            (id, content, category, tags, importance, created_at,
             last_accessed, access_count, source, related_ids, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            entry.id, entry.content, entry.category,
            json.dumps(entry.tags), entry.importance,
            entry.created_at, entry.last_accessed, entry.access_count,
            entry.source, json.dumps(entry.related_ids),
            json.dumps(entry.metadata),
        ))
        self.conn.execute("""
            INSERT OR REPLACE INTO memories_fts(rowid, content, category, tags)
            SELECT rowid, content, category, tags FROM memories WHERE id = ?
        """, (entry.id,))
        self.conn.commit()
        return entry.id

    def get(self, memory_id: str) -> Optional[MemoryEntry]:
        """Retrieve a single memory by ID."""
        row = self.conn.execute(
            "SELECT * FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_entry(row)

    def search_fulltext(self, query: str, limit: int = 10) -> List[MemoryEntry]:
        """Full-text search across memory content."""
        try:
            rows = self.conn.execute("""
                SELECT m.* FROM memories m
                JOIN memories_fts f ON m.rowid = f.rowid
                WHERE memories_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (query, limit)).fetchall()
            return [self._row_to_entry(r) for r in rows]
        except Exception:
            return []

    def get_by_category(self, category: str, limit: int = 20) -> List[MemoryEntry]:
        """Get memories filtered by category, ordered by importance."""
        rows = self.conn.execute("""
            SELECT * FROM memories WHERE category = ?
            ORDER BY importance DESC, created_at DESC
            LIMIT ?
        """, (category, limit)).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def get_recent(self, limit: int = 20) -> List[MemoryEntry]:
        """Get the most recently created memories."""
        rows = self.conn.execute("""
            SELECT * FROM memories
            ORDER BY created_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def update_access(self, memory_id: str):
        """Update access timestamp and increment access count."""
        self.conn.execute("""
            UPDATE memories
            SET last_accessed = ?, access_count = access_count + 1
            WHERE id = ?
        """, (time.time(), memory_id))
        self.conn.commit()

    def add_relation(self, source_id: str, target_id: str, relation: str, weight: float = 1.0):
        """Create a relation edge in the knowledge graph."""
        self.conn.execute("""
            INSERT OR REPLACE INTO knowledge_graph (source_id, target_id, relation, weight, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (source_id, target_id, relation, weight, time.time()))
        self.conn.commit()

    def get_related(self, memory_id: str) -> List[Tuple[str, str, float]]:
        """Get all relations for a memory (bidirectional)."""
        rows = self.conn.execute("""
            SELECT target_id, relation, weight FROM knowledge_graph
            WHERE source_id = ?
            UNION
            SELECT source_id, relation, weight FROM knowledge_graph
            WHERE target_id = ?
            ORDER BY weight DESC
        """, (memory_id, memory_id)).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    def decay_importance(self, decay_rate: float = 0.02, half_life_days: float = 30.0):
        """Apply temporal decay to all memories based on last access time."""
        now = time.time()
        rows = self.conn.execute("SELECT id, importance, last_accessed FROM memories").fetchall()
        for row in rows:
            days_since_access = (now - row[2]) / 86400
            decay = math.exp(-decay_rate * days_since_access / half_life_days)
            new_importance = max(0.1, row[1] * decay)
            if abs(new_importance - row[1]) > 0.01:
                self.conn.execute(
                    "UPDATE memories SET importance = ? WHERE id = ?",
                    (new_importance, row[0])
                )
        self.conn.commit()

    def prune(self, min_importance: float = 0.1, max_entries: int = 10000) -> int:
        """Remove low-importance memories beyond the max count."""
        count = self.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        if count <= max_entries:
            return 0
        cursor = self.conn.execute("""
            DELETE FROM memories WHERE id IN (
                SELECT id FROM memories
                WHERE importance < ?
                ORDER BY importance ASC, last_accessed ASC
                LIMIT ?
            )
        """, (min_importance, count - max_entries))
        deleted = cursor.rowcount
        self.conn.commit()
        return deleted

    def get_stats(self) -> Dict[str, Any]:
        """Get memory store statistics."""
        total = self.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        categories = self.conn.execute(
            "SELECT category, COUNT(*) FROM memories GROUP BY category"
        ).fetchall()
        relations = self.conn.execute("SELECT COUNT(*) FROM knowledge_graph").fetchone()[0]
        return {
            "total_memories": total,
            "categories": {r[0]: r[1] for r in categories},
            "total_relations": relations,
        }

    def _row_to_entry(self, row) -> MemoryEntry:
        """Convert a SQLite row to a MemoryEntry."""
        return MemoryEntry(
            id=row["id"],
            content=row["content"],
            category=row["category"],
            tags=json.loads(row["tags"]),
            importance=row["importance"],
            created_at=row["created_at"],
            last_accessed=row["last_accessed"],
            access_count=row["access_count"],
            source=row["source"],
            related_ids=json.loads(row["related_ids"]),
            metadata=json.loads(row["metadata"]),
        )


# -- Vector Memory Store (Redis) ------------------------------------------------


class VectorMemoryStore:
    """Redis-backed vector store for semantic search using RediSearch."""

    def __init__(self, redis_url: str = "redis://localhost:6379/0",
                 index_name: str = "hermes:memory:vectors",
                 dimensions: int = 1536):
        self.index_name = index_name
        self.dimensions = dimensions
        self.prefix = "hermes:mem:"
        self.client = None

        if not REDIS_AVAILABLE:
            return

        try:
            self.client = redis.from_url(redis_url, decode_responses=False)
            self._ensure_index()
        except Exception as e:
            print(f"[memory] Redis connection failed: {e}", flush=True)
            self.client = None

    def _ensure_index(self):
        """Create RediSearch vector index if it does not exist."""
        if not self.client or not REDIS_SEARCH_AVAILABLE:
            return
        try:
            self.client.ft(self.index_name).info()
        except Exception:
            try:
                schema = (
                    TextField("content"),
                    TextField("category"),
                    TagField("tags"),
                    NumericField("importance"),
                    NumericField("created_at"),
                    VectorField(
                        "embedding",
                        "FLAT",
                        {
                            "TYPE": "FLOAT32",
                            "DIM": self.dimensions,
                            "DISTANCE_METRIC": "COSINE",
                        },
                    ),
                )
                definition = IndexDefinition(
                    prefix=[self.prefix], index_type=IndexType.HASH
                )
                self.client.ft(self.index_name).create_index(
                    schema, definition=definition
                )
            except Exception as e:
                print(f"[memory] Index creation failed: {e}", flush=True)

    def store(self, entry: MemoryEntry, embedding: List[float]) -> str:
        """Store memory with its embedding vector in Redis."""
        if not self.client:
            return entry.id

        try:
            key = f"{self.prefix}{entry.id}"
            if NUMPY_AVAILABLE:
                embedding_bytes = np.array(embedding, dtype=np.float32).tobytes()
            else:
                import struct
                embedding_bytes = struct.pack(f"{len(embedding)}f", *embedding)

            self.client.hset(key, mapping={
                "content": entry.content.encode(),
                "category": entry.category.encode(),
                "tags": ",".join(entry.tags).encode(),
                "importance": str(entry.importance).encode(),
                "created_at": str(entry.created_at).encode(),
                "embedding": embedding_bytes,
                "memory_id": entry.id.encode(),
            })
        except Exception as e:
            print(f"[memory] Redis store failed: {e}", flush=True)

        return entry.id

    def search(self, query_embedding: List[float], top_k: int = 10,
               category_filter: Optional[str] = None) -> List[Tuple[str, float]]:
        """Semantic search: find most similar memories by vector distance."""
        if not self.client or not REDIS_SEARCH_AVAILABLE:
            return []

        try:
            if NUMPY_AVAILABLE:
                query_bytes = np.array(query_embedding, dtype=np.float32).tobytes()
            else:
                import struct
                query_bytes = struct.pack(f"{len(query_embedding)}f", *query_embedding)

            filter_str = "*"
            if category_filter:
                filter_str = f"@category:{{{category_filter}}}"

            q = (
                Query(f"({filter_str})=>[KNN {top_k} @embedding  AS score]")
                .sort_by("score")
                .return_fields("memory_id", "score", "content", "importance")
                .dialect(2)
            )

            results = self.client.ft(self.index_name).search(
                q, query_params={"vec": query_bytes}
            )

            return [
                (
                    doc["memory_id"].decode() if isinstance(doc["memory_id"], bytes) else doc["memory_id"],
                    1.0 - float(doc["score"])
                )
                for doc in results.docs
            ]
        except Exception as e:
            print(f"[memory] Vector search failed: {e}", flush=True)
            return []

    def delete(self, memory_id: str):
        """Delete a memory vector from Redis."""
        if self.client:
            try:
                self.client.delete(f"{self.prefix}{memory_id}")
            except Exception:
                pass


# -- Unified Memory Engine -------------------------------------------------------


class MemoryEngine:
    """
    Unified memory engine combining vector search, structured storage,
    flat files, and Obsidian vault sync.

    This is the main interface used by Hermes for all memory operations.
    Implements patterns from mem0 (importance scoring, temporal decay)
    and chroma (vector search with embeddings).
    """

    def __init__(self, config: Optional[dict] = None):
        config = config or {}

        self.structured = StructuredMemoryStore(
            db_path=config.get("db_path", "/data/.hermes/memory.db")
        )
        self.vector = VectorMemoryStore(
            redis_url=config.get("redis_url", os.environ.get("REDIS_URL", "redis://localhost:6379/0")),
            index_name=config.get("index_name", "hermes:memory:vectors"),
        )
        self.embedder = EmbeddingProvider()

        self.similarity_threshold = config.get("similarity_threshold", 0.72)
        self.temporal_decay_rate = config.get("temporal_decay_rate", 0.02)
        self.decay_half_life = config.get("decay_half_life_days", 30.0)
        self.max_memories = config.get("max_memories", 10000)

        self.memory_file = Path(config.get("memory_path", "/data/.hermes/memories/MEMORY.md"))
        self.user_file = Path(config.get("user_path", "/data/.hermes/memories/USER.md"))
        self.vault_path = Path(config.get("vault_path", "/data/.hermes/vault"))

    def remember(self, content: str, category: str = "general",
                 importance: float = 0.5, tags: Optional[List[str]] = None,
                 source: str = "", metadata: Optional[dict] = None) -> str:
        """
        Store a new memory. Generates embedding, stores in both vector and structured stores.
        Also syncs to Obsidian vault and flat MEMORY.md.
        Returns the memory ID.
        """
        entry = MemoryEntry(
            content=content,
            category=category,
            importance=importance,
            tags=tags or [],
            source=source,
            metadata=metadata or {},
        )

        embedding = self.embedder.embed(content)
        entry.embedding = embedding

        self.structured.store(entry)
        self.vector.store(entry, embedding)

        self._auto_relate(entry, embedding)
        self._append_flat(entry)
        self._sync_to_vault(entry)

        return entry.id

    def recall(self, query: str, top_k: int = 10,
               category: Optional[str] = None,
               min_importance: float = 0.0) -> List[MemoryQueryResult]:
        """
        Retrieve relevant memories using semantic search + importance weighting.
        Returns ranked results combining vector similarity and importance scores.
        """
        query_embedding = self.embedder.embed(query)
        vector_results = self.vector.search(
            query_embedding, top_k=top_k * 2, category_filter=category
        )

        results = []
        now = time.time()

        for memory_id, similarity in vector_results:
            if similarity < self.similarity_threshold:
                continue

            entry = self.structured.get(memory_id)
            if not entry or entry.importance < min_importance:
                continue

            days_since_access = (now - entry.last_accessed) / 86400
            decay = math.exp(
                -self.temporal_decay_rate * days_since_access / self.decay_half_life
            )

            final_score = (
                similarity * 0.5 +
                entry.importance * 0.3 +
                decay * 0.2
            )

            results.append(MemoryQueryResult(
                entry=entry,
                relevance_score=similarity,
                decay_factor=decay,
                final_score=final_score,
            ))

            self.structured.update_access(memory_id)

        results.sort(key=lambda r: r.final_score, reverse=True)
        return results[:top_k]

    def recall_by_category(self, category: str, limit: int = 10) -> List[MemoryEntry]:
        """Retrieve memories by category, ordered by importance."""
        return self.structured.get_by_category(category, limit)

    def recall_recent(self, limit: int = 10) -> List[MemoryEntry]:
        """Retrieve most recent memories."""
        return self.structured.get_recent(limit)

    def search_text(self, query: str, limit: int = 10) -> List[MemoryEntry]:
        """Full-text search across memory content."""
        return self.structured.search_fulltext(query, limit)

    def relate(self, source_id: str, target_id: str, relation: str, weight: float = 1.0):
        """Create an explicit relationship between two memories."""
        self.structured.add_relation(source_id, target_id, relation, weight)

    def get_context_for_task(self, task_description: str,
                             max_tokens: int = 2000) -> str:
        """
        Generate a context block of relevant memories for injection into a prompt.
        Used by the gateway to enrich every LLM call with relevant memory.
        """
        results = self.recall(task_description, top_k=5)
        if not results:
            return ""

        context_parts = ["## Relevant Memory Context\n"]
        total_chars = 0
        char_limit = max_tokens * 4

        for r in results:
            entry_text = (
                f"[{r.entry.category}] (importance: {r.entry.importance:.2f}, "
                f"relevance: {r.relevance_score:.2f})\n"
                f"{r.entry.content}\n"
            )
            if total_chars + len(entry_text) > char_limit:
                break
            context_parts.append(entry_text)
            total_chars += len(entry_text)

        return "\n".join(context_parts)

    def maintenance(self):
        """Run periodic maintenance: decay importance, prune old entries."""
        self.structured.decay_importance(self.temporal_decay_rate, self.decay_half_life)
        pruned = self.structured.prune(min_importance=0.1, max_entries=self.max_memories)
        if pruned > 0:
            print(f"[memory] Pruned {pruned} low-importance memories", flush=True)

    def export_knowledge_graph(self) -> dict:
        """Export the knowledge graph for visualization."""
        nodes = []
        edges = []
        for entry in self.structured.get_recent(100):
            nodes.append({
                "id": entry.id,
                "label": entry.content[:50],
                "category": entry.category,
                "importance": entry.importance,
            })
            for target_id, relation, weight in self.structured.get_related(entry.id):
                edges.append({
                    "source": entry.id,
                    "target": target_id,
                    "relation": relation,
                    "weight": weight,
                })
        return {"nodes": nodes, "edges": edges}

    def _auto_relate(self, entry: MemoryEntry, embedding: List[float]):
        """Automatically find and create relations to similar existing memories."""
        similar = self.vector.search(embedding, top_k=3)
        for memory_id, similarity in similar:
            if memory_id != entry.id and similarity > 0.85:
                self.structured.add_relation(
                    entry.id, memory_id, "similar", weight=similarity
                )

    def _append_flat(self, entry: MemoryEntry):
        """Append to flat MEMORY.md for backward compatibility."""
        try:
            timestamp = datetime.fromtimestamp(
                entry.created_at, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            line = (
                f"\n## {timestamp}\n"
                f"Category: {entry.category}\n"
                f"Importance: {entry.importance}\n"
                f"{entry.content}\n"
            )
            self.memory_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.memory_file, "a") as f:
                f.write(line)
        except Exception as e:
            print(f"[memory] Flat file append failed: {e}", flush=True)

    def _sync_to_vault(self, entry: MemoryEntry):
        """
        Sync memory entry to Obsidian vault as a markdown file with frontmatter.
        Creates one .md file per memory in the vault directory structure.
        """
        try:
            vault_dir = self.vault_path / entry.category
            vault_dir.mkdir(parents=True, exist_ok=True)

            safe_id = entry.id.replace("-", "")[:12]
            timestamp = datetime.fromtimestamp(
                entry.created_at, tz=timezone.utc
            ).strftime("%Y-%m-%d")
            filename = f"{timestamp}_{safe_id}.md"
            filepath = vault_dir / filename

            tags_str = ", ".join(f"\"{t}\"" for t in entry.tags)
            frontmatter = (
                "---\n"
                f"id: {entry.id}\n"
                f"category: {entry.category}\n"
                f"importance: {entry.importance}\n"
                f"tags: [{tags_str}]\n"
                f"source: {entry.source}\n"
                f"created: {timestamp}\n"
                "---\n\n"
            )

            content = frontmatter + entry.content + "\n"
            filepath.write_text(content)
        except Exception as e:
            print(f"[memory] Vault sync failed: {e}", flush=True)
