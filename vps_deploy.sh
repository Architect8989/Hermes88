#!/usr/bin/env bash
# =============================================================================
# Rhodawk AI — Hermes Autonomous Architect
# Single-Shot VPS Deployment Script  v4.0
#
# Supports:  Ubuntu 20.04 / 22.04 / 24.04 LTS · Debian 11 / 12
# Arch:      amd64 · arm64
#
# ── Quick start (fresh VPS, nothing installed) ────────────────────────────────
#
#   curl -fsSL https://raw.githubusercontent.com/Architect8989/Hermes88/main/vps_deploy.sh | bash
#
# ── Unattended / CI / cloud-init (set secrets before piping) ─────────────────
#
#   TELEGRAM_BOT_TOKEN=xxx \
#   DO_INFERENCE_API_KEY=xxx \
#   GITHUB_PAT=xxx \
#   bash vps_deploy.sh
#
# ── Re-run safely (idempotent — skips already-completed steps) ───────────────
#
#   bash vps_deploy.sh
#
# ── Update only (pull latest code and rebuild) ────────────────────────────────
#
#   UPDATE_ONLY=1 bash vps_deploy.sh
#
# ── What this script does, in order ──────────────────────────────────────────
#
#   1.  OS + architecture compatibility check
#   2.  System update + essential packages
#   3.  Swap file (4 GB) when total RAM < 6 GB
#   4.  Docker CE + Docker Compose v2 plugin (official repo, idempotent)
#   5.  UFW firewall — allow SSH, deny port 7860 from public
#   6.  Repo clone (or pull if already cloned / inside repo)
#   7.  Secret collection — interactive (hidden input) or from env vars
#   8.  Validate .env — fails fast before wasting build time
#   9.  Docker image build from Dockerfile.vps + container start
#  10.  Health-check poll — waits up to 120 s for hermes-gateway RUNNING
#  11.  Systemd unit — rhodawk-hermes.service (auto-start on reboot)
#  12.  Log rotation cron for Docker JSON logs
#  13.  Deployment summary + quick-reference command card
#
# =============================================================================

set -euo pipefail

# ── Terminal colours ──────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
RESET='\033[0m'

info()  { echo -e "${CYAN}[info]${RESET}  $*"; }
ok()    { echo -e "${GREEN}[ok]${RESET}    $*"; }
warn()  { echo -e "${YELLOW}[warn]${RESET}  $*"; }
step()  { echo -e "\n${BOLD}── $* ${DIM}──────────────────────────────────────────${RESET}"; }
fatal() { echo -e "${RED}[fatal]${RESET} $*" >&2; exit 1; }

# ── Script version + banner ───────────────────────────────────────────────────
SCRIPT_VERSION="4.0"
REPO_URL="https://github.com/Architect8989/Hermes88"
INSTALL_DIR_DEFAULT="${HOME}/hermes"

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}║  Rhodawk AI — Hermes Autonomous Architect                           ║${RESET}"
echo -e "${BOLD}║  Single-Shot VPS Installer  v${SCRIPT_VERSION}                                ║${RESET}"
echo -e "${BOLD}║  Agents: hermes-agent │ openclaude │ openclaw │ jcode               ║${RESET}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════════════════════╝${RESET}"
echo ""

# ── Detect pipe mode (curl | bash loses TTY) ─────────────────────────────────
PIPE_MODE=false
[[ ! -t 0 ]] && PIPE_MODE=true

# ── Detect unattended mode ────────────────────────────────────────────────────
UNATTENDED=false
if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${DO_INFERENCE_API_KEY:-}" && -n "${GITHUB_PAT:-}" ]]; then
    UNATTENDED=true
    info "Unattended mode — all required env vars already set."
fi

# ── Update-only mode ─────────────────────────────────────────────────────────
UPDATE_ONLY="${UPDATE_ONLY:-false}"

# =============================================================================
# STEP 1 — OS + ARCHITECTURE CHECK
# =============================================================================
step "1. OS + architecture compatibility check"

[[ -f /etc/os-release ]] || fatal "Cannot detect OS. Ubuntu 20.04+ or Debian 11+ required."
# shellcheck source=/dev/null
source /etc/os-release

