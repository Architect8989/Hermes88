#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# start_openclaw.sh — Start OpenClaw multi-platform gateway
# Source: https://github.com/openclaw/openclaw
# Role:   Delivers messages to WhatsApp, Discord, Slack, Signal and 20+ more
#         (Telegram is handled by hermes-agent natively)
# LLM:    DigitalOcean Inference (DeepSeek R1), fallback NVIDIA NIM
# ─────────────────────────────────────────────────────────────────────────────

if ! command -v openclaw &>/dev/null; then
    echo "[openclaw] Binary not in PATH — install may have failed (non-critical)."
    echo "[openclaw] Telegram delivery is handled by hermes-agent natively."
    sleep infinity
fi

echo "[openclaw] Starting multi-platform gateway (DO Inference primary)..."
exec openclaw gateway --allow-unconfigured 2>&1