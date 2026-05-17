#!/usr/bin/env python3
"""
send_file.py — Send a file (or text) to Telegram (fixed v2)

Usage:
    python3 /app/send_file.py <file_path> [--caption "text"] [--chat-id 123456]
    python3 /app/send_file.py --text "plain message" [--chat-id 123456]

FIX: .txt, .md, .log files previously went through send_markdown_file() which
     called send_text() (rendered inline as a chat message, NOT a file attachment).
     Now ALL files — regardless of extension — are sent via sendDocument,
     which creates a real Telegram file attachment the user can download.
     send_markdown_file() is removed entirely.

Chat ID resolution order:
    1. --chat-id CLI argument
    2. TELEGRAM_CHAT_ID environment variable  ← set this for private mode
    3. Auto-discover from getUpdates (last resort — picks most recent sender)
"""

import argparse
import os
import sys
import time
from pathlib import Path

import requests

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
API_BASE   = f"https://api.telegram.org/bot{BOT_TOKEN}"
MAX_TEXT   = 4000


def _die(msg: str) -> None:
    print(f"[send_file] ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def _discover_chat_id() -> int:
    """
    Fall back to auto-discovering chat ID from recent updates.
    Only used when neither --chat-id nor TELEGRAM_CHAT_ID is set.

    SECURITY: This is inherently unsafe in production. If no TELEGRAM_CHAT_ID
    is configured, refuse to auto-discover to prevent sending files to random users.
    """
    print(
        "[send_file] FATAL: TELEGRAM_CHAT_ID is not set and --chat-id was not provided.\n"
        "  Auto-discovery is disabled for security (files could be sent to any user).\n"
        "  Set TELEGRAM_CHAT_ID in .env to your Telegram chat ID.\n"
        "  Find it: message @userinfobot on Telegram.",
        file=sys.stderr,
    )
    sys.exit(1)


def resolve_chat_id(cli_chat_id: str | None) -> int:
    if cli_chat_id:
        return int(cli_chat_id)
    env_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if env_id:
        # Support comma-separated list — use the first one
        first = env_id.split(",")[0].strip()
        if first.lstrip("-").isdigit():
            return int(first)
    return _discover_chat_id()


def _check(response: requests.Response, action: str) -> None:
    try:
        data = response.json()
    except Exception:
        data = {"ok": False, "description": response.text}
    if not data.get("ok"):
        _die(f"{action} failed: {data.get('description', data)}")
    print(f"[send_file] {action} OK", file=sys.stderr)


def send_text(chat_id: int, text: str, parse_mode: str = "") -> None:
    """Send a plain text message (for --text flag only)."""
    chunks = [text[i : i + MAX_TEXT] for i in range(0, max(len(text), 1), MAX_TEXT)]
    for chunk in chunks:
        payload: dict = {"chat_id": chat_id, "text": chunk}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        r = requests.post(f"{API_BASE}/sendMessage", json=payload, timeout=15)
        _check(r, "sendMessage")
        time.sleep(0.3)


def send_document(chat_id: int, file_path: str | Path, caption: str = "") -> None:
    """
    Send any file as a real Telegram document attachment (sendDocument).
    This is now used for ALL file types — .txt, .md, .pdf, .pptx, .docx, etc.
    The user receives a proper file they can tap to download, not inline text.
    """
    file_path = Path(file_path)
    with open(file_path, "rb") as fh:
        payload: dict = {"chat_id": chat_id}
        if caption:
            payload["caption"] = caption[:1024]
        r = requests.post(
            f"{API_BASE}/sendDocument",
            data=payload,
            files={
                "document": (file_path.name, fh, "application/octet-stream")
            },
            timeout=60,
        )
    _check(r, f"sendDocument({file_path.name})")


def main() -> None:
    if not BOT_TOKEN:
        _die("TELEGRAM_BOT_TOKEN is not set")

    parser = argparse.ArgumentParser(
        description="Send a file or plain text message to Telegram"
    )
    parser.add_argument("file", nargs="?", help="Path to file to send as document")
    parser.add_argument("--text",    help="Send a plain text message instead of a file")
    parser.add_argument("--caption", default="", help="Caption shown under the file")
    parser.add_argument(
        "--chat-id", dest="chat_id",
        help="Telegram chat ID to send to (overrides TELEGRAM_CHAT_ID env var)",
    )
    args = parser.parse_args()

    if not args.file and not args.text:
        parser.print_help()
        sys.exit(1)

    chat_id = resolve_chat_id(args.chat_id)

    if args.text:
        send_text(chat_id, args.text)
        return

    file_path = Path(args.file)
    if not file_path.exists():
        _die(f"File not found: {file_path}")

    # FIX: ALL files go through sendDocument — no more inline text for .txt/.md.
    # The old send_markdown_file() path is removed. Every file is a real attachment.
    send_document(chat_id, file_path, caption=args.caption)


if __name__ == "__main__":
    main()
