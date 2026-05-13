#!/usr/bin/env python3
"""
gateway/run.py — Rhodawk AI Telegram Gateway v7.0 (Jarvis-Architecture)

v7.0 fixes over v3.0 (what was actually running on the VPS):
  JARVIS-1  MEMORY.md auto-injected into every system prompt — model always starts
            knowing everything written, zero cold starts
  JARVIS-2  User profile auto-injected — name, notes, first seen, last task always
            in context; /reset clears conversation but never profile
  JARVIS-3  Session-start briefing — first message of session gets real git log,
            service status, recent memory block injected automatically
  JARVIS-4  Mandatory post-task memory write — system writes summary after every
            task automatically; model doesn't choose to remember, it always does
  JARVIS-5  tool_choice="required" enforced for first 2 rounds on data queries —
            model cannot skip tools and write fabricated search results

  FIX-A  Anti-fabrication interceptor — catches fake search/stat/file output
         when no tool call was made; forces tool-required retry
  FIX-B  Data query classifier — pattern matches search/fetch/run/count queries
  FIX-C  User profile SQLite table — never cleared by /reset, /profile command
  FIX-D  %%FILE:%% delivery contract — terminal intercept blocks "say attached"
  FIX-E  Bare git push intercepted — redirected to push-commit utility
  FIX-F  Scratchpad extraction hardened — hex strings and prose no longer
         extracted as shell commands
  FIX-G  Brave API URL corrected — /res/v1/web/search?q= not /v1/web?query=
  FIX-H  DDG auto-fallback when Brave/search fails — injected as correction
"""

import asyncio
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from openai import AsyncOpenAI
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("gateway")

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
API_KEY        = os.environ.get("OPENAI_API_KEY") or os.environ.get("DO_INFERENCE_API_KEY", "")
BASE_URL       = (os.environ.get("OPENAI_BASE_URL")
                  or os.environ.get("DO_INFERENCE_BASE_URL", "https://inference.do-ai.run/v1"))
MODEL          = (os.environ.get("OPENAI_MODEL")
                  or os.environ.get("HERMES_MODEL", "deepseek-v4-pro"))
HERMES_HOME    = os.environ.get("HERMES_HOME", "/data/.hermes")
CAMOFOX_HOST   = os.environ.get("CAMOFOX_HOST", "camofox")
CAMOFOX_PORT   = os.environ.get("CAMOFOX_PORT", "9377")
CAMOFOX_KEY    = os.environ.get("CAMOFOX_ACCESS_KEY", "")
BRAVE_API_KEY  = os.environ.get("BRAVE_API_KEY", "")
GITHUB_PAT     = os.environ.get("GITHUB_PAT", "")
MAX_MSG_LENGTH  = 4000
MAX_HISTORY     = 20
MAX_TOOL_ROUNDS = 25

if not BRAVE_API_KEY:
    logger.warning("[gateway] BRAVE_API_KEY not set — DuckDuckGo fallback active")
if not GITHUB_PAT:
    logger.warning("[gateway] GITHUB_PAT not set — git push via push-commit will fail")

# ── Chat ID whitelist ─────────────────────────────────────────────────────────
_raw_ids = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
ALLOWED_CHAT_IDS: set[int] = set()
if _raw_ids:
    for _part in _raw_ids.split(","):
        _p = _part.strip()
        if _p.lstrip("-").isdigit():
            ALLOWED_CHAT_IDS.add(int(_p))

def _is_allowed(chat_id: int) -> bool:
    return True if not ALLOWED_CHAT_IDS else chat_id in ALLOWED_CHAT_IDS


# ── FIX-B: Data query classifier ─────────────────────────────────────────────
_DATA_QUERY_RE = re.compile(
    r"\b("
    r"search|find|look ?up|look for|latest|recent|current|now|today|"
    r"how many|how much|count|number of|what is the|what are the|"
    r"fetch|get|retrieve|pull|check|status of|price of|value of|"
    r"news|article|headline|update|report|list all|list the|"
    r"star[s]?|fork[s]?|issue[s]?|commit[s]?|repo|repository|"
    r"push|commit|deploy|run|execute|create|write|generate|make|build|"
    r"clone|install|send|post|upload|download|available|"
    r"github|huggingface|twitter|linkedin|youtube|digitalocean|"
    r"tell me|show me|give me|what version|which version"
    r")\b",
    re.IGNORECASE,
)
_PURE_CONVO_RE = re.compile(
    r"^(hi+|hello|hey|thanks|thank you|ok+|okay|sure|got it|great|good morning|good night|"
    r"what does .{1,30} stand for|who are you|what is your name|"
    r"explain .{1,60}|define .{1,60})[\s\?\.!]*$",
    re.IGNORECASE,
)

def _is_data_query(text: str) -> bool:
    if _PURE_CONVO_RE.match(text.strip()):
        return False
    return bool(_DATA_QUERY_RE.search(text))


# ── FIX-A: Anti-fabrication detector ─────────────────────────────────────────
_FAKE_PATTERNS = [
    re.compile(r"^Title:\s+.{10,}\nURL:\s+https?://", re.MULTILINE),
    re.compile(r"Running Brave Search (query|for)\s*:", re.IGNORECASE),
    re.compile(r"as of (January|February|March|April|May|June|July|August|September|October|November|December) 20\d\d", re.IGNORECASE),
    re.compile(r"^Description:\s+.{20,}", re.MULTILINE),
    re.compile(r"Here'?s? (a |the )?production.ready", re.IGNORECASE),
    re.compile(r"latest articles and resources", re.IGNORECASE),
    re.compile(r"Visit the Official Documentation", re.IGNORECASE),
    re.compile(r"follow these steps.*\n.*\n.*documentation", re.IGNORECASE | re.DOTALL),
]

