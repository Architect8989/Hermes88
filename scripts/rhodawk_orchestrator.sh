#!/bin/bash
# =============================================================================
# Rhodawk AI - Peak Orchestrator
# Master orchestration script for initializing and verifying all services.
#
# Called by init_and_start.sh after environment setup is complete.
# Performs: Redis check, memory store init, vault init, background services.
#
# Copyright (c) 2024-2025 Rhodawk AI. All rights reserved.
# =============================================================================

set -euo pipefail

# -- Configuration -------------------------------------------------------------

REDIS_URL="${REDIS_URL:-redis://localhost:6379/0}"
REDIS_HOST="${REDIS_HOST:-localhost}"
REDIS_PORT="${REDIS_PORT:-6379}"
MEMORY_DB="${MEMORY_DB_PATH:-/data/.hermes/memory.db}"
VAULT_PATH="${OBSIDIAN_VAULT_PATH:-/data/.hermes/obsidian-vault}"
LOG_DIR="/data/.hermes/logs"
MAX_REDIS_RETRIES=30
RETRY_INTERVAL=2

# -- Functions -----------------------------------------------------------------

log_info() {
    echo "[rhodawk-orchestrator] $(date '+%Y-%m-%d %H:%M:%S') INFO: $*"
}

log_warn() {
    echo "[rhodawk-orchestrator] $(date '+%Y-%m-%d %H:%M:%S') WARN: $*" >&2
}

log_error() {
    echo "[rhodawk-orchestrator] $(date '+%Y-%m-%d %H:%M:%S') ERROR: $*" >&2
}

# -- Step 1: Check Redis Connectivity -----------------------------------------

check_redis() {
    log_info "Checking Redis connectivity at ${REDIS_HOST}:${REDIS_PORT}..."
    local retries=0

    while [ $retries -lt $MAX_REDIS_RETRIES ]; do
        if redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" ping 2>/dev/null | grep -q "PONG"; then
            log_info "Redis is reachable (PONG received)"
            return 0
        fi
        retries=$((retries + 1))
        log_warn "Redis not ready (attempt ${retries}/${MAX_REDIS_RETRIES}), retrying in ${RETRY_INTERVAL}s..."
        sleep "$RETRY_INTERVAL"
    done

    log_error "Redis not reachable after ${MAX_REDIS_RETRIES} attempts"
    return 1
}

# -- Step 2: Initialize Memory Store ------------------------------------------

init_memory_store() {
    log_info "Initializing memory store at ${MEMORY_DB}..."
    local db_dir
    db_dir="$(dirname "$MEMORY_DB")"
    mkdir -p "$db_dir"

    if [ ! -f "$MEMORY_DB" ]; then
        log_info "Creating new SQLite memory database..."
        python3 -c "
from rhodawk_core.memory import StructuredMemoryStore
store = StructuredMemoryStore(db_path='${MEMORY_DB}')
print('[memory] Database initialized with schema')
" 2>/dev/null || log_warn "Memory store initialization via Python failed (non-critical)"
    else
        log_info "Memory database already exists ($(du -h "$MEMORY_DB" | cut -f1))"
    fi
}

# -- Step 3: Initialize Obsidian Vault ----------------------------------------

init_vault() {
    log_info "Initializing Obsidian vault at ${VAULT_PATH}..."
    mkdir -p "${VAULT_PATH}/daily"
    mkdir -p "${VAULT_PATH}/projects"
    mkdir -p "${VAULT_PATH}/research"
    mkdir -p "${VAULT_PATH}/decisions"
    mkdir -p "${VAULT_PATH}/templates"
    mkdir -p "${VAULT_PATH}/people"
    mkdir -p "${VAULT_PATH}/systems"

    # Create .obsidian config directory if it does not exist
    if [ ! -d "${VAULT_PATH}/.obsidian" ]; then
        mkdir -p "${VAULT_PATH}/.obsidian"
        echo '{"dailyNoteFolder":"daily","templateFolder":"templates"}' \
            > "${VAULT_PATH}/.obsidian/daily-notes.json"
        log_info "Obsidian vault config created"
    fi
    log_info "Vault directory structure ready"
}

# -- Step 4: Start Event Consumer (background) --------------------------------

start_event_consumer() {
    log_info "Event consumer will be managed by supervisord (event-router program)"
}

# -- Step 5: Start Task Workers (background) -----------------------------------

start_task_workers() {
    log_info "Task workers will be managed by supervisord (task-workers program)"
}

# -- Step 6: Start Proactive Scanner (background) -----------------------------

start_proactive_scanner() {
    log_info "Proactive scanner is part of hermes-gateway (built-in cron triggers)"
}

# -- Step 7: Health Check All Services ----------------------------------------

health_check_services() {
    log_info "Running initial health check..."
    local healthy=0
    local total=0

    # Check supervisord is running
    total=$((total + 1))
    if supervisorctl -c /etc/supervisor/conf.d/rhodawk.conf status >/dev/null 2>&1; then
        healthy=$((healthy + 1))
        log_info "  supervisord: OK"
    else
        log_warn "  supervisord: not yet started (expected at this stage)"
    fi

    # Check Redis
    total=$((total + 1))
    if redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" ping 2>/dev/null | grep -q "PONG"; then
        healthy=$((healthy + 1))
        log_info "  redis: OK"
    else
        log_warn "  redis: not reachable"
    fi

    log_info "Health check: ${healthy}/${total} services OK"
}

# -- Step 8: Print Startup Banner ----------------------------------------------

print_banner() {
    echo ""
    echo "============================================================"
    echo "  Rhodawk AI - Hermes88 Peak Architecture"
    echo "  Status: INITIALIZED"
    echo "  Time:   $(date '+%Y-%m-%d %H:%M:%S %Z')"
    echo "============================================================"
    echo "  Gateway:    hermes-gateway (supervisord)"
    echo "  Events:     event-router (Redis PubSub)"
    echo "  Tasks:      task-workers (Redis sorted set)"
    echo "  Coding:     openclaude-grpc (:50051)"
    echo "  Scaffold:   jcode-server (:7865)"
    echo "  Memory:     ${MEMORY_DB}"
    echo "  Vault:      ${VAULT_PATH}"
    echo "  Redis:      ${REDIS_HOST}:${REDIS_PORT}"
    echo "============================================================"
    echo ""
}

# -- Main Execution ------------------------------------------------------------

main() {
    log_info "Starting Rhodawk Peak Orchestrator..."
    mkdir -p "$LOG_DIR"

    check_redis || log_warn "Continuing without Redis (degraded mode)"
    init_memory_store
    init_vault
    start_event_consumer
    start_task_workers
    start_proactive_scanner
    health_check_services
    print_banner

    log_info "Orchestrator initialization complete"
}

main "$@"
