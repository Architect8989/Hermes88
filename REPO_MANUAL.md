# Hermes88 Repository Manual

Commit: 4838c92 (refactor: replace all custom wrapper code with real CLI pass-throughs)
Branch: main
Date of analysis: current HEAD

---

## 1. Repository Identity

Single-commit refactored codebase. The prior version (1,240-line gateway/run.py that reimplemented hermes-agent) has been replaced with a 93-line pass-through that exec's the real hermes-agent binary.

Total files: 119
Languages: Python, Bash, TypeScript, Go, Rust, YAML, Protobuf, Markdown

---

## 2. Architectural Pattern (Post-Refactor)

The system now follows a pass-through architecture:
- gateway/run.py: 93 lines. Tries `hermes-agent gateway`, falls back to `openclaw gateway`. If neither binary exists, exits with install instructions.
- scripts/jcode: 33 lines. Locates the real jcode binary in PATH/cargo/local. If not found, exits with install instructions. No fake wrapper logic.
- skills/openclaude_grpc/server.py: 128 lines. Locates the real `openclaude` binary via `shutil.which`. Spawns it as a subprocess with `--print --prompt ... --workdir ...`. Streams stdout line-by-line as gRPC TextChunk messages. All agentic logic (tools, file ops, bash) is delegated to the real openclaude process.
- rhodawk_core/orchestrator.py: 73 lines. Locates `hermes` or `hermes-agent` binary. Passes prompt via `--message` flag. Returns stdout as content.
- rhodawk_core/task_engine.py: 96 lines. Submits tasks by spawning `hermes-agent --message "..."` as a background process. Minimal wrapper.
- supervisord.conf: 210 lines. hermes-gateway now tries `hermes-agent --config` first, falls to gateway/run.py second.

The refactor principle: if an external tool already does X, call the tool. Do not reimplement X.

---

## 3. File-by-File Inventory

### Runtime Core

| File | Lines | Role |
|------|-------|------|
| gateway/run.py | 93 | Entry point. exec's hermes-agent or openclaw. |
| main.py | 158 | Process supervisor. Tries hermes-agent CLI, module, then gateway/run.py. Crash recovery loop (max 10). |
| bot/telegram_bot.py | 1,696 | CLI utility: push-commit, bounded-run, ingest-media, health-check, rotate-camofox-key. Unchanged from original. |
| send_file.py | 173 | Telegram file sender. Resolves chat ID, sends via sendDocument API. |
| scripts/init_and_start.sh | 571 | Container bootstrap. Validates secrets, exports env, expands templates, deploys configs, starts supervisord. |
| scripts/jcode | 33 | Pass-through to real jcode binary. Searches PATH, ~/.cargo/bin, /usr/local/bin. |
| scripts/watchdog.py | 136 | Polls supervisord, alerts operator on FATAL/EXITED processes via Telegram. |
| scripts/rhodawk_orchestrator.sh | (new) | Orchestration helper script. |
| scripts/health_check.sh | (new) | Health check script. |

### Sub-Agent Integration

| File | Lines | Role |
|------|-------|------|
| skills/openclaude_grpc/server.py | 128 | gRPC server. Spawns real `openclaude` CLI as subprocess. Streams output as TextChunk. |
| skills/openclaude_grpc/client.py | 128 | gRPC client. Connects to :50051. Falls back to direct `openclaude --print` CLI call. |
| skills/openclaude_grpc/agentic_client.py | 565 | Multi-iteration coding loop. Plan->edit->verify->iterate. Uses DO Inference API directly. |
| skills/jcode_swarm/spawn.py | 68 | Parallel asyncio spawner. Clones repos, calls `jcode run --message`. |
| skills/jcode_swarm/coordinator.py | 593 | Task decomposition via LLM, parallel workers, retry on failure. |
| skills/jcode_swarm/session_manager.py | (new) | jcode session persistence manager. |
| skills/jcode_swarm/coord_daemon.py | (new) | Coordination daemon. |

### rhodawk_core/ Package

| File | Lines | Role |
|------|-------|------|
| __init__.py | 56 | Exports all major classes. |
| orchestrator.py | 73 | Pass-through to hermes-agent binary for model routing. |
| task_engine.py | 96 | Pass-through to hermes-agent for background tasks. |
| memory.py | 733 | SQLite structured store + Redis vector search + Obsidian sync. |
| tools.py | 731 | Tool registry with decorator-based registration. |
| event_bus.py | (exists) | Redis PubSub event system. |
| synthesis.py | (exists) | Response formatting. |
| proactive.py | (exists) | Proactive intelligence scanning. |
| audit.py | (exists) | Append-only audit logging. |
| skill_engine.py | (new) | Skill learning and retrieval engine. |
| operator_model.py | (new) | Operator preference modeling. |
| image_gen.py | (new) | Image generation via FAL.ai. |
| search.py | (new) | Multi-tier search cascade. |

