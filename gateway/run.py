#!/usr/bin/env python3
"""
gateway/run.py — Rhodawk AI Telegram Gateway v5.0 (Jarvis-Grade)

Root cause fixes (v5.0):
  FIX-A  tool_choice="required" enforced for ALL data/action queries — model can no longer
         skip tools and write fake search results, fake stats, or fabricated file content
  FIX-B  Anti-fabrication interceptor — detects fake search-result patterns in model text
         responses that had zero tool calls; forces a tool-required retry automatically
  FIX-C  Persistent user profile — SQLite user_profiles table, never cleared by /reset
         Captures Telegram name on first message, injected into every system prompt
         so the model knows who it's talking to across all sessions
  FIX-D  %%FILE:%% contract enforced in system prompt with concrete example — model
         must send file content via the tag, never as inline prose
  FIX-E  Bare git push intercepted in scratchpad — redirected to push-commit utility
         which handles credentials; prevents "could not read Username" failures
  FIX-F  /profile command — view and update your operator identity from Telegram
  FIX-1  Per-request UTC timestamp injection (date hallucination eliminated)
  FIX-2  SQLite conversation persistence (survives restarts)
  FIX-3  Dynamic system prompt per request
  FIX-4  Numeric consistency guard + scratchpad FINAL ANSWER RULE
  FIX-5  DuckDuckGo search fallback (zero API key)
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
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
API_KEY          = os.environ.get("OPENAI_API_KEY") or os.environ.get("DO_INFERENCE_API_KEY", "")
BASE_URL         = os.environ.get("OPENAI_BASE_URL") or os.environ.get("DO_INFERENCE_BASE_URL", "https://inference.do-ai.run/v1")
MODEL            = os.environ.get("OPENAI_MODEL") or os.environ.get("HERMES_MODEL", "deepseek-v4-pro")
HERMES_HOME      = os.environ.get("HERMES_HOME", "/data/.hermes")
CAMOFOX_HOST     = os.environ.get("CAMOFOX_HOST", "camofox")
CAMOFOX_PORT     = os.environ.get("CAMOFOX_PORT", "9377")
CAMOFOX_KEY      = os.environ.get("CAMOFOX_ACCESS_KEY", "")
BRAVE_API_KEY    = os.environ.get("BRAVE_API_KEY", "")
GITHUB_PAT       = os.environ.get("GITHUB_PAT", "")
MAX_MSG_LENGTH   = 4000
MAX_HISTORY      = 20
MAX_TOOL_ROUNDS  = 25

if not BRAVE_API_KEY:
    logger.warning("[gateway] BRAVE_API_KEY not set — DuckDuckGo will be used for search")

# ── Chat ID whitelist ─────────────────────────────────────────────────────────
_raw_ids = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
ALLOWED_CHAT_IDS: set[int] = set()
if _raw_ids:
    for _part in _raw_ids.split(","):
        _p = _part.strip()
        if _p.lstrip("-").isdigit():
            ALLOWED_CHAT_IDS.add(int(_p))
    logger.info(f"[gateway] Whitelist: {ALLOWED_CHAT_IDS}")
else:
    logger.warning("[gateway] TELEGRAM_CHAT_ID not set — open to all users")


def _is_allowed(chat_id: int) -> bool:
    return True if not ALLOWED_CHAT_IDS else chat_id in ALLOWED_CHAT_IDS


# ── FIX-A: Data query classifier ─────────────────────────────────────────────
# Queries matching these patterns REQUIRE real tool execution.
# tool_choice="required" is enforced on the first LLM call for these.
_DATA_QUERY_PATTERNS = re.compile(
    r"\b("
    r"search|find|look up|look for|latest|recent|current|now|today|"
    r"how many|how much|count|number of|what is the|what are the|"
    r"fetch|get|retrieve|pull|check|status of|price of|value of|"
    r"news|article|headline|update|report|"
    r"star[s]?|fork[s]?|issue[s]?|commit[s]?|repo|repository|"
    r"push|commit|deploy|run|execute|create|write|generate|make|build|"
    r"clone|install|send|post|upload|download|"
    r"github|huggingface|twitter|linkedin|youtube|"
    r"tell me about|show me|give me|list"
    r")\b",
    re.IGNORECASE,
)


def _is_data_query(text: str) -> bool:
    """Returns True if this query needs real tool execution (not a pure conversation)."""
    pure_convo = re.compile(
        r"^(hi|hello|hey|thanks|thank you|ok|okay|sure|got it|great|good|"
        r"what does .{1,30} stand for|who are you|what is your name|"
        r"explain .{1,60}|define .{1,60}|what is a .{1,40})[\s\?\.!]*$",
        re.IGNORECASE,
    )
    if pure_convo.match(text.strip()):
        return False
    return bool(_DATA_QUERY_PATTERNS.search(text))


# ── FIX-B: Anti-fabrication detector ─────────────────────────────────────────
_FAKE_SEARCH_PATTERNS = [
    re.compile(r"Title:\s+.+\nURL:\s+https?://", re.MULTILINE),
    re.compile(r"Running Brave Search for", re.IGNORECASE),
    re.compile(r"as of (January|February|March|April|May|June|July|August|September|October|November|December) 20\d\d", re.IGNORECASE),
    re.compile(r"Description:\s+.{20,}", re.MULTILINE),
    re.compile(r"Here'?s? (a|the) production.ready", re.IGNORECASE),
    re.compile(r"Here'?s? (the )?(file|content|code|result|output)", re.IGNORECASE),
]


def _looks_fabricated(text: str, tool_calls_were_made: bool) -> bool:
    """
    Returns True if the model response looks like fabricated search results,
    fabricated file content, or hallucinated output — AND no tool calls were made.
    """
    if tool_calls_were_made:
        return False  # If tools ran, the content is real
    for pat in _FAKE_SEARCH_PATTERNS:
        if pat.search(text):
            logger.warning(f"[anti-fab] Fabrication pattern detected: {pat.pattern[:60]}")
            return True
    return False


# ── Tool definitions ──────────────────────────────────────────────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "terminal",
            "description": (
                "Run a shell command inside the container. Returns real stdout + stderr. "
                "Use for: web search (DDG/Brave), curl API calls, git operations via push-commit utility, "
                "python3 scripts, delegating to openclaude, delegating to jcode, "
                "file reads/writes, docker commands, any executable task. "
                "MANDATORY: Call this tool first. NEVER write command output as text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command (via /bin/bash -c)"},
                    "timeout": {"type": "integer", "description": "Seconds (default 60, max 600)", "default": 60}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": (
                "Fetch a URL via curl, return plain text (HTML stripped). "
                "Use for: public JSON APIs, GitHub raw files, RSS, plain HTML pages. "
                "For JS-rendered / Cloudflare sites use camofox_browse."
            ),
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
            "description": (
                "Open URL in camofox stealth browser (real headless Chromium). "
                "Use for: JS SPAs, Cloudflare, LinkedIn, YouTube. Slower than web_fetch."
            ),
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


# ── Command sanitizer ─────────────────────────────────────────────────────────
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
    script_escaped = script.replace("'", "'\\''")
    return pre + "'" + script_escaped + "'" + remainder


# FIX-E: Intercept bare git push — redirect to push-commit utility
def _intercept_bare_git_push(cmd: str) -> str:
    """
    Bare `git push` fails silently (no credentials in container).
    Redirect to push-commit utility which handles auth properly.
    """
    if re.search(r'\bgit\s+push\b', cmd) and 'push-commit' not in cmd and 'push_commit' not in cmd:
        logger.warning(f"[intercept] Bare git push detected — redirecting to push-commit: {cmd[:80]}")
        return (
            "echo '[HERMES] Bare git push intercepted — use push-commit utility:' && "
            "echo 'python3 /app/bot/telegram_bot.py push-commit --repo URL --token $GITHUB_PAT --workdir DIR --message MSG' && "
            "echo 'Or set remote with credentials: git remote set-url origin https://$GITHUB_PAT@github.com/OWNER/REPO.git'"
        )
    return cmd


# ── Real tool execution ───────────────────────────────────────────────────────
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
        content = re.sub(r"<style[^>]*>.*?</style>", " ", content, flags=re.DOTALL | re.IGNORECASE)
        content = re.sub(r"<[^>]+>", " ", content)
        content = re.sub(r"\s+", " ", content).strip()
        return (content[:4000] + "\n...[truncated]") if len(content) > 4000 else (content or "(empty)")
    except subprocess.TimeoutExpired:
        return f"[web_fetch] Timed out after {timeout}s"
    except Exception as exc:
        return f"[web_fetch] Error: {exc}"


def _run_camofox_browse_sync(url: str, wait_seconds: int = 3) -> str:
    import requests as req_lib
    camofox_base = f"http://{CAMOFOX_HOST}:{CAMOFOX_PORT}"
    wait_seconds = max(1, min(int(wait_seconds), 10))
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if CAMOFOX_KEY:
        headers["Authorization"] = f"Bearer {CAMOFOX_KEY}"
    try:
        h = req_lib.get(f"{camofox_base}/health", headers=headers, timeout=5)
        if h.status_code != 200:
            return f"[camofox] DOWN at {camofox_base}. Use web_fetch as fallback."
    except Exception as exc:
        return f"[camofox] Unreachable: {exc}. Use web_fetch."
    session_id = f"hermes-{uuid.uuid4().hex[:8]}"
    tab_id = None
    try:
        resp = req_lib.post(f"{camofox_base}/sessions/{session_id}/tabs", json={"url": url}, headers=headers, timeout=10)
        data = resp.json()
        tab_id = data.get("tabId") or data.get("id") or data.get("tab_id")
        if not tab_id:
            return f"[camofox] Tab creation failed: {json.dumps(data)[:300]}"
        time.sleep(wait_seconds)
        snap_resp = req_lib.get(f"{camofox_base}/tabs/{tab_id}/snapshot", headers=headers, timeout=15)
        snap = snap_resp.json()
        text = snap.get("text") or snap.get("content") or snap.get("body") or ""
        if not text:
            return f"[camofox] Empty snapshot for {url}"
        return text[:6000] + ("\n...[truncated]" if len(text) > 6000 else "")
    except Exception as exc:
        return f"[camofox] Error: {exc}"
    finally:
        if tab_id:
            try:
                import requests as r2
                r2.delete(f"{camofox_base}/tabs/{tab_id}", headers=headers, timeout=5)
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


# ── File delivery ─────────────────────────────────────────────────────────────
_FILE_TAG_RE = re.compile(r"%%FILE:(?P<name>[^\n%]+?)%%\s*\n(?P<content>.*?)%%/FILE%%", re.DOTALL)


async def _send_file_attachment(bot, chat_id: int, filename: str, content: str) -> None:
    safe = filename.strip().replace("/", "_").replace("\\", "_") or "file.txt"
    suffix = Path(safe).suffix or ".txt"
    with tempfile.NamedTemporaryFile(mode="w", suffix=suffix, prefix="hermes_", delete=False, encoding="utf-8") as tmp:
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
            await update.message.reply_text(remaining[i : i + MAX_MSG_LENGTH])


# ── SQLite — DB path ──────────────────────────────────────────────────────────
_DB_PATH = os.path.join(HERMES_HOME, "sessions", "conversations.db")


def _ensure_db() -> None:
    db_dir = os.path.dirname(_DB_PATH)
    os.makedirs(db_dir, exist_ok=True)
    con = sqlite3.connect(_DB_PATH)
    con.execute(
        "CREATE TABLE IF NOT EXISTS sessions "
        "(user_id INTEGER PRIMARY KEY, history TEXT, updated_at TEXT)"
    )
    # FIX-C: persistent user profiles — NEVER cleared by /reset
    con.execute(
        "CREATE TABLE IF NOT EXISTS user_profiles ("
        "user_id INTEGER PRIMARY KEY, "
        "display_name TEXT, "
        "telegram_username TEXT, "
        "telegram_first_name TEXT, "
        "first_seen TEXT, "
        "last_seen TEXT, "
        "notes TEXT DEFAULT ''"
        ")"
    )
    con.commit()
    con.close()


# ── FIX-2: Conversation persistence ──────────────────────────────────────────
def _load_history(user_id: int) -> list[dict]:
    try:
        _ensure_db()
        con = sqlite3.connect(_DB_PATH)
        row = con.execute("SELECT history FROM sessions WHERE user_id=?", (user_id,)).fetchone()
        con.close()
        if row:
            return json.loads(row[0])
    except Exception as exc:
        logger.warning(f"[memory] Load failed {user_id}: {exc}")
    return []


def _save_history(user_id: int, history: list[dict]) -> None:
    try:
        _ensure_db()
        ts = datetime.now(timezone.utc).isoformat()
        con = sqlite3.connect(_DB_PATH)
        con.execute(
            "INSERT OR REPLACE INTO sessions (user_id, history, updated_at) VALUES (?,?,?)",
            (user_id, json.dumps(history), ts),
        )
        con.commit()
        con.close()
    except Exception as exc:
        logger.warning(f"[memory] Save failed {user_id}: {exc}")


def _trim(history: list[dict]) -> list[dict]:
    return history[-MAX_HISTORY:] if len(history) > MAX_HISTORY else history


# ── FIX-C: User profile persistence ──────────────────────────────────────────
def _load_profile(user_id: int) -> dict:
    try:
        _ensure_db()
        con = sqlite3.connect(_DB_PATH)
        row = con.execute(
            "SELECT display_name, telegram_username, telegram_first_name, first_seen, last_seen, notes "
            "FROM user_profiles WHERE user_id=?", (user_id,)
        ).fetchone()
        con.close()
        if row:
            return {
                "display_name": row[0] or "",
                "telegram_username": row[1] or "",
                "telegram_first_name": row[2] or "",
                "first_seen": row[3] or "",
                "last_seen": row[4] or "",
                "notes": row[5] or "",
            }
    except Exception as exc:
        logger.warning(f"[profile] Load failed {user_id}: {exc}")
    return {}


def _upsert_profile(user_id: int, **kwargs) -> None:
    try:
        _ensure_db()
        ts = datetime.now(timezone.utc).isoformat()
        con = sqlite3.connect(_DB_PATH)
        existing = con.execute(
            "SELECT display_name, telegram_username, telegram_first_name, first_seen, notes "
            "FROM user_profiles WHERE user_id=?", (user_id,)
        ).fetchone()
        if existing:
            con.execute(
                "UPDATE user_profiles SET "
                "display_name=COALESCE(?,display_name), "
                "telegram_username=COALESCE(?,telegram_username), "
                "telegram_first_name=COALESCE(?,telegram_first_name), "
                "last_seen=?, notes=COALESCE(?,notes) "
                "WHERE user_id=?",
                (
                    kwargs.get("display_name"),
                    kwargs.get("telegram_username"),
                    kwargs.get("telegram_first_name"),
                    ts,
                    kwargs.get("notes"),
                    user_id,
                ),
            )
        else:
            con.execute(
                "INSERT INTO user_profiles "
                "(user_id, display_name, telegram_username, telegram_first_name, first_seen, last_seen, notes) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    user_id,
                    kwargs.get("display_name", ""),
                    kwargs.get("telegram_username", ""),
                    kwargs.get("telegram_first_name", ""),
                    ts, ts,
                    kwargs.get("notes", ""),
                ),
            )
        con.commit()
        con.close()
    except Exception as exc:
        logger.warning(f"[profile] Upsert failed {user_id}: {exc}")


def _sync_profile_from_telegram(update: Update) -> None:
    """Capture Telegram identity on first message and keep it updated."""
    user = update.effective_user
    if not user:
        return
    _upsert_profile(
        user.id,
        telegram_first_name=user.first_name or "",
        telegram_username=user.username or "",
        display_name=user.first_name or user.username or "",
    )


# ── FIX-4: Numeric consistency guard ─────────────────────────────────────────
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


# ── System prompt ─────────────────────────────────────────────────────────────
_GATEWAY_ADDENDUM = """

