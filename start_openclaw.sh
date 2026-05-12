#!/bin/bash
# start_openclaw.sh — OpenClaw gateway launcher
# Non-critical: sleeps gracefully if binary missing or unconfigured.
#
# FIX: Added --port 18789 to match supervisord.conf and the port that
#      hermes-agent + health-check probe. Without it, openclaw binds to
#      its default port (varies by version) instead of the expected 18789.

echo "[openclaw] Starting multi-platform gateway on :18789 (DO Inference primary)..."

if ! command -v openclaw &>/dev/null; then
    echo "[openclaw] Binary not found — sleeping (non-critical)"
    exec sleep infinity
fi

exec openclaw gateway --port 18789 --allow-unconfigured