def _looks_fabricated(text: str, tool_calls_made: bool) -> bool:
    if tool_calls_made:
        return False
    for pat in _FAKE_PATTERNS:
        if pat.search(text):
            logger.warning(f"[anti-fab] Hit: {pat.pattern[:50]}")
            return True
    return False


# ── Tool definitions ──────────────────────────────────────────────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "terminal",
            "description": (
                "Run ANY shell command. Returns real stdout + stderr. "
                "MANDATORY for: web search (DDG/Brave), curl, git via push-commit, "
                "python3, openclaude, jcode, file I/O, checking versions/stats. "
                "Call this FIRST. Never write command output as text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer", "default": 60}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "curl + HTML strip. Public APIs, raw GitHub files, RSS.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "timeout": {"type": "integer", "default": 15}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "camofox_browse",
            "description": "Headless Chromium stealth browser. JS SPAs, Cloudflare, LinkedIn, YouTube.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "wait_seconds": {"type": "integer", "default": 3}
                },
                "required": ["url"]
            }
        }
    }
]


# ── Command sanitisers ────────────────────────────────────────────────────────
def _fix_python_c_quotes(cmd: str) -> str:
    marker = 'python3 -c "'
    idx = cmd.find(marker)
    if idx == -1:
        return cmd
    pre   = cmd[:idx] + "python3 -c "
    after = cmd[idx + len(marker):]
    last_q = after.rfind('"')
    if last_q == -1:
        return cmd
    script    = after[:last_q]
    remainder = after[last_q + 1:]
    if '"' not in script:
        return cmd
    return pre + "'" + script.replace("'", "'\\''") + "'" + remainder


# FIX-E: Intercept bare git push → push-commit utility
def _intercept_bare_git_push(cmd: str) -> str:
    if re.search(r'\bgit\s+push\b', cmd) and 'push-commit' not in cmd and 'push_commit' not in cmd:
        logger.warning(f"[intercept] Bare git push → redirecting: {cmd[:80]}")
        return (
            "echo '[HERMES ERROR] Bare git push has no credentials in this container.' && "
            "echo 'Use push-commit utility:' && "
            "echo 'python3 /app/bot/telegram_bot.py push-commit "
            "--repo https://github.com/OWNER/REPO "
            "--token $GITHUB_PAT --workdir /tmp/repos/DIR --message MSG'"
        )
    return cmd


# ── Tool executors ────────────────────────────────────────────────────────────
def _run_terminal(command: str, timeout: int = 60) -> str:
    timeout = max(1, min(int(timeout), 600))
    command = _fix_python_c_quotes(command)
    command = _intercept_bare_git_push(command)
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=timeout, executable="/bin/bash",
        )
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        if not output:
            return f"(exit {result.returncode}, no output)"
        prefix = f"[exit {result.returncode}] " if result.returncode != 0 else ""
        return prefix + output[:4000] + ("\n...[truncated]" if len(output) > 4000 else "")
    except subprocess.TimeoutExpired:
        return f"[terminal] Timed out after {timeout}s"
    except Exception as exc:
        return f"[terminal] Error: {exc}"


def _run_web_fetch(url: str, timeout: int = 15) -> str:
    timeout = max(1, min(int(timeout), 60))
    try:
        result = subprocess.run(
            ["curl", "-sL", "--max-time", str(timeout),
             "-H", "User-Agent: Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
             "-H", "Accept: text/html,application/xhtml+xml,application/json,*/*",
             url],
            capture_output=True, text=True, timeout=timeout + 5,
        )
        content = result.stdout.strip()
        if not content:
            return f"[web_fetch] Empty (exit {result.returncode}): {result.stderr.strip()[:200]}"
        content = re.sub(r"<script[^>]*>.*?</script>", " ", content, flags=re.DOTALL | re.IGNORECASE)
        content = re.sub(r"<style[^>]*>.*?</style>",  " ", content, flags=re.DOTALL | re.IGNORECASE)
        content = re.sub(r"<[^>]+>", " ", content)
        content = re.sub(r"\s+", " ", content).strip()
        return (content[:4000] + "\n...[truncated]") if len(content) > 4000 else (content or "(empty)")
    except subprocess.TimeoutExpired:
        return f"[web_fetch] Timed out after {timeout}s"
    except Exception as exc:
        return f"[web_fetch] Error: {exc}"


def _run_camofox_browse_sync(url: str, wait_seconds: int = 3) -> str:
    try:
        import requests as rlib
    except ImportError:
        return "[camofox] requests not installed — use web_fetch"
    camofox_base = f"http://{CAMOFOX_HOST}:{CAMOFOX_PORT}"
    wait_seconds = max(1, min(int(wait_seconds), 10))
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if CAMOFOX_KEY:
        headers["Authorization"] = f"Bearer {CAMOFOX_KEY}"
    try:
        h = rlib.get(f"{camofox_base}/health", headers=headers, timeout=5)
        if h.status_code != 200:
            return f"[camofox] DOWN at {camofox_base}. Use web_fetch."
    except Exception as exc:
        return f"[camofox] Unreachable: {exc}. Use web_fetch."
    session_id = f"hermes-{uuid.uuid4().hex[:8]}"
    tab_id = None
    try:
        resp = rlib.post(f"{camofox_base}/sessions/{session_id}/tabs",
                         json={"url": url}, headers=headers, timeout=10)
        data = resp.json()
        tab_id = data.get("tabId") or data.get("id") or data.get("tab_id")
        if not tab_id:
            return f"[camofox] Tab creation failed: {json.dumps(data)[:300]}"
        time.sleep(wait_seconds)
        snap = rlib.get(f"{camofox_base}/tabs/{tab_id}/snapshot", headers=headers, timeout=15).json()
        text = snap.get("text") or snap.get("content") or snap.get("body") or ""
        if not text:
            return f"[camofox] Empty snapshot for {url}"
        return text[:6000] + ("\n...[truncated]" if len(text) > 6000 else "")
    except Exception as exc:
        return f"[camofox] Error: {exc}"
    finally:
        if tab_id:
            try:
                rlib.delete(f"{camofox_base}/tabs/{tab_id}", headers=headers, timeout=5)
            except Exception:
                pass


