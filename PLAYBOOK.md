# Rhodawk AI — Hermes Code Stabilizer PLAYBOOK

> Designed from source. Every detail traced to an actual file in this repo.

---

## Table of Contents

1. [What This System Is](#1-what-this-system-is)
2. [Repository Layout](#2-repository-layout)
3. [Required Secrets](#3-required-secrets)
4. [Container Build](#4-container-build)
5. [Boot Sequence](#5-boot-sequence)
6. [Process Management](#6-process-management)
7. [Agent Roster](#7-agent-roster)
8. [LLM Routing](#8-llm-routing)
9. [Hermes Agent Configuration](#9-hermes-agent-configuration)
10. [Memory System](#10-memory-system)
11. [DevOps Pipeline — Step by Step](#11-devops-pipeline--step-by-step)
12. [Error Handling](#12-error-handling)
13. [Runtime Directory Layout](#13-runtime-directory-layout)
14. [Environment Variables Written at Boot](#14-environment-variables-written-at-boot)

---

## 1. What This System Is

Rhodawk AI Code Stabilizer is a fully autonomous DevOps bot that:

1. Accepts a GitHub repo URL and a bug description via a Telegram message.
2. Clones the repo, searches the codebase, and generates a fix using an AI coding agent.
3. Runs the repo's own test suite in a bounded 3-attempt loop.
4. Escalates to a multi-agent swarm if all 3 attempts fail.
5. Pushes a commit to GitHub and reports the commit hash back to the user via Telegram.

**No human approval is required at any step.** `HERMES_YOLO_MODE=1` is set globally.

---

## 2. Repository Layout

```
/
├── .env.example                   # Secret names and descriptions (no values)
├── .gitattributes                 # Git LFS rules for binary/model files
├── Dockerfile                     # Single-stage ubuntu:22.04 container
├── README.md                      # HuggingFace Space card + architecture diagram
├── supervisord.conf               # Process manager — hermes-gateway + openclaw-gateway
│
├── bot/
│   └── memory.py                  # SQLite async memory (aiosqlite) — sessions, messages, executions
│
├── hermes_config/
│   ├── config.yaml                # hermes-agent CLI config — model, terminal, display
│   └── SOUL.md                    # Agent persona, behaviour rules, LLM routing table
│
├── scripts/
│   ├── init_and_start.sh          # Entry point — validates secrets, writes configs, starts supervisord
│   └── start_openclaw.sh          # OpenClaw gateway launcher (graceful no-op if binary missing)
│
└── skills/
    └── devops-pipeline/
        └── skill.md               # Hermes skill — full 7-step pipeline definition
```

---

## 3. Required Secrets

Set in HuggingFace Space settings at:
`https://huggingface.co/spaces/Architect8999/Hermes/settings`

| Secret | Source | Used By |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | @BotFather on Telegram | hermes-agent (Telegram gateway) |
| `NVIDIA_NIM_API_KEY` | https://build.nvidia.com/ | hermes-agent (orchestration) + NIM fallback for openclaude/jcode |
| `DO_INFERENCE_API_KEY` | https://cloud.digitalocean.com/gen-ai | openclaude (coding), jcode (swarm), openclaw (gateway) |
| `GITHUB_PAT` | GitHub → Settings → Tokens (repo + workflow scopes) | git push of fix commits |

The init script validates all four at container startup and exits with `[FATAL]` if any are missing.

---

## 4. Container Build

**Base image:** `ubuntu:22.04`

**System packages installed (apt):**
- `python3.11`, `python3.11-dev`, `python3-pip`
- `curl`, `wget`, `git`
- `supervisor`
- `build-essential`, `ca-certificates`

**Additional system tools:**
- `ripgrep` `14.1.0` — installed from `BurntSushi/ripgrep` `.deb` package (required by openclaude for codebase search)
- `Node.js 24` — installed via `nodesource/setup_24.x`

**Python 3.11 aliases set:**
```
python3 → python3.11
python  → python3.11
```

**Agent installations in build order:**

| Step | Command | Agent |
|---|---|---|
| 1 | `pip3 install "hermes-agent[messaging,mcp,pty]"` | hermes-agent |
| 2 | `npm install -g @gitlawb/openclaude` | openclaude |
| 3 | `npm install -g openclaw@latest` (non-fatal if fails) | openclaw |
| 4 | `curl` tarball from `github.com/1jehuang/jcode/releases/download/v0.12.0/jcode-linux-x86_64.tar.gz` → `/usr/local/bin/jcode` | jcode |

**Git global config set during build:**
```
user.email = hermes@rhodawk.ai
user.name  = Hermes Bot
safe.directory = *
```

**Directories created during build:**
- `/tmp/repos` — cloned repositories go here at runtime
- `/var/log`
- `/root/.hermes/skills`
- `/root/.openclaw`

**Exposed port:** `7860`

**Container entrypoint:** `/app/scripts/init_and_start.sh`

---

## 5. Boot Sequence

`CMD ["/app/scripts/init_and_start.sh"]` runs on container start. The script uses `set -e` — any failure exits immediately.

```
init_and_start.sh
│
├── 1. Validate 4 required secrets (exit [FATAL] if any missing)
│
├── 2. Set shell variables for LLM routing:
│      DO_BASE_URL, DO_PRIMARY_MODEL, DO_FALLBACK_MODEL_1, DO_FALLBACK_MODEL_2
│      NIM_BASE_URL, NIM_PRIMARY_MODEL, NIM_FALLBACK_MODEL
│
├── 3. Configure hermes-agent:
│      mkdir -p /root/.hermes/skills/devops-pipeline
│      Write /root/.hermes/.env            (all credentials + model vars)
│      Copy  /app/hermes_config/config.yaml → /root/.hermes/config.yaml
│      Copy  /app/hermes_config/SOUL.md    → /root/.hermes/SOUL.md
│      Copy  /app/skills/devops-pipeline/skill.md → /root/.hermes/skills/devops-pipeline/skill.md
│
├── 4. Configure openclaw:
│      mkdir -p /root/.openclaw
│      Write /root/.openclaw/config.yaml   (DO primary, NIM fallback)
│
├── 5. Tool availability check (prints versions of all 6 tools to stdout)
│
└── 6. exec supervisord -c /etc/supervisor/conf.d/rhodawk.conf
```

---

## 6. Process Management

`supervisord.conf` runs two long-lived processes. Both log to `/dev/stdout` and `/dev/stderr`.

**Global supervisord settings:**
- `nodaemon=true` (foreground, no daemonizing)
- `user=root`
- `logfile=/dev/stdout` (unbuffered)
- Unix socket: `/var/run/supervisor.sock` (chmod 0700)

**[program:hermes-gateway]**

| Setting | Value |
|---|---|
| `command` | `hermes gateway run` |
| `directory` | `/root` |
| `autostart` | true |
| `autorestart` | true |
| `startretries` | 5 |
| `startsecs` | 10 |
| `priority` | 10 (starts first) |
| `environment` | `HERMES_HOME="/root/.hermes"`, `HERMES_YOLO_MODE="1"`, `HERMES_ACCEPT_HOOKS="1"`, `PYTHONUNBUFFERED="1"` |

**[program:openclaw-gateway]**

| Setting | Value |
|---|---|
| `command` | `/app/scripts/start_openclaw.sh` |
| `directory` | `/root` |
| `autostart` | true |
| `autorestart` | true |
| `startretries` | 3 |
| `startsecs` | 10 |
| `priority` | 20 (starts after hermes-gateway) |

`start_openclaw.sh` checks whether the `openclaw` binary is in PATH. If not found, it `sleep infinity` — the process stays up without crashing supervisord. If found, it runs `exec openclaw gateway`.

---

## 7. Agent Roster

Four agents, all installed into the same container:

| # | Name | Source | Install | Binary/Command | Role |
|---|---|---|---|---|---|
| 1 | **Hermes** | `NousResearch/hermes-agent` | `pip install "hermes-agent[messaging,mcp,pty]"` | `hermes gateway run` | Orchestrator, Telegram gateway, memory |
| 2 | **OpenClaude** | `Gitlawb/openclaude` | `npm install -g @gitlawb/openclaude` | `openclaude --print "<prompt>"` | Coding agent — reads files, writes patches |
| 3 | **OpenClaw** | `openclaw/openclaw` | `npm install -g openclaw@latest` | `openclaw gateway` | Multi-platform delivery gateway (non-critical) |
| 4 | **JCode** | `1jehuang/jcode` | Prebuilt binary v0.12.0 | `jcode` | Multi-agent swarm for complex multi-file bugs |

OpenClaw is explicitly marked non-critical in both the Dockerfile (`|| echo "[openclaw] install skipped"`) and `start_openclaw.sh` (`sleep infinity` on missing binary). Telegram delivery works without it via hermes-agent natively.

---

## 8. LLM Routing

Two API providers. Each agent has a designated primary and fallback.

| Agent | Primary Provider | Primary Model ID | Fallback Provider | Fallback Model ID |
|---|---|---|---|---|
| **Hermes** | NVIDIA NIM | `deepseek-ai/deepseek-r1` | NIM waterfall (see below) | — |
| **OpenClaude** | DigitalOcean Inference | `deepseek-ai/DeepSeek-V4-Pro` | NVIDIA NIM | `deepseek-ai/deepseek-v4-pro` |
| **JCode** | DigitalOcean Inference | `deepseek-ai/DeepSeek-V4-Pro` | NVIDIA NIM | `deepseek-ai/deepseek-v4-pro` |
| **OpenClaw** | DigitalOcean Inference | `deepseek-ai/DeepSeek-V4-Pro` | NVIDIA NIM | `deepseek-ai/deepseek-v4-pro` |

**Important — model ID casing:**
- DigitalOcean Inference API requires **title-case**: `deepseek-ai/DeepSeek-V4-Pro`
- NVIDIA NIM API requires **lowercase**: `deepseek-ai/deepseek-v4-pro`
Using the wrong casing results in a model-not-found error even though the model exists.

**DO Inference base URL:** `https://inference.do-ai.run/v1`

**DO model waterfall** (if primary fails, try in order):
1. `deepseek-ai/DeepSeek-V4-Pro`
2. `deepseek-ai/deepseek-v4-flash`
3. `deepseek-ai/DeepSeek-V3.2`

**NVIDIA NIM base URL:** `https://integrate.api.nvidia.com/v1`

**Hermes NIM waterfall** (all free tier, all >100k context):
1. `deepseek-ai/deepseek-r1`
2. `deepseek-ai/deepseek-v3`
3. `deepseek-ai/deepseek-r1-distill-llama-70b`
4. `qwen/qwen2.5-coder-32b-instruct`
5. `meta/llama-3.1-405b-instruct`
6. `nvidia/llama-3.1-nemotron-70b-instruct`

**OpenClaude/JCode NIM fallback waterfall:**
1. `deepseek-ai/deepseek-v4-pro`
2. `deepseek-ai/deepseek-v4-flash`
3. `deepseek-ai/deepseek-r1`
4. `qwen/qwen2.5-coder-32b-instruct`
5. `meta/llama-3.1-405b-instruct`

**OpenClaude compatibility flag** (required for using any OpenAI-compatible API with openclaude):
```
CLAUDE_CODE_USE_OPENAI=1
```

---

## 9. Hermes Agent Configuration

Two config files are written during the boot sequence. Source files live in `/app/hermes_config/`, runtime copies in `/root/.hermes/`.

### `/root/.hermes/config.yaml`

```yaml
model:
  provider: "nvidia"
  default: "deepseek-ai/deepseek-r1"

terminal:
  backend: local
  approval_mode: yolo      # no confirmation prompts ever

platform_toolsets:
  telegram: [hermes-telegram]

display:
  compact: true
  tool_progress: all
  interim_assistant_messages: true
  streaming: true
  show_reasoning: false
  background_process_notifications: all
```

The `hermes-telegram` toolset provides: `terminal`, `file`, `web`, `vision`, `image_gen`, `tts`, `browser`, `skills`, `todo`, `cronjob`, `messaging`.

### `/root/.hermes/SOUL.md`

Defines Hermes's persona and hard behavioural rules:
- Immediately execute the DevOps Pipeline skill on any message containing a GitHub URL + bug description. No confirmation.
- Stream one short progress line per step.
- `HERMES_YOLO_MODE=1` — never ask for tool approval.
- Never discuss "ProjectZeo". Never generate compliance documents.
- Responses must be short — designed for Telegram message format.

---

## 10. Memory System

**File:** `bot/memory.py`
**Storage:** SQLite at `/tmp/memory.db` (ephemeral — lost on container restart)
**Library:** `aiosqlite` (async)
**Logging:** `loguru`

### Tables

**`sessions`** — one row per Telegram chat_id

| Column | Type | Default | Description |
|---|---|---|---|
| `chat_id` | INTEGER PK | — | Telegram chat ID |
| `repo_url` | TEXT | — | Current repo being worked on |
| `bug_description` | TEXT | — | Bug description from user |
| `state` | TEXT | `'idle'` | Pipeline state |
| `strike_count` | INTEGER | `0` | Failed test attempts (max 3) |
| `branch` | TEXT | — | Target branch |
| `created_at` | REAL | — | Unix timestamp |
| `updated_at` | REAL | — | Unix timestamp |

**`messages`** — conversation history per chat_id

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK autoincrement | — |
| `chat_id` | INTEGER | Telegram chat ID |
| `role` | TEXT | `"user"` or `"assistant"` |
| `content` | TEXT | Message text |
| `timestamp` | REAL | Unix timestamp |

**`executions`** — audit log of every pipeline run

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK autoincrement | — |
| `chat_id` | INTEGER | — |
| `repo_url` | TEXT | — |
| `bug_description` | TEXT | — |
| `status` | TEXT | — |
| `result` | TEXT | — |
| `commit_hash` | TEXT | — |
| `started_at` | REAL | Unix timestamp |
| `ended_at` | REAL | Unix timestamp |

### Functions

| Function | Signature | Description |
|---|---|---|
| `init_db()` | `async` | Creates all 3 tables if not exist |
| `upsert_session()` | `async (chat_id, **kwargs)` | Insert or update a session row |
| `get_session()` | `async (chat_id) → dict\|None` | Fetch a session by chat_id |
| `increment_strikes()` | `async (chat_id) → int` | Increment strike_count, return new value |
| `reset_strikes()` | `async (chat_id)` | Set strike_count to 0 |
| `add_message()` | `async (chat_id, role, content)` | Append to messages table |
| `get_recent_messages()` | `async (chat_id, limit=10) → list[dict]` | Last N messages in chronological order |
| `log_execution()` | `async (chat_id, repo_url, bug_description, status, result, commit_hash)` | Write to executions table |
| `clear_session()` | `async (chat_id)` | Delete session + messages rows for chat_id |

---

## 11. DevOps Pipeline — Step by Step

**Source file:** `skills/devops-pipeline/skill.md`
**Trigger:** Any Telegram message containing a GitHub URL (`https://github.com/...`) and a bug description.
**Execution:** Immediate. No confirmation requested.

---

### Step 1 — Parse and Clone

Hermes extracts the GitHub URL and bug description from the message.

```bash
REPO_NAME=$(basename "$REPO_URL" .git)
CLONE_PATH="/tmp/repos/$(date +%s)/$REPO_NAME"
mkdir -p "$(dirname "$CLONE_PATH")"
git clone --depth 1 "$REPO_URL" "$CLONE_PATH"
```

Telegram update sent: `Cloned <repo> — dispatching OpenClaude (DO Inference)...`

---

### Step 2 — Search Relevant Files

```bash
cd "$CLONE_PATH"
rg -l . --type py --type js --type ts 2>/dev/null | head -30
```

Scopes the fix to relevant source files only. Output used to focus openclaude's context.

---

### Step 3 — Fix with OpenClaude (Attempts 1–3)

Run headless via Hermes's `terminal` tool.

**3a — Primary (DigitalOcean Inference):**

```bash
cd "$CLONE_PATH"
CLAUDE_CODE_USE_OPENAI=1 \
OPENAI_BASE_URL="${DO_INFERENCE_BASE_URL}" \
OPENAI_API_KEY="${DO_INFERENCE_API_KEY}" \
OPENAI_MODEL="deepseek-ai/DeepSeek-V4-Pro" \
openclaude --print "Fix this bug: $BUG_DESC. Read relevant files, apply a minimal correct patch, write fixes directly to files."
```

**3b — Fallback (NVIDIA NIM), if 3a fails:**

```bash
cd "$CLONE_PATH"
CLAUDE_CODE_USE_OPENAI=1 \
OPENAI_BASE_URL="${NIM_BASE_URL}" \
OPENAI_API_KEY="${NIM_API_KEY}" \
OPENAI_MODEL="deepseek-ai/deepseek-v4-pro" \
openclaude --print "Fix this bug: $BUG_DESC. Read relevant files, apply a minimal correct patch, write fixes directly to files."
```

3a fails when: exit code non-zero, connection refused, or rate-limit error in output.

Telegram update: `OpenClaude patch applied via [DO Inference DeepSeek-V4-Pro | NIM deepseek-v4-pro fallback] — running tests (attempt N/3)...`

---

### Step 4 — Run Tests (3-Strike Loop)

Test suite auto-detected in this priority order:

```bash
cd "$CLONE_PATH"
if [ -f pytest.ini ] || find . -name "test_*.py" -maxdepth 4 | grep -q .; then
  python -m pytest --tb=short -q
elif [ -f package.json ] && grep -q '"test"' package.json; then
  npm test
elif [ -f Makefile ] && grep -q "^test" Makefile; then
  make test
else
  echo "NO_TEST_SUITE"
fi
```

**Decision tree:**

| Result | Action |
|---|---|
| Tests pass | Go to Step 6 |
| Fail, strike 1 | Re-run Step 3 with test output appended to prompt |
| Fail, strike 2 | Re-run Step 3 with test output appended to prompt |
| Fail, strike 3 | Go to Step 5 (JCode swarm) |
| `NO_TEST_SUITE` | Skip to Step 6, commit fix directly |

Telegram update on failure: `Tests failed (strike N/3) — retrying...`

---

### Step 5 — JCode Swarm (Strike 3 Fallback Only)

Invoked only after 3 failed OpenClaude attempts. JCode spawns parallel agents across the entire repo simultaneously.

**5a — Primary (DigitalOcean Inference):**

```bash
cd "$CLONE_PATH"
OPENAI_API_KEY="${DO_INFERENCE_API_KEY}" \
OPENAI_BASE_URL="${DO_INFERENCE_BASE_URL}" \
OPENAI_MODEL="deepseek-ai/DeepSeek-V4-Pro" \
jcode "$BUG_DESC

3 prior OpenClaude attempts failed all tests. Last failure:
$TEST_OUTPUT

Use parallel agents across multiple files to find and fix the root cause."
```

**5b — Fallback (NVIDIA NIM), if 5a fails:**

```bash
cd "$CLONE_PATH"
OPENAI_API_KEY="${NIM_API_KEY}" \
OPENAI_BASE_URL="${NIM_BASE_URL}" \
OPENAI_MODEL="deepseek-ai/deepseek-v4-pro" \
jcode "$BUG_DESC

3 prior OpenClaude attempts failed all tests. Last failure:
$TEST_OUTPUT

Use parallel agents across multiple files to find and fix the root cause."
```

Tests re-run after JCode. If still failing (both DO and NIM exhausted): show full test output in Telegram, ask user to retry or abort.

Telegram update: `Escalating to JCode multi-agent swarm via [DO Inference | NIM fallback] (final attempt)...`

---

### Step 6 — Commit and Push

```bash
cd "$CLONE_PATH"
SHORT_DESC=$(echo "$BUG_DESC" | head -c 60 | tr '\n' ' ')
git add -A
git commit -m "fix: $SHORT_DESC"
AUTH_URL="https://x-token-auth:${GITHUB_PAT}@$(echo "$REPO_URL" | sed 's|https://||')"
git push "$AUTH_URL" HEAD:main
COMMIT_HASH=$(git rev-parse HEAD)
echo "PUSHED:$COMMIT_HASH"
```

Authentication: `x-token-auth` scheme with `GITHUB_PAT`. Target branch is always `main`.

---

### Step 7 — Report to User

```
Fix pushed!
Repo: <repo_url>
Commit: <hash>
Branch: main
Attempts used: N/3
LLM used: DO Inference (DeepSeek-V4-Pro) [or: NIM fallback]
```

---

## 12. Error Handling

| Situation | What Happens |
|---|---|
| Missing secret at startup | `[FATAL]` log, container exits 1 |
| Clone fails | Pipeline stops. User told to verify repo URL. |
| DO Inference unreachable / non-2xx | Immediately retry same call with NVIDIA NIM |
| DO Inference rate-limited | Immediately retry that single call with NVIDIA NIM |
| openclaw binary missing | `start_openclaw.sh` runs `sleep infinity` — process stays up, Telegram delivery still works via hermes-agent |
| No test suite found | `NO_TEST_SUITE` output → skip tests, commit fix directly, notify user |
| Strike 3 — OpenClaude exhausted | Escalate to JCode swarm |
| JCode fails on both DO and NIM | Show last test output in Telegram. Ask user to retry or abort. |
| git push fails | Show error. Tell user to verify `GITHUB_PAT` scope. |

---

## 13. Runtime Directory Layout

```
/
├── app/                           # Container app root (COPY . /app/)
│   ├── hermes_config/
│   ├── scripts/
│   └── skills/
│
├── tmp/
│   ├── memory.db                  # SQLite — sessions, messages, executions
│   └── repos/
│       └── <epoch_ts>/
│           └── <repo_name>/       # git clone --depth 1 target per job
│
├── var/
│   └── run/
│       └── supervisor.sock        # supervisord Unix socket
│
└── root/
    ├── .hermes/
    │   ├── .env                   # All credentials (written by init_and_start.sh)
    │   ├── config.yaml            # Copied from /app/hermes_config/config.yaml
    │   ├── SOUL.md                # Copied from /app/hermes_config/SOUL.md
    │   └── skills/
    │       └── devops-pipeline/
    │           └── skill.md       # Copied from /app/skills/devops-pipeline/skill.md
    └── .openclaw/
        └── config.yaml            # Written by init_and_start.sh (DO primary, NIM fallback)
```

---

## 14. Environment Variables Written at Boot

`init_and_start.sh` writes `/root/.hermes/.env` at container start. These are the exact keys written:

| Variable | Source / Value |
|---|---|
| `NVIDIA_API_KEY` | `$NVIDIA_NIM_API_KEY` — hermes config.yaml "nvidia" provider |
| `DO_INFERENCE_API_KEY` | `$DO_INFERENCE_API_KEY` HF secret |
| `DO_INFERENCE_BASE_URL` | `https://inference.do-ai.run/v1` |
| `DO_PRIMARY_MODEL` | `deepseek-ai/DeepSeek-V4-Pro` |
| `DO_FALLBACK_MODEL_1` | `deepseek-ai/deepseek-v4-flash` |
| `DO_FALLBACK_MODEL_2` | `deepseek-ai/DeepSeek-V3.2` |
| `NIM_API_KEY` | `$NVIDIA_NIM_API_KEY` HF secret |
| `NIM_BASE_URL` | `https://integrate.api.nvidia.com/v1` |
| `NIM_PRIMARY_MODEL` | `deepseek-ai/deepseek-v4-pro` |
| `NIM_FALLBACK_MODEL` | `deepseek-ai/deepseek-v4-flash` |
| `OPENAI_API_KEY` | `$DO_INFERENCE_API_KEY` (shell default for subprocesses) |
| `OPENAI_BASE_URL` | `https://inference.do-ai.run/v1` (shell default) |
| `OPENAI_MODEL` | `deepseek-ai/DeepSeek-V4-Pro` (shell default) |
| `TELEGRAM_BOT_TOKEN` | `$TELEGRAM_BOT_TOKEN` HF secret |
| `GITHUB_PAT` | `$GITHUB_PAT` HF secret |
| `CLAUDE_CODE_USE_OPENAI` | `1` — enables OpenAI-compat mode in openclaude |

`/root/.openclaw/config.yaml` is also written with DO as primary and NIM as fallback, using the same variables expanded at boot time.

---

## 15. Peak Architecture — v5.0 Upgrades

### Memory Unification
`bot/memory.py` (SQLite) has been removed. All state tracking now uses Hermes native memory:
- `/data/.hermes/memories/MEMORY.md` — operational state, execution log, provider health
- `/data/.hermes/memories/USER.md` — user modeling for Rhodawk founder

### OpenClaude gRPC Seam
OpenClaude now runs as a persistent gRPC server at `localhost:50051` via supervisord.
`_self_heal()` calls `python3 /app/skills/openclaude_grpc/client.py` instead of `openclaude --print`.
The gRPC client uses bidirectional streaming with auto-approve for all action_required callbacks (YOLO mode).
Falls back to CLI if gRPC server is unavailable.

### jcode Swarm Upgrade
After 3 strikes, `run_bounded` escalates to `skills/jcode_swarm/spawn.py`:
- Extracts per-module failing test paths
- Spawns parallel jcode workers (max 3 simultaneous)
- Each worker clones the relevant module and fixes it independently

### Hermes Skills Auto-Growth
`hermes_config/config.yaml` now sets `skills.auto_extract: true`.
After 10 turns of complex work, Hermes extracts a new skill entry.
After every successful push, SOUL.md instructs Hermes to append learnings to `devops-pipeline/SKILL.md`.

### Cron Scheduler
`/data/.hermes/cron/nightly_sweep.yaml` — 2AM nightly sweep against next 3 repos in target list.
`/data/.hermes/cron/weekly_traction.yaml` — Monday 9AM investor-ready metrics digest.
`/data/target_list.json` — 100-repo target list of active Python OSS projects.

### Shared MCP Layer
`mcp_shared.json` deployed to all three agents at boot:
- `/data/.hermes/mcp.json` (Hermes)
- `/root/.jcode/mcp.json` (jcode)
- `/root/.claude/mcp.json` (openclaude)
MCP servers: `@modelcontextprotocol/server-filesystem` and `@modelcontextprotocol/server-github`.

### openclaude Agent Routing
`openclaude_settings.json` deployed to `/root/.claude/settings.json` at boot.
Per-task-type model routing: Explore → groq-llama | Plan/Code → DO deepseek | Review → NIM kimi-k2.6.

### New File Tree
```
skills/
  openclaude_grpc/
    SKILL.md              # gRPC skill description for Hermes
    client.py             # Bidirectional gRPC client (AgentService.Chat)
  jcode_swarm/
    SKILL.md              # Swarm skill description for Hermes
    spawn.py              # Parallel multi-repo worker launcher
openclaude_grpc/
  openclaude.proto        # Real proto from @gitlawb/openclaude
hermes_config/
  memories/MEMORY.md      # Operational memory template
  memories/USER.md        # User modeling template
  cron/nightly_sweep.yaml # Nightly Rhodawk cron
  cron/weekly_traction.yaml # Weekly traction digest cron
data/
  target_list.json        # 100 target repos for Rhodawk sweep
mcp_shared.json           # Shared MCP config for all 3 agents
openclaude_settings.json  # Per-task model routing for openclaude
```
