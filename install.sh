#!/usr/bin/env bash
# =============================================================================
# Rhodawk AI — Hermes Code Stabilizer
# Single-Shot VPS Installer  v3.0
#
# Usage (fresh Ubuntu 22.04 VPS, nothing pre-installed):
#   curl -fsSL https://huggingface.co/spaces/Architect8999/Hermes/raw/main/install.sh | bash
#
# Or if you already cloned the repo:
#   bash install.sh
#
# Unattended mode (CI / cloud-init):
#   TELEGRAM_BOT_TOKEN=xxx \
#   DO_INFERENCE_API_KEY=xxx \
#   GITHUB_PAT=xxx \
#   bash install.sh
#
# What this script does (in order):
#   1.  OS compatibility check (Ubuntu / Debian required)
#   2.  System update + essential packages
#   3.  Swap file (4 GB) if total RAM < 6 GB
#   4.  Docker CE + Docker Compose plugin (official repo)
#   5.  UFW firewall — allow SSH, block 7860 from public
#   6.  Clone repo (or reuse current dir / pull if already cloned)
#   7.  Secret collection (interactive or from env vars in unattended mode)
#   8.  Validate .env before building
#   9.  Build Docker image from Dockerfile.vps and start with docker compose
#  10.  Poll health check — wait up to 90 s for hermes-gateway RUNNING
#  11.  Systemd unit — auto-start container on VPS reboot
#  12.  Log-rotation cron for Docker JSON logs
#  13.  Final status + quick-reference command card
# =============================================================================

set -euo pipefail

# ── Colour helpers ─────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()  { echo -e "${CYAN}[info]${RESET}  $*"; }
ok()    { echo -e "${GREEN}[ok]${RESET}    $*"; }
warn()  { echo -e "${YELLOW}[warn]${RESET}  $*"; }
step()  { echo -e "\n${BOLD}── $* ──────────────────────────────────────────${RESET}"; }
fatal() { echo -e "${RED}[fatal]${RESET} $*" >&2; exit 1; }

# ── Banner ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}║  Rhodawk AI — Hermes Code Stabilizer                            ║${RESET}"
echo -e "${BOLD}║  Single-Shot VPS Installer  v3.0                                ║${RESET}"
echo -e "${BOLD}║  Agents: hermes-agent │ openclaude │ openclaw │ jcode            ║${RESET}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════════════════╝${RESET}"
echo ""

# ── Detect if running from pipe (curl | bash) vs local ────────────────────────
RUNNING_FROM_PIPE=false
if [ ! -t 0 ]; then
    RUNNING_FROM_PIPE=true
fi

# ── Detect unattended mode (all required env vars already set) ─────────────────
UNATTENDED=false
if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${DO_INFERENCE_API_KEY:-}" && -n "${GITHUB_PAT:-}" ]]; then
    UNATTENDED=true
    info "Unattended mode detected — all required env vars are set."
fi

# =============================================================================
# 1. OS CHECK
# =============================================================================
step "1. OS compatibility check"

[[ -f /etc/os-release ]] || fatal "Cannot detect OS. Ubuntu 22.04 LTS is required."
source /etc/os-release

if [[ "$ID" != "ubuntu" && "$ID" != "debian" && "${ID_LIKE:-}" != *"debian"* ]]; then
    fatal "Unsupported OS: ${PRETTY_NAME}. Ubuntu 22.04 LTS recommended."
fi
ok "OS: ${PRETTY_NAME}"

# Determine sudo usage
if [[ $EUID -ne 0 ]]; then
    SUDO="sudo"
    info "Non-root user — will use sudo for system commands."
else
    SUDO=""
    warn "Running as root. For production consider a non-root user."
fi

# =============================================================================
# 2. SYSTEM UPDATE + PACKAGES
# =============================================================================
step "2. System update + essential packages"

$SUDO apt-get update -qq
$SUDO apt-get install -y -qq \
    ca-certificates curl gnupg lsb-release \
    git make ufw jq \
    2>/dev/null || true
ok "Essential packages installed."

# =============================================================================
# 3. SWAP FILE (only if RAM < 6 GB)
# =============================================================================
step "3. Swap memory"

TOTAL_RAM_KB=$(grep MemTotal /proc/meminfo | awk '{print $2}')
TOTAL_RAM_GB=$(( TOTAL_RAM_KB / 1024 / 1024 ))