async def _execute_tool(name: str, args: dict) -> str:
    if name == "terminal":
        return await asyncio.to_thread(_run_terminal, args.get("command", ""), args.get("timeout", 60))
    elif name == "web_fetch":
        return await asyncio.to_thread(_run_web_fetch, args.get("url", ""), args.get("timeout", 15))
    elif name == "camofox_browse":
        return await asyncio.to_thread(_run_camofox_browse_sync, args.get("url", ""), args.get("wait_seconds", 3))
    return f"[gateway] Unknown tool: {name}"


# ── FIX-D: File delivery ──────────────────────────────────────────────────────
_FILE_TAG_RE = re.compile(r"%%FILE:(?P<name>[^\n%]+?)%%\s*\n(?P<content>.*?)%%/FILE%%", re.DOTALL)

async def _send_file_attachment(bot, chat_id: int, filename: str, content: str) -> None:
    safe = filename.strip().replace("/", "_").replace("\\", "_") or "file.txt"
    suffix = Path(safe).suffix or ".txt"
    with tempfile.NamedTemporaryFile(mode="w", suffix=suffix, prefix="hermes_",
                                     delete=False, encoding="utf-8") as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        with open(tmp_path, "rb") as fh:
            await bot.send_document(chat_id=chat_id, document=fh, filename=safe, caption="")
    finally:
        try:
            Path(tmp_path).unlink()
        except Exception:
            pass

async def _deliver_reply(update: Update, reply: str) -> None:
    bot = update.get_bot()
    chat_id = update.effective_chat.id
    remaining = reply
    for match in _FILE_TAG_RE.finditer(reply):
        try:
            await _send_file_attachment(bot, chat_id, match.group("name").strip(), match.group("content"))
        except Exception as exc:
            await update.message.reply_text(f"File send failed: {exc}")
        remaining = remaining.replace(match.group(0), "").strip()
    remaining = remaining.strip()
    if remaining:
        for i in range(0, max(len(remaining), 1), MAX_MSG_LENGTH):
            await update.message.reply_text(remaining[i: i + MAX_MSG_LENGTH])


# ── SQLite ────────────────────────────────────────────────────────────────────
_DB_PATH = os.path.join(HERMES_HOME, "sessions", "conversations.db")