## Runtime Environment

- Current UTC time: {utc_ts}
- Operator: {operator_name}
- Telegram username: {telegram_username}
- Container: Ubuntu 22.04 | Python 3.11 | Node 24 | Bun | ripgrep | git | jq
- tool terminal  — real bash. Use for EVERYTHING that needs execution.
- tool web_fetch — curl + HTML strip. Plain APIs, raw files.
- tool camofox_browse — headless Chromium at http://{camofox_host}:{camofox_port}. JS sites.
- openclaude gRPC: python3 /app/skills/openclaude_grpc/client.py --prompt "..." --workdir DIR --model deepseek-r1-distill-llama-70b
- jcode swarm: JCODE_MODEL=kimi-k2.6 jcode run --message "..."
- push utility: python3 /app/bot/telegram_bot.py push-commit --repo URL --token $GITHUB_PAT --workdir DIR --message "msg"
- health check: python3 /app/bot/telegram_bot.py health-check

## File Delivery — MANDATORY FORMAT

To deliver any file (code, config, report, script), use EXACTLY this format:
%%FILE:filename.ext%%
<file content here — complete, not truncated>
%%/FILE%%

Example:
%%FILE:.env.example%%
# App config
PORT=8000
DATABASE_URL=postgresql://user:pass@localhost/db
SECRET_KEY=change-me-in-production
%%/FILE%%

