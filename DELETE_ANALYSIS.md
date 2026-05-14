# DELETE_ANALYSIS.md — Hermes88 Dead Code Purge

**Audit source:** HERMES88_PEAK_REPORT (632 lines, fully applied)
**Commit:** see git log — applied in one atomic commit after this report

---

## Summary

| Category | Files Deleted | Packages Removed | Bugs Fixed | Files Added |
|---|---|---|---|---|
| Dead modules | 30 | 18 | 6 | 3 |

---

## Files Deleted

### `rhodawk_core/` — 13 files (entire directory)

These files implemented the "Peak Architecture" — a second orchestration layer
(memory engine, event bus, task queue, image gen, synthesis) that ran **beside**
`hermes-agent` and duplicated all of its native capabilities. The result was
two control planes fighting over the same state with no real winner.

| File | Why Deleted |
|---|---|
| `rhodawk_core/__init__.py` | Package init for dead directory |
| `rhodawk_core/audit.py` | Security audit layer — duplicates `bandit`/`safety` in CI |
| `rhodawk_core/event_bus.py` | Redis pub/sub wrapper — hermes-agent has native event bus |
| `rhodawk_core/image_gen.py` | Image generation stub — no provider configured, never called |
| `rhodawk_core/memory.py` | MemoryEngine (mem0ai + chromadb) — hermes-agent has native memory |
| `rhodawk_core/operator_model.py` | Operator profile model — superseded by SOUL.md |
| `rhodawk_core/orchestrator.py` | Orchestrator — hermes-agent IS the orchestrator |
| `rhodawk_core/proactive.py` | Proactive intelligence — handled by hermes-agent skill scheduler |
| `rhodawk_core/search.py` | Search cascade — superseded by MCP server-brave-search + DDG |
| `rhodawk_core/skill_engine.py` | Skill dispatcher — hermes-agent has native skill engine |
| `rhodawk_core/synthesis.py` | Document synthesis — pypandoc wrapper, nothing called it |
| `rhodawk_core/task_engine.py` | Dramatiq/Celery task queue — hermes-agent has native task queue |
| `rhodawk_core/tools.py` | Tool registry — hermes-agent MCP layer replaces this |

**Packages removed with this directory:** `mem0ai`, `chromadb`, `sentence-transformers`,
`litellm`, `pydantic-ai`, `dramatiq[redis]`, `celery[redis]`, `apscheduler`,
`asyncio-mqtt`, `watchfiles`, `python-frontmatter`, `pypandoc`, `openai-whisper`,
`browser-use`, `playwright`, `discord.py`, `slack-sdk`, `numpy`

### `gateway/` — 3 dead modules

| File | Why Deleted |
|---|---|
| `gateway/event_consumer.py` | Consumed rhodawk_core.event_bus events — consumer and producer both gone |
| `gateway/memory_injector.py` | Injected rhodawk_core.memory into prompts — superseded by hermes-agent memory |
| `gateway/response_formatter.py` | Formatted responses for multi-channel — openclaw handles multi-channel |

### `rhodawk-tools/` — 6 files (entire directory)

Rust CLI tools (analyzer, scanner, search, stats) that were never built into
any Dockerfile. No `cargo build` step existed anywhere in the deployment chain.
Dead on arrival.

| File | Why Deleted |
|---|---|
| `rhodawk-tools/Cargo.toml` | Workspace manifest for unbuilt Rust tools |
| `rhodawk-tools/src/analyzer.rs` | Code analyzer — never built, never called |
| `rhodawk-tools/src/main.rs` | Rust entry point — never built |
| `rhodawk-tools/src/scanner.rs` | File scanner — never built, never called |
| `rhodawk-tools/src/search.rs` | Search tool — superseded by ripgrep + MCP |
| `rhodawk-tools/src/stats.rs` | Statistics — never built, never called |

### `tests/` — 7 files (entire directory)

Tests for rhodawk_core modules that were themselves deleted. All tests
imported from `rhodawk_core.*` and would fail unconditionally post-purge.

