# Rhodawk AI -- Hermes88

```
 ____  _               _               _       _    ___
|  _ \| |__   ___   __| | __ ___      _| | __  / \  |_ _|
| |_) | '_ \ / _ \ / _` |/ _` \ \ /\ / / |/ / / _ \  | |
|  _ <| | | | (_) | (_| | (_| |\ V  V /|   < / ___ \ | |
|_| \_\_| |_|\___/ \__,_|\__,_| \_/\_/ |_|\_\/_/   \_\___|

         H E R M E S 8 8  --  P E A K  v 1 . 0
```

**JARVIS-Grade Autonomous Intelligence System**

Hermes88 is a multi-language, event-driven AI assistant that operates as a proactive
digital chief of staff. It monitors events, anticipates needs, executes complex multi-step
tasks autonomously, and communicates results through Telegram -- all while maintaining
persistent semantic memory and a human-readable knowledge vault.

---

## Architecture

```
                         +------------------+
                         |   OPERATOR       |
                         | (Telegram/Voice/ |
                         |  WhatsApp/Slack) |
                         +--------+---------+
                                  |
                    +-------------v--------------+
                    |    HERMES GATEWAY           |
                    |  (Event Router + Dispatcher)|
                    |  Port: Telegram Polling     |
                    +--+------+------+------+----+
                       |      |      |      |
          +------------+  +---+--+ +-+----+ +--------+
          |               |      | |      |          |
+---------v----+ +--------v-+ +--v-v--+ +-v--------+ +--------+
| EVENT BUS    | | TASK     | | MEMORY| | PERCEPT  | | CRON   |
| (Redis PubSub| | QUEUE    | | ENGINE| | ENGINE   | | ENGINE |
| + Webhooks)  | | (Redis + | | (Vec  | | (GitHub  | | (APSch)|
|              | |  SQLite) | | +SQL) | |  Monitor | |        |
+---------+----+ +----+-----+ +---+---+ |  System) | +---+----+
          |           |           |      +-----+----+     |
          |     +-----v-----+    |             |          |
          |     | WORKER    |    |             |          |
          |     | POOL      |    |             |          |
          |     | (asyncio) |    |             |          |
          |     +--+--+--+--+    |             |          |
          |        |  |  |       |             |          |
    +-----v--+ +---v-++-v---+ +-v---+    +----v----+    |
    |OPENCLAUDE| |JCODE||CAMOFOX| |OPENCLAW|    |WEBHOOKS|    |
    |gRPC     | |SWARM||BROWSER| |GATEWAY|    |SERVER  |    |
    |:50051   | |:7865||:9377  | |:18789 |    |:8080   |    |
    |(Agentic | |     ||       | |       |    |        |    |
    | Loop)   | |     ||       | |       |    |        |    |
    +---------+ +-----++-------+ +-------+    +--------+    |
                                                             |
    +--------------------------------------------------------v--+
    |                    SANDBOX POOL                            |
    |  (Per-task Docker containers for untrusted code execution)|
    +-----------------------------------------------------------+
```

---

## Multi-Language Breakdown

| Language | Component | Purpose |
|----------|-----------|---------|
| **Python** | `gateway/`, `skills/`, `bot/` | Core AI orchestration, memory engine, gateway, all skills |
| **TypeScript** | `webhook/` | Webhook receiver server (GitHub, Stripe, system events) |
| **Go** | `sandbox/` | Sandbox manager -- ephemeral container lifecycle management |
| **Rust** | `rhodawk-tools/` | High-performance CLI tools (code search, analysis, statistics) |
| **Shell** | `scripts/`, `deploy.sh`, `install.sh` | Deployment automation, process management, system setup |

---

## Components

| Component | Directory | Description |
|-----------|-----------|-------------|
| **Hermes Gateway** | `gateway/` | Primary Telegram interface, event routing, conversation management |
| **Skills** | `skills/` | 16 specialized skill modules (see below) |
| **Webhook Server** | `webhook/` | TypeScript HTTP server for inbound webhooks (GitHub, Stripe, system) |
| **Sandbox Manager** | `sandbox/` | Go HTTP service managing ephemeral Docker containers for code execution |
| **Rhodawk Tools** | `rhodawk-tools/` | Rust CLI for fast code analysis, file scanning, and repository statistics |
| **Configuration** | `hermes_config/` | SOUL persona, model config, cron schedules, Obsidian templates |
| **OpenClaude gRPC** | `openclaude_grpc/` | Protocol buffer definitions for the precision coding agent |

---

## Open-Source Integrations

Hermes88 integrates **39 open-source projects** across 11 categories:

