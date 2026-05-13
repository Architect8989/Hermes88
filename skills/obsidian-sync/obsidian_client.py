#!/usr/bin/env python3
"""
Obsidian Vault Sync Skill for Hermes88.
Bidirectional synchronization between Hermes semantic memory and an
Obsidian vault. Exports memories as markdown notes with frontmatter,
backlinks, and knowledge graph structure.

Rhodawk AI -- Peak Architecture v10.0
"""
import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Ensure rhodawk_core is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

try:
    from rhodawk_core.memory import MemoryEngine, MemoryEntry
    MEMORY_AVAILABLE = True
except ImportError:
    MEMORY_AVAILABLE = False


@dataclass
class ObsidianNote:
    """Represents a note in the Obsidian vault."""
    path: str = ""
    title: str = ""
    content: str = ""
    frontmatter: dict = field(default_factory=dict)
    backlinks: list = field(default_factory=list)
    tags: list = field(default_factory=list)
    created_at: str = ""
    modified_at: str = ""

    def to_markdown(self) -> str:
        """Convert to Obsidian-compatible markdown with frontmatter."""
        lines = ["---"]
        for key, value in self.frontmatter.items():
            if isinstance(value, list):
                lines.append(f"{key}: {json.dumps(value)}")
            elif isinstance(value, (int, float)):
                lines.append(f"{key}: {value}")
            else:
                lines.append(f"{key}: \"{value}\"")
        lines.append("---")
        lines.append("")
        lines.append(f"# {self.title}")
        lines.append("")
        lines.append(self.content)

        if self.backlinks:
            lines.append("")
            lines.append("## Related")
            lines.append("")
            for link in self.backlinks:
                lines.append(f"- [[{link}]]")

        if self.tags:
            lines.append("")
            lines.append("## Tags")
            lines.append("")
            lines.append(" ".join(f"#{t}" for t in self.tags))

        return "\n".join(lines) + "\n"

    @classmethod
    def from_markdown(cls, content: str, path: str = "") -> "ObsidianNote":
        """Parse an Obsidian markdown note."""
        note = cls(path=path)

        # Parse frontmatter
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                frontmatter_text = parts[1].strip()
                note.frontmatter = cls._parse_frontmatter(frontmatter_text)
                content = parts[2].strip()

        # Extract title from first heading
        title_match = re.match(r'^#\s+(.+)$', content, re.MULTILINE)
        if title_match:
            note.title = title_match.group(1)

        # Extract backlinks [[link]]
        note.backlinks = re.findall(r'\[\[([^\]]+)\]\]', content)

        # Extract tags #tag
        note.tags = re.findall(r'#([\w/]+)', content)

        note.content = content
        note.created_at = note.frontmatter.get("created", "")
        note.modified_at = note.frontmatter.get("modified", "")

        return note

    @staticmethod
    def _parse_frontmatter(text: str) -> dict:
        """Parse YAML-like frontmatter."""
        result = {}
        for line in text.split("\n"):
            if ":" in line:
                key, value = line.split(":", 1)
                key = key.strip()
                value = value.strip().strip('"')
                # Try to parse as JSON for lists
                try:
                    value = json.loads(value)
                except (json.JSONDecodeError, ValueError):
                    pass
                result[key] = value
        return result


class SyncState:
    """Tracks synchronization state between Hermes and Obsidian."""

    def __init__(self, state_path: str = "/data/.hermes/obsidian_sync_state.json"):
        """Initialize sync state tracker."""
        self.state_path = Path(state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state: dict = self._load()

    def _load(self) -> dict:
        """Load sync state from disk."""
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text())
            except Exception:
                pass
        return {
            "last_export": 0.0,
            "last_import": 0.0,
            "exported_ids": [],
            "imported_files": [],
        }

    def save(self):
        """Save sync state to disk."""
        self.state_path.write_text(json.dumps(self._state, indent=2))

    @property
    def last_export(self) -> float:
        """Timestamp of last export."""
        return self._state.get("last_export", 0.0)

    @last_export.setter
    def last_export(self, value: float):
        self._state["last_export"] = value

    @property
    def last_import(self) -> float:
        """Timestamp of last import."""
        return self._state.get("last_import", 0.0)

    @last_import.setter
    def last_import(self, value: float):
        self._state["last_import"] = value

    @property
    def exported_ids(self) -> list:
        """List of memory IDs that have been exported."""
        return self._state.get("exported_ids", [])

    def mark_exported(self, memory_id: str):
        """Mark a memory as exported."""
        if memory_id not in self._state.get("exported_ids", []):
            self._state.setdefault("exported_ids", []).append(memory_id)

    def is_exported(self, memory_id: str) -> bool:
        """Check if a memory has been exported."""
        return memory_id in self._state.get("exported_ids", [])


