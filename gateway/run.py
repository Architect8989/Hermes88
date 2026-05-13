#!/usr/bin/env python3
"""
gateway/run.py — Hermes Gateway dispatcher.

ARCHITECTURAL CONSTRAINT — READ BEFORE MODIFYING:
  hermes-agent and openclaw are NOT interchangeable fallbacks.
  They are competing gateway operating systems. Both own:
    - the Telegram bot token (long-poll ownership)
    - the agentic loop (who decides what to do with a message)
    - channel routing and session state

  Running one as a "fallback" for the other means two processes
  competing for the same bot token. Telegram will reject the
  second poller's getUpdates calls. You will get duplicate sends,
  dropped messages, and split session state.

  THE CORRECT MODEL: pick exactly one. The other must not be installed
  in the same container.

PICK ONE and set HERMES_GATEWAY:

  Option A — hermes-agent (Python, pip-installable):
    pip3 install 'hermes-agent[messaging,pty,mcp,acp]'
    hermes setup          # wizard: Telegram token, model, memory config
    hermes gateway        # starts gateway; owns the agentic loop
    export HERMES_GATEWAY=hermes

  Option B — openclaw (Node.js, npm-installable):
    npm install -g openclaw@latest
    openclaw onboard --install-daemon
    openclaw gateway --port 18789
    export HERMES_GATEWAY=openclaw

  If you choose openclaw: delete gateway/run.py and bot/telegram_bot.py.
  openclaw is the control plane — it does not dock to a Python agent.

  If you choose hermes: remove openclaw from your Dockerfile and
  docker-compose.yml entirely.
"""

import os
import shutil
import sys

HERMES_HOME  = os.environ.get("HERMES_HOME", "/data/.hermes")
GATEWAY_YAML = os.environ.get(
    "HERMES_GATEWAY_CONFIG",
    os.path.join(HERMES_HOME, "gateway.yaml"),
)

_GATEWAY = os.environ.get("HERMES_GATEWAY", "").strip().lower()

if not _GATEWAY:
    print(
        "\n[gateway] FATAL: HERMES_GATEWAY is not set.\n"
        "\n"
        "  hermes-agent and openclaw cannot coexist in the same container.\n"
        "  Both own the Telegram bot token and the agentic loop.\n"
        "  You must pick one:\n"
        "\n"
        "    export HERMES_GATEWAY=hermes    # use hermes-agent\n"
        "    export HERMES_GATEWAY=openclaw  # use openclaw\n"
        "\n"
        "  See gateway/run.py for the full explanation.",
        file=sys.stderr,
        flush=True,
    )
    sys.exit(1)

if _GATEWAY not in ("hermes", "openclaw"):
    print(
        f"\n[gateway] FATAL: HERMES_GATEWAY={_GATEWAY!r} is not valid.\n"
        "  Accepted values: hermes | openclaw\n",
        file=sys.stderr,
        flush=True,
    )
    sys.exit(1)


def _start_hermes() -> None:
    """
    Replace this process with hermes-agent's gateway.
    os.execvpe never returns on success.
    """
    binary = shutil.which("hermes") or shutil.which("hermes-agent")
    if not binary:
        print(
            "\n[gateway] FATAL: HERMES_GATEWAY=hermes but the hermes binary is not installed.\n"
            "\n"
            "  Install: pip3 install 'hermes-agent[messaging,pty,mcp,acp]'\n"
            "  Setup:   hermes setup\n"
            "\n"
            "  Do not set HERMES_GATEWAY=hermes unless hermes-agent is installed.\n",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)

    cmd = [binary, "gateway"]
    if os.path.exists(GATEWAY_YAML):
        cmd += ["--config", GATEWAY_YAML]

    print(f"[gateway] exec: {' '.join(cmd)}", flush=True)
    os.execvpe(binary, cmd, os.environ)
    # unreachable


def _start_openclaw() -> None:
    """
    Replace this process with openclaw's gateway.
    os.execvpe never returns on success.

    Note: if HERMES_GATEWAY=openclaw, this file should not exist at all.
    openclaw is a gateway OS — it does not run under a Python dispatcher.
    The correct setup is to remove gateway/run.py and point supervisord
    directly at the openclaw binary.
    """
    binary = shutil.which("openclaw")
    if not binary:
        print(
            "\n[gateway] FATAL: HERMES_GATEWAY=openclaw but openclaw is not installed.\n"
            "\n"
            "  Install: npm install -g openclaw@latest\n"
            "  Setup:   openclaw onboard --install-daemon\n"
            "\n"
            "  Do not set HERMES_GATEWAY=openclaw unless openclaw is installed.\n",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)

    port = os.environ.get("OPENCLAW_PORT", "18789")
    cmd  = [binary, "gateway", "--port", port, "--verbose"]

    print(f"[gateway] exec: {' '.join(cmd)}", flush=True)
    os.execvpe(binary, cmd, os.environ)
    # unreachable


def main() -> None:
    print(f"[gateway] HERMES_HOME={HERMES_HOME}", flush=True)
    print(f"[gateway] HERMES_GATEWAY={_GATEWAY}", flush=True)

    if _GATEWAY == "hermes":
        _start_hermes()
    else:
        _start_openclaw()

    # os.execvpe above never returns — reaching here means exec failed
    print("[gateway] FATAL: os.execvpe failed unexpectedly.", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
