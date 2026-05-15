# =============================================================================
# Rhodawk AI — Hermes Code Stabilizer
# Makefile — short aliases for common operations
#
# Usage:
#   make deploy     — first-time build + start
#   make up         — start (no rebuild)
#   make update     — pull latest code + rebuild + restart
#   make restart    — restart the bot process without rebuilding
#   make logs       — stream live logs (Ctrl+C to stop)
#   make status     — show container + agent process health
#   make backup     — back up the SQLite memory database
#   make shell      — open a bash shell inside the running container
#   make stop       — stop the container (data preserved)
#   make clean      — remove container + image (data preserved in volume)
#   make destroy    — remove everything including persistent data (IRREVERSIBLE)
# =============================================================================

.PHONY: deploy up update restart logs status backup shell stop clean destroy \
        check-env help

CONTAINER  := hermes
COMPOSE    := docker compose
BACKUP_DIR := ./backups

# ── Default target — show help ─────────────────────────────────────────────────
help:
        @echo ""
        @echo "  Rhodawk AI — Hermes Code Stabilizer"
        @echo ""
        @echo "  make deploy     Build image and start the bot (first run)"
        @echo "  make up         Start without rebuilding"
        @echo "  make update     Pull latest code, rebuild, restart"
        @echo "  make restart    Restart bot process (no image rebuild)"
        @echo "  make logs       Stream live logs  (Ctrl+C to stop)"
        @echo "  make status     Container health + supervisord agent status"
        @echo "  make backup     Back up SQLite memory DB to ./backups/"
        @echo "  make shell      Open bash inside the running container"
        @echo "  make stop       Stop the container  (data preserved)"
        @echo "  make clean      Remove container + image  (data preserved)"
        @echo "  make destroy    !! Remove everything including volume data !!"
        @echo ""

# ── check-env — verify .env exists and has required secrets ───────────────────
check-env:
        @if [ ! -f .env ]; then \
                echo "[error] .env not found. Run: cp .env.example .env && nano .env"; \
                exit 1; \
        fi
        @for key in TELEGRAM_BOT_TOKEN DO_INFERENCE_API_KEY GITHUB_PAT; do \
                val=$$(grep -E "^$${key}=" .env | cut -d= -f2- | tr -d '[:space:]'); \
                if [ -z "$$val" ]; then \
                        echo "[error] $$key is missing or empty in .env"; \
                        exit 1; \
                fi; \
        done
        @echo "[ok] .env looks good."

# ── deploy — build image and start (first-time setup) ─────────────────────────
deploy: check-env
        $(COMPOSE) up -d --build
        @echo ""
        @echo "[ok] Hermes is starting. Waiting for health check..."
        @for i in $$(seq 1 18); do \
                status=$$(docker inspect --format='{{.State.Health.Status}}' $(CONTAINER) 2>/dev/null || echo "starting"); \
                if [ "$$status" = "healthy" ]; then \
                        echo "[ok] hermes-gateway is HEALTHY. Send /start to your bot on Telegram."; \
                        break; \
                fi; \
                echo "  Waiting... [$$i/18] status=$$status"; \
                sleep 5; \
        done

# ── up — start without rebuilding ─────────────────────────────────────────────
up: check-env
        $(COMPOSE) up -d
        @echo "[ok] Container started. Run 'make logs' to stream output."

# ── update — pull latest, rebuild, restart ────────────────────────────────────
update:
        @echo "[update] Pulling latest code..."
        git pull origin main
        @echo "[update] Rebuilding image..."
        $(COMPOSE) build --pull
        @echo "[update] Restarting container..."
        $(COMPOSE) up -d
        @echo "[ok] Update complete. Run 'make logs' to confirm."

# ── restart — restart the bot without rebuilding the image ────────────────────
restart:
        $(COMPOSE) restart $(CONTAINER)
        @echo "[ok] Container restarted."

# ── logs — stream all container logs ──────────────────────────────────────────
logs:
        $(COMPOSE) logs -f $(CONTAINER)

# ── status — container + supervisord agent process health ─────────────────────
status:
        @echo ""
        @echo "── Container ──────────────────────────────────────────────────────"
        @$(COMPOSE) ps
        @echo ""
        @echo "── Supervisord agent processes ────────────────────────────────────"
        @docker exec $(CONTAINER) \
                supervisorctl -c /etc/supervisor/conf.d/rhodawk.conf status 2>/dev/null \
                || echo "[warn] Container is not running."
        @echo ""
        @echo "── Health check detail ────────────────────────────────────────────"
        @docker inspect --format='HealthStatus: {{.State.Health.Status}}' $(CONTAINER) 2>/dev/null \
                || echo "[warn] Container is not running."
        @echo ""

# ── backup — copy SQLite DB out of the container ──────────────────────────────
backup:
        @mkdir -p $(BACKUP_DIR)
        $(eval STAMP := $(shell date +%Y%m%d_%H%M%S))
        docker cp $(CONTAINER):/data/memory.db $(BACKUP_DIR)/memory_$(STAMP).db
        @echo "[ok] Backed up to $(BACKUP_DIR)/memory_$(STAMP).db"
        @ls -lh $(BACKUP_DIR)/memory_*.db | tail -5

# ── shell — interactive bash inside the running container ─────────────────────
shell:
        docker exec -it $(CONTAINER) bash

# ── stop — stop the container, preserve data ──────────────────────────────────
stop:
        $(COMPOSE) down
        @echo "[ok] Stopped. Data volume preserved."

# ── clean — remove container + image, preserve data volume ────────────────────
clean: stop
        docker rmi rhodawk-hermes 2>/dev/null || true
        @echo "[ok] Container and image removed. Volume 'hermes-data' preserved."
        @echo "     Rebuild with: make deploy"

# ── destroy — full teardown including persistent volume data ──────────────────
destroy:
        @echo ""
        @echo "  WARNING: This permanently deletes the container, image, AND all"
        @echo "  persistent data (memory DB, sessions, logs) in the hermes-data volume."
        @echo ""
        @read -p "  Type DESTROY to confirm: " confirm; \
        if [ "$$confirm" = "DESTROY" ]; then \
                $(COMPOSE) down --volumes; \
                docker rmi rhodawk-hermes 2>/dev/null || true; \
                echo "[ok] Full teardown complete."; \
        else \
                echo "[cancelled] Nothing was deleted."; \
        fi
