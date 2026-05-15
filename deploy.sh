#!/usr/bin/env bash
# =============================================================================
# Rhodawk AI — Hermes Code Stabilizer
# One-command VPS installer
#
# Usage (fresh VPS, nothing installed):
#   curl -fsSL https://huggingface.co/spaces/Architect8999/Hermes/raw/main/deploy.sh | bash
#
# Or if you've already cloned the repo:
#   bash deploy.sh
#
# What this does:
#   1. Checks OS compatibility (Ubuntu/Debian required)
#   2. Installs Docker + Docker Compose if not present
#   3. Clones the repo (or uses the current directory if already inside it)
#   4. Prompts you to enter each required secret interactively
#   5. Writes the .env file (never echoes secret values to terminal)
#   6. Builds the Docker image and starts the container
#   7. Streams startup logs and confirms the bot is healthy
# =============================================================================

set -euo pipefail

# ── Colours ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()  { echo -e "${CYAN}[info]${RESET}  $*"; }
ok()    { echo -e "${GREEN}[ok]${RESET}    $*"; }
warn()  { echo -e "${YELLOW}[warn]${RESET}  $*"; }
fatal() { echo -e "${RED}[fatal]${RESET} $*" >&2; exit 1; }

# ── Banner ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}║  Rhodawk AI — Hermes Code Stabilizer  VPS Installer             ║${RESET}"
echo -e "${BOLD}║  Agents: hermes-agent │ openclaude │ openclaw │ jcode            ║${RESET}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════════════════╝${RESET}"
echo ""

# ── OS check ───────────────────────────────────────────────────────────────────
if [[ ! -f /etc/os-release ]]; then
    fatal "Cannot detect OS. This script requires Ubuntu 20.04+ or Debian 11+."
fi
source /etc/os-release
if [[ "$ID" != "ubuntu" && "$ID" != "debian" && "$ID_LIKE" != *"debian"* ]]; then
    fatal "Unsupported OS: $PRETTY_NAME. Ubuntu 22.04 LTS recommended."
fi
info "Detected OS: $PRETTY_NAME"

# ── Root / sudo check ──────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    SUDO="sudo"
    info "Running as non-root — will use sudo for system installs."
else
    SUDO=""
    warn "Running as root. Consider creating a non-root user for production."
fi

# ── Docker install ─────────────────────────────────────────────────────────────
install_docker() {
    info "Installing Docker..."
    $SUDO apt-get update -qq
    $SUDO apt-get install -y -qq ca-certificates curl gnupg lsb-release
    $SUDO install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | $SUDO gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    $SUDO chmod a+r /etc/apt/keyrings/docker.gpg
    echo \
        "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
        https://download.docker.com/linux/${ID} \
        $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        | $SUDO tee /etc/apt/sources.list.d/docker.list > /dev/null
    $SUDO apt-get update -qq
    $SUDO apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin
    $SUDO systemctl enable docker --now
    # Add current user to docker group (takes effect on next login)
    if [[ -n "${SUDO_USER:-}" ]]; then
        $SUDO usermod -aG docker "$SUDO_USER"
        warn "Added $SUDO_USER to the 'docker' group. Log out and back in to use docker without sudo."
    fi
    ok "Docker installed: $(docker --version)"
}

if command -v docker &>/dev/null; then
    ok "Docker already installed: $(docker --version)"
else
    install_docker
fi

# Verify docker compose is available (v2 plugin style)
if ! docker compose version &>/dev/null; then
    fatal "docker compose plugin not found. Re-run the installer or install manually: https://docs.docker.com/compose/install/"
fi
ok "Docker Compose: $(docker compose version --short)"

# ── Repo setup ─────────────────────────────────────────────────────────────────
REPO_URL="https://huggingface.co/spaces/Architect8999/Hermes"
INSTALL_DIR="$HOME/hermes"

# Detect if we're already inside the cloned repo
if [[ -f "$(pwd)/Dockerfile.vps" && -f "$(pwd)/docker-compose.yml" ]]; then
    INSTALL_DIR="$(pwd)"
    info "Already inside the repo at $INSTALL_DIR — skipping clone."
elif [[ -d "$INSTALL_DIR/.git" ]]; then
    info "Repo already cloned at $INSTALL_DIR — pulling latest..."
    git -C "$INSTALL_DIR" pull origin main
    ok "Repo up to date."
else
    info "Cloning repo to $INSTALL_DIR..."
    git clone "$REPO_URL" "$INSTALL_DIR"
    ok "Cloned to $INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# ── Secret collection ──────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}── Configure secrets ──────────────────────────────────────────────${RESET}"
echo "  Enter each value below. Input is hidden. Press Enter to skip optional keys."
echo ""