NEVER send file content as inline prose. ALWAYS use the %%FILE:%% tag.
The tag triggers a real Telegram sendDocument so the user gets a proper download.

## Git Push — MANDATORY RULE

NEVER use bare `git push` — there are no git credentials in the shell environment.
ALWAYS push via the push-commit utility:

python3 /app/bot/telegram_bot.py push-commit \\
  --repo https://github.com/OWNER/REPO \\
  --token $GITHUB_PAT \\
  --workdir /tmp/repos/myrepo \\
  --message "feat: description"

If pushing to HuggingFace: use --token $HF_TOKEN with the HF repo URL.

## Web Search — MANDATORY TOOL CALL

NEVER write search results from memory. ALWAYS call the terminal tool first:

Option 0 (default — no API key needed):
python3 -c '
from duckduckgo_search import DDGS
results = DDGS().text("QUERY", max_results=5)
for r in results:
    print(r["title"]); print(r["href"]); print(r["body"][:300]); print()
'

Option 1 (if BRAVE_API_KEY is set):
curl -s "https://api.search.brave.com/res/v1/web/search?q=QUERY&count=5" \\
  -H "Accept: application/json" -H "X-Subscription-Token: $BRAVE_API_KEY" | jq -r '.web.results[] | "\\(.title)\\n\\(.url)\\n\\(.description)\\n"'