class ObsidianVault:
    """Manages the Obsidian vault directory structure."""

    # Subdirectory mapping by category
    CATEGORY_DIRS = {
        "research": "research",
        "code_change": "development",
        "decision": "decisions",
        "task_outcome": "tasks",
        "financial": "financial",
        "competitive_intel": "competitive",
        "operator_preference": "preferences",
        "system_event": "system",
        "general": "notes",
    }

    def __init__(self, vault_path: str = ""):
        """
        Initialize vault manager.

        Args:
            vault_path: Path to the Obsidian vault directory.
        """
        self.vault_path = Path(
            vault_path or os.environ.get(
                "OBSIDIAN_VAULT_PATH", "/data/.hermes/obsidian-vault"
            )
        )
        self._ensure_structure()

    def _ensure_structure(self):
        """Create vault directory structure."""
        self.vault_path.mkdir(parents=True, exist_ok=True)
        for subdir in set(self.CATEGORY_DIRS.values()):
            (self.vault_path / subdir).mkdir(exist_ok=True)
        (self.vault_path / "daily").mkdir(exist_ok=True)
        (self.vault_path / "graph").mkdir(exist_ok=True)
        (self.vault_path / "templates").mkdir(exist_ok=True)

    def write_note(self, note: ObsidianNote):
        """Write a note to the vault."""
        if not note.path:
            # Generate path from category and title
            category = note.frontmatter.get("category", "general")
            subdir = self.CATEGORY_DIRS.get(category, "notes")
            slug = self._slugify(note.title)
            note.path = f"{subdir}/{slug}.md"

        full_path = self.vault_path / note.path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(note.to_markdown())

    def read_note(self, path: str) -> Optional[ObsidianNote]:
        """Read a note from the vault."""
        full_path = self.vault_path / path
        if not full_path.exists():
            return None

        content = full_path.read_text(encoding="utf-8")
        return ObsidianNote.from_markdown(content, path)

    def list_notes(self, subdir: str = "") -> list:
        """List all notes in the vault (or a subdirectory)."""
        search_path = self.vault_path / subdir if subdir else self.vault_path
        notes = []
        for md_file in search_path.rglob("*.md"):
            rel_path = str(md_file.relative_to(self.vault_path))
            notes.append(rel_path)
        return sorted(notes)

    def get_modified_since(self, timestamp: float) -> list:
        """Get notes modified since a timestamp."""
        modified = []
        for md_file in self.vault_path.rglob("*.md"):
            if md_file.stat().st_mtime > timestamp:
                rel_path = str(md_file.relative_to(self.vault_path))
                modified.append(rel_path)
        return modified

    def write_daily_note(self, date: Optional[datetime] = None) -> str:
        """
        Create or update today's daily note.

        Args:
            date: Date for the note (default: today).

        Returns:
            Path to the daily note.
        """
        date = date or datetime.now(timezone.utc)
        date_str = date.strftime("%Y-%m-%d")
        filename = f"daily/{date_str}.md"

        note = ObsidianNote(
            path=filename,
            title=f"Daily Note - {date_str}",
            frontmatter={
                "date": date_str,
                "type": "daily",
                "created": date.isoformat(),
            },
            content=(
                f"## {date.strftime('%A, %B %d, %Y')}\n\n"
                f"### Tasks\n\n"
                f"### Notes\n\n"
                f"### Events\n\n"
            ),
        )

        self.write_note(note)
        return filename

    def _slugify(self, text: str) -> str:
        """Convert text to a filesystem-safe slug."""
        # Remove special characters, replace spaces with hyphens
        slug = re.sub(r'[^\w\s-]', '', text.lower())
        slug = re.sub(r'[\s_]+', '-', slug)
        slug = slug.strip('-')[:60]
        return slug or "untitled"


