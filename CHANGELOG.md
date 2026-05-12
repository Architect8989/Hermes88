# Changelog

All notable changes to Rhodawk AI — Hermes Code Stabilizer are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

---

## [2.1.0] — 2025-05-07

### Added
- `Dockerfile.vps` — VPS-optimized Dockerfile with `VOLUME ["/data"]` declaration
  and `HEALTHCHECK` polling supervisord every 30 s (`commit b4ce045`)
- `VPS_DEPLOYMENT.md` — end-to-end VPS deployment guide covering prerequisites,
  Docker setup, secret configuration, persistent storage, systemd auto-start,
  monitoring, update workflow, troubleshooting, and security hardening (`commit b4ce045`)
- `docker-compose.yml` — single-file compose config with named volume, healthcheck,
  log rotation (50 MB / 5 files), and `restart: unless-stopped` (`commit 3a936ad`)
- `deploy.sh` — one-command VPS installer (`curl ... | bash`): installs Docker,
  clones repo, collects secrets interactively with hidden input, writes `.env` at
  mode 600, validates all required keys, builds image, polls healthcheck, and prints
  a quick-reference command card on success (`commit 5747e7a`)

---

## [2.0.0] — 2025-04-xx

### Changed (Breaking)
- Replaced `hermes-agent` (NousResearch, not on PyPI) with a self-contained
  `python-telegram-bot` runtime (`hermes_bot.py`). No black-box pip dependency.
- `supervisord.conf` updated: `[program:hermes-gateway]` now runs
  `python3 /app/hermes_bot.py` instead of `hermes gateway run`.
- Persistent storage path changed from `/root/.hermes/` to `/data/.hermes/`
  to align with HuggingFace Space persistent volume semantics.

### Added
- `hermes_bot.py` — full Telegram gateway with asyncio pipeline, inline keyboard
  retry/abort buttons, and streaming progress messages.
- `bot/memory.py` — async SQLite memory module (`aiosqlite`) tracking sessions,
  conversation messages, and execution audit log across three tables.
- `bot/telegram_bot.py` — headless utility exposing three CLI actions:
  - `bounded-run` — 3-strike self-healing command runner (openclaude on failure)
  - `push-commit` — 3-layer resilient push, auto-detects GitHub vs HuggingFace
  - `ingest-media` — image / PDF / ZIP / text extraction
- `skills/devops-pipeline/SKILL.md` — v2 dynamic skill format (v2.1.0) with full
  agent delegation matrix, parallel scaffolding patterns, and error routing table.

### Improved
- HuggingFace push chain: `huggingface_hub.HfApi.create_commit()` → local git →
  raw HTTP multipart POST (3 layers, same pattern as GitHub chain).
- GitHub push chain: local git → GitHub REST API (Python requests) →
  auto-generated Node.js GitHub API script (3 layers).
- LLM routing: DigitalOcean Inference `deepseek-ai/DeepSeek-V4-Pro` as primary
  for OpenClaude + JCode; NVIDIA NIM as fallback. Model ID casing enforced
  (title-case for DO, lowercase for NIM).

---

## [1.0.0] — 2025-03-xx

### Added
- Initial release: 4-agent software factory on HuggingFace Spaces.
- `Dockerfile` — ubuntu:22.04 base, Node.js 24, Python 3.11, ripgrep 14.1.0,
  `@gitlawb/openclaude`, `openclaw@latest`, `jcode` binary, supervisord.
- `hermes_config/config.yaml` — hermes-agent CLI config (NVIDIA NIM provider,
  yolo approval mode, Telegram toolset).
- `hermes_config/SOUL.md` — agent persona, behaviour rules, and LLM routing table.
- `scripts/init_and_start.sh` — boot sequence: secret validation, `/data/.hermes/`
  config write, openclaw config write, tool availability check, supervisord exec.
- `scripts/start_openclaw.sh` — graceful no-op (`sleep infinity`) when openclaw
  binary is absent; prevents supervisord crash on missing optional agent.
- `skills/devops-pipeline/skill.md` — v1 hardcoded 7-step pipeline
  (clone → ripgrep → openclaude 3x → JCode swarm → commit → push → report).
- `.env.example` — annotated template for all required and optional secrets.

---

[Unreleased]: https://huggingface.co/spaces/Architect8999/Hermes/compare/v2.1.0...HEAD
[2.1.0]: https://huggingface.co/spaces/Architect8999/Hermes/compare/v2.0.0...v2.1.0
[2.0.0]: https://huggingface.co/spaces/Architect8999/Hermes/compare/v1.0.0...v2.0.0
[1.0.0]: https://huggingface.co/spaces/Architect8999/Hermes/commits/main