## ABSOLUTE RULES — VIOLATION = JARVIS FAILURE

1. CALL TOOLS FIRST. Never write the answer before the tool runs.
   Every search result, every stat, every file, every number = tool call required.

2. ZERO FABRICATION. If you write "Title: X\\nURL: Y" without calling terminal first,
   you are lying to the operator. This is a critical failure.

3. FILE CONTENT = FILE TAG. Any file you generate goes in %%FILE:%% tags. Period.

4. GIT PUSH = push-commit utility. Never bare git push.

5. NUMBERS from tool output only. If the tool said 14634, you say 14634.
   You do not say 14,000 or approximately 15k or ~145677.

6. DATE = the timestamp at the top of this prompt. Not your training data.
   If asked what day it is: read the timestamp above. Answer exactly.

7. MEMORY WRITES. After any meaningful task completion, write a summary to
   /data/.hermes/memories/MEMORY.md so the next session can skip re-discovery.
"""


def _load_soul() -> str:
    for path in [Path(HERMES_HOME) / "SOUL.md", Path("/app/hermes_config/SOUL.md")]:
        if path.exists():
            return path.read_text()
    return "You are Hermes, Rhodawk AI executive intelligence. Direct. Execute first."


def _build_system_prompt(user_id: int | None = None) -> str:
    """Build per-request system prompt with live timestamp + operator identity."""
    utc_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Operator identity injection
    operator_name = "Operator"
    telegram_username = "unknown"
    if user_id:
        profile = _load_profile(user_id)
        if profile:
            operator_name = profile.get("display_name") or profile.get("telegram_first_name") or "Operator"
            telegram_username = "@" + profile.get("telegram_username", "") if profile.get("telegram_username") else "unknown"

    addendum = _GATEWAY_ADDENDUM.format(
        utc_ts=utc_ts,
        operator_name=operator_name,
        telegram_username=telegram_username,
        camofox_host=CAMOFOX_HOST,
        camofox_port=CAMOFOX_PORT,
    )
    return _load_soul() + addendum


# ── OpenAI client ─────────────────────────────────────────────────────────────
openai_client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL)


# ── Scratchpad fallback ───────────────────────────────────────────────────────
_BASH_BLOCK_RE = re.compile(r"```(?:bash|shell|sh|terminal|cmd|console|python3?)?\n(.*?)```", re.DOTALL | re.IGNORECASE)
_INLINE_CMD_RE = re.compile(r"^(?:Running|Executing|Command):\s*(.+)$", re.MULTILINE)


def _extract_commands(text: str) -> list[str]:
    cmds: list[str] = []
    for m in _BASH_BLOCK_RE.finditer(text):
        block = m.group(1).strip()
        if block:
            cmds.append(block)
    if not cmds:
        for m in _INLINE_CMD_RE.finditer(text):
            cmd = m.group(1).strip()
            if cmd:
                cmds.append(cmd)
    return cmds[:5]


async def _execute_text_scratchpad(content: str, messages: list[dict], update: Update) -> bool:
    cmds = _extract_commands(content)
    if not cmds:
        return False
    logger.warning(f"[scratchpad] {len(cmds)} command(s) extracted from model text")
    executed_pairs: list[str] = []
    for cmd in cmds:
        try:
            await update.message.reply_text(f"Executing (scratchpad): {cmd[:200]}")
        except Exception:
            pass
        result = await asyncio.to_thread(_run_terminal, cmd, 60)
        logger.info(f"[scratchpad] cmd={cmd[:80]!r} result={len(result)}chars")
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
            "SYSTEM NOTE: commands executed for real. Actual outputs:\n\n"
            + "\n---\n".join(executed_pairs)
            + "\n\nFINAL ANSWER RULE: Use ONLY the exact values from the outputs above. "
            "Copy numbers verbatim. Do not type any number that does not appear in the output."
        ),
    })
    return True


# ── Agentic loop ──────────────────────────────────────────────────────────────
async def _agent_loop(messages: list[dict], update: Update, user_msg: str) -> str:
    scratchpad_used = False
    all_tool_results: list[str] = []
    tool_calls_ever_made = False

    # FIX-A: Force tools on data queries
    first_call_tool_choice = "required" if _is_data_query(user_msg) else "auto"
    if first_call_tool_choice == "required":
        logger.info(f"[anti-fab] Data query detected — tool_choice=required for first call")

    for _round in range(MAX_TOOL_ROUNDS):
        tool_choice = first_call_tool_choice if _round == 0 else "auto"

        resp = await openai_client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            tool_choice=tool_choice,
            max_tokens=4096,
            temperature=0.05,
        )
        choice = resp.choices[0]
        msg = choice.message
        content = msg.content or ""

        # ── No tool calls ────────────────────────────────────────────────────
        if not msg.tool_calls:
            # FIX-B: Anti-fabrication check
            if _looks_fabricated(content, tool_calls_ever_made):
                logger.warning("[anti-fab] Fabricated response detected — forcing tool retry")
                messages.append({"role": "assistant", "content": content})
                messages.append({
                    "role": "user",
                    "content": (
                        "STOP. Your last response was fabricated — you wrote search results, "
                        "file content, or statistics without calling any tool. "
                        "This is a critical failure. You MUST call the terminal tool NOW to get real data. "
                        "Do not write any more text until you have called terminal and seen the real output."
                    ),
                })
                first_call_tool_choice = "required"
                continue

            # Scratchpad fallback
            if not scratchpad_used:
                executed = await _execute_text_scratchpad(content, messages, update)
                if executed:
                    scratchpad_used = True
                    continue

            # FIX-4: Numeric consistency check
            if all_tool_results and content and not _verify_numbers(all_tool_results, content):
                messages.append({"role": "assistant", "content": content})
                messages.append({
                    "role": "user",
                    "content": (
                        "CORRECTION: your answer contains numbers not in the tool output. "
                        "Re-read the actual outputs above and restate with exact values only."
                    ),
                })
                all_tool_results = []
                continue

            return content

        # ── Tool calls made ──────────────────────────────────────────────────
        scratchpad_used = False
        tool_calls_ever_made = True
        first_call_tool_choice = "auto"  # only enforce on first call

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
                progress = f"Running: {fn_args.get('command', '')[:180]}"
            elif fn_name == "web_fetch":
                progress = f"Fetching: {fn_args.get('url', '')[:180]}"
            elif fn_name == "camofox_browse":
                progress = f"Browsing (stealth): {fn_args.get('url', '')[:160]}"
            else:
                progress = f"Tool: {fn_name}"

            try:
                await update.message.reply_text(progress)
            except Exception:
                pass

            result = await _execute_tool(fn_name, fn_args)
            logger.info(f"[tool:{fn_name}] {len(result)}chars")
            all_tool_results.append(result)

            preview = result.strip()[:500]
            if preview:
                await asyncio.sleep(0.35)
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

    return "[hermes] Max tool rounds reached."


# ── Memory command helpers ────────────────────────────────────────────────────
_MEMORY_FILE_CANDIDATES = [
    Path("/data/.hermes/memories/MEMORY.md"),
    Path("/app/hermes_config/memories/MEMORY.md"),
]


def _memory_path() -> Path:
    primary = _MEMORY_FILE_CANDIDATES[0]
    primary.parent.mkdir(parents=True, exist_ok=True)
    return primary


def _read_memory() -> str:
    for p in _MEMORY_FILE_CANDIDATES:
        if p.exists():
            return p.read_text(encoding="utf-8")
    return "(empty — no MEMORY.md found)"


def _append_memory(note: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = f"\n## {ts}\n{note.strip()}\n"
    p = _memory_path()
    if not p.exists():
        p.write_text(f"# Hermes Long-Term Memory\n{entry}", encoding="utf-8")
        return f"Memory file created. Note saved at {ts}."
    with open(p, "a", encoding="utf-8") as f:
        f.write(entry)
    return f"Appended to MEMORY.md at {ts}."


def _clear_memory() -> str:
    p = _memory_path()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    p.write_text(f"# Hermes Long-Term Memory\n\n(Cleared {ts})\n", encoding="utf-8")
    return f"MEMORY.md cleared at {ts}."


# ── Telegram handlers ─────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_chat.id):
        return
    _sync_profile_from_telegram(update)
    profile = _load_profile(update.effective_user.id)
    name = profile.get("display_name") or profile.get("telegram_first_name") or update.effective_user.first_name or "Operator"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    await update.message.reply_text(
        f"Hermes online. Good to see you, {name}.\n"
        f"UTC: {ts}\n"
        f"Model: {MODEL}\n"
        f"Tools: terminal | web_fetch | camofox_browse\n"
        "Commands: /reset /status /memory /profile /help"
    )


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_chat.id):
        return
    user_id = update.effective_user.id
    _save_history(user_id, [])
    profile = _load_profile(user_id)
    name = profile.get("display_name") or "Operator"
    await update.message.reply_text(f"Conversation cleared, {name}. Your profile and long-term memory are intact.")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_chat.id):
        return
    await update.message.reply_text(
        "Hermes — Rhodawk AI\n\n"
        "/start             — Status + UTC time\n"
        "/reset             — Clear conversation (profile kept)\n"
        "/status            — Stack health check\n"
        "/memory            — Read long-term memory\n"
        "/memory <note>     — Append note to memory\n"
        "/memory clear      — Wipe memory\n"
        "/profile           — View your operator profile\n"
        "/profile name <x>  — Set your display name\n"
        "/profile notes <x> — Add profile notes\n"
        "/help              — This message\n\n"
        f"Model: {MODEL} | Endpoint: {BASE_URL}"
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
            content = _read_memory()
            if len(content) > 3500:
                content = f"(showing last 3500 of {len(content)} chars)\n\n" + content[-3500:]
            await update.message.reply_text(content or "(empty)")
        elif args_text.lower() == "clear":
            await update.message.reply_text(_clear_memory())
        else:
            await update.message.reply_text(_append_memory(args_text))
    except Exception as exc:
        await update.message.reply_text(f"Memory error: {exc}")


async def cmd_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /profile              — view profile
    /profile name <x>     — set display name
    /profile notes <x>    — set profile notes (context for Hermes)
    """
    if not _is_allowed(update.effective_chat.id):
        return
    user_id = update.effective_user.id
    args_text = update.message.text.partition(" ")[2].strip()
    await update.message.chat.send_action(ChatAction.TYPING)

    if not args_text:
        profile = _load_profile(user_id)
        if not profile:
            await update.message.reply_text("No profile yet. Send any message and it will be created.")
            return
        lines = [
            f"Display name: {profile.get('display_name') or '(not set)'}",
            f"Telegram: @{profile.get('telegram_username') or '(none)'} ({profile.get('telegram_first_name', '')})",
            f"First seen: {profile.get('first_seen', 'unknown')}",
            f"Last seen: {profile.get('last_seen', 'unknown')}",
            f"Notes: {profile.get('notes') or '(none)'}",
        ]
        await update.message.reply_text("\n".join(lines))

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
            await update.message.reply_text(f"Profile notes updated.")
        else:
            await update.message.reply_text("Usage: /profile notes <context for Hermes>")
    else:
        await update.message.reply_text(
            "Usage:\n"
            "/profile — view\n"
            "/profile name <x> — set name\n"
            "/profile notes <x> — set context notes"
        )


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    if not _is_allowed(update.effective_chat.id):
        logger.warning(f"[gateway] Blocked chat_id={update.effective_chat.id}")
        return

    user_id  = update.effective_user.id
    user_msg = update.message.text.strip()

    # FIX-C: sync Telegram identity on every message
    _sync_profile_from_telegram(update)
    _upsert_profile(user_id)  # update last_seen

    await update.message.chat.send_action(ChatAction.TYPING)

    history = _load_history(user_id)
    history.append({"role": "user", "content": user_msg})
    history = _trim(history)

    # FIX-1 + FIX-3: per-request prompt with live timestamp + operator identity
    system_prompt = _build_system_prompt(user_id)
    messages = [{"role": "system", "content": system_prompt}] + list(history)

    try:
        reply = await _agent_loop(messages, update, user_msg)
        history.append({"role": "assistant", "content": reply})
        _save_history(user_id, _trim(history))
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
        logger.info(f"[gateway] SQLite DB ready: {_DB_PATH}")
    except Exception as exc:
        logger.warning(f"[gateway] DB init failed: {exc}")

    logger.info(f"[gateway] Model   : {MODEL}")
    logger.info(f"[gateway] Endpoint: {BASE_URL}")
    logger.info(f"[gateway] Camofox : {CAMOFOX_HOST}:{CAMOFOX_PORT}")
    logger.info(f"[gateway] Search  : {'Brave (primary) + DDG (fallback)' if BRAVE_API_KEY else 'DuckDuckGo only'}")
    logger.info(f"[gateway] Git push: {'GITHUB_PAT set' if GITHUB_PAT else 'GITHUB_PAT NOT SET — pushes will fail'}")
    if ALLOWED_CHAT_IDS:
        logger.info(f"[gateway] Whitelist: {ALLOWED_CHAT_IDS}")

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