if [[ $TOTAL_RAM_GB -lt 6 ]]; then
    if swapon --show | grep -q '/swapfile'; then
        ok "Swapfile already exists — skipping."
    else
        info "RAM = ${TOTAL_RAM_GB} GB — creating 4 GB swapfile..."
        $SUDO fallocate -l 4G /swapfile
        $SUDO chmod 600 /swapfile
        $SUDO mkswap /swapfile -q
        $SUDO swapon /swapfile
        # Persist across reboots
        grep -q '/swapfile' /etc/fstab 2>/dev/null \
            || echo '/swapfile none swap sw 0 0' | $SUDO tee -a /etc/fstab > /dev/null
        # Tune swappiness for server workloads
        $SUDO sysctl -w vm.swappiness=10 > /dev/null
        grep -q 'vm.swappiness' /etc/sysctl.conf 2>/dev/null \
            || echo 'vm.swappiness=10' | $SUDO tee -a /etc/sysctl.conf > /dev/null
        ok "4 GB swapfile created and activated (swappiness=10)."
    fi
else
    ok "RAM = ${TOTAL_RAM_GB} GB — swap not required."
fi

# =============================================================================
# 4. DOCKER CE + COMPOSE PLUGIN
# =============================================================================
step "4. Docker CE + Docker Compose plugin"

install_docker() {
    info "Installing Docker from official repo..."
    $SUDO install -m 0755 -d /etc/apt/keyrings
    curl -fsSL "https://download.docker.com/linux/${ID}/gpg" \
        | $SUDO gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    $SUDO chmod a+r /etc/apt/keyrings/docker.gpg

    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
      https://download.docker.com/linux/${ID} \
      $(. /etc/os-release && echo "${VERSION_CODENAME}") stable" \
      | $SUDO tee /etc/apt/sources.list.d/docker.list > /dev/null

    $SUDO apt-get update -qq
    $SUDO apt-get install -y -qq \
        docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin

    $SUDO systemctl enable docker --now
    ok "Docker installed: $(docker --version)"
}

if command -v docker &>/dev/null; then
    ok "Docker already installed: $(docker --version)"
else
    install_docker
fi

# Add current non-root user to docker group
if [[ -n "${SUDO_USER:-}" ]]; then
    $SUDO usermod -aG docker "$SUDO_USER" 2>/dev/null || true
    warn "Added ${SUDO_USER} to the 'docker' group. Re-login to run docker without sudo."
fi

# Verify compose plugin
docker compose version &>/dev/null \
    || fatal "docker compose plugin not found. Re-run this installer or install manually."
ok "Docker Compose: $(docker compose version --short)"

# =============================================================================
# 5. UFW FIREWALL
# =============================================================================
step "5. UFW firewall"

if command -v ufw &>/dev/null; then
    # Always allow SSH before touching ufw — prevents lockout
    $SUDO ufw allow ssh   > /dev/null 2>&1 || true
    # Block the container's internal port from public internet
    $SUDO ufw deny 7860   > /dev/null 2>&1 || true
    # Block internal service ports from public internet.
    # These ports are for inter-container traffic only and must not be
    # exposed externally: Redis, sandbox-manager (both ports), camofox,
    # webhook-receiver, and openclaw gateway.
    $SUDO ufw deny 6379   > /dev/null 2>&1 || true  # Redis
    $SUDO ufw deny 8081   > /dev/null 2>&1 || true  # sandbox-manager (legacy)
    $SUDO ufw deny 8090   > /dev/null 2>&1 || true  # sandbox-manager
    $SUDO ufw deny 9377   > /dev/null 2>&1 || true  # camofox stealth browser
    $SUDO ufw deny 9000   > /dev/null 2>&1 || true  # webhook-receiver
    $SUDO ufw deny 18789  > /dev/null 2>&1 || true  # openclaw gateway
    # Enable (non-interactively) only if not already active
    if $SUDO ufw status | grep -q "Status: active"; then
        ok "UFW already active — rules updated."
    else
        $SUDO ufw --force enable > /dev/null 2>&1 || true
        ok "UFW enabled: SSH allowed, internal service ports blocked."
    fi
else
    warn "ufw not available — skipping firewall configuration."
fi

# =============================================================================
# 6. REPO SETUP
# =============================================================================
step "6. Repository"

REPO_URL="https://huggingface.co/spaces/Architect8999/Hermes"
INSTALL_DIR="${HOME}/hermes"

if [[ -f "$(pwd)/Dockerfile.vps" && -f "$(pwd)/docker-compose.yml" ]]; then
    # We're already inside the repo (e.g. user ran: bash install.sh)
    INSTALL_DIR="$(pwd)"
    info "Already inside the repo at ${INSTALL_DIR} — skipping clone."
elif [[ -d "${INSTALL_DIR}/.git" ]]; then
    info "Repo already cloned at ${INSTALL_DIR} — pulling latest..."
    git -C "${INSTALL_DIR}" pull origin main
    ok "Repo up to date."
else
    info "Cloning repo to ${INSTALL_DIR}..."
    git clone "${REPO_URL}" "${INSTALL_DIR}"
    ok "Cloned to ${INSTALL_DIR}"
