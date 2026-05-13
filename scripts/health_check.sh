#!/bin/bash
# =============================================================================
# Rhodawk AI - Comprehensive Health Check
# Probes all services and outputs JSON status report.
#
# Exit codes:
#   0 - All services healthy
#   1 - Degraded (some services down, core operational)
#   2 - Critical (core services down)
#
# Copyright (c) 2024-2025 Rhodawk AI. All rights reserved.
# =============================================================================

set -uo pipefail

# -- Configuration -------------------------------------------------------------

REDIS_HOST="${REDIS_HOST:-localhost}"
REDIS_PORT="${REDIS_PORT:-6379}"
CAMOFOX_HOST="${CAMOFOX_HOST:-camofox}"
CAMOFOX_PORT="${CAMOFOX_PORT:-9377}"
WEBHOOK_HOST="${WEBHOOK_HOST:-webhook-receiver}"
WEBHOOK_PORT="${WEBHOOK_PORT:-9000}"
SANDBOX_HOST="${SANDBOX_MANAGER_HOST:-sandbox-manager}"
SANDBOX_PORT="${SANDBOX_MANAGER_PORT:-8090}"

# -- Probe Functions -----------------------------------------------------------

check_hermes_gateway() {
    # Check hermes-gateway via supervisorctl
    if supervisorctl -c /etc/supervisor/conf.d/rhodawk.conf status hermes-gateway 2>/dev/null | grep -q "RUNNING"; then
        echo "healthy"
    else
        echo "unhealthy"
    fi
}

check_redis() {
    # Check Redis connectivity
    if redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" ping 2>/dev/null | grep -q "PONG"; then
        echo "healthy"
    else
        echo "unhealthy"
    fi
}

check_openclaude_grpc() {
    # Check openclaude-grpc via supervisorctl
    if supervisorctl -c /etc/supervisor/conf.d/rhodawk.conf status openclaude-grpc 2>/dev/null | grep -q "RUNNING"; then
        echo "healthy"
    else
        # May be sleeping (non-critical)
        echo "degraded"
    fi
}

check_camofox() {
    # Check camofox health endpoint
    if curl -sf --max-time 5 "http://${CAMOFOX_HOST}:${CAMOFOX_PORT}/health" >/dev/null 2>&1; then
        echo "healthy"
    else
        echo "unhealthy"
    fi
}

check_webhook_server() {
    # Check webhook-receiver health endpoint
    if curl -sf --max-time 5 "http://${WEBHOOK_HOST}:${WEBHOOK_PORT}/health" >/dev/null 2>&1; then
        echo "healthy"
    else
        echo "unhealthy"
    fi
}

check_sandbox_manager() {
    # Check sandbox-manager health endpoint
    if curl -sf --max-time 5 "http://${SANDBOX_HOST}:${SANDBOX_PORT}/health" >/dev/null 2>&1; then
        echo "healthy"
    else
        echo "unhealthy"
    fi
}

check_event_router() {
    # Check event-router via supervisorctl
    if supervisorctl -c /etc/supervisor/conf.d/rhodawk.conf status event-router 2>/dev/null | grep -q "RUNNING"; then
        echo "healthy"
    else
        echo "degraded"
    fi
}

check_task_workers() {
    # Check task-workers via supervisorctl
    if supervisorctl -c /etc/supervisor/conf.d/rhodawk.conf status task-workers 2>/dev/null | grep -q "RUNNING"; then
        echo "healthy"
    else
        echo "degraded"
    fi
}

# -- Main Health Check ---------------------------------------------------------

main() {
    local timestamp
    timestamp="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

    # Run all probes
    local hermes_status
    hermes_status=$(check_hermes_gateway)
    local redis_status
    redis_status=$(check_redis)
    local openclaude_status
    openclaude_status=$(check_openclaude_grpc)
    local camofox_status
    camofox_status=$(check_camofox)
    local webhook_status
    webhook_status=$(check_webhook_server)
    local sandbox_status
    sandbox_status=$(check_sandbox_manager)
    local event_router_status
    event_router_status=$(check_event_router)
    local task_workers_status
    task_workers_status=$(check_task_workers)

    # Determine overall status
    local overall="healthy"
    local unhealthy_count=0
    local critical_down=false

    # Core services (hermes-gateway + redis) are critical
    if [ "$hermes_status" = "unhealthy" ] || [ "$redis_status" = "unhealthy" ]; then
        critical_down=true
    fi

    for status in "$hermes_status" "$redis_status" "$openclaude_status" "$camofox_status" "$webhook_status" "$sandbox_status" "$event_router_status" "$task_workers_status"; do
        if [ "$status" = "unhealthy" ]; then
            unhealthy_count=$((unhealthy_count + 1))
        fi
    done

    if [ "$critical_down" = true ]; then
        overall="critical"
    elif [ $unhealthy_count -gt 0 ]; then
        overall="degraded"
    fi

    # Output JSON report
    cat <<EOF
{
  "status": "${overall}",
  "timestamp": "${timestamp}",
  "services": {
    "hermes-gateway": "${hermes_status}",
    "redis": "${redis_status}",
    "openclaude-grpc": "${openclaude_status}",
    "camofox": "${camofox_status}",
    "webhook-receiver": "${webhook_status}",
    "sandbox-manager": "${sandbox_status}",
    "event-router": "${event_router_status}",
    "task-workers": "${task_workers_status}"
  },
  "summary": {
    "total": 8,
    "healthy": $((8 - unhealthy_count)),
    "unhealthy": ${unhealthy_count}
  }
}
EOF

    # Set exit code
    if [ "$overall" = "critical" ]; then
        exit 2
    elif [ "$overall" = "degraded" ]; then
        exit 1
    fi
    exit 0
}

main "$@"