class ObsidianSync:
    """
    Bidirectional sync between Hermes memory and Obsidian vault.

    Export: Memory entries -> Obsidian markdown notes
    Import: Obsidian notes -> Memory entries
    """

    def __init__(self, vault_path: str = ""):
        """
        Initialize sync manager.

        Args:
            vault_path: Path to Obsidian vault.
        """
        self.vault = ObsidianVault(vault_path)
        self.sync_state = SyncState()
        self._engine: Optional[object] = None

        if MEMORY_AVAILABLE:
            try:
                self._engine = MemoryEngine()
            except Exception as e:
                print(f"[obsidian] Memory engine init failed: {e}", flush=True)

    def export_to_vault(self, since: Optional[float] = None,
                        category: str = "") -> int:
        """
        Export memory entries to Obsidian vault.

        Args:
            since: Export entries created after this timestamp.
                   Default: last export time.
            category: Only export entries of this category.

        Returns:
            Number of notes exported.
        """
        if not self._engine:
            print("[obsidian] Memory engine not available", flush=True)
            return 0

        since = since or self.sync_state.last_export
        exported = 0

        # Get recent memories
        try:
            if category:
                entries = self._engine.recall_by_category(category, limit=50)
            else:
                entries = self._engine.recall_recent(limit=50)
        except Exception as e:
            print(f"[obsidian] Memory recall error: {e}", flush=True)
            return 0

        for entry in entries:
            # Skip if already exported or too old
            if self.sync_state.is_exported(entry.id):
                continue
            if since > 0 and entry.created_at < since:
                continue

            # Convert to Obsidian note
            note = self._memory_to_note(entry)
            self.vault.write_note(note)
            self.sync_state.mark_exported(entry.id)
            exported += 1

        # Update sync state
        self.sync_state.last_export = time.time()
        self.sync_state.save()

        print(f"[obsidian] Exported {exported} memories to vault", flush=True)
        return exported

    def import_from_vault(self) -> int:
        """
        Import new/modified Obsidian notes into Hermes memory.

        Returns:
            Number of notes imported.
        """
        if not self._engine:
            print("[obsidian] Memory engine not available", flush=True)
            return 0

        modified = self.vault.get_modified_since(self.sync_state.last_import)
        imported = 0

        for note_path in modified:
            # Skip daily notes and templates
            if note_path.startswith("daily/") or note_path.startswith("templates/"):
                continue

            note = self.vault.read_note(note_path)
            if not note:
                continue

            # Check if this is an exported note (has memory_id in frontmatter)
            if note.frontmatter.get("memory_id"):
                # Skip -- this is a note we exported
                continue

            # Import as new memory
            try:
                category = note.frontmatter.get("category", "general")
                importance = float(note.frontmatter.get("importance", 0.5))

                self._engine.remember(
                    content=f"{note.title}\n{note.content[:1000]}",
                    category=category,
                    importance=importance,
                    tags=note.tags,
                    source="obsidian_import",
                )
                imported += 1
            except Exception as e:
                print(f"[obsidian] Import error for {note_path}: {e}", flush=True)

        # Update sync state
        self.sync_state.last_import = time.time()
        self.sync_state.save()

        print(f"[obsidian] Imported {imported} notes from vault", flush=True)
        return imported

    def sync(self, direction: str = "both") -> dict:
        """
        Run synchronization.

        Args:
            direction: "export", "import", or "both".

        Returns:
            Dict with export/import counts.
        """
        result = {"exported": 0, "imported": 0}

        if direction in ("export", "both"):
            result["exported"] = self.export_to_vault()

        if direction in ("import", "both"):
            result["imported"] = self.import_from_vault()

        return result

    def export_knowledge_graph(self) -> str:
        """
        Export the knowledge graph in a format suitable for Obsidian Graph View.

        Returns:
            Path to the exported graph file.
        """
        if not self._engine:
            print("[obsidian] Memory engine not available", flush=True)
            return ""

        try:
            graph = self._engine.export_knowledge_graph()
        except Exception as e:
            print(f"[obsidian] Graph export error: {e}", flush=True)
            return ""

        # Write as JSON for potential visualization plugins
        graph_path = self.vault.vault_path / "graph" / "knowledge_graph.json"
        graph_path.write_text(json.dumps(graph, indent=2))

        # Also write as markdown index
        index_lines = ["# Knowledge Graph Index\n"]
        for node in graph.get("nodes", []):
            label = node.get("label", "")[:50]
            category = node.get("category", "")
            index_lines.append(f"- [[{label}]] ({category})")

        index_path = self.vault.vault_path / "graph" / "INDEX.md"
        index_path.write_text("\n".join(index_lines) + "\n")

        print(
            f"[obsidian] Exported knowledge graph: "
            f"{len(graph.get('nodes', []))} nodes, "
            f"{len(graph.get('edges', []))} edges",
            flush=True,
        )
        return str(graph_path)

    def generate_daily_note(self) -> str:
        """
        Generate today's daily note with memory context.

        Returns:
            Path to the daily note.
        """
        today = datetime.now(timezone.utc)
        date_str = today.strftime("%Y-%m-%d")
        filename = f"daily/{date_str}.md"

        # Gather context for today
        content_parts = [f"## {today.strftime('%A, %B %d, %Y')}\n"]

        # Add recent memories
        if self._engine:
            try:
                recent = self._engine.recall_recent(limit=5)
                if recent:
                    content_parts.append("### Recent Context\n")
                    for entry in recent:
                        content_parts.append(
                            f"- [{entry.category}] {entry.content[:100]}"
                        )
                    content_parts.append("")
            except Exception:
                pass

        content_parts.extend([
            "### Today's Focus\n",
            "",
            "### Notes\n",
            "",
            "### End of Day Review\n",
            "",
        ])

        note = ObsidianNote(
            path=filename,
            title=f"Daily Note - {date_str}",
            frontmatter={
                "date": date_str,
                "type": "daily",
                "created": today.isoformat(),
            },
            content="\n".join(content_parts),
        )

        self.vault.write_note(note)
        print(f"[obsidian] Daily note: {filename}", flush=True)
        return filename

    def _memory_to_note(self, entry) -> ObsidianNote:
        """Convert a MemoryEntry to an ObsidianNote."""
        # Generate title from content
        title = entry.content.split("\n")[0][:60]
        if not title:
            title = f"Memory {entry.id[:8]}"

        # Build frontmatter
        created = datetime.fromtimestamp(
            entry.created_at, tz=timezone.utc
        ).isoformat()

        frontmatter = {
            "memory_id": entry.id,
            "category": entry.category,
            "importance": entry.importance,
            "created": created,
            "tags": entry.tags,
            "source": entry.source,
        }

        # Build backlinks from related memories
        backlinks = []
        if hasattr(entry, "related_ids") and entry.related_ids:
            for rel_id in entry.related_ids[:5]:
                backlinks.append(f"memory-{rel_id[:8]}")

        return ObsidianNote(
            title=title,
            content=entry.content,
            frontmatter=frontmatter,
            backlinks=backlinks,
            tags=entry.tags + [entry.category],
        )