SUPPORTED_OS=false
if [[ "$ID" == "ubuntu" || "$ID" == "debian" || "${ID_LIKE:-}" == *"debian"* ]]; then
    SUPPORTED_OS=true
fi
[[ "$SUPPORTED_OS" == true ]] || fatal "Unsupported OS: ${PRETTY_NAME}. Ubuntu 20.04+ or Debian 11+ required."
ok "OS: ${PRETTY_NAME}"

ARCH=$(uname -m)
case "$ARCH" in
    x86_64)  DPKG_ARCH="amd64" ;;
    aarch64) DPKG_ARCH="arm64" ;;
    *)       fatal "Unsupported architecture: $ARCH. amd64 and arm64 are supported." ;;
esac
ok "Architecture: ${ARCH} (${DPKG_ARCH})"

# Determine sudo prefix
if [[ $EUID -ne 0 ]]; then
    SUDO="sudo"
    info "Non-root user — will prefix system commands with sudo."
else
    SUDO=""
    warn "Running as root. Consider a non-root user for production."
fi

# Early-exit for update-only mode
if [[ "${UPDATE_ONLY}" == "1" || "${UPDATE_ONLY}" == "true" ]]; then
    step "Update-only mode"
    # Locate install dir
    if [[ -f "$(pwd)/Dockerfile.vps" && -f "$(pwd)/docker-compose.yml" ]]; then
        INSTALL_DIR="$(pwd)"
    elif [[ -d "${INSTALL_DIR_DEFAULT}/.git" ]]; then
        INSTALL_DIR="${INSTALL_DIR_DEFAULT}"
    else
        fatal "Cannot locate Hermes install directory. Run without UPDATE_ONLY=1 first."
    fi
    info "Install directory: ${INSTALL_DIR}"
    git -C "${INSTALL_DIR}" pull origin main
    ok "Pulled latest code."
    cd "${INSTALL_DIR}"
    docker compose build --pull
    docker compose up -d
    ok "Container updated and restarted."
    echo ""
    echo -e "${GREEN}${BOLD}Update complete. Run: docker compose logs -f hermes${RESET}"
    echo ""
    exit 0
fi

# =============================================================================
# STEP 2 — SYSTEM UPDATE + ESSENTIAL PACKAGES
# =============================================================================
step "2. System update + essential packages"

$SUDO apt-get update -qq
$SUDO apt-get install -y -qq \
    ca-certificates curl gnupg lsb-release \
    git make ufw jq \
    2>/dev/null || true
ok "Essential packages installed."

# =============================================================================
# STEP 3 — SWAP FILE (only when RAM < 6 GB)
# =============================================================================
step "3. Swap memory"

TOTAL_RAM_KB=$(grep MemTotal /proc/meminfo | awk '{print $2}')
TOTAL_RAM_GB=$(( TOTAL_RAM_KB / 1024 / 1024 ))

if [[ $TOTAL_RAM_GB -lt 6 ]]; then
    if swapon --show 2>/dev/null | grep -q '/swapfile'; then
        ok "Swapfile already active — skipping."
    else
        info "RAM = ${TOTAL_RAM_GB} GB — creating 4 GB swapfile..."
        $SUDO fallocate -l 4G /swapfile
        $SUDO chmod 600 /swapfile
        $SUDO mkswap /swapfile -q
        $SUDO swapon /swapfile
        grep -q '/swapfile' /etc/fstab 2>/dev/null \
            || echo '/swapfile none swap sw 0 0' | $SUDO tee -a /etc/fstab > /dev/null
        $SUDO sysctl -w vm.swappiness=10 > /dev/null
        grep -q 'vm.swappiness' /etc/sysctl.conf 2>/dev/null \
            || echo 'vm.swappiness=10' | $SUDO tee -a /etc/sysctl.conf > /dev/null
        ok "4 GB swapfile created and activated (swappiness=10)."
    fi
else
    ok "RAM = ${TOTAL_RAM_GB} GB — swap not required."
fi

# =============================================================================
# STEP 4 — DOCKER CE + COMPOSE PLUGIN
# =============================================================================
step "4. Docker CE + Docker Compose v2 plugin"