---

## [3.0.0] — 2025-05-10

### Changed (SOUL.md v7.0 — Full Replacement)
- Complete rewrite of `hermes_config/SOUL.md` from v6.0 to v7.0.
- Added CEO persona layer: Hermes now identifies as the operator's
  "antagonist-self" — no doubt, no hesitation, zero process theater.
- Added 5 explicit Output Rules (Hedge Reducer, Direct Mode, Plain Text,
  Execution Mandate, Task Completion Standard) replacing implicit guidance.
- Replaced `tool_call` JSON format references in SOUL.md with imperative
  natural-language action verbs — fixes deepseek-r1-distill-llama-70b
  outputting tool call JSON as plain text instead of executing.
- Added Long-Horizon Task Protocol (DeerFlow context engineering pattern):
  decompose → sequential sub-goals → background PID polling → checkpoint
  → final report with commit hash.
- Added GOAP Planning Protocol (Ruflo goal-oriented action planning):
  STATE → GOAL → ACTIONS → BLOCKERS → EXECUTE within same turn.
- Added Web Fetch Decision Tree: camofox health check before every URL
  fetch, with automatic fallback to spoofed-UA curl when camofox is down.
- Added openclaude gRPC pre-check and exact invocation pattern to prevent
  vague prompts that cause code block output instead of file writes.
- Added focused context file pattern for sub-agent delegation.

### Changed (gateway.yaml v7.0 delta)
- `temperature`: 0.2 → 0.05 — deterministic tool dispatch, eliminates
  model outputting prose where tool calls should be.
- `timeout`: 600 → 900 — 15-minute LLM request window for long tasks.
- `gateway_timeout`: 1800 → 3600 — allow hour-long autonomous tasks.
- `max_tokens`: 8192 → 16384 — more room for complex reasoning chains.
- `context_window_limit`: 28000 → 60000 — modern deepseek context budget.
- `max_history_messages`: 30 → 50 — longer session memory.
- `max_foreground_timeout`: 3600 → 7200 — 2-hour terminal tasks allowed.
- Added `CAMOFOX_ACCESS_KEY` to `tools.terminal.passthrough_env`.

### Changed (config.yaml — aligned with gateway.yaml v7.0)
- Same parameter deltas applied to `hermes_config/config.yaml` for
  consistency: max_tokens, temperature, context_limit, gateway_timeout,
  max_history_messages, max_foreground_timeout.

### Added (New Skill Files)
- `skills/research-deep/SKILL.md` — Deep Research skill: 3-5 search
  angles → full page fetch → structured report → save to
  `/data/.hermes/research/` → Telegram summary.
- `skills/competitive-intel/SKILL.md` — Competitive Intelligence skill
  for XBOW, CrowdStrike Falcon, GitHub Advanced Security, Semgrep.
  Outputs Competitor Card: pricing, moat, weakness, Rhodawk counter.
- `skills/security-audit/SKILL.md` — Security Audit skill: clone →
  bandit + safety + semgrep → aggregate → JSON report in
  `/data/.hermes/audit_reports/`.
- `skills/security-audit/aggregate.py` — Python aggregator: merges
  bandit + safety JSON into unified Rhodawk audit report with
  critical/high/medium/low bucketing.
- `skills/stealth-browse/SKILL.md` — Stealth Browser skill (Camofox
  v7.1): core fetch, YouTube transcript, cookie auth, session limits,
  fallback to spoofed-UA curl when camofox is down.
- `skills/openclaude_grpc/SKILL.md` — Updated with gRPC pre-check,
  exact invocation pattern, and prompt format guidance to prevent
  code block output instead of file writes.

### Added (v7.1 — Camofox Stealth Browser)
- `docker-compose.yml` — Added `camofox` service: node:20-slim,
  clones `jo-inc/camofox-browser`, runs on port 9377, named volume
  `camofox_data`, `restart: unless-stopped`.
- `.env.example` — Added `CAMOFOX_ACCESS_KEY` with generation command.
- Camofox resolves ~40% research skill blind spot: Cloudflare-protected
  sites, JS SPAs, LinkedIn/Crunchbase, YouTube transcripts. Coverage
  improves from ~60% to ~95%+. Deploy on DO VPS only (not HF Spaces).

### Changed (init_and_start.sh v4.1 → v4.2)
- Updated banner version string to v7.1.
- Added deployment of new skill files:
  research-deep, competitive-intel, security-audit, stealth-browse.
- Creates `/data/.hermes/audit_reports/` on startup.
