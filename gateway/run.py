#!/usr/bin/env python3
"""
gateway/run.py — Hermes Gateway

Routes all messaging and agent work through hermes-agent (NousResearch/hermes-agent).
Falls back to openclaw (openclaw/openclaw) for multi-channel delivery.

hermes-agent provides natively:
  - Telegram, Discord, Slack, WhatsApp, Signal, iMessage, Email channels
  - Full agent loop: tool calling, 40+ tools, MCP, subagent spawning
  - Persistent memory, skill learning, cron scheduling
  - Multi-provider model routing with failover (200+ models)

openclaw provides natively:
  - 20+ channels: WhatsApp, Telegram, Slack, Discord, Signal, iMessage, ...
  - Local-first Gateway with session routing and DM pairing
  - Real-time Canvas, Voice Wake, Talk Mode

Install hermes-agent:
  pip3 install 'hermes-agent[messaging,pty,mcp,acp]'
  hermes setup        # interactive wizard — Telegram token, model, memory
  hermes gateway      # start messaging gateway

Install openclaw:
  npm install -g openclaw@latest
  openclaw onboard --install-daemon
  openclaw gateway --port 18789
"""

import os
import shutil
import sys

HERMES_HOME  = os.environ.get("HERMES_HOME", "/data/.hermes")
GATEWAY_YAML = os.environ.get(
    "HERMES_GATEWAY_CONFIG",
    os.path.join(HERMES_HOME, "gateway.yaml"),
)


def _try_hermes() -> None:
    """
    Start hermes-agent's messaging gateway.
    execvpe replaces this process — only returns if the binary is not found.
    """
    binary = shutil.which("hermes") or shutil.which("hermes-agent")
    if not binary:
        return
    cmd = [binary, "gateway"]
    if os.path.exists(GATEWAY_YAML):
        cmd += ["--config", GATEWAY_YAML]
    print(f"[gateway] starting hermes gateway: {' '.join(cmd)}", flush=True)
    os.execvpe(binary, cmd, os.environ)


def _try_openclaw() -> None:
    """
    Start openclaw's messaging gateway as fallback.
    execvpe replaces this process — only returns if the binary is not found.
    """
    binary = shutil.which("openclaw")
    if not binary:
        return
    port = os.environ.get("OPENCLAW_PORT", "18789")
    cmd  = [binary, "gateway", "--port", port, "--verbose"]
    print(f"[gateway] starting openclaw gateway: {' '.join(cmd)}", flush=True)
    os.execvpe(binary, cmd, os.environ)


def main() -> None:
    print(f"[gateway] HERMES_HOME={HERMES_HOME}", flush=True)

    _try_hermes()    # execvpe replaces this process if binary exists; only continues on missing
    _try_openclaw()  # same

    print(
        "\n[gateway] FATAL: Neither hermes-agent nor openclaw is installed.\n"
        "\n"
        "  Option A — hermes-agent (full agent loop, recommended):\n"
        "    pip3 install 'hermes-agent[messaging,pty,mcp,acp]'\n"
        "    hermes setup\n"
        "\n"
        "  Option B — openclaw (multi-channel relay):\n"
        "    npm install -g openclaw@latest\n"
        "    openclaw onboard --install-daemon\n",
        file=sys.stderr,
        flush=True,
    )
    sys.exit(1)


if __name__ == "__main__":
    main()