install_docker() {
    info "Installing Docker CE from official repo..."
    $SUDO install -m 0755 -d /etc/apt/keyrings

    # Support both ubuntu and debian GPG key paths
    DOCKER_GPG_URL="https://download.docker.com/linux/${ID}/gpg"
    curl -fsSL "${DOCKER_GPG_URL}" \
        | $SUDO gpg --dearmor -o /etc/apt/keyrings/docker.gpg 2>/dev/null
    $SUDO chmod a+r /etc/apt/keyrings/docker.gpg

    echo \
        "deb [arch=${DPKG_ARCH} signed-by=/etc/apt/keyrings/docker.gpg] \
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

# Add current non-root user to docker group so they can run docker without sudo
if [[ -n "${SUDO_USER:-}" ]]; then
    $SUDO usermod -aG docker "${SUDO_USER}" 2>/dev/null || true
    warn "Added ${SUDO_USER} to the 'docker' group. Re-login to use docker without sudo."
fi

docker compose version &>/dev/null \
    || fatal "docker compose plugin not found. Re-run this script or install manually."
ok "Docker Compose: $(docker compose version --short)"

# =============================================================================
# STEP 5 — UFW FIREWALL
# =============================================================================
step "5. UFW firewall"

if command -v ufw &>/dev/null; then
    $SUDO ufw allow ssh   > /dev/null 2>&1 || true
    $SUDO ufw deny  7860  > /dev/null 2>&1 || true
    if $SUDO ufw status 2>/dev/null | grep -q "Status: active"; then
        ok "UFW already active — SSH allowed, port 7860 denied."
    else
        $SUDO ufw --force enable > /dev/null 2>&1 || true
        ok "UFW enabled — SSH allowed, port 7860 blocked from public internet."
    fi
else
    warn "ufw not found — skipping firewall configuration."
fi

# =============================================================================
# STEP 6 — REPOSITORY SETUP
# =============================================================================
step "6. Repository"

if [[ -f "$(pwd)/Dockerfile.vps" && -f "$(pwd)/docker-compose.yml" ]]; then
    # Already inside the repo (e.g. user ran: bash vps_deploy.sh from within clone)
    INSTALL_DIR="$(pwd)"
    info "Already inside the repo at ${INSTALL_DIR} — skipping clone."
elif [[ -d "${INSTALL_DIR_DEFAULT}/.git" ]]; then
    info "Repo already cloned at ${INSTALL_DIR_DEFAULT} — pulling latest..."
    git -C "${INSTALL_DIR_DEFAULT}" pull origin main
    INSTALL_DIR="${INSTALL_DIR_DEFAULT}"
    ok "Repo updated."
else
    info "Cloning repo to ${INSTALL_DIR_DEFAULT}..."
    git clone "${REPO_URL}" "${INSTALL_DIR_DEFAULT}"
    INSTALL_DIR="${INSTALL_DIR_DEFAULT}"
    ok "Cloned to ${INSTALL_DIR}"
fi

cd "${INSTALL_DIR}"
INSTALL_DIR_ABS="$(realpath "${INSTALL_DIR}")"

# =============================================================================
# STEP 7 — SECRET COLLECTION
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
    # shellcheck disable=SC2229
    printf -v "${var_name}" '%s' "${value}"
}

SKIP_SECRETS=false

if [[ "${UNATTENDED}" == "true" ]]; then
    info "Writing .env from environment variables (unattended mode)..."
    {
        echo "# Rhodawk AI — Hermes secrets"
        echo "# Generated by vps_deploy.sh (unattended) on $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
        echo ""
        echo "TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}"
        echo "DO_INFERENCE_API_KEY=${DO_INFERENCE_API_KEY}"
        echo "GITHUB_PAT=${GITHUB_PAT}"
        [[ -n "${NVIDIA_NIM_API_KEY:-}" ]] && echo "NVIDIA_NIM_API_KEY=${NVIDIA_NIM_API_KEY}"
        [[ -n "${HF_TOKEN:-}" ]]           && echo "HF_TOKEN=${HF_TOKEN}"
        [[ -n "${BRAVE_API_KEY:-}" ]]      && echo "BRAVE_API_KEY=${BRAVE_API_KEY}"
    } > .env
    chmod 600 .env
    ok ".env written (unattended mode)."
    SKIP_SECRETS=true