| Category | Key Projects |
|----------|-------------|
| Memory & Knowledge | mem0, ChromaDB, LlamaIndex, Obsidian |
| Agent Orchestration | LiteLLM, pydantic-ai, AutoGen, CrewAI |
| Tools & Capabilities | MCP Servers, browser-use, Scrapegraph-ai, Jina Reader |
| Task Execution | dramatiq, APScheduler, Prefect |
| Communication | python-telegram-bot, AutoGPT, OpenHands |
| Security & Auditing | Bandit, Semgrep, TruffleHog, Safety, gVisor |
| Performance | ripgrep, Ruff, ast-grep |
| Infrastructure | Redis Stack, Traefik, Docker Compose, containerd |
| Browser Automation | Camofox, Playwright |
| Data & Knowledge Graph | Neo4j, Qdrant |
| Voice & Media | Whisper, whisper.cpp |

See [`INTEGRATIONS.md`](INTEGRATIONS.md) for full documentation of each project:
why it was chosen, how it integrates, and license compatibility.

---

## File Tree

```
hermes88/
|-- README.md                         # This file
|-- PEAK_ARCHITECTURE.md              # Complete system blueprint
|-- INTEGRATIONS.md                   # Open-source integration documentation
|-- PLAYBOOK.md                       # Operational playbook
|-- VPS_DEPLOYMENT.md                 # VPS deployment guide
|-- CHANGELOG.md                      # Version history
|-- LICENSE                           # MIT license
|
|-- docker-compose.peak.yml           # Peak architecture (all 5 services)
|-- docker-compose.yml                # Legacy single-service compose
|-- Dockerfile.peak                   # Main Hermes container
|-- Dockerfile.webhook                # Webhook receiver container
|-- Dockerfile.sandbox                # Ephemeral sandbox base image
|-- Dockerfile.sandbox-manager        # Sandbox manager container
|-- Dockerfile.camofox                # Stealth browser container
|
|-- gateway/                          # Telegram gateway (Python)
|   |-- run.py                        # Main gateway (1241 lines)
|   |-- event_consumer.py            # Redis event consumer
|   |-- memory_injector.py           # Pre-prompt memory injection
|   |-- response_formatter.py        # Channel-specific formatting
|
|-- webhook/                          # Webhook server (TypeScript)
|   |-- src/server.ts                # Express HTTP server
|   |-- src/handlers/               # Route handlers (GitHub, Stripe, system)
|
|-- sandbox/                          # Sandbox manager (Go)
|   |-- cmd/manager/main.go         # HTTP server entry point
|   |-- internal/container/          # Container lifecycle management
|
|-- rhodawk-tools/                    # CLI tools (Rust)
|   |-- src/main.rs                  # CLI entry point
|   |-- src/search.rs               # Parallel file search
|   |-- src/analyzer.rs             # Code analysis
|   |-- src/scanner.rs              # Directory scanning
|   |-- src/stats.rs                # Repository statistics
|
|-- skills/                           # Skill modules (16 skills)
|   |-- devops-pipeline/             # CI/CD and code fixing
|   |-- openclaude_grpc/             # Precision coding agent
|   |-- jcode_swarm/                 # Parallel agent swarm
|   |-- research-deep/              # Deep research and analysis
|   |-- competitive-intel/          # Market intelligence
|   |-- security-audit/             # Security scanning (4 tools)
|   |-- stealth-browse/             # Stealth web browsing
|   |-- openclaw_channel/           # Multi-channel relay
|   |-- voice-transcription/        # Speech-to-text
|   |-- email-imap-smtp/            # Email reading and sending
|   |-- calendar-google/            # Google Calendar integration
|   |-- financial-stripe/           # Stripe monitoring
|   |-- rss-monitor/                # RSS feed monitoring
|   |-- social-media-monitor/       # Social media tracking (future)
|   |-- document-generation/        # Template-based documents
|   |-- deployment-orchestration/   # Multi-target deployment
|   |-- obsidian-sync/              # Obsidian vault synchronization
|
|-- hermes_config/                    # Configuration
|   |-- SOUL.md                      # Agent persona (v10.0 JARVIS-grade)
|   |-- config.yaml                  # Core engine config
|   |-- gateway.yaml                 # Gateway + model config
|   |-- cron/                        # Scheduled job definitions
|   |-- obsidian/                    # Obsidian vault templates
|   |-- memories/                    # Flat-file memory (backward compat)
|
|-- scripts/                          # Automation scripts
|   |-- init_and_start.sh           # Container bootstrap
|   |-- watchdog.py                  # Process health monitor
```

---

## Quick Start

### Prerequisites

- Docker and Docker Compose v2+
- A Telegram bot token (from @BotFather)
- DigitalOcean Inference API key
- GitHub personal access token

### Deploy

```bash
# Clone the repository
git clone https://github.com/nicedayfor/Hermes88.git
cd Hermes88

# Configure secrets
cp .env.example .env
nano .env  # Fill in all required variables

# Deploy all services (peak architecture)
docker compose -f docker-compose.peak.yml up -d --build

# Check status
docker compose -f docker-compose.peak.yml ps

# View logs
docker compose -f docker-compose.peak.yml logs -f hermes
```

