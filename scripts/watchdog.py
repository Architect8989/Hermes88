#!/usr/bin/env python3
"""
watchdog.py — Rhodawk Hermes process health monitor

Polls supervisord every 60 seconds. If any managed process enters FATAL
or EXITED state, sends a Telegram alert to the owner's chat ID. When the
process recovers (returns to RUNNING), sends a recovery notice.

Config (all from environment — no config file needed):
    TELEGRAM_BOT_TOKEN  — required (same token the bot uses)
    TELEGRAM_CHAT_ID    — required (your chat ID, e.g. 8215100512)

Runs as a supervisord [program:watchdog] child process.
Uses only Python stdlib — no external dependencies.
"""

import os
import subprocess
import time
import urllib.request
import urllib.parse
import json
import sys

# ── Config ──────────────────────────────────────────────────────────────────
POLL_INTERVAL  = 60    # seconds between checks
ALERT_COOLDOWN = 300   # seconds before re-alerting the same FATAL process
SUPERVISORCTL  = [
    "supervisorctl",
    "-c", "/etc/supervisor/conf.d/rhodawk.conf",
    "status",
]

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# Optional processes — EXITED/FATAL state is expected and should NOT trigger
# alerts. These are enhancement services that degrade gracefully when absent.
#   jcode-server  — jcode swarm coordinator (optional, cargo install jcode)
#   openclaw-gateway — multi-channel relay (disabled until channels configured)
OPTIONAL_PROCESSES: set[str] = {"jcode-server", "openclaw-gateway"}

# ── State ────────────────────────────────────────────────────────────────────
last_alerted:  dict[str, float] = {}   # process → last alert timestamp
last_state:    dict[str, str]   = {}   # process → last known state


# ── Telegram helper ──────────────────────────────────────────────────────────
def send_telegram(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        print(f"[watchdog] Telegram not configured — would send: {text[:120]}", flush=True)
        return
    payload = json.dumps({
        "chat_id":    CHAT_ID,
        "text":       text,
        "parse_mode": "HTML",
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"[watchdog] Alert sent (HTTP {resp.status})", flush=True)
    except Exception as exc:
        print(f"[watchdog] Alert send failed: {exc}", flush=True)


# ── Supervisord poller ────────────────────────────────────────────────────────
def get_process_states() -> dict[str, str]:
    """Returns {process_name: state} from supervisorctl status output."""
    try:
        result = subprocess.run(
            SUPERVISORCTL,
            capture_output=True,
            text=True,
            timeout=10,
        )
        states: dict[str, str] = {}
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                states[parts[0]] = parts[1]
        return states
    except Exception as exc:
        print(f"[watchdog] supervisorctl error: {exc}", flush=True)
        return {}


# ── Main loop ────────────────────────────────────────────────────────────────
def run() -> None:
    print(
        f"[watchdog] Started — polling every {POLL_INTERVAL}s | "
        f"alerting chat_id={CHAT_ID or '(not set)'}",
        flush=True,
    )

    while True:
        now    = time.time()
        states = get_process_states()

        for name, state in states.items():
            prev = last_state.get(name)

            # Skip optional processes — their EXITED state is expected and normal.
            # jcode-server: optional swarm coordinator (not installed → stays RUNNING
            #   via sleep infinity after the supervisord.conf fix).
            # openclaw-gateway: disabled until channels are configured via onboard.
            if name in OPTIONAL_PROCESSES:
                print(
                    f"[watchdog] {name} → {state} (optional — no alert)",
                    flush=True,
                )
                last_state[name] = state
                continue

            if state in ("FATAL", "EXITED"):
                last_alert = last_alerted.get(name, 0)
                if now - last_alert > ALERT_COOLDOWN:
                    last_alerted[name] = now
                    msg = (
                        f"🚨 <b>Hermes alert</b>\n\n"
                        f"Process <b>{name}</b> is <b>{state}</b>.\n\n"
                        f"Check logs:\n<code>docker logs --tail=40 hermes</code>"
                    )
                    print(f"[watchdog] {name} → {state} — sending alert", flush=True)
                    send_telegram(msg)
                else:
                    remaining = int(ALERT_COOLDOWN - (now - last_alert))
                    print(
                        f"[watchdog] {name} still {state} — "
                        f"cooldown {remaining}s remaining",
                        flush=True,
                    )

            elif state == "RUNNING" and prev in ("FATAL", "EXITED"):
                # Process recovered
                last_alerted.pop(name, None)
                msg = (
                    f"✅ <b>Hermes recovered</b>\n\n"
                    f"Process <b>{name}</b> is back to <b>RUNNING</b>."
                )
                print(f"[watchdog] {name} recovered ({prev} → RUNNING) — sending notice", flush=True)
                send_telegram(msg)

            last_state[name] = state

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