read_secret() {
    local prompt="$1"
    local var_name="$2"
    local required="${3:-yes}"
    local value=""

    while true; do
        read -rsp "  $prompt: " value
        echo ""
        if [[ -z "$value" ]]; then
            if [[ "$required" == "yes" ]]; then
                echo -e "  ${RED}Required — cannot be empty. Try again.${RESET}"
            else
                echo -e "  ${YELLOW}Skipped (optional).${RESET}"
                break
            fi
        else
            ok "$var_name set."
            break
        fi
    done
    # Export to caller via global
    printf -v "$var_name" '%s' "$value"
}

# If .env already exists, ask whether to overwrite
if [[ -f .env ]]; then
    echo -e "${YELLOW}[warn]${RESET}  .env already exists."
    read -rp "  Overwrite with new values? [y/N] " OVERWRITE
    if [[ "${OVERWRITE,,}" != "y" ]]; then
        info "Keeping existing .env. Proceeding with current secrets."
        SKIP_SECRETS=true
    fi
fi

if [[ "${SKIP_SECRETS:-false}" != "true" ]]; then
    echo ""
    echo "  Required secrets (all 3 must be set):"
    echo ""

    read_secret "TELEGRAM_BOT_TOKEN  (from @BotFather on Telegram)" TELEGRAM_BOT_TOKEN "yes"
    read_secret "DO_INFERENCE_API_KEY (from cloud.digitalocean.com/gen-ai)" DO_INFERENCE_API_KEY "yes"
    read_secret "GITHUB_PAT          (repo + workflow scopes, from github.com/settings/tokens)" GITHUB_PAT "yes"

    echo ""
    echo "  Optional secrets:"
    echo ""
    read_secret "HF_TOKEN            (write token from huggingface.co/settings/tokens — skip if not pushing to HF)" HF_TOKEN "no"

    # Write .env
    {
        echo "# Rhodawk AI — Hermes secrets"
        echo "# Generated by deploy.sh on $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
        echo ""
        echo "TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}"
        echo "DO_INFERENCE_API_KEY=${DO_INFERENCE_API_KEY}"
        echo "GITHUB_PAT=${GITHUB_PAT}"
        if [[ -n "${HF_TOKEN:-}" ]]; then
            echo "HF_TOKEN=${HF_TOKEN}"
        fi
    } > .env
    chmod 600 .env
    ok ".env written (mode 600 — readable only by current user)."
fi

# ── Validate .env before build ─────────────────────────────────────────────────
echo ""
info "Validating .env..."
MISSING=()
for KEY in TELEGRAM_BOT_TOKEN DO_INFERENCE_API_KEY GITHUB_PAT; do
    val=$(grep -E "^${KEY}=" .env 2>/dev/null | cut -d= -f2- | tr -d '[:space:]')
    if [[ -z "$val" ]]; then
        MISSING+=("$KEY")
    fi
done

if [[ ${#MISSING[@]} -gt 0 ]]; then
    fatal "Missing required secrets in .env: ${MISSING[*]}\nEdit .env manually and re-run: bash deploy.sh"
fi
ok "All required secrets present."

# ── Build & start ──────────────────────────────────────────────────────────────
echo ""
info "Building Docker image (this takes 3–8 minutes on first run)..."
docker compose build --progress=plain 2>&1 | grep -E "(Step|RUN|COPY|FROM|---> |Successfully|error|Error)" || true

echo ""
info "Starting container..."
docker compose up -d

echo ""
info "Waiting for hermes-gateway to come online (up to 90s)..."
for i in $(seq 1 18); do
    STATUS=$(docker inspect --format='{{.State.Health.Status}}' hermes 2>/dev/null || echo "starting")
    if [[ "$STATUS" == "healthy" ]]; then
        ok "hermes-gateway is HEALTHY."
        break
    fi
    if [[ "$STATUS" == "unhealthy" ]]; then
        warn "Container reports unhealthy. Check logs:"
        docker compose logs --tail=40 hermes
        fatal "Startup failed. Fix the error above, then re-run: bash deploy.sh"
    fi
    echo -ne "  Waiting... [${i}/18] status=${STATUS}\r"
    sleep 5
done

# ── Final status ───────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}── Deployment complete ────────────────────────────────────────────${RESET}"
docker compose ps
echo ""
echo -e "${BOLD}Useful commands:${RESET}"
echo "  docker compose logs -f hermes          — stream live logs"
echo "  docker compose restart hermes          — restart the bot"
echo "  docker compose down                    — stop the bot"
echo "  docker compose up -d --build           — rebuild after git pull"
echo "  docker exec hermes supervisorctl \\
    -c /etc/supervisor/conf.d/rhodawk.conf \\
    status                                     — check agent processes"
echo ""
echo -e "${GREEN}${BOLD}Hermes is running. Open Telegram and send your bot /start.${RESET}"
echo ""