fi

cd "${INSTALL_DIR}"

# =============================================================================
# 7. SECRET COLLECTION
# =============================================================================
step "7. Configure secrets"

read_secret() {
    local prompt="$1" var_name="$2" required="${3:-yes}" value=""
    while true; do
        read -rsp "  ${prompt}: " value
        echo ""
        if [[ -z "$value" ]]; then
            if [[ "$required" == "yes" ]]; then
                echo -e "  ${RED}Required — cannot be empty. Try again.${RESET}"
            else
                echo -e "  ${YELLOW}Skipped (optional).${RESET}"
                break
            fi
        else
            ok "${var_name} set."
            break
        fi
    done
    printf -v "${var_name}" '%s' "${value}"
}

SKIP_SECRETS=false

if [[ "${UNATTENDED}" == "true" ]]; then
    # Unattended: write .env from env vars (never echo values)
    info "Writing .env from environment variables (unattended mode)..."
    {
        echo "# Rhodawk AI — Hermes secrets"
        echo "# Generated by install.sh (unattended) on $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
        echo ""
        echo "TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}"
        echo "DO_INFERENCE_API_KEY=${DO_INFERENCE_API_KEY}"
        echo "GITHUB_PAT=${GITHUB_PAT}"
        [[ -n "${HF_TOKEN:-}" ]]       && echo "HF_TOKEN=${HF_TOKEN}"
        [[ -n "${BRAVE_API_KEY:-}" ]]  && echo "BRAVE_API_KEY=${BRAVE_API_KEY}"
    } > .env
    chmod 600 .env
    ok ".env written (unattended mode)."
    SKIP_SECRETS=true
fi

if [[ "${SKIP_SECRETS}" == "false" ]]; then
    if [[ -f .env ]]; then
        warn ".env already exists."
        read -rp "  Overwrite with new values? [y/N] " OVERWRITE
        [[ "${OVERWRITE,,}" == "y" ]] || SKIP_SECRETS=true
    fi
fi

if [[ "${SKIP_SECRETS}" == "false" ]]; then
    echo ""
    echo "  Required secrets:"
    echo ""

    read_secret "TELEGRAM_BOT_TOKEN  (from @BotFather on Telegram → /newbot)" \
        TELEGRAM_BOT_TOKEN "yes"
    read_secret "DO_INFERENCE_API_KEY (from cloud.digitalocean.com/gen-ai)" \
        DO_INFERENCE_API_KEY "yes"
    read_secret "GITHUB_PAT           (repo + workflow scopes — github.com/settings/tokens)" \
        GITHUB_PAT "yes"

    echo ""
    echo "  Optional secrets (press Enter to skip):"
    echo ""
    read_secret "HF_TOKEN            (write token from huggingface.co/settings/tokens)" \
        HF_TOKEN "no"
    read_secret "BRAVE_API_KEY       (from api.search.brave.com — enables web search)" \
        BRAVE_API_KEY "no"

    {
        echo "# Rhodawk AI — Hermes secrets"
        echo "# Generated by install.sh on $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
        echo ""
        echo "TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}"
        echo "DO_INFERENCE_API_KEY=${DO_INFERENCE_API_KEY}"
        echo "GITHUB_PAT=${GITHUB_PAT}"
        [[ -n "${HF_TOKEN:-}" ]]      && echo "HF_TOKEN=${HF_TOKEN}"
        [[ -n "${BRAVE_API_KEY:-}" ]] && echo "BRAVE_API_KEY=${BRAVE_API_KEY}"
    } > .env
    chmod 600 .env
    ok ".env written (mode 600 — readable only by current user)."
fi

# =============================================================================
# 8. VALIDATE .env
# =============================================================================
step "8. Validate .env"

MISSING=()
for KEY in TELEGRAM_BOT_TOKEN DO_INFERENCE_API_KEY GITHUB_PAT; do
    val=$(grep -E "^${KEY}=" .env 2>/dev/null | cut -d= -f2- | tr -d '[:space:]')
    [[ -z "$val" ]] && MISSING+=("$KEY")
done