fi

if [[ "${SKIP_SECRETS}" == "false" ]]; then
    if [[ -f .env ]]; then
        warn ".env already exists."
        if [[ "${PIPE_MODE}" == "true" ]]; then
            info "Pipe mode — keeping existing .env."
            SKIP_SECRETS=true
        else
            read -rp "  Overwrite with new values? [y/N] " OVERWRITE
            [[ "${OVERWRITE,,}" == "y" ]] || SKIP_SECRETS=true
        fi
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

    read_secret "NVIDIA_NIM_API_KEY  (from build.nvidia.com — enables NIM fallback)" \
        NVIDIA_NIM_API_KEY "no"
    read_secret "HF_TOKEN            (write token — huggingface.co/settings/tokens)" \
        HF_TOKEN "no"
    read_secret "BRAVE_API_KEY       (from api.search.brave.com — enables web search)" \
        BRAVE_API_KEY "no"

    {
        echo "# Rhodawk AI — Hermes secrets"
        echo "# Generated by vps_deploy.sh on $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
        echo ""
        echo "TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}"
        echo "DO_INFERENCE_API_KEY=${DO_INFERENCE_API_KEY}"
        echo "GITHUB_PAT=${GITHUB_PAT}"
        [[ -n "${NVIDIA_NIM_API_KEY:-}" ]] && echo "NVIDIA_NIM_API_KEY=${NVIDIA_NIM_API_KEY}"
        [[ -n "${HF_TOKEN:-}" ]]           && echo "HF_TOKEN=${HF_TOKEN}"
        [[ -n "${BRAVE_API_KEY:-}" ]]      && echo "BRAVE_API_KEY=${BRAVE_API_KEY}"
    } > .env
    chmod 600 .env
    ok ".env written (mode 600 — readable only by current user)."
fi

# =============================================================================
# STEP 8 — VALIDATE .env
# =============================================================================
step "8. Validate .env"

MISSING=()
for KEY in TELEGRAM_BOT_TOKEN DO_INFERENCE_API_KEY GITHUB_PAT; do
    val=$(grep -E "^${KEY}=" .env 2>/dev/null | cut -d= -f2- | tr -d '[:space:]')
    [[ -z "$val" ]] && MISSING+=("$KEY")
done

if [[ ${#MISSING[@]} -gt 0 ]]; then
    fatal "Missing required secrets in .env: ${MISSING[*]}\nEdit .env and re-run: bash vps_deploy.sh"
fi
ok "All required secrets present in .env."

# Report optional secrets status
for KEY in NVIDIA_NIM_API_KEY HF_TOKEN BRAVE_API_KEY; do
    val=$(grep -E "^${KEY}=" .env 2>/dev/null | cut -d= -f2- | tr -d '[:space:]')
    if [[ -n "$val" ]]; then
        info "Optional: ${KEY} — present"
    else
        info "Optional: ${KEY} — not set (skipped)"
    fi
done

# =============================================================================
# STEP 9 — BUILD DOCKER IMAGE + START CONTAINER
# =============================================================================
step "9. Build Docker image and start container"

# Pull latest base image before build so we always use a fresh Ubuntu 22.04
info "Pulling base image ubuntu:22.04..."
docker pull ubuntu:22.04 2>/dev/null || warn "Could not pull ubuntu:22.04 — using cached image."

info "Building from Dockerfile.vps (5–12 min on first run)..."
docker compose build \
    --progress=plain \
    2>&1 | grep -E "(#[0-9]+|Step|RUN|COPY|FROM|---> |Successfully built|ERROR|error)" || true

echo ""
info "Starting container in detached mode..."
docker compose up -d

# =============================================================================
# STEP 10 — HEALTH CHECK POLL
# =============================================================================
step "10. Waiting for hermes-gateway to come online"

info "Polling health status (up to 120 s)..."
HEALTHY=false
MAX_POLLS=24  # 24 × 5 s = 120 s
for i in $(seq 1 ${MAX_POLLS}); do
    STATUS=$(docker inspect --format='{{.State.Health.Status}}' hermes 2>/dev/null || echo "starting")

    if [[ "$STATUS" == "healthy" ]]; then
        HEALTHY=true
        ok "hermes-gateway is HEALTHY after $((i * 5))s."
        break
    fi

    if [[ "$STATUS" == "unhealthy" ]]; then
        warn "Container reports UNHEALTHY — last 60 log lines:"
        docker compose logs --tail=60 hermes 2>/dev/null || true
        fatal "Startup failed. Fix errors above, then re-run: bash vps_deploy.sh"
    fi

    printf "  [%02d/%02d] status=%-12s — waiting 5s...\r" "$i" "${MAX_POLLS}" "$STATUS"
    sleep 5
done
echo ""

if [[ "${HEALTHY}" == "false" ]]; then
    warn "Health check did not confirm HEALTHY within 120 s."
    warn "The container may still be initialising. Check with:"
    warn "  docker compose logs -f hermes"
fi

# =============================================================================
# STEP 11 — SYSTEMD UNIT (auto-start on reboot)
# =============================================================================
step "11. systemd auto-start service"

COMPOSE_BIN="$(command -v docker)"
SYSTEMD_UNIT_PATH="/etc/systemd/system/rhodawk-hermes.service"

$SUDO tee "${SYSTEMD_UNIT_PATH}" > /dev/null << UNIT
[Unit]
Description=Rhodawk AI — Hermes Autonomous Architect
Documentation=${REPO_URL}
After=docker.service network-online.target
Wants=network-online.target
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=${INSTALL_DIR_ABS}
ExecStartPre=-${COMPOSE_BIN} compose -f ${INSTALL_DIR_ABS}/docker-compose.yml pull --quiet
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
ok "systemd unit installed: rhodawk-hermes.service"
info "Container will auto-restart on every VPS reboot."

# =============================================================================
# STEP 12 — DOCKER LOG ROTATION
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

ok "Log rotation configured (daily · 7-day retention · max 100 MB per file)."

# =============================================================================
# STEP 13 — DEPLOYMENT SUMMARY
# =============================================================================
step "13. Deployment summary"

echo ""
docker compose ps 2>/dev/null || true
echo ""

echo "Agent process status (may take a moment):"
docker exec hermes \
    supervisorctl -c /etc/supervisor/conf.d/rhodawk.conf status \
    2>/dev/null || warn "supervisord not ready yet — try again in 30 s."

echo ""
echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════════════════════════════╗${RESET}"
echo -e "${GREEN}${BOLD}║  Hermes is RUNNING.                                                 ║${RESET}"
echo -e "${GREEN}${BOLD}║  Open Telegram and send /start to your bot.                         ║${RESET}"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════════════════════════════╝${RESET}"
echo ""
echo -e "${BOLD}Install directory:${RESET} ${INSTALL_DIR_ABS}"
echo -e "${BOLD}Secrets file:${RESET}      ${INSTALL_DIR_ABS}/.env  (mode 600)"
echo -e "${BOLD}Data volume:${RESET}       hermes-data (Docker volume — persists across rebuilds)"
echo ""
echo -e "${BOLD}Quick reference:${RESET}"
echo ""
echo "  # Stream live logs"
echo "  docker compose logs -f hermes"
echo ""
echo "  # Restart the bot"
echo "  docker compose restart hermes"
echo ""
echo "  # Rebuild after code update"
echo "  git pull origin main && docker compose up -d --build"
echo "    — or —"
echo "  UPDATE_ONLY=1 bash vps_deploy.sh"
echo ""
echo "  # Check individual agent health"
echo "  docker exec hermes supervisorctl -c /etc/supervisor/conf.d/rhodawk.conf status"
echo ""
echo "  # Back up memory database"
echo "  docker cp hermes:/data/memory.db ./memory_backup_\$(date +%Y%m%d).db"
echo ""
echo "  # systemd service status"
echo "  sudo systemctl status rhodawk-hermes"
echo ""
echo "  # Open shell inside container"
echo "  docker exec -it hermes bash"
echo ""
echo "  # Stop the bot (data preserved)"
echo "  docker compose down"
echo ""
echo "  # Full teardown incl. persistent data (IRREVERSIBLE)"
echo "  docker compose down --volumes"
echo ""
