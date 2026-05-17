# Skill: obsidian-sync (Peak v1.0)

## Purpose
Bidirectional synchronization between Hermes semantic memory and an
Obsidian vault. Exports memories as markdown notes with frontmatter,
backlinks, and knowledge graph structure.

## When This Skill Applies
- "Sync memory to Obsidian"
- "Export today's research to vault"
- "Import notes from Obsidian"
- "Generate daily note"
- Proactive: nightly sync of new memories to vault

## Vault Path
Default: /data/.hermes/obsidian-vault/
Configurable via OBSIDIAN_VAULT_PATH environment variable.

## Sync to Obsidian
python3 /app/skills/obsidian-sync/obsidian_client.py sync --direction export

## Import from Obsidian
python3 /app/skills/obsidian-sync/obsidian_client.py sync --direction import

## Generate daily note
python3 /app/skills/obsidian-sync/obsidian_client.py daily-note

## Export knowledge graph
python3 /app/skills/obsidian-sync/obsidian_client.py export-graph

## Protocol
1. Read memory entries since last sync timestamp
2. Convert each entry to Obsidian-compatible markdown:
   - YAML frontmatter (category, tags, importance, date)
   - Wikilinks for related memories ([[related-note]])
   - Tags as #category/subcategory
3. Write to vault directory structure:
   - /daily/ for daily notes
   - /research/ for research entries
   - /decisions/ for decision records
   - /tasks/ for task outcomes
   - /graph/ for knowledge graph export
4. Update sync timestamp

## Obsidian Note Format
```markdown
---
id: memory-uuid
category: research
importance: 0.8
tags: [security, competitor, xbow]
created: 2025-01-15T10:30:00Z
related: [memory-uuid-2, memory-uuid-3]
---

# Note Title

Content of the memory entry.

## Related
- [[related-note-title]]
- [[another-related-note]]
```

## Error Handling
- If vault directory missing: create it
- If file conflict: prefer newer version
- If import finds invalid frontmatter: skip and log