### One-Command VPS Install

```bash
curl -fsSL https://raw.githubusercontent.com/nicedayfor/Hermes88/main/install.sh | bash
```

See [`VPS_DEPLOYMENT.md`](VPS_DEPLOYMENT.md) for the complete deployment guide.

---

## Environment Variables

| Variable | Description | Required |
|----------|-------------|:--------:|
| `TELEGRAM_BOT_TOKEN` | Telegram Bot API token from @BotFather | Yes |
| `TELEGRAM_CHAT_ID` | Operator's Telegram chat ID (restricts access) | Yes |
| `DO_INFERENCE_API_KEY` | DigitalOcean GenAI Inference API key | Yes |
| `GITHUB_PAT` | GitHub personal access token (repo + workflow scopes) | Yes |
| `NVIDIA_NIM_API_KEY` | NVIDIA NIM API key (LLM fallback) | Yes |
| `BRAVE_API_KEY` | Brave Search API key (web search) | Optional |
| `HF_TOKEN` | HuggingFace token (Space push) | Optional |
| `CAMOFOX_ACCESS_KEY` | Bearer token for Camofox REST API | Optional |
| `REDIS_URL` | Redis connection URL (auto-configured in compose) | Auto |
| `MEMORY_DB_PATH` | SQLite memory database path | Auto |
| `OBSIDIAN_VAULT_PATH` | Obsidian vault directory | Auto |

---

## Skills

Hermes88 includes 16 specialized skills, each with its own `SKILL.md` definition:

| # | Skill | Description |
|---|-------|-------------|
| 1 | **devops-pipeline** | Clone, fix, test, commit, push -- full autonomous CI/CD |
| 2 | **openclaude_grpc** | Precision coding agent with agentic iteration loop |
| 3 | **jcode_swarm** | Parallel multi-agent swarm for complex tasks |
| 4 | **research-deep** | Multi-source research with synthesis and citations |
| 5 | **competitive-intel** | Market intelligence and competitor analysis |
| 6 | **security-audit** | 4-tool security scan (Bandit, Semgrep, TruffleHog, Safety) |
| 7 | **stealth-browse** | Anti-detection web browsing via Camofox |
| 8 | **openclaw_channel** | Multi-platform content delivery |
| 9 | **voice-transcription** | Speech-to-text via Whisper |
| 10 | **email-imap-smtp** | Email monitoring and sending with VIP alerts |
| 11 | **calendar-google** | Google Calendar read/write |
| 12 | **financial-stripe** | Stripe revenue monitoring and alerts |
| 13 | **rss-monitor** | RSS feed monitoring with content extraction |
| 14 | **social-media-monitor** | Social media tracking and alerts |
| 15 | **document-generation** | Template-based document creation (pitch decks, proposals) |
| 16 | **deployment-orchestration** | Multi-target deployment automation |
| 17 | **obsidian-sync** | Bidirectional Obsidian vault synchronization |

---

## Development

### Adding a New Skill

1. Create a directory under `skills/` with your skill name
2. Add a `SKILL.md` file defining the skill's triggers, inputs, outputs, and behavior
3. Implement the skill logic in Python
4. Register the skill in `hermes_config/config.yaml`
5. Add any cron schedules to `hermes_config/cron/`

### Running Tests

```bash
# Python tests
python3 -m pytest tests/ -q

# Validate Python syntax
python3 -c "import ast; ast.parse(open('rhodawk_core/orchestrator.py').read())"

# Validate TypeScript (if tsc available)
npx tsc --noEmit --project webhook/tsconfig.json

# Validate Go
go build ./...    # from sandbox/ directory

# Validate Rust
cargo check       # from rhodawk-tools/ directory
```

### Project Conventions

- **Python**: Type hints, comprehensive docstrings, explicit error handling
- **TypeScript**: Strict mode, interface-first design, Express middleware pattern
- **Go**: Standard library preferred, explicit error returns, structured logging
- **Rust**: Zero unsafe, proper error handling with thiserror/anyhow

---

## Services (Peak Architecture)

| Service | Port | Technology | Purpose |
|---------|------|------------|---------|
| hermes | -- | Python + supervisord | Core AI engine, gateway, all skills |
| redis | 6379 | Redis Stack 7.4 | Memory vectors, event bus, task queue |
| camofox | 9377 | Modified Firefox | Stealth headless browser |
| webhook-receiver | 9000 | TypeScript + Express | Inbound webhooks (GitHub, Stripe) |
| sandbox-manager | 8090 | Go + net/http | Ephemeral container management |

---

## License

MIT License. See [`LICENSE`](LICENSE) for details.

---

## Credits

Hermes88 is built on 39 open-source projects representing $45M+ in development value.
See [`INTEGRATIONS.md`](INTEGRATIONS.md) for the complete list with rationale and
license compatibility analysis.

---

*Rhodawk AI -- Autonomous Architect, Peak v1.0*