def _ensure_db() -> None:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    con = sqlite3.connect(_DB_PATH)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            user_id    INTEGER PRIMARY KEY,
            history    TEXT,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS user_profiles (
            user_id              INTEGER PRIMARY KEY,
            display_name         TEXT DEFAULT '',
            telegram_username    TEXT DEFAULT '',
            telegram_first_name  TEXT DEFAULT '',
            first_seen           TEXT DEFAULT '',
            last_seen            TEXT DEFAULT '',
            last_task            TEXT DEFAULT '',
            notes                TEXT DEFAULT ''
        );
    """)
    con.commit()
    con.close()

def _load_history(user_id: int) -> list[dict]:
    try:
        _ensure_db()
        con = sqlite3.connect(_DB_PATH)
        row = con.execute("SELECT history FROM sessions WHERE user_id=?", (user_id,)).fetchone()
        con.close()
        return json.loads(row[0]) if row else []
    except Exception as exc:
        logger.warning(f"[db] Load history failed {user_id}: {exc}")
        return []

def _save_history(user_id: int, history: list[dict]) -> None:
    try:
        _ensure_db()
        con = sqlite3.connect(_DB_PATH)
        con.execute(
            "INSERT OR REPLACE INTO sessions (user_id, history, updated_at) VALUES (?,?,?)",
            (user_id, json.dumps(history), datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
        con.close()
    except Exception as exc:
        logger.warning(f"[db] Save history failed {user_id}: {exc}")

def _trim(history: list[dict]) -> list[dict]:
    return history[-MAX_HISTORY:] if len(history) > MAX_HISTORY else history


# ── FIX-C: User profile (never cleared by /reset) ────────────────────────────
def _load_profile(user_id: int) -> dict:
    try:
        _ensure_db()
        con = sqlite3.connect(_DB_PATH)
        row = con.execute(
            "SELECT display_name, telegram_username, telegram_first_name, "
            "first_seen, last_seen, last_task, notes "
            "FROM user_profiles WHERE user_id=?", (user_id,)
        ).fetchone()
        con.close()
        if row:
            return {
                "display_name":        row[0] or "",
                "telegram_username":   row[1] or "",
                "telegram_first_name": row[2] or "",
                "first_seen":          row[3] or "",
                "last_seen":           row[4] or "",
                "last_task":           row[5] or "",
                "notes":               row[6] or "",
            }
    except Exception as exc:
        logger.warning(f"[profile] Load failed {user_id}: {exc}")
    return {}

def _upsert_profile(user_id: int, **kwargs) -> None:
    try:
        _ensure_db()
        ts = datetime.now(timezone.utc).isoformat()
        con = sqlite3.connect(_DB_PATH)
        exists = con.execute("SELECT 1 FROM user_profiles WHERE user_id=?", (user_id,)).fetchone()
        if exists:
            con.execute(
                "UPDATE user_profiles SET "
                "display_name=COALESCE(NULLIF(?,display_name),display_name,?), "
                "telegram_username=COALESCE(NULLIF(?,telegram_username),telegram_username,?), "
                "telegram_first_name=COALESCE(NULLIF(?,telegram_first_name),telegram_first_name,?), "
                "last_seen=?, "
                "last_task=COALESCE(NULLIF(?,last_task),last_task,?), "
                "notes=COALESCE(NULLIF(?,notes),notes,?) "
                "WHERE user_id=?",
                (
                    kwargs.get("display_name"), kwargs.get("display_name", ""),
                    kwargs.get("telegram_username"), kwargs.get("telegram_username", ""),
                    kwargs.get("telegram_first_name"), kwargs.get("telegram_first_name", ""),
                    ts,
                    kwargs.get("last_task"), kwargs.get("last_task", ""),
                    kwargs.get("notes"), kwargs.get("notes", ""),
                    user_id,
                ),
            )
        else:
            con.execute(
                "INSERT INTO user_profiles "
                "(user_id, display_name, telegram_username, telegram_first_name, "
                "first_seen, last_seen, last_task, notes) VALUES (?,?,?,?,?,?,?,?)",
                (
                    user_id,
                    kwargs.get("display_name", ""),
                    kwargs.get("telegram_username", ""),
                    kwargs.get("telegram_first_name", ""),
                    ts, ts,
                    kwargs.get("last_task", ""),
                    kwargs.get("notes", ""),
                ),
            )
        con.commit()
        con.close()
    except Exception as exc:
        logger.warning(f"[profile] Upsert failed {user_id}: {exc}")

def _sync_profile_from_telegram(update: Update) -> None:
    user = update.effective_user
    if not user:
        return
    _upsert_profile(
        user.id,
        telegram_first_name=user.first_name or "",
        telegram_username=user.username or "",
        display_name=user.first_name or user.username or "",
    )


# ── JARVIS-1: Memory auto-injection ──────────────────────────────────────────
_MEMORY_PATHS = [
    Path(HERMES_HOME) / "memories" / "MEMORY.md",
    Path("/app/hermes_config/memories/MEMORY.md"),
]

def _memory_file_path() -> Path:
    p = _MEMORY_PATHS[0]
    p.parent.mkdir(parents=True, exist_ok=True)
    return p

def _read_memory_for_context(max_chars: int = 3000) -> str:
    """Read MEMORY.md tail — injected into every system prompt automatically."""
    for p in _MEMORY_PATHS:
        if p.exists():
            content = p.read_text(encoding="utf-8").strip()
            if not content:
                return ""
            if len(content) > max_chars:
                return "(... earlier entries omitted ...)\n\n" + content[-max_chars:]
            return content
    return ""

# JARVIS-4: Auto-write memory after every completed task
def _auto_write_memory(user_msg: str, reply: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = f"\n## {ts}\nTask: {user_msg.strip()[:120]}\nResult: {reply.strip()[:200]}\n"
    p = _memory_file_path()
    try:
        if not p.exists():
            p.write_text(f"# Hermes Long-Term Memory\n{entry}", encoding="utf-8")
        else:
            with open(p, "a", encoding="utf-8") as f:
                f.write(entry)
    except Exception as exc:
        logger.warning(f"[memory] Auto-write failed: {exc}")


# ── JARVIS-3: Session-start briefing ─────────────────────────────────────────
def _build_briefing_block() -> str:
    checks = [
        ("git_log",    "git -C /app log --oneline -5 2>/dev/null || echo '(no git log)'"),
        ("services",   "supervisorctl status 2>/dev/null | head -8 || echo '(no supervisor)'"),
        ("disk",       "df -h / 2>/dev/null | tail -1"),
        ("memory_tail","tail -15 /data/.hermes/memories/MEMORY.md 2>/dev/null || echo '(no memory file yet)'"),
    ]
    lines = ["[SESSION START — REAL ENVIRONMENT STATUS]"]
    for label, cmd in checks:
        try:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                                    timeout=5, executable="/bin/bash")
            out = ((result.stdout or "") + (result.stderr or "")).strip()
            lines.append(f"{label}: {out[:300]}")
        except Exception as exc:
            lines.append(f"{label}: (error: {exc})")
    lines.append("[END STATUS]")
    return "\n".join(lines)


# ── System prompt ─────────────────────────────────────────────────────────────
_SOUL_PATHS = [Path(HERMES_HOME) / "SOUL.md", Path("/app/hermes_config/SOUL.md")]

def _load_soul() -> str:
    for p in _SOUL_PATHS:
        if p.exists():
            return p.read_text()
    return "You are Hermes, Rhodawk AI intelligence. Execute first, report after."


# FIX-G: Correct Brave API URL | FIX-H: DDG fallback instruction
_RUNTIME_BLOCK = """

═══════════════════════════════════════════════════════
HERMES RUNTIME v7.0 — READ BEFORE EVERY RESPONSE
═══════════════════════════════════════════════════════

UTC NOW:       {utc_ts}
OPERATOR:      {operator_name}
TELEGRAM:      {telegram_username}
FIRST SEEN:    {first_seen}
NOTES:         {operator_notes}
LAST TASK:     {last_task}
MODEL:         {model}
GITHUB PAT:    {github_pat_status}
BRAVE SEARCH:  {brave_status}

──── LONG-TERM MEMORY (auto-loaded every session) ────
{memory_block}
──── END MEMORY ────

──── ENVIRONMENT STATUS ────
{briefing_block}
──── END STATUS ────

══════════════════════════════════════════════════════
TOOLS — call these, never narrate them:
  terminal       — real bash. Use for EVERYTHING executable.
  web_fetch      — curl + HTML strip. Public APIs, raw files.
  camofox_browse — headless Chromium. JS sites, Cloudflare.

WEB SEARCH — ALWAYS call terminal first. NEVER write results from memory:

  DDG (default, no API key needed):
    python3 -c '
from duckduckgo_search import DDGS
for r in DDGS().text("QUERY HERE", max_results=5):
    print(r["title"]); print(r["href"]); print(r["body"][:300]); print()
