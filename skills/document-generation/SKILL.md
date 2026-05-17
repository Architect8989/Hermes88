# Skill: document-generation (Peak v1.0)

## Purpose
Generate professional documents: pitch decks, proposals, reports,
contracts, and technical documentation.

## When This Skill Applies
- "Generate a pitch deck for [topic]"
- "Write a proposal for [client]"
- "Create a technical design doc for [feature]"
- "Write an investor update"
- "Generate a security audit report"

## Supported Formats
- Markdown (default)
- PDF (via pandoc + LaTeX or weasyprint)
- HTML (for email embedding)
- Plain text

## Generate document
python3 /app/skills/document-generation/doc_generator.py \
  --type pitch-deck \
  --topic "Rhodawk AI DevSecOps Platform" \
  --format pdf \
  --output /tmp/rhodawk_pitch.pdf

## Document Types
- pitch-deck: 10-slide investor deck structure
- proposal: client proposal with pricing tiers
- tech-design: RFC-style technical design document
- audit-report: security audit findings report
- weekly-report: investor update email
- changelog: release notes from git history

## Protocol
1. Select template based on document type
2. Gather context from memory (relevant past decisions, data points)
3. Generate content via LLM with document-specific prompt
4. Format according to output type
5. Deliver via %%FILE%% tag to operator

## Error Handling
- If pandoc unavailable: fall back to markdown output
- If template missing: use generic structure
- If output too large: split into sections