# -- CLI Interface ---------------------------------------------------------------

def main():
    """CLI entry point for Obsidian sync."""
    parser = argparse.ArgumentParser(
        description="Obsidian Vault Sync -- Rhodawk AI Hermes88"
    )
    sub = parser.add_subparsers(dest="command")

    # Sync
    sync_p = sub.add_parser("sync", help="Synchronize memory and vault")
    sync_p.add_argument(
        "--direction", default="both",
        choices=["export", "import", "both"],
    )

    # Daily note
    sub.add_parser("daily-note", help="Generate today's daily note")

    # Export graph
    sub.add_parser("export-graph", help="Export knowledge graph")

    # List notes
    list_p = sub.add_parser("list", help="List vault notes")
    list_p.add_argument("--subdir", default="")

    # Status
    sub.add_parser("status", help="Show sync status")

    args = parser.parse_args()
    sync = ObsidianSync()

    if args.command == "sync":
        result = sync.sync(direction=args.direction)
        print(
            f"Sync complete: {result['exported']} exported, "
            f"{result['imported']} imported"
        )

    elif args.command == "daily-note":
        path = sync.generate_daily_note()
        print(f"Daily note: {path}")

    elif args.command == "export-graph":
        path = sync.export_knowledge_graph()
        if path:
            print(f"Graph exported: {path}")
        else:
            sys.exit(1)

    elif args.command == "list":
        notes = sync.vault.list_notes(args.subdir)
        print(f"Notes in vault ({len(notes)}):")
        for note in notes[:50]:
            print(f"  {note}")
        if len(notes) > 50:
            print(f"  ... and {len(notes) - 50} more")

    elif args.command == "status":
        state = sync.sync_state
        last_export = datetime.fromtimestamp(
            state.last_export, tz=timezone.utc
        ).isoformat() if state.last_export else "never"
        last_import = datetime.fromtimestamp(
            state.last_import, tz=timezone.utc
        ).isoformat() if state.last_import else "never"
        print(f"Last export: {last_export}")
        print(f"Last import: {last_import}")
        print(f"Exported memories: {len(state.exported_ids)}")
        print(f"Vault path: {sync.vault.vault_path}")
        total_notes = len(sync.vault.list_notes())
        print(f"Total notes in vault: {total_notes}")

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