'

  Brave (when BRAVE_API_KEY is set):
    curl -s "https://api.search.brave.com/res/v1/web/search?q=QUERY&count=5" \
      -H "Accept: application/json" \
      -H "X-Subscription-Token: $BRAVE_API_KEY" | \
      jq -r '.web.results[] | "\(.title)\n\(.url)\n\(.description)\n"'

  IMPORTANT: If Brave returns error or empty, immediately retry with DDG.
  NEVER say "search unavailable". Always try DDG as fallback.

VERSION CHECKS — always call terminal, never write from memory:
  python3 --version && pip show openai | grep Version

API LISTS — always call terminal:
  curl -s "$DO_INFERENCE_BASE_URL/models" \
    -H "Authorization: Bearer $DO_INFERENCE_API_KEY" | jq -r '.data[].id'

FILE DELIVERY — THE ONLY WAY TO SEND A FILE IS WITH %%FILE%% TAGS IN YOUR REPLY:

  When user asks to "create and send", "make and send", "write and deliver" a file:
  Step 1: Write the file content DIRECTLY into your reply inside %%FILE%% tags.
  Step 2: Do NOT echo it to disk first. Do NOT say "I've attached". Just output the tags.

  CORRECT — this is what actually sends a file to the user:
  %%FILE:check.sh%%
  #!/bin/bash
  echo hello world
  %%/FILE%%

  WRONG — these NEVER deliver a file to the user:
  - echo "echo hello world" > check.sh    (writes to container disk only, user gets nothing)
  - "The file is attached"                (it is not attached, nothing was sent)
  - "Here is the content:"               (inline text, not a file attachment)

  THE GATEWAY INTERCEPTS %%FILE%% TAGS AND SENDS THEM AS REAL TELEGRAM DOCUMENTS.
  If you don't use the tags, no file is ever sent. Period.

GIT PUSH — use push-commit utility. NEVER bare git push (no credentials):
  python3 /app/bot/telegram_bot.py push-commit \
    --repo https://github.com/Architect8989/Hermes88 \
    --token $GITHUB_PAT \
    --workdir /tmp/repos/REPONAME \
    --message "feat: description"

SUB-AGENTS:
  openclaude: python3 /app/skills/openclaude_grpc/client.py --prompt "..." --workdir DIR
  jcode:      OPENAI_BASE_URL=$DO_INFERENCE_BASE_URL OPENAI_API_KEY=$DO_INFERENCE_API_KEY jcode run --message "..."
  health:     python3 /app/bot/telegram_bot.py health-check

ABSOLUTE RULES — breaking any = Jarvis failure:
  1. TOOLS BEFORE TEXT. For any data/action query: call terminal first.
  2. ZERO FABRICATION. Search results, versions, stats, file content = tool call required.
  3. FILE TAG. Any file goes in %%FILE:%% tags. "Here it is:" with inline text = FAILURE.
  4. PUSH-COMMIT. Never bare git push. Always push-commit.
  5. NUMBERS from tool output only. Verbatim. No rounding.
  6. DATE = UTC timestamp above. Not training data.
  7. ADDRESS OPERATOR BY NAME. Use "{operator_name}" naturally.
  8. DDG FALLBACK. If any search/API fails, retry with DDG immediately.
