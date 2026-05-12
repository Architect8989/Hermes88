# Rhodawk AI — Hermes VPS Deployment Guide

Deploy the full Rhodawk AI Code Stabilizer stack on any Linux VPS in a single click using Docker.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Server Setup](#2-server-setup)
3. [Clone the Repository](#3-clone-the-repository)
4. [Configure Environment Variables](#4-configure-environment-variables)
5. [Single-Click Deploy](#5-single-click-deploy)
6. [Verify the Deployment](#6-verify-the-deployment)
7. [docker compose (Recommended)](#7-docker-compose-recommended)
8. [Persistent Storage](#8-persistent-storage)
9. [Auto-Start on Reboot (systemd)](#9-auto-start-on-reboot-systemd)
10. [Monitoring and Logs](#10-monitoring-and-logs)
11. [Updating the Bot](#11-updating-the-bot)
12. [Stopping and Removing](#12-stopping-and-removing)
13. [Troubleshooting](#13-troubleshooting)
14. [Security Hardening](#14-security-hardening)

---

## 1. Prerequisites

### Minimum VPS specs

| Resource | Minimum | Recommended |
|---|---|---|
| CPU | 2 vCPU | 4 vCPU |
| RAM | 4 GB | 8 GB |
| Disk | 20 GB SSD | 40 GB SSD |
| OS | Ubuntu 22.04 LTS | Ubuntu 22.04 LTS |
| Network | 100 Mbps | 1 Gbps |

> RAM note: OpenClaude (Node.js) + Python bot + supervisord together use ~1.5–2 GB at idle. Leave headroom for cloned repos and compilation during bug fixes.

### Required accounts and API keys before you start

| Key | Where to get it | Required? |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Message @BotFather on Telegram → `/newbot` | Yes |
| `NVIDIA_NIM_API_KEY` | https://build.nvidia.com/ → API Keys | Yes |
| `DO_INFERENCE_API_KEY` | https://cloud.digitalocean.com/gen-ai → API Keys | Yes |
| `GITHUB_PAT` | https://github.com/settings/tokens → Classic token → scopes: `repo` + `workflow` | Yes |
| `HF_TOKEN` | https://huggingface.co/settings/tokens → New token → Role: write | Optional |

---

## 2. Server Setup

SSH into your VPS and install Docker:

```bash
# Update system packages
sudo apt-get update && sudo apt-get upgrade -y

# Install Docker (official script — works on Ubuntu 22.04)
curl -fsSL https://get.docker.com | bash

# Add your user to the docker group (avoids sudo on every docker command)
sudo usermod -aG docker $USER

# Apply group change without logging out
newgrp docker

# Verify Docker is running
docker --version
docker compose version
```

---

## 3. Clone the Repository

```bash
# Clone from HuggingFace (public space)
git clone https://huggingface.co/spaces/Architect8999/Hermes hermes
cd hermes
```

If the Space is private, use your HuggingFace token:

```bash
git clone https://YOUR_HF_USERNAME:YOUR_HF_TOKEN@huggingface.co/spaces/Architect8999/Hermes hermes
cd hermes
```

---

## 4. Configure Environment Variables

Copy the example env file and fill in your values:

```bash
cp .env.example .env
nano .env      # or: vim .env / code .env
```

Fill in every value:

```bash
# ── Required ──────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN=<paste from @BotFather>

NVIDIA_NIM_API_KEY=<paste from build.nvidia.com>

DO_INFERENCE_API_KEY=<paste from cloud.digitalocean.com/gen-ai>

GITHUB_PAT=<paste GitHub PAT — needs repo + workflow scopes>

# ── Optional ──────────────────────────────────────────────────────────────────
# Only needed if you want to push commits to HuggingFace repos
HF_TOKEN=<paste from huggingface.co/settings/tokens>
```

Verify the file looks right (without revealing values):

```bash
grep -E "^[A-Z]" .env | cut -d= -f1
# Should print: TELEGRAM_BOT_TOKEN  NVIDIA_NIM_API_KEY  DO_INFERENCE_API_KEY  GITHUB_PAT
```

> **Security:** Never commit `.env` to git. It is already listed in `.gitignore`. Confirm with: `git status .env` — it must show as ignored or untracked, never staged.

---

## 5. Single-Click Deploy

### Option A — One command (bare Docker)

Build and run with a single command:

```bash
docker build -f Dockerfile.vps -t rhodawk-hermes . \
  && docker run -d \
       --name hermes \
       --env-file .env \
       -v hermes-data:/data \
       --restart unless-stopped \
       rhodawk-hermes
```

The container starts supervisord which launches both processes automatically:
- **hermes-gateway** — python-telegram-bot (Telegram interface + orchestration)
- **openclaw-gateway** — OpenClaw multi-platform delivery (non-critical)

---

## 6. Verify the Deployment

### Check the container is running

```bash
docker ps --filter name=hermes
```

Expected output:
```
CONTAINER ID   IMAGE             COMMAND                  CREATED          STATUS                    PORTS     NAMES
abc123def456   rhodawk-hermes    "/app/scripts/init_a…"   2 minutes ago    Up 2 minutes (healthy)              hermes
```

The `(healthy)` status confirms supervisord is running and hermes-gateway is live.

### Stream live logs

```bash
docker logs -f hermes
```

You should see the boot sequence:
```
╔══════════════════════════════════════════════════════════════════════╗
║  Rhodawk AI — Autonomous Architect  v2                              ║
║  Agents: hermes-agent | openclaude | openclaw | jcode               ║
╚══════════════════════════════════════════════════════════════════════╝

[init] All required secrets present.
[hermes] Config ready.
[openclaw] Config ready.
[check] Agent + tool availability:
  1. hermes-agent  : installed
  2. openclaude    : ...
  3. openclaw      : installed (non-critical)
  ...
[supervisord] Starting all agent processes...
```

### Test the bot

Open Telegram and send your bot `/start`. It should reply with the welcome message.

---

## 7. docker compose (Recommended)

Create `docker-compose.yml` in the repo root:

```yaml
services:
  hermes:
    build:
      context: .
      dockerfile: Dockerfile.vps
    image: rhodawk-hermes:latest
    container_name: hermes
    env_file: .env
    volumes:
      - hermes-data:/data
    restart: unless-stopped
    healthcheck:
      test:
        - CMD
        - supervisorctl
        - -c
        - /etc/supervisor/conf.d/rhodawk.conf
        - status
        - hermes-gateway
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 60s

volumes:
  hermes-data:
    driver: local
```

Deploy:

```bash
# Build and start in detached mode
docker compose up -d --build

# Follow logs
docker compose logs -f

# Stop
docker compose down

# Restart just the bot process (without rebuilding the image)
docker compose restart hermes
```

---

## 8. Persistent Storage

The container stores all runtime state in `/data/`:

| Path | Contents | Persistence |
|---|---|---|
| `/data/memory.db` | SQLite DB — sessions, messages, executions | Survives restarts |
| `/data/.hermes/` | hermes-agent config, SOUL.md, skills | Survives restarts |
| `/data/.hermes/logs/` | Agent logs | Survives restarts |
| `/data/.hermes/sessions/` | Session snapshots | Survives restarts |
| `/tmp/repos/` | Cloned repos (ephemeral scratch) | Lost on restart — intentional |

The named volume `hermes-data` maps to `/data/` inside the container. Docker manages it automatically.

### Inspect the volume

```bash
docker volume inspect hermes-data
```

### Back up the database

```bash
docker cp hermes:/data/memory.db ./memory_backup_$(date +%Y%m%d).db
```

### Restore from backup

```bash
docker cp ./memory_backup_YYYYMMDD.db hermes:/data/memory.db
docker restart hermes
```

---

## 9. Auto-Start on Reboot (systemd)

If you used bare `docker run` (not docker compose), create a systemd unit to auto-start the container on VPS reboot:

```bash
sudo nano /etc/systemd/system/rhodawk-hermes.service
```

Paste:

```ini
[Unit]
Description=Rhodawk AI Hermes Code Stabilizer
After=docker.service
Requires=docker.service

[Service]
Type=simple
Restart=always
RestartSec=10
ExecStartPre=-/usr/bin/docker stop hermes
ExecStartPre=-/usr/bin/docker rm hermes
ExecStart=/usr/bin/docker run \
    --name hermes \
    --env-file /home/YOUR_USER/hermes/.env \
    -v hermes-data:/data \
    rhodawk-hermes
ExecStop=/usr/bin/docker stop hermes

[Install]
WantedBy=multi-user.target
```

> Replace `/home/YOUR_USER/hermes/.env` with the absolute path to your `.env` file.

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable rhodawk-hermes
sudo systemctl start rhodawk-hermes

# Check status
sudo systemctl status rhodawk-hermes
```

> If using docker compose instead, `restart: unless-stopped` in `docker-compose.yml` is sufficient — Docker's own daemon handles restarts automatically when the Docker service starts.

---

## 10. Monitoring and Logs

### Container-level logs (all supervisord output)

```bash
# Follow live
docker logs -f hermes

# Last 200 lines
docker logs --tail=200 hermes

# Since a specific time
docker logs --since="2025-01-01T00:00:00" hermes
```

### supervisord process status (inside container)

```bash
docker exec hermes supervisorctl -c /etc/supervisor/conf.d/rhodawk.conf status
```

Expected output:
```
hermes-gateway    RUNNING   pid 42, uptime 0:15:30
openclaw-gateway  RUNNING   pid 43, uptime 0:15:29
```

### Restart a specific agent process

```bash
# Restart only the hermes bot (without stopping the container)
docker exec hermes supervisorctl -c /etc/supervisor/conf.d/rhodawk.conf restart hermes-gateway

# Restart openclaw gateway
docker exec hermes supervisorctl -c /etc/supervisor/conf.d/rhodawk.conf restart openclaw-gateway
```

### Health check status

```bash
docker inspect --format='{{json .State.Health}}' hermes | python3 -m json.tool
```

### Memory database (SQLite)

```bash
# Open interactive SQLite shell inside the container
docker exec -it hermes sqlite3 /data/memory.db

# View recent pipeline executions
SELECT chat_id, repo_url, status, commit_hash, datetime(ended_at, 'unixepoch') as ended
FROM executions
ORDER BY ended_at DESC
LIMIT 10;

# View active sessions
SELECT chat_id, state, repo_url, strike_count FROM sessions;

# Exit
.quit
```

---

## 11. Updating the Bot

Pull the latest code and rebuild:

```bash
cd hermes

# Pull latest from HuggingFace Space
git pull origin main

# Rebuild image and restart (zero-downtime swap)
docker compose up -d --build

# Or with bare Docker:
docker build -f Dockerfile.vps -t rhodawk-hermes . \
  && docker stop hermes \
  && docker rm hermes \
  && docker run -d \
       --name hermes \
       --env-file .env \
       -v hermes-data:/data \
       --restart unless-stopped \
       rhodawk-hermes
```

Your `/data/` volume (memory DB, sessions, logs) is preserved across rebuilds.

---

## 12. Stopping and Removing

### Stop the container (data preserved)

```bash
docker stop hermes

# or
docker compose down
```

### Remove container + image (data preserved in volume)

```bash
docker rm hermes
docker rmi rhodawk-hermes
```

### Full teardown including persistent data

```bash
docker compose down --volumes
# or
docker volume rm hermes-data
```

> **Warning:** `--volumes` permanently deletes the SQLite memory DB and all hermes config in `/data/`. This is irreversible.

---

## 13. Troubleshooting

### Bot does not respond on Telegram

1. Check the token is set correctly:
   ```bash
   grep TELEGRAM_BOT_TOKEN .env
   ```
2. Confirm hermes-gateway is RUNNING:
   ```bash
   docker exec hermes supervisorctl -c /etc/supervisor/conf.d/rhodawk.conf status
   ```
3. Look for errors in logs:
   ```bash
   docker logs --tail=100 hermes | grep -E "ERROR|FATAL|Traceback"
   ```

### Container exits immediately

The `init_and_start.sh` script exits with `[FATAL]` if any required secret is missing. Check:

```bash
docker logs hermes | grep FATAL
# Example: [FATAL] Missing HF Space secrets: NVIDIA_NIM_API_KEY DO_INFERENCE_API_KEY
```

Add the missing keys to `.env` and restart.

### `[FATAL] Missing HF Space secrets`

The boot script checks for `TELEGRAM_BOT_TOKEN`, `NVIDIA_NIM_API_KEY`, `DO_INFERENCE_API_KEY`, and `GITHUB_PAT`. All four must be set. Run:

```bash
# Verify all four are present and non-empty
for key in TELEGRAM_BOT_TOKEN NVIDIA_NIM_API_KEY DO_INFERENCE_API_KEY GITHUB_PAT; do
  val=$(grep "^${key}=" .env | cut -d= -f2-)
  if [ -z "$val" ]; then
    echo "MISSING: $key"
  else
    echo "OK: $key"
  fi
done
```

### Pipeline fails with "DO Inference" errors

DigitalOcean Inference requires **title-case** model IDs: `deepseek-ai/DeepSeek-V4-Pro`
The bot automatically falls back to NVIDIA NIM when DO Inference is unreachable.
Check your `DO_INFERENCE_API_KEY` is valid at https://cloud.digitalocean.com/gen-ai.

### GitHub push fails

Verify your `GITHUB_PAT` has both `repo` and `workflow` scopes:
- Go to https://github.com/settings/tokens
- Click your token → confirm `repo` and `workflow` are checked

The bot uses a 3-layer push chain (local git → GitHub API Python → GitHub API Node.js) before reporting failure.

### `openclaw` not running (non-critical)

OpenClaw is optional. If it fails to install or start, `start_openclaw.sh` runs `sleep infinity` to keep supervisord happy. Telegram delivery still works via the hermes-gateway. This is expected behavior.

### Container health is "unhealthy"

```bash
# Check what the healthcheck is seeing
docker exec hermes supervisorctl -c /etc/supervisor/conf.d/rhodawk.conf status hermes-gateway
```

If `hermes-gateway` shows `BACKOFF` or `FATAL`, read the logs:
```bash
docker logs --tail=50 hermes
```

### Out of disk space (repos fill up /tmp)

Cloned repos in `/tmp/repos/` are ephemeral and cleared on container restart. If disk is filling:

```bash
# Check disk usage inside container
docker exec hermes du -sh /tmp/repos/

# Clear all cloned repos manually
docker exec hermes rm -rf /tmp/repos/*
```

---

## 14. Security Hardening

### Firewall — block the exposed port from public access

Port `7860` is exposed by the container for supervisord's internal use. It should not be publicly accessible:

```bash
# UFW — deny 7860 from external access
sudo ufw deny 7860
sudo ufw allow ssh
sudo ufw enable
```

### Secrets management

- Never commit `.env` to git (already in `.gitignore`)
- Rotate your API keys regularly (especially `GITHUB_PAT`)
- Use GitHub's Fine-Grained PATs scoped to specific repos when possible
- Consider using Docker Secrets or a secrets manager (Vault, AWS Secrets Manager) for production deployments

### Run as non-root (advanced)

The current container runs as root (required by supervisord and some agent tools). For hardened deployments:

```bash
# Add to docker run for additional isolation
docker run -d \
  --name hermes \
  --env-file .env \
  -v hermes-data:/data \
  --security-opt no-new-privileges:true \
  --cap-drop ALL \
  --cap-add SETUID \
  --cap-add SETGID \
  --restart unless-stopped \
  rhodawk-hermes
```

### Keep the image updated

Rebuild weekly to pull in OS security patches:

```bash
# Pull latest Ubuntu 22.04 base and rebuild
docker pull ubuntu:22.04
docker compose up -d --build
```

---

## Quick Reference Card

```bash
# ── Deploy ─────────────────────────────────────────────────────────────────────
git clone https://huggingface.co/spaces/Architect8999/Hermes hermes && cd hermes
cp .env.example .env && nano .env
docker compose up -d --build

# ── Status ─────────────────────────────────────────────────────────────────────
docker ps --filter name=hermes
docker exec hermes supervisorctl -c /etc/supervisor/conf.d/rhodawk.conf status
docker logs -f hermes

# ── Restart ────────────────────────────────────────────────────────────────────
docker compose restart
docker exec hermes supervisorctl -c /etc/supervisor/conf.d/rhodawk.conf restart hermes-gateway

# ── Update ─────────────────────────────────────────────────────────────────────
git pull origin main && docker compose up -d --build

# ── Backup DB ──────────────────────────────────────────────────────────────────
docker cp hermes:/data/memory.db ./memory_backup_$(date +%Y%m%d).db

# ── Stop ───────────────────────────────────────────────────────────────────────
docker compose down
```

---

*Rhodawk AI — Autonomous Architect v2*
