#!/usr/bin/env python3
"""
main.py — Rhodawk AI Hermes Gateway Entry Point

24/7 long-running gateway service. Starts hermes-agent with the configured
gateway.yaml. This is the canonical entry point — it does NOT require any
--cmd flags to stay alive. Supervisor (supervisord.conf) calls this process,
but it can also be run directly:

    python3 main.py                    # standard start
    HERMES_HOME=/data/.hermes python3 main.py

Resolves the logic conflict where telegram_bot.py was being treated as both
a utility script AND a long-running service. This file is exclusively the
long-running service. telegram_bot.py is exclusively a utility script.

Module resolution order for hermes-agent:
  1. `hermes-agent` CLI binary      (preferred — installed by pip as entry_point)
  2. `python3 -m gateway.run`       (hermes-agent's own gateway module fallback)
  3. `python3 -m gateway.run`       (built-in fallback — always present in package)
"""

import os
import subprocess
import sys
import time
from pathlib import Path


RETRY_DELAY_SECONDS = 5
MAX_CONSECUTIVE_CRASHES = 10


def _hermes_home() -> str:
    return os.environ.get("HERMES_HOME", "/data/.hermes")


def _gateway_config() -> str:
    return os.environ.get(
        "HERMES_GATEWAY_CONFIG",
        os.path.join(_hermes_home(), "gateway.yaml"),
    )


def _build_env() -> dict:
    env = dict(os.environ)
    env["HERMES_HOME"] = _hermes_home()
    env["HERMES_GATEWAY_CONFIG"] = _gateway_config()
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def _start_with_cli(env: dict) -> int:
    """Start hermes-agent via its installed CLI entry point."""
    return subprocess.run(["hermes-agent"], env=env).returncode


def _start_with_module(env: dict) -> int:
    """Start hermes-agent via python -m gateway.run (hermes-agent's own gateway module)."""
    return subprocess.run(
        [sys.executable, "-m", "gateway.run"], env=env
    ).returncode


def _start_with_gateway(env: dict) -> int:
    """
    Start the built-in Telegram gateway (gateway/run.py).
    This is the self-contained fallback when hermes-agent is not installed.
    gateway/run.py is the production-ready gateway: python-telegram-bot polling
    loop + AsyncOpenAI calling DO Inference, file-delivery via %%FILE:%% tags,
    chat-ID whitelist, and per-user conversation history.
    """
    return subprocess.run(
        [sys.executable, "-m", "gateway.run"], env=env
    ).returncode


def _verify_config() -> None:
    cfg = _gateway_config()
    if not Path(cfg).exists():
        print(
            f"[main] WARNING: gateway config not found at {cfg}. "
            "Run init_and_start.sh first, or set HERMES_GATEWAY_CONFIG.",
            file=sys.stderr,
        )
    else:
        print(f"[main] Gateway config: {cfg}")


def main() -> None:
    print(f"[main] Rhodawk AI — Hermes Gateway starting (HERMES_HOME={_hermes_home()})")
    _verify_config()

    env = _build_env()
    crashes = 0

    # Detect which start method is available — try in priority order:
    #   1. hermes-agent CLI   (full autonomous agent — terminal tool, skills, memory, cron)
    #   2. python3 -m gateway.run  (hermes-agent gateway module — same package)
    #   3. python -m gateway.run   (built-in gateway fallback — always available)
    import shutil
    if shutil.which("hermes-agent"):
        start_fn = _start_with_cli
        method_name = "hermes-agent CLI"
    else:
        try:
            import importlib
            importlib.import_module("hermes_cli")
            start_fn = _start_with_module
            method_name = "python -m gateway.run"
        except ImportError:
            # hermes-agent not installed — fall back to the built-in gateway.
            # gateway/run.py is production-ready: python-telegram-bot + openai + DO Inference.
            print(
                "[main] hermes-agent not installed — falling back to built-in gateway.run. "
                "Install hermes-agent to enable the terminal tool, skills, memory and cron.",
                file=sys.stderr,
            )
            start_fn = _start_with_gateway
            method_name = "python -m gateway.run (built-in fallback)"

    print(f"[main] Using start method: {method_name}")

    while True:
        try:
            rc = start_fn(env)
            if rc == 0:
                print("[main] hermes-agent exited cleanly.")
                break
            crashes += 1
            print(
                f"[main] hermes-agent exited with code {rc} "
                f"(crash {crashes}/{MAX_CONSECUTIVE_CRASHES}) — "
                f"restarting in {RETRY_DELAY_SECONDS}s",
                file=sys.stderr,
            )
            if crashes >= MAX_CONSECUTIVE_CRASHES:
                print(
                    f"[main] FATAL: {MAX_CONSECUTIVE_CRASHES} consecutive crashes — aborting.",
                    file=sys.stderr,
                )
                sys.exit(1)
            time.sleep(RETRY_DELAY_SECONDS)

        except FileNotFoundError:
            print(
                f"[main] Binary not found for method '{method_name}'. "
                "Ensure hermes-agent is installed: pip3 install 'hermes-agent[messaging,pty,mcp,acp]'",
                file=sys.stderr,
            )
            sys.exit(1)
        except KeyboardInterrupt:
            print("[main] Shutdown requested (SIGINT).", file=sys.stderr)
            sys.exit(0)


if __name__ == "__main__":
    main()