### Configuration

| File | Lines | Role |
|------|-------|------|
| hermes_config/SOUL.md | 375 | System prompt. v10.0. 10 output rules, anti-fabrication protocol, task routing matrix, 4-tier search cascade, camofox toolkit, image generation, skill learning, jcode sessions, synthesis templates. |
| hermes_config/config.yaml | 250 | hermes-agent config. Multi-provider failover (DO->Anthropic->OpenRouter), semantic memory, task queue, event bus, cron, proactive intelligence, sandbox. |
| hermes_config/gateway.yaml | 246 | Gateway config. Telegram platform, webhooks, tools, memory, cron, sessions, logging. |
| mcp_shared.json | (exists) | MCP server definitions (filesystem, github, fetch, brave-search). |
| openclaude_settings.json | (exists) | Per-task model routing for openclaude. |

### Infrastructure

| File | Lines | Role |
|------|-------|------|
| docker-compose.yml | 135 | Production compose. Redis Stack + hermes + camofox. Redis is now included. |
| docker-compose.peak.yml | (exists) | Extended compose with webhook-receiver, sandbox-manager. |
| Dockerfile.vps | 214 | Main container. Ubuntu 22.04, Python 3.11, Node 24, Bun, hermes-agent, openclaude, MCP servers. |
| Dockerfile.peak | (exists) | Extended Dockerfile. |
| Dockerfile.camofox | (exists) | Camofox browser container. |
| Dockerfile.webhook | (exists) | Webhook receiver container. |
| Dockerfile.sandbox | (exists) | Sandbox base image. |
| Dockerfile.sandbox-manager | (exists) | Sandbox manager. |
| supervisord.conf | 210 | Process manager. hermes-gateway (hermes-agent first), openclaude-grpc, jcode-server, openclaw-gateway, watchdog. |
| supervisord.peak.conf | (exists) | Extended supervisor config. |

---

## 4. External Tool Integration Status

### hermes-agent (NousResearch/hermes-agent)
- **Integration method:** supervisord.conf attempts `hermes-agent --config $HERMES_HOME/config.yaml` as primary process.
- **Fallback:** gateway/run.py (93 lines) which itself tries `hermes-agent gateway` then `openclaw gateway`.
- **Config surface:** hermes_config/config.yaml (250 lines), hermes_config/gateway.yaml (246 lines), hermes_config/SOUL.md (375 lines).
- **Status:** Correct architectural position. hermes-agent IS the control plane. Config is comprehensive.
- **Unverifiable claim:** Whether hermes-agent actually reads all config keys defined in config.yaml. The config includes keys like `event_bus`, `task_queue`, `proactive`, `sandbox` which may or may not be recognized by the actual hermes-agent package.

### openclaude (Gitlawb/openclaude)
- **Integration method:** server.py spawns `openclaude --print --prompt ... --workdir ...` as subprocess.
- **Fallback in client.py:** Direct CLI call `openclaude --print --prompt ...` when gRPC unavailable.
- **YOLO flag:** `--dangerously-skip-permissions` passed when HERMES_YOLO_MODE=1.
- **Status:** Pass-through is architecturally correct. All tool calling, file ops, bash execution handled by openclaude binary itself.
- **Unverifiable claim:** Whether openclaude's stdout produces structured output that maps to ToolCallStart/ToolCallResult proto messages. Current server.py streams raw lines as TextChunk only.

### jcode (1jehuang/jcode)
- **Integration method:** scripts/jcode is a 33-line locator script. Searches 5 paths for the binary. exec's it with all args passed through.
- **Session persistence:** session_manager.py manages project-scoped sessions via `--session` flag.
- **Coordination:** coord_daemon.py and coordinator.py handle parallel task decomposition.
- **Status:** Correct. No fake wrapper. Real binary required. If not installed, clear error message.
- **Unverifiable claim:** Whether `jcode serve --port 7865` provides the conflict-notification protocol described in supervisord.conf comments. Cannot verify without access to the jcode repo.