if [[ ${#MISSING[@]} -gt 0 ]]; then
    fatal "Missing required secrets in .env: ${MISSING[*]}\nEdit .env and re-run: bash install.sh"
fi
ok "All required secrets present in .env."

# =============================================================================
# 9. BUILD + START (docker compose, Dockerfile.vps)
# =============================================================================
step "9. Build Docker image and start container"

info "Building from Dockerfile.vps (this takes 5–10 min on first run)..."
docker compose build \
    --progress=plain \
    2>&1 | grep -E "(Step|RUN|COPY|FROM|---> |Successfully built|error|Error|#[0-9])" || true

echo ""
info "Starting container in detached mode..."
docker compose up -d

# =============================================================================
# 10. HEALTH CHECK POLL
# =============================================================================
step "10. Waiting for hermes-gateway to come online"

info "Polling health status (up to 90 s)..."
HEALTHY=false
for i in $(seq 1 18); do
    STATUS=$(docker inspect --format='{{.State.Health.Status}}' hermes 2>/dev/null || echo "starting")
    if [[ "$STATUS" == "healthy" ]]; then
        HEALTHY=true
        ok "hermes-gateway is HEALTHY."
        break
    fi
    if [[ "$STATUS" == "unhealthy" ]]; then
        warn "Container reports unhealthy — printing last 50 log lines:"
        docker compose logs --tail=50 hermes || true
        fatal "Startup failed. Fix the error above, then re-run: bash install.sh"
    fi
    echo -ne "  [${i}/18] status=${STATUS} — waiting 5 s...\r"
    sleep 5
done
echo ""

if [[ "${HEALTHY}" == "false" ]]; then
    warn "Health check did not confirm HEALTHY within 90 s."
    warn "The container may still be starting. Check with: docker compose logs -f hermes"
fi

# =============================================================================
# 11. SYSTEMD UNIT (auto-start on reboot)
# =============================================================================
step "11. systemd auto-start on reboot"

SYSTEMD_UNIT_PATH="/etc/systemd/system/rhodawk-hermes.service"
COMPOSE_BIN=$(command -v docker || echo "/usr/bin/docker")
INSTALL_DIR_ABS="$(realpath "${INSTALL_DIR}")"

$SUDO tee "${SYSTEMD_UNIT_PATH}" > /dev/null << UNIT
[Unit]
Description=Rhodawk AI — Hermes Code Stabilizer
Documentation=https://huggingface.co/spaces/Architect8999/Hermes
After=docker.service network-online.target
Wants=network-online.target
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=${INSTALL_DIR_ABS}
ExecStartPre=${COMPOSE_BIN} compose -f ${INSTALL_DIR_ABS}/docker-compose.yml pull --quiet || true
ExecStart=${COMPOSE_BIN} compose -f ${INSTALL_DIR_ABS}/docker-compose.yml up -d
ExecStop=${COMPOSE_BIN} compose -f ${INSTALL_DIR_ABS}/docker-compose.yml down
ExecReload=${COMPOSE_BIN} compose -f ${INSTALL_DIR_ABS}/docker-compose.yml restart
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
UNIT

$SUDO systemctl daemon-reload
$SUDO systemctl enable rhodawk-hermes > /dev/null 2>&1
ok "systemd unit installed and enabled: rhodawk-hermes.service"
info "Container will auto-start on every VPS reboot."

# =============================================================================
# 12. LOG ROTATION
# =============================================================================
step "12. Docker log rotation"

$SUDO tee /etc/logrotate.d/docker-rhodawk > /dev/null << 'LOGROTATE'
/var/lib/docker/containers/*/*-json.log {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    maxsize 100M
}
LOGROTATE

ok "Log rotation configured (daily, 7-day retention, max 100 MB per file)."

# =============================================================================
# 13. FINAL STATUS + QUICK REFERENCE
# =============================================================================
step "13. Deployment summary"

echo ""
docker compose ps
echo ""

# Supervisord process status
echo "Agent process status:"
docker exec hermes \
    supervisorctl -c /etc/supervisor/conf.d/rhodawk.conf status \
    2>/dev/null || warn "Cannot query supervisord yet — container may still be starting."

echo ""
echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════════════════════════╗${RESET}"
echo -e "${GREEN}${BOLD}║  Hermes is RUNNING.                                             ║${RESET}"
echo -e "${GREEN}${BOLD}║  Open Telegram and send /start to your bot.                     ║${RESET}"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════════════════════════╝${RESET}"
echo ""
echo -e "${BOLD}Quick reference:${RESET}"
echo "  docker compose logs -f hermes                          # stream logs"
echo "  docker compose restart hermes                          # restart bot"
echo "  docker compose up -d --build                           # rebuild after update"
echo "  docker exec hermes supervisorctl \\
    -c /etc/supervisor/conf.d/rhodawk.conf status              # agent health"
echo "  docker cp hermes:/data/memory.db ./backup.db           # backup DB"
echo "  sudo systemctl status rhodawk-hermes                   # systemd status"
echo "  git pull origin main && docker compose up -d --build   # update bot"
echo ""
echo -e "${BOLD}Install directory:${RESET} ${INSTALL_DIR_ABS}"
echo -e "${BOLD}Data volume:${RESET}       hermes-data (docker volume — persists across rebuilds)"
echo -e "${BOLD}Secrets:${RESET}           ${INSTALL_DIR_ABS}/.env (mode 600)"
echo ""