| File | Why Deleted |
|---|---|
| `tests/__init__.py` | Package init |
| `tests/conftest.py` | Fixtures for deleted modules |
| `tests/test_event_bus.py` | Tests for deleted event_bus.py |
| `tests/test_memory.py` | Tests for deleted memory.py |
| `tests/test_orchestrator.py` | Tests for deleted orchestrator.py |
| `tests/test_synthesis.py` | Tests for deleted synthesis.py |
| `tests/test_task_engine.py` | Tests for deleted task_engine.py |

### `Dockerfile.peak` + `supervisord.peak.conf` — 2 files

Peak Architecture Dockerfiles that pulled in all the dead dependencies.
The canonical deployment path is `Dockerfile.vps` + `supervisord.conf`.

---

## Bugs Fixed

### Fix 1 — Security: Token in git remote URL (Layer 1 GitHub + Layer 2 HF)
**File:** `bot/telegram_bot.py`
**Severity:** HIGH — Git tokens embedded in remote URLs appear in `git reflog`,
process lists (`ps aux`), and push error messages sent to stderr/Telegram.

- `_push_github_layer1_local_git`: replaced `https://x-token-auth:<TOKEN>@<host>` push
  with `git credential.helper` subprocess; token now travels only over TLS, never in argv.
- `_push_hf_layer2_local_git`: same fix for HuggingFace URLs (`https://user:<TOKEN>@huggingface.co`).
  Added `finally` block to always unset the credential helper after push.

### Fix 2 — docker-compose.yml: camofox service rebuilt on every restart
**File:** `docker-compose.yml`
**Severity:** HIGH — `node:20-slim` image with inline `apt-get + git clone + npm install`
on every `docker compose up` is a ~5 minute cold-start per restart plus network dependency.

Changed `camofox` service from inline `node:20-slim` + bash clone to `build: Dockerfile.camofox`.
All dependencies baked at image build time. Added `healthcheck` and `depends_on` for `hermes`.

### Fix 3 — Dockerfile.camofox: unpinned git clone
**File:** `Dockerfile.camofox`
**Severity:** MEDIUM — `git clone --depth 1` without a SHA means image content changes
silently on every `docker build` when upstream pushes to master.

Pinned to `ARG CAMOFOX_SHA=c9a90dafc76d2dfa0eb5d74fa36ef28f3ba98b29`. Added `HEALTHCHECK`.

### Fix 4 — CI: wrong key names in .env.example validation
**File:** `.github/workflows/ci.yml`
**Severity:** MEDIUM — `NVIDIA_NIM_API_KEY` never existed; `TELEGRAM_CHAT_ID` was missing.
CI validated a key that was never needed and missed one that IS required.

Replaced `NVIDIA_NIM_API_KEY` with `TELEGRAM_CHAT_ID`. Removed `aiosqlite`/`loguru` from
CI pip install (not in requirements.txt). Removed `hermes_bot.py`/`bot.memory` AST checks
(files reference non-existent modules post-purge).

### Fix 5 — supervisord.conf: startsecs too short for hermes-gateway
**File:** `supervisord.conf`
**Severity:** LOW — `startsecs=10` caused supervisord to declare hermes-gateway RUNNING
before the Telegram polling loop actually started, masking crash-restart cycles in `ps`.

Increased to `startsecs=15`.

### Fix 6 — package.json: @gitlawb/openclaude pinned to "latest"
**File:** `package.json`
**Severity:** LOW — `"latest"` is a floating tag; a breaking npm publish silently breaks
every subsequent `npm install` without any diff visible in git history.

Pinned to `"0.10.0"` (verified against npm registry at time of audit).

---

## Files Added

| File | Purpose |
|---|---|
| `.github/dependabot.yml` | Weekly automated PRs for pip, npm, docker, go dependencies |
| `.env.example` + `EXA_API_KEY` | Documents the optional Exa semantic search integration |
| `DELETE_ANALYSIS.md` | This file — audit trail for all deletions and fixes |