### openclaw (openclaw/openclaw)
- **Integration method:** supervisord.conf starts `openclaw gateway --port 18789` with TELEGRAM_BOT_TOKEN explicitly unset.
- **Config:** init_and_start.sh writes /root/.openclaw/openclaw.json with DO Inference provider.
- **Channel isolation:** Telegram channel disabled in openclaw config to prevent token conflict with hermes-gateway.
- **Status:** Correct architecture. openclaw acts as multi-channel relay (non-Telegram) only.
- **Unverifiable claim:** Whether openclaw actually bridges non-Telegram channels INTO hermes-agent via ACP subagent protocol. No wiring code for this exists in the repo.

### camofox-browser (jo-inc/camofox-browser)
- **Integration method:** docker-compose.yml runs node:20-slim container that clones camofox-browser and runs `npm start`.
- **SOUL.md describes:** camofox_browse, camofox_act, camofox_extract, camofox_screenshot, camofox_auth, camofox_youtube.
- **Actual tool registration:** None in gateway/run.py (which is now just an exec wrapper). These tools would need to be registered in hermes-agent's tool system.
- **Status:** Container deployment correct. Whether hermes-agent natively supports camofox tools is unverifiable without testing against a running instance.

---

## 5. What Changed From Prior Version

| Problem | Prior State | Current State |
|---------|-------------|---------------|
| gateway/run.py reimplements hermes-agent | 1,240-line custom gateway | 93-line exec pass-through |
| scripts/jcode is fake wrapper | 126-line bash script calling DO Inference API | 33-line binary locator |
| openclaude server is single-shot API call | server.py calls openai API directly | server.py spawns real openclaude binary |
| orchestrator.py has custom HTTP code | 538-line urllib implementation | 73-line pass-through to hermes-agent CLI |
| task_engine.py has custom Redis queue | 595-line Redis sorted-set implementation | 96-line pass-through to hermes-agent |
| No Redis in docker-compose.yml | Only hermes + camofox | Redis Stack added as first-class service |
| All models route to one provider | Single DO Inference base_url | config.yaml has 4-tier failover: DO->DO fallback->Anthropic->OpenRouter |
| openclaw fights for Telegram token | Both poll same token | TELEGRAM_BOT_TOKEN explicitly unset before openclaw starts |
| SOUL.md describes capabilities not in code | Camofox toolkit, search tiers | Expanded SOUL.md (375 lines) with full tool documentation |

---

## 6. Verified Working vs Requires External Binary

### Works Without External Binaries
- bot/telegram_bot.py (push-commit, bounded-run, ingest-media)
- send_file.py
- scripts/watchdog.py
- scripts/init_and_start.sh (config deployment)
- rhodawk_core/memory.py (SQLite path, degraded mode)
- skills/openclaude_grpc/agentic_client.py (uses DO Inference API directly)

### Requires hermes-agent Binary
- gateway/run.py (primary path)
- supervisord.conf hermes-gateway process
- rhodawk_core/orchestrator.py
- rhodawk_core/task_engine.py

### Requires openclaude Binary
- skills/openclaude_grpc/server.py
- skills/openclaude_grpc/client.py (fallback path)