══════════════════════════════════════════════════════
"""

def _build_system_prompt(user_id: int | None = None, is_fresh_session: bool = False) -> str:
    utc_ts         = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    operator_name  = "Operator"
    telegram_username = "unknown"
    first_seen     = "unknown"
    operator_notes = "(none)"
    last_task      = "(none — first session)"

    if user_id:
        profile = _load_profile(user_id)
        if profile:
            operator_name     = (profile.get("display_name")
                                 or profile.get("telegram_first_name") or "Operator")
            tg               = profile.get("telegram_username", "")
            telegram_username = f"@{tg}" if tg else "unknown"
            first_seen        = profile.get("first_seen", "unknown")
            operator_notes    = profile.get("notes") or "(none)"
            last_task         = profile.get("last_task") or "(none)"

    memory_block   = _read_memory_for_context(3000) or "(empty — no entries yet)"
    briefing_block = _build_briefing_block() if is_fresh_session else "(mid-session)"

    runtime = _RUNTIME_BLOCK.format(
        utc_ts=utc_ts,
        operator_name=operator_name,
        telegram_username=telegram_username,
        first_seen=first_seen,
        operator_notes=operator_notes,
        last_task=last_task,
        model=MODEL,
        github_pat_status="SET" if GITHUB_PAT else "NOT SET — pushes will fail",
        brave_status="SET — use Brave first, DDG fallback" if BRAVE_API_KEY else "NOT SET — use DDG only",
        memory_block=memory_block,
        briefing_block=briefing_block,
    )

    return _load_soul() + runtime


# ── OpenAI client ─────────────────────────────────────────────────────────────
openai_client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL)


# ── FIX-F: Hardened scratchpad extraction ────────────────────────────────────
# Patterns that indicate a real shell command (not hex hashes or prose)
_CMD_START_RE = re.compile(
    r"^("
    r"python3?|pip3?|node|npm|npx|bun|curl|wget|git|jq|cat|ls|cd|mkdir|rm|cp|mv|"
    r"echo|printf|export|env|source|bash|sh|exec|sudo|apt|apt-get|"
    r"docker|supervisorctl|systemctl|ps|kill|pgrep|pkill|"
    r"find|grep|awk|sed|sort|head|tail|wc|tee|"
    r"tar|zip|unzip|gzip|"
    r"./|/usr/|/bin/|/app/|/tmp/|/data/"
    r")",
    re.IGNORECASE,
)
_HEX_ONLY_RE = re.compile(r"^[0-9a-f]{20,}$", re.IGNORECASE)
_BASH_BLOCK_RE = re.compile(
    r"```(?:bash|shell|sh|terminal|cmd|console|python3?)?\n(.*?)```",
    re.DOTALL | re.IGNORECASE
)
_INLINE_CMD_RE = re.compile(
    r"^(?:Running|Executing|Command|Run|Execute):\s*(.+)$", re.MULTILINE
)

def _extract_scratchpad_commands(text: str) -> list[str]:
    """
    FIX-F: Extract real shell commands from model text.
    Filters out: hex strings, prose, markdown headers, plain text.
    """
    cmds: list[str] = []

    # 1. Fenced code blocks first (most reliable)
    for m in _BASH_BLOCK_RE.finditer(text):
        block = m.group(1).strip()
        if not block:
            continue
        # Validate: first line must look like a real command
        first_line = block.split("\n")[0].strip()
        if _CMD_START_RE.match(first_line) and not _HEX_ONLY_RE.match(first_line):
            cmds.append(block)

    if cmds:
        return cmds[:5]

    # 2. "Running: ..." / "Executing: ..." inline patterns
    for m in _INLINE_CMD_RE.finditer(text):
        cmd = m.group(1).strip()
        if _CMD_START_RE.match(cmd) and not _HEX_ONLY_RE.match(cmd):
            cmds.append(cmd)

    return cmds[:5]


async def _execute_text_scratchpad(content: str, messages: list[dict], update: Update) -> bool:
    cmds = _extract_scratchpad_commands(content)
    if not cmds:
        return False
    logger.warning(f"[scratchpad] Executing {len(cmds)} command(s) from model text")
    executed_pairs: list[str] = []
    for cmd in cmds:
        try:
            await update.message.reply_text(f"Executing (scratchpad): {cmd[:200]}")
        except Exception:
            pass
        result = await asyncio.to_thread(_run_terminal, cmd, 60)
        preview = result.strip()[:500]
        try:
            await update.message.reply_text(
                f"Output:\n{preview}" + ("\n...[truncated]" if len(result) > 500 else "")
            )
        except Exception:
            pass
        executed_pairs.append(f"$ {cmd}\n{result}")

    messages.append({"role": "assistant", "content": content})
    messages.append({
        "role": "user",
        "content": (
            "SYSTEM: commands executed for real. Actual outputs:\n\n"
            + "\n---\n".join(executed_pairs)
            + "\n\nFINAL ANSWER RULE: Copy numbers and facts verbatim from the outputs above. "
            "Do not add, round, or paraphrase any value that came from a tool."
        ),
    })
    return True


# ── Numeric consistency guard ─────────────────────────────────────────────────
def _verify_numbers(tool_results: list[str], final_answer: str) -> bool:
    answer_nums = set(re.findall(r'\b\d{5,}\b', final_answer))
    if not answer_nums:
        return True
    result_nums: set[str] = set()
    for r in tool_results:
        result_nums |= set(re.findall(r'\b\d{5,}\b', r))
    fabricated = answer_nums - result_nums
    if fabricated:
        logger.warning(f"[verify] Fabricated numbers: {fabricated}")
        return False
    return True


# ── JARVIS-5: Agentic loop ────────────────────────────────────────────────────
async def _agent_loop(messages: list[dict], update: Update, user_msg: str) -> str:
    scratchpad_used   = False
    all_tool_results: list[str] = []
    tool_calls_ever   = False
    is_data           = _is_data_query(user_msg)
    forced_rounds     = 2 if is_data else 0

    if is_data:
        logger.info(f"[anti-fab] Data query — tool_choice=required x{forced_rounds} rounds")

    for _round in range(MAX_TOOL_ROUNDS):
        tool_choice = "required" if _round < forced_rounds else "auto"

        resp = await openai_client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            tool_choice=tool_choice,
            max_tokens=4096,
            temperature=0.05,
        )
        choice  = resp.choices[0]
        msg     = choice.message
        content = msg.content or ""

        # ── No tool calls ────────────────────────────────────────────────────
        if not msg.tool_calls:
            # FIX-D: File delivery interceptor — catch "attached" lies
            _wants_file = any(x in user_msg.lower() for x in [
                "send", "attach", "deliver", "file", "download", "give me", "create"
            ])
            _has_file_tag = "%%FILE:" in content
            _fake_delivery = any(x in content.lower() for x in [
                "is attached", "the script is included", "i've attached",
                "file is attached", "i have attached", "here's the file",
                "attached for your", "file has been", "file created",
                "created the file", "the file check", "included as a file",
            ])
            if _wants_file and _fake_delivery and not _has_file_tag:
                logger.warning("[FIX-D] Model claimed file attached without %%FILE%% tag — forcing correction")
                messages.append({"role": "assistant", "content": content})
                messages.append({
                    "role": "user",
                    "content": (
                        "[GATEWAY SYSTEM] ERROR: You said the file was attached but NO FILE WAS SENT. "
                        "Writing to disk (echo > file.sh) does NOT send anything to the user. "
                        "You MUST rewrite your response using %%FILE%% tags:\n\n"
                        "%%FILE:filename.sh%%\n"
                        "#!/bin/bash\n"
                        "...file content here...\n"
                        "%%/FILE%%\n\n"
                        "Output ONLY the %%FILE%%...%%/FILE%% block. Nothing else."
                    ),
                })
                forced_rounds = 0  # next response should be text with %%FILE%% tag
                continue

            # FIX-A: Anti-fabrication check
            if _looks_fabricated(content, tool_calls_ever):
                logger.warning("[anti-fab] Fabricated response — forcing tool retry")
                messages.append({"role": "assistant", "content": content})
                messages.append({
                    "role": "user",
                    "content": (
                        "STOP. That response is fabricated — you wrote search results, "
                        "statistics, versions, or file content without calling any tool. "
                        "This violates the ZERO FABRICATION rule. "
                        "Call terminal NOW with a real command. "
                        "Do not write any more text until you have seen real tool output."
                    ),
                })
                forced_rounds = _round + 2
                continue

            # FIX-F: Scratchpad fallback (hardened extraction)
            if not scratchpad_used:
                executed = await _execute_text_scratchpad(content, messages, update)
                if executed:
                    scratchpad_used = True
                    continue

            # Numeric consistency check
            if all_tool_results and content and not _verify_numbers(all_tool_results, content):
                messages.append({"role": "assistant", "content": content})
                messages.append({
                    "role": "user",
                    "content": (
                        "Your answer contains numbers not in the tool output. "
                        "Re-read the outputs and restate with exact verbatim values only."
                    ),
                })
                all_tool_results = []
                continue

            return content

        # ── Tool calls ───────────────────────────────────────────────────────
        scratchpad_used = False
        tool_calls_ever = True
        forced_rounds   = max(forced_rounds, 0)

        messages.append({
            "role": "assistant",
            "content": content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ],
        })

        for tc in msg.tool_calls:
            fn_name = tc.function.name
            try:
                fn_args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                fn_args = {}

            if fn_name == "terminal":
                progress = f"Running: {fn_args.get('command', '')[:200]}"
            elif fn_name == "web_fetch":
                progress = f"Fetching: {fn_args.get('url', '')[:200]}"
            elif fn_name == "camofox_browse":
                progress = f"Browsing: {fn_args.get('url', '')[:180]}"
            else:
                progress = f"Tool: {fn_name}"

            try:
                await update.message.reply_text(progress)
            except Exception:
                pass

            result = await _execute_tool(fn_name, fn_args)
            logger.info(f"[tool:{fn_name}] {len(result)} chars")
            all_tool_results.append(result)

            preview = result.strip()[:500]
            if preview:
                await asyncio.sleep(0.3)
                try:
                    await update.message.reply_text(
                        f"Output:\n{preview}" + ("\n...[truncated]" if len(result) > 500 else "")
                    )
                except Exception:
                    pass

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

            # FIX-H: Aggressive DDG auto-retry when search/fetch fails
            # Detect: Brave error, jq null, curl exit codes, empty results, HTTP errors
            _cmd = fn_args.get("command", "")
            _is_search_cmd = any(x in _cmd.lower() for x in [
                "search", "brave", "ddg", "duckduck", "news", "query", "q="
            ])
            _result_failed = any(x in result.lower()[:300] for x in [
                "cannot iterate over null", "exit 5", "exit 1", "exit 6", "exit 7",
                "error (at", "parse error", "connection refused", "could not resolve",
                "unauthorized", "forbidden", "rate limit", "no results", "empty",
                "null\nnull", "jq: error",
            ]) or (result.strip() == "" or result.strip() == "(exit 0, no output)")

            if fn_name == "terminal" and _is_search_cmd and _result_failed:
                # Extract the original query from the failed command
                _query_match = re.search(
                    r'(?:q=|query=|text\(["\'])([^"\'&\n]+)', _cmd, re.IGNORECASE
                )
                _query = _query_match.group(1).replace("+", " ") if _query_match else "the topic"
                logger.warning(f"[FIX-H] Search failed — forcing DDG retry for: {_query[:60]}")
                messages.append({
                    "role": "user",
                    "content": (
                        f"[GATEWAY SYSTEM] The previous search command FAILED. "
                        f"You MUST call terminal NOW with DDG to search for: {_query}\n\n"
                        "USE THIS EXACT COMMAND (substitute the query):\n"
                        "python3 -c '\n"
                        "from duckduckgo_search import DDGS\n"
                        f"for r in DDGS().text(\"{_query}\", max_results=5):\n"
                        "    print(r[\"title\"]); print(r[\"href\"]); print(r[\"body\"][:300]); print()\n"
                        "'\n\n"
                        "Do NOT respond until you have real DDG results."
                    ),
                })
                forced_rounds = _round + 3  # force tool calls for next 3 rounds

    return "[hermes] Max tool rounds reached."


# ── Memory command helpers ────────────────────────────────────────────────────
def _read_memory_display() -> str:
    for p in _MEMORY_PATHS:
        if p.exists():
            return p.read_text(encoding="utf-8")
    return "(empty — no MEMORY.md found)"

def _append_memory_manual(note: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = f"\n## {ts}\n{note.strip()}\n"
    p = _memory_file_path()
    if not p.exists():
        p.write_text(f"# Hermes Long-Term Memory\n{entry}", encoding="utf-8")
        return f"Memory file created. Note saved at {ts}."
    with open(p, "a", encoding="utf-8") as f:
        f.write(entry)
    return f"Appended at {ts}."

def _clear_memory_file() -> str:
    p = _memory_file_path()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    p.write_text(f"# Hermes Long-Term Memory\n\n(Cleared {ts})\n", encoding="utf-8")
    return f"MEMORY.md cleared at {ts}."


# ── Telegram handlers ─────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_chat.id):
        return
    _sync_profile_from_telegram(update)
    profile = _load_profile(update.effective_user.id)
    name = (profile.get("display_name") or profile.get("telegram_first_name")
            or update.effective_user.first_name or "Operator")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    await update.message.reply_text(
        f"Hermes v7.0 online. Good to see you, {name}.\n"
        f"UTC: {ts}\n"
        f"Model: {MODEL}\n"
        f"Memory: auto-injected every session\n"
        "Commands: /reset /status /memory /profile /help"
    )


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_chat.id):
        return
    user_id = update.effective_user.id
    _save_history(user_id, [])
    profile = _load_profile(user_id)
    name = profile.get("display_name") or profile.get("telegram_first_name") or "Operator"
    await update.message.reply_text(
        f"Conversation cleared, {name}.\n"
        "Profile and long-term memory are intact."
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_chat.id):
        return
    await update.message.reply_text(
        "Hermes v7.0 — Rhodawk AI\n\n"
        "/start             — Status + UTC time\n"
        "/reset             — Clear conversation (profile + memory kept)\n"
        "/status            — Stack health check\n"
        "/memory            — Read long-term memory\n"
        "/memory <note>     — Append note\n"
        "/memory clear      — Wipe memory\n"
        "/profile           — View operator profile\n"
        "/profile name <x>  — Set display name\n"
        "/profile notes <x> — Set context notes\n"
        "/help              — This message\n\n"
        f"Model: {MODEL}"
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_chat.id):
        return
    await update.message.chat.send_action(ChatAction.TYPING)
    result = await asyncio.to_thread(
        _run_terminal,
        "python3 /app/bot/telegram_bot.py health-check 2>&1 || "
        "supervisorctl -c /etc/supervisor/conf.d/rhodawk.conf status 2>/dev/null || "
        "echo '[status] supervisorctl unavailable'",
        30,
    )
    await update.message.reply_text(result[:MAX_MSG_LENGTH])


async def cmd_memory(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_chat.id):
        return
    args_text = update.message.text.partition(" ")[2].strip()
    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        if not args_text:
            content = _read_memory_display()
            if len(content) > 3500:
                content = f"(showing last 3500 of {len(content)} chars)\n\n" + content[-3500:]
            await update.message.reply_text(content or "(empty)")
        elif args_text.lower() == "clear":
            await update.message.reply_text(_clear_memory_file())
        else:
            await update.message.reply_text(_append_memory_manual(args_text))
    except Exception as exc:
        await update.message.reply_text(f"Memory error: {exc}")


async def cmd_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_chat.id):
        return
    user_id = update.effective_user.id
    args_text = update.message.text.partition(" ")[2].strip()
    await update.message.chat.send_action(ChatAction.TYPING)

    if not args_text:
        profile = _load_profile(user_id)
        if not profile:
            await update.message.reply_text("No profile yet. Send any message to create it.")
            return
        await update.message.reply_text(
            f"Name: {profile.get('display_name') or '(not set)'}\n"
            f"Telegram: @{profile.get('telegram_username') or 'none'} "
            f"({profile.get('telegram_first_name', '')})\n"
            f"First seen: {profile.get('first_seen', 'unknown')}\n"
            f"Last seen: {profile.get('last_seen', 'unknown')}\n"
            f"Last task: {profile.get('last_task') or '(none)'}\n"
            f"Notes: {profile.get('notes') or '(none)'}"
        )
    elif args_text.lower().startswith("name "):
        new_name = args_text[5:].strip()
        if new_name:
            _upsert_profile(user_id, display_name=new_name)
            await update.message.reply_text(f"Display name set to: {new_name}")
        else:
            await update.message.reply_text("Usage: /profile name <your name>")
    elif args_text.lower().startswith("notes "):
        notes = args_text[6:].strip()
        if notes:
            _upsert_profile(user_id, notes=notes)
            await update.message.reply_text("Profile notes updated.")
        else:
            await update.message.reply_text("Usage: /profile notes <context>")
    else:
        await update.message.reply_text(
            "/profile           — view\n"
            "/profile name <x>  — set name\n"
            "/profile notes <x> — set notes"
        )


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    if not _is_allowed(update.effective_chat.id):
        logger.warning(f"[gateway] Blocked chat_id={update.effective_chat.id}")
        return

    user_id  = update.effective_user.id
    user_msg = update.message.text.strip()

    # Sync Telegram identity + update last_seen on every message
    _sync_profile_from_telegram(update)

    await update.message.chat.send_action(ChatAction.TYPING)

    history       = _load_history(user_id)
    fresh_session = (len(history) == 0)

    history.append({"role": "user", "content": user_msg})
    history = _trim(history)

    # JARVIS-1+2+3: system prompt with memory + profile + briefing
    system_prompt = _build_system_prompt(user_id, is_fresh_session=fresh_session)
    messages = [{"role": "system", "content": system_prompt}] + list(history)

    try:
        reply = await _agent_loop(messages, update, user_msg)
        history.append({"role": "assistant", "content": reply})
        _save_history(user_id, _trim(history))

        # JARVIS-4: Auto-write memory after every non-trivial exchange
        if len(user_msg) > 15 and len(reply) > 20:
            _auto_write_memory(user_msg, reply)
            _upsert_profile(user_id, last_task=user_msg.strip()[:200])

        await _deliver_reply(update, reply)

    except Exception as exc:
        logger.error(f"[gateway] Error user {user_id}: {exc}", exc_info=True)
        await update.message.reply_text(f"Error: {type(exc).__name__}: {str(exc)[:300]}")


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    if not TELEGRAM_TOKEN:
        logger.error("[gateway] FATAL: TELEGRAM_BOT_TOKEN not set")
        sys.exit(1)
    if not API_KEY:
        logger.error("[gateway] FATAL: DO_INFERENCE_API_KEY not set")
        sys.exit(1)

    try:
        _ensure_db()
        logger.info(f"[gateway] SQLite: {_DB_PATH}")
    except Exception as exc:
        logger.warning(f"[gateway] DB init failed: {exc}")

    logger.info(f"[gateway] Hermes v7.0 — Jarvis Architecture")
    logger.info(f"[gateway] Model     : {MODEL}")
    logger.info(f"[gateway] Memory    : auto-injected from {_MEMORY_PATHS[0]}")
    logger.info(f"[gateway] Anti-fab  : tool_choice=required x2 on data queries")
    logger.info(f"[gateway] Scratchpad: hardened extraction (FIX-F)")
    logger.info(f"[gateway] Search    : {'Brave+DDG' if BRAVE_API_KEY else 'DDG only'}")
    logger.info(f"[gateway] Git push  : {'GITHUB_PAT set' if GITHUB_PAT else 'GITHUB_PAT MISSING'}")
    if ALLOWED_CHAT_IDS:
        logger.info(f"[gateway] Whitelist : {ALLOWED_CHAT_IDS}")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("reset",   cmd_reset))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("memory",  cmd_memory))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
