---
title: Hermes Code Stabilizer
emoji: 🔧
colorFrom: indigo
colorTo: purple
sdk: docker
pinned: false
---

# Rhodawk AI — Hermes Code Stabilizer

A multi-agent, autonomous DevOps pipeline controlled via Telegram.  
Send a GitHub repo URL and a bug description — Hermes clones, fixes, tests, commits, and reports back.

---

## Deploy to VPS (one command)

On any fresh Ubuntu 22.04 VPS:

```bash
curl -fsSL https://huggingface.co/spaces/Architect8999/Hermes/raw/main/install.sh | bash
```

That single command: updates the system, allocates swap if RAM < 6 GB, installs Docker, configures UFW firewall, clones the repo, collects secrets interactively, builds from `Dockerfile.vps`, starts the container, installs a systemd unit for auto-start on reboot, and configures log rotation.

See [`VPS_DEPLOYMENT.md`](VPS_DEPLOYMENT.md) for the full guide.

**Unattended / CI mode** (set env vars before running):

```bash
TELEGRAM_BOT_TOKEN=xxx \
DO_INFERENCE_API_KEY=xxx \
GITHUB_PAT=xxx \
curl -fsSL https://huggingface.co/spaces/Architect8999/Hermes/raw/main/install.sh | bash
```

**Or manually with docker compose:**

```bash
git clone https://huggingface.co/spaces/Architect8999/Hermes hermes
cd hermes
cp .env.example .env && nano .env      # fill in 4 secrets
docker compose up -d --build
```

---

## Required Secrets

| Variable | Where to get it | Required |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | @BotFather on Telegram → `/newbot` | Yes |
| `NVIDIA_NIM_API_KEY` | https://build.nvidia.com/ | Yes |
| `DO_INFERENCE_API_KEY` | https://cloud.digitalocean.com/gen-ai | Yes |
| `GITHUB_PAT` | https://github.com/settings/tokens (scopes: `repo` + `workflow`) | Yes |
| `HF_TOKEN` | https://huggingface.co/settings/tokens (role: write) | Optional |

For HuggingFace Spaces, set these under **Settings → Variables and secrets**.  
For VPS, copy `.env.example` → `.env` and fill in each value.

---

## Architecture

```
Telegram User
     │
     ▼
┌─────────────────────────────────────────────────────┐
│  Hermes Gateway  (hermes_bot.py)                    │
│  • python-telegram-bot  — Telegram interface        │
│  • SQLite via aiosqlite — session + audit memory    │
│  • supervisord          — process management        │
└────────────────────┬────────────────────────────────┘
                     │
          ┌──────────┼──────────────┐
          ▼          ▼              ▼
   ┌────────────┐ ┌───────────┐ ┌────────┐
   │ OpenClaude │ │  OpenClaw │ │  JCode │
   │ (Node.js)  │ │ (Node.js) │ │ (bin)  │
   │ Precision  │ │ Multi-plat│ │ Swarm  │
   │ coder      │ │ delivery  │ │ scaffold│
   └────────────┘ └───────────┘ └────────┘
          │
   DigitalOcean Inference (primary)
   deepseek-ai/DeepSeek-V4-Pro
          │
   NVIDIA NIM (fallback)
   deepseek-ai/deepseek-v4-pro
```

---

## Pipeline Flow

1. User sends a GitHub or HuggingFace repo URL + bug description via Telegram
2. Hermes clones the repo into `/tmp/repos/<chat_id>/`
3. OpenClaude searches with ripgrep, generates a fix (DO Inference primary, NIM fallback)
4. Tests run in a bounded 3-strike loop — openclaude self-heals on each failure
5. Strike 3 exhausted → escalate to JCode multi-agent swarm
6. On success: commit pushed via 3-layer resilient push chain, hash reported in Telegram
7. On full failure: inline `[Retry]` / `[Abort]` buttons in Telegram

### Push chain layers

| Layer | GitHub | HuggingFace |
|---|---|---|
| 1 | `git push x-token-auth:TOKEN@github.com/…` | `huggingface_hub.HfApi.create_commit()` |
| 2 | GitHub REST API — blobs/trees/commits (Python) | `git push user:TOKEN@huggingface.co/…` |
| 3 | Auto-generated Node.js GitHub API script | HuggingFace HTTP API multipart POST |

---

## Agent Roster

| Agent | Role | LLM |
|---|---|---|
| **Hermes** (`hermes_bot.py`) | Telegram gateway, orchestration, memory | NVIDIA NIM `deepseek-ai/deepseek-v4-pro` |
| **OpenClaude** (`@gitlawb/openclaude`) | Precision coder — surgical edits, bug fixes | DO Inference `deepseek-ai/DeepSeek-V4-Pro` → NIM fallback |
| **OpenClaw** (`openclaw@latest`) | Multi-platform delivery (non-critical) | DO Inference → NIM fallback |
| **JCode** (`1jehuang/jcode`) | Parallel scaffolding swarm (strike-3 escalation) | DO Inference → NIM fallback |

---

## Deployment Files

| File | Purpose |
|---|---|
| [`Dockerfile`](Dockerfile) | HuggingFace Spaces container (original) |
| [`Dockerfile.vps`](Dockerfile.vps) | VPS-optimized container with `VOLUME` + `HEALTHCHECK` |
| [`docker-compose.yml`](docker-compose.yml) | Compose config — named volume, log rotation, auto-restart |
| [`deploy.sh`](deploy.sh) | One-command installer — `curl \| bash` |
| [`VPS_DEPLOYMENT.md`](VPS_DEPLOYMENT.md) | Full VPS deployment guide |
| [`CHANGELOG.md`](CHANGELOG.md) | Version history |

---

## Quick Reference (after deploy)

```bash
docker compose logs -f hermes                          # stream live logs
docker compose restart hermes                          # restart the bot
docker compose up -d --build                           # rebuild after git pull
docker exec hermes supervisorctl \
  -c /etc/supervisor/conf.d/rhodawk.conf status        # check agent processes
docker cp hermes:/data/memory.db ./backup.db           # back up the database
```

---

*Rhodawk AI — Autonomous Architect v2*