### Requires jcode Binary
- scripts/jcode
- supervisord.conf jcode-server process
- skills/jcode_swarm/* (all coordinators)

### Requires openclaw Binary
- supervisord.conf openclaw-gateway process
- gateway/run.py (secondary path)

### Requires Redis Stack
- rhodawk_core/memory.py (vector search)
- config.yaml task_queue, event_bus, cron job store
- gateway.yaml task_queue, memory.semantic

---

## 7. Critical Observations

### 7.1 The refactor is architecturally correct
The system now properly delegates to external tools instead of reimplementing them. gateway/run.py is 93 lines instead of 1,240 because it exec's hermes-agent.

### 7.2 rhodawk_core is still partially dead code
orchestrator.py (73 lines) and task_engine.py (96 lines) are thin wrappers around hermes-agent CLI. memory.py (733 lines), tools.py (731 lines), and proactive.py remain substantial implementations that are not called by gateway/run.py (which is now just an exec).

However: hermes-agent itself may load and use these modules if configured to do so via config.yaml's `skills_dir` pointing to /data/.hermes/skills/. This is an architectural bet that hermes-agent's skill system will discover and use rhodawk_core modules.

### 7.3 Fallback chain has a gap
If hermes-agent is not installed AND openclaw is not installed, gateway/run.py exits with error. The prior 1,240-line custom gateway (which actually worked standalone) has been deleted. There is no longer a standalone fallback that functions without any external binary.

### 7.4 Config surface exceeds what hermes-agent likely supports
config.yaml (250 lines) includes keys like `sandbox`, `proactive`, `event_bus`, `task_queue` with detailed sub-configurations. Whether hermes-agent's actual config parser recognizes these keys is unknown. hermes-agent's documented config schema may be narrower.

### 7.5 SOUL.md references tools that require runtime wiring
SOUL.md describes `camofox_browse`, `camofox_act`, `camofox_extract`, `camofox_screenshot`, `camofox_auth`, `camofox_youtube`, `generate_image`, `find_skill`. These must be registered as hermes-agent tools or MCP servers to be callable by the LLM. No MCP server definition for camofox exists in mcp_shared.json.

### 7.6 Multi-provider failover now includes Anthropic and OpenRouter
config.yaml defines a 4-tier failover chain with different base_urls:
- DO Inference (primary)
- DO Inference lighter model
- Anthropic claude-3-5-haiku (https://api.anthropic.com/v1)
- OpenRouter qwen-2.5-72b (https://openrouter.ai/api/v1)

This requires ANTHROPIC_API_KEY and OPENROUTER_API_KEY env vars. .env.example should be checked for these.

### 7.7 Docker Compose now includes Redis
Redis Stack is a first-class service in docker-compose.yml with health checks, persistence, and REDIS_URL auto-injected into the hermes container.

---

## 8. Dependency Matrix

| Dependency | Required By | Install Method | Fallback If Missing |
|-----------|-------------|----------------|---------------------|
| hermes-agent | gateway, supervisord, orchestrator | pip install | FATAL (system non-functional) |
| openclaude | server.py, client.py | npm install -g | gRPC server returns error |
| jcode | scripts/jcode, supervisord | cargo install | sleep infinity |
| openclaw | supervisord, gateway fallback | npm install -g | sleep infinity |
| Redis Stack | memory, task_queue, event_bus, cron | docker-compose service | Flat-file degradation (config.yaml graceful_degradation) |
| python-telegram-bot | bot/telegram_bot.py | pip | Telegram features unavailable |
| openai (Python) | agentic_client.py | pip | agentic loop unavailable |
| grpcio | server.py, client.py | pip | gRPC path unavailable |
| Node.js 20+ | MCP servers, camofox | apt/nodesource | MCP tools unavailable |
| Bun | openclaude source | npm install -g bun | Python server fallback |

---

## 9. Network Access Requirements

| Endpoint | Purpose | Required By |
|----------|---------|-------------|
| https://inference.do-ai.run/v1 | Primary LLM provider | All agent work |
| https://api.anthropic.com/v1 | Fallback LLM provider | config.yaml tier 3 |
| https://openrouter.ai/api/v1 | Last-resort LLM provider | config.yaml tier 4 |
| https://api.telegram.org | Telegram Bot API | hermes-agent, watchdog |
| https://api.github.com | GitHub operations | push-commit, MCP |
| https://api.search.brave.com | Brave Search | SOUL.md tier 2 |
| https://api.exa.ai | Exa semantic search | SOUL.md tier 3 |
| camofox:9377 (internal) | Headless browser | SOUL.md tier 4, stealth-browse |
| redis:6379 (internal) | Memory, queue, events | docker-compose.yml |

---

## 10. Boot Sequence

1. `docker compose up -d` starts redis, hermes, camofox containers
2. Redis health check passes (redis-cli ping)
3. hermes container starts, runs `/app/scripts/init_and_start.sh`
4. init_and_start.sh validates secrets (TELEGRAM_BOT_TOKEN, DO_INFERENCE_API_KEY, GITHUB_PAT)
5. Exports all env vars, expands template variables in config.yaml and gateway.yaml
6. Writes /data/.hermes/.env, copies SOUL.md, skills, cron configs
7. Configures jcode (~/.jcode/config.toml), openclaw (~/.openclaw/openclaw.json)
8. Deploys MCP config to hermes-agent, jcode, openclaude
9. Pre-warms jcode sessions, initializes skill engine, operator model schema
10. exec supervisord starts 5 processes: hermes-gateway, openclaude-grpc, jcode-server, openclaw-gateway, watchdog
11. hermes-gateway process tries `hermes-agent --config /data/.hermes/config.yaml`
12. If hermes-agent binary exists: full agent loop starts (Telegram polling, tools, memory, cron)
13. If hermes-agent missing: falls to gateway/run.py which exec's openclaw or exits

---

## 11. Tool Source Repo Accessibility

All 5 source repos listed in the task were inaccessible from this sandbox (network mode: INTEGRATIONS_ONLY). Analysis is based solely on:
- Code evidence within this repository
- Comments referencing upstream capabilities
- CLI flags and invocation patterns observed in scripts
- Configuration files targeting these tools

No claims about upstream tool capabilities are independently verified against their source code.
