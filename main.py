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
    """Start hermes-agent via its installed CLI entry point (`hermes` binary)."""
    import shutil
    binary = shutil.which("hermes") or shutil.which("hermes-agent")
    return subprocess.run([binary], env=env).returncode


def _start_with_gateway(env: dict) -> int:
    """
    Start the gateway via python -m gateway.run.
    Used both when hermes-agent's gateway module is available and as
    the self-contained fallback. gateway/run.py handles dispatch to
    hermes or openclaw based on HERMES_GATEWAY env var.
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
    if shutil.which("hermes-agent") or shutil.which("hermes"):
        start_fn = _start_with_cli
        method_name = "hermes CLI"
    else:
        # hermes-agent not installed — use the built-in gateway dispatcher.
        # gateway/run.py dispatches to hermes or openclaw based on HERMES_GATEWAY env.
        print(
            "[main] hermes-agent not installed — using built-in gateway/run.py dispatcher. "
            "Install hermes-agent to enable the terminal tool, skills, memory and cron.",
            file=sys.stderr,
        )
        start_fn = _start_with_gateway
        method_name = "python -m gateway.run (built-in dispatcher)"

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
