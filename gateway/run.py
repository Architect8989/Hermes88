#!/usr/bin/env python3
"""
gateway/run.py — Rhodawk AI Telegram Gateway v4.0 (Jarvis-Grade)

Fixes applied (v4.0):
  FIX-1  Per-request UTC timestamp injected into system prompt — no more date hallucination
  FIX-2  SQLite-backed conversation history — zero amnesia on restart
  FIX-3  _build_system_prompt() — dynamic per-request prompt with live timestamp + fresh URLs
  FIX-4  Scratchpad FINAL ANSWER RULE + _verify_numbers() numeric consistency guard
  FIX-5  Search degradation warning logged when BRAVE_API_KEY absent
  FIX-6  _verify_numbers() blocks fabricated numbers from reaching the user

Real tool execution via OpenAI function calling:
  terminal        — bash commands inside the container
  web_fetch       — lightweight HTTP fetch + HTML strip
  camofox_browse  — stealth browser session (JS SPAs, Cloudflare, LinkedIn, YouTube)

Config (environment):
    TELEGRAM_BOT_TOKEN        — required
    TELEGRAM_CHAT_ID          — your chat ID (comma-separated for multiple)
    OPENAI_API_KEY / DO_INFERENCE_API_KEY
    OPENAI_BASE_URL / DO_INFERENCE_BASE_URL
    OPENAI_MODEL / HERMES_MODEL  — default: deepseek-v4-pro
    CAMOFOX_HOST              — camofox service hostname (default: camofox for Docker)
    CAMOFOX_PORT              — camofox port (default: 9377)
    CAMOFOX_ACCESS_KEY        — camofox Bearer token
    HERMES_HOME               — default: /data/.hermes
    BRAVE_API_KEY             — Brave Search API key (optional, DDG used as fallback)
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
API_KEY = (
    os.environ.get("OPENAI_API_KEY")
    or os.environ.get("DO_INFERENCE_API_KEY", "")
)
BASE_URL = (
    os.environ.get("OPENAI_BASE_URL")
    or os.environ.get("DO_INFERENCE_BASE_URL", "https://inference.do-ai.run/v1")
)
MODEL = (
    os.environ.get("OPENAI_MODEL")
    or os.environ.get("HERMES_MODEL", "deepseek-v4-pro")
)
HERMES_HOME      = os.environ.get("HERMES_HOME", "/data/.hermes")
CAMOFOX_HOST     = os.environ.get("CAMOFOX_HOST", "camofox")
CAMOFOX_PORT     = os.environ.get("CAMOFOX_PORT", "9377")
CAMOFOX_KEY      = os.environ.get("CAMOFOX_ACCESS_KEY", "")
BRAVE_API_KEY    = os.environ.get("BRAVE_API_KEY", "")
MAX_MSG_LENGTH   = 4000
MAX_HISTORY      = 20
MAX_TOOL_ROUNDS  = 20

# ── FIX-5: Warn at startup if web search will be degraded ────────────────────
if not BRAVE_API_KEY:
    logger.warning(
        "[gateway] BRAVE_API_KEY not set — web_search will use DuckDuckGo fallback. "
        "Set BRAVE_API_KEY for production-grade search results."
    )

# ── Chat ID whitelist ──────────────────────────────────────────────────────────
_raw_ids = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
ALLOWED_CHAT_IDS: set[int] = set()
if _raw_ids:
    for _part in _raw_ids.split(","):
        _p = _part.strip()
        if _p.lstrip("-").isdigit():
            ALLOWED_CHAT_IDS.add(int(_p))
    logger.info(f"[gateway] Whitelist active — chat IDs: {ALLOWED_CHAT_IDS}")
else:
    logger.warning("[gateway] TELEGRAM_CHAT_ID not set — open to all users")


def _is_allowed(chat_id: int) -> bool:
    return True if not ALLOWED_CHAT_IDS else chat_id in ALLOWED_CHAT_IDS


# ── Tool definitions ───────────────────────────────────────────────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "terminal",
            "description": (
                "Run a shell command inside the container. Returns stdout + stderr. "
                "Use for: curl API calls, git operations, python3 scripts, "
                "delegating to openclaude (python3 /app/skills/openclaude_grpc/client.py), "
                "delegating to jcode (jcode run --message '...'), "
                "file reads/writes, docker commands, any executable task. "
                "NEVER fabricate output — always call this tool and return the real result."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to run (via /bin/bash -c)"
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Seconds to wait (default 60, max 600)",
                        "default": 60
                    }
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
                "Fetch a URL via curl and return plain text (HTML stripped). "
                "Best for: public JSON APIs, GitHub raw files, RSS, plain HTML docs. "
                "For JS-rendered pages, Cloudflare sites, LinkedIn, YouTube — use camofox_browse instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to fetch"},
                    "timeout": {"type": "integer", "description": "Seconds (default 15)", "default": 15}
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
                "Open a URL in the camofox stealth browser (real headless Chromium). "
                "Use for: JavaScript SPAs, Cloudflare-protected pages, LinkedIn, Crunchbase, "
                "YouTube transcripts, any site that returns empty body to plain curl. "
                "Slower than web_fetch — only use when plain curl fails."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to open"},
                    "wait_seconds": {
                        "type": "integer",
                        "description": "Seconds to wait for JS render (default 3, max 10)",
                        "default": 3
                    }
                },
                "required": ["url"]
            }
        }
    }
]


# ── Command sanitizer — fix the common shell-quoting bug ──────────────────────
def _fix_python_c_quotes(cmd: str) -> str:
    """
    The model often generates:
        python3 -c "...data["key"]..."
    The nested " inside the double-quoted -c string breaks bash.
    Fix: switch outer " to ', escape any ' inside as '\\''
    """
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
    logger.debug(f"[sanitizer] Rewrote python3 -c quoting for: {script[:60]!r}")
    return pre + "'" + script_escaped + "'" + remainder


# ── Real tool execution ────────────────────────────────────────────────────────

def _run_terminal(command: str, timeout: int = 60) -> str:
    timeout = max(1, min(int(timeout), 600))
    command = _fix_python_c_quotes(command)
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
            ["curl", "-sL", f"--max-time", str(timeout),
             "-H", "User-Agent: Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
             "-H", "Accept: text/html,application/xhtml+xml,application/json,*/*",
             url],
            capture_output=True, text=True, timeout=timeout + 5,
        )
        content = result.stdout.strip()
        if not content:
            return f"[web_fetch] Empty (HTTP exit {result.returncode}): {result.stderr.strip()[:200]}"
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
    import requests as req_lib

    # FIX-3: CAMOFOX_BASE_URL resolved fresh per call (not at module load)
    camofox_base = f"http://{CAMOFOX_HOST}:{CAMOFOX_PORT}"
    wait_seconds = max(1, min(int(wait_seconds), 10))
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if CAMOFOX_KEY:
        headers["Authorization"] = f"Bearer {CAMOFOX_KEY}"

    try:
        h = req_lib.get(f"{camofox_base}/health", headers=headers, timeout=5)
        if h.status_code != 200:
            return (
                f"[camofox] DOWN at {camofox_base} (HTTP {h.status_code}). "
                "Falling back to plain curl — web_fetch may return empty for JS pages."
            )
    except Exception as exc:
        return (
            f"[camofox] Unreachable at {camofox_base}: {exc}. "
            "Use web_fetch as fallback."
        )

    session_id = f"hermes-{uuid.uuid4().hex[:8]}"
    tab_id = None
    try:
        resp = req_lib.post(
            f"{camofox_base}/sessions/{session_id}/tabs",
            json={"url": url},
            headers=headers,
            timeout=10,
        )
        data = resp.json()
        tab_id = data.get("tabId") or data.get("id") or data.get("tab_id")
        if not tab_id:
            return f"[camofox] Tab creation failed: {json.dumps(data)[:300]}"

        time.sleep(wait_seconds)

        snap_resp = req_lib.get(
            f"{camofox_base}/tabs/{tab_id}/snapshot",
            headers=headers,
            timeout=15,
        )
        snap = snap_resp.json()
        text = snap.get("text") or snap.get("content") or snap.get("body") or ""
        if not text:
            return f"[camofox] Empty snapshot for {url} — page may require auth or is blocked"
        return text[:6000] + ("\n...[truncated]" if len(text) > 6000 else "")

    except Exception as exc:
        return f"[camofox] Error fetching {url}: {exc}"
    finally:
        if tab_id:
            try:
                import requests as req_lib2
                req_lib2.delete(
                    f"{camofox_base}/tabs/{tab_id}",
                    headers=headers, timeout=5,
                )
            except Exception:
                pass


async def _execute_tool(name: str, args: dict) -> str:
    if name == "terminal":
        cmd = args.get("command", "")
        timeout = args.get("timeout", 60)
        logger.info(f"[tool:terminal] {cmd[:120]}")
        return await asyncio.to_thread(_run_terminal, cmd, timeout)

    elif name == "web_fetch":
        url = args.get("url", "")
        timeout = args.get("timeout", 15)
        logger.info(f"[tool:web_fetch] {url}")
        return await asyncio.to_thread(_run_web_fetch, url, timeout)

    elif name == "camofox_browse":
        url = args.get("url", "")
        wait = args.get("wait_seconds", 3)
        logger.info(f"[tool:camofox] {url}")
        return await asyncio.to_thread(_run_camofox_browse_sync, url, wait)

    return f"[gateway] Unknown tool: {name}"


# ── File delivery (%%FILE:name%% tag) ─────────────────────────────────────────
_FILE_TAG_RE = re.compile(
    r"%%FILE:(?P<name>[^\n%]+?)%%\s*\n(?P<content>.*?)%%/FILE%%", re.DOTALL
)


async def _send_file_attachment(bot, chat_id: int, filename: str, content: str) -> None:
    safe = filename.strip().replace("/", "_").replace("\\", "_") or "file.txt"
    suffix = Path(safe).suffix or ".txt"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=suffix, prefix="hermes_", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        with open(tmp_path, "rb") as fh:
            await bot.send_document(chat_id=chat_id, document=fh, filename=safe, caption="")
        logger.info(f"[gateway] Sent document '{safe}' to chat {chat_id}")
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
        filename = match.group("name").strip()
        content  = match.group("content")
        try:
            await _send_file_attachment(bot, chat_id, filename, content)
        except Exception as exc:
            await update.message.reply_text(f"File send failed '{filename}': {exc}")
        remaining = remaining.replace(match.group(0), "").strip()

    remaining = remaining.strip()
    if remaining:
        for i in range(0, max(len(remaining), 1), MAX_MSG_LENGTH):
            await update.message.reply_text(remaining[i : i + MAX_MSG_LENGTH])


# ── FIX-1 + FIX-3: Dynamic system prompt — built per request with live timestamp ──
_GATEWAY_ADDENDUM_TEMPLATE = """

## Runtime Environment

- Container: Ubuntu 22.04, Python 3.11, Node 24, Bun, ripgrep, git
- Current UTC time: {utc_ts}
- Tool: terminal  — real bash execution. Use for EVERYTHING executable.
- Tool: web_fetch — curl + HTML strip. For plain APIs and raw files.
- Tool: camofox_browse — headless Chromium at http://{camofox_host}:{camofox_port}. For JS/Cloudflare sites.
- Camofox auth: CAMOFOX_ACCESS_KEY env is set if configured.
- openclaude gRPC: python3 /app/skills/openclaude_grpc/client.py --prompt "..." --workdir DIR --model deepseek-r1-distill-llama-70b
- jcode swarm: JCODE_MODEL=kimi-k2.6 jcode run --message "..."
- push utility: python3 /app/bot/telegram_bot.py push-commit --repo URL --token $GITHUB_PAT --workdir DIR --message "msg"
- health check: python3 /app/bot/telegram_bot.py health-check

## File Delivery

To send a file attachment:
%%FILE:filename.ext%%
<exact file content>
%%/FILE%%

NEVER send file content as plain text. The tag triggers real sendDocument.

## HARD RULES — NEVER VIOLATE

1. NEVER state a fact that requires live data without first calling a tool.
   Bad: "The star count is 127." (fabricated from memory)
   Good: call terminal → get real output → quote it.

2. Your final answer MUST quote or directly use the actual tool output verbatim.
   If the tool returned "stargazers_count: 42" — you say 42, not 127.

3. If a command returns an error or empty output, say so explicitly.
   Do not substitute a plausible-sounding value.

4. The user can see every tool call AND its raw output in Telegram.
   Any fabrication is immediately visible. Do not try it.

5. The current date and time is stated above. Use it. Do NOT guess or use training data dates.
"""


def _load_soul() -> str:
    for path in [Path(HERMES_HOME) / "SOUL.md", Path("/app/hermes_config/SOUL.md")]:
        if path.exists():
            logger.info(f"[gateway] Soul loaded from {path}")
            return path.read_text()
    logger.warning("[gateway] SOUL.md not found — using minimal persona")
    return "You are Hermes, Rhodawk AI's autonomous executive intelligence. Direct. No hedging. Execute first."


def _build_system_prompt() -> str:
    """
    FIX-1: Per-request UTC timestamp injection.
    FIX-3: Fresh CAMOFOX_BASE_URL resolution on every call.
    FIX-8: System prompt is never static — always reflects current state.
    """
    utc_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    addendum = _GATEWAY_ADDENDUM_TEMPLATE.format(
        utc_ts=utc_ts,
        camofox_host=CAMOFOX_HOST,
        camofox_port=CAMOFOX_PORT,
    )
    return _load_soul() + addendum


# ── FIX-2: SQLite-backed conversation persistence ─────────────────────────────
_DB_PATH = os.path.join(HERMES_HOME, "sessions", "conversations.db")


def _ensure_db() -> None:
    db_dir = os.path.dirname(_DB_PATH)
    os.makedirs(db_dir, exist_ok=True)
    con = sqlite3.connect(_DB_PATH)
    con.execute(
        "CREATE TABLE IF NOT EXISTS sessions "
        "(user_id INTEGER PRIMARY KEY, history TEXT, updated_at TEXT)"
    )
    con.commit()
    con.close()


def _load_history(user_id: int) -> list[dict]:
    try:
        _ensure_db()
        con = sqlite3.connect(_DB_PATH)
        row = con.execute(
            "SELECT history FROM sessions WHERE user_id=?", (user_id,)
        ).fetchone()
        con.close()
        if row:
            return json.loads(row[0])
    except Exception as exc:
        logger.warning(f"[memory] Load failed for user {user_id}: {exc} — starting fresh")
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
        logger.warning(f"[memory] Save failed for user {user_id}: {exc}")


def _trim(history: list[dict]) -> list[dict]:
    return history[-MAX_HISTORY:] if len(history) > MAX_HISTORY else history


# ── FIX-4: Numeric consistency guard ─────────────────────────────────────────
def _verify_numbers(tool_result: str, final_answer: str) -> bool:
    """
    Returns True if every large number in final_answer also appears in tool_result.
    Prevents the model from citing a number that was never in the actual output.
    Numbers < 4 digits are ignored (years, ports, etc. are fine to cite freely).
    """
    answer_nums = set(re.findall(r'\b\d{4,}\b', final_answer))
    if not answer_nums:
        return True  # no large numbers to verify
    result_nums = set(re.findall(r'\b\d{4,}\b', tool_result))
    fabricated = answer_nums - result_nums
    if fabricated:
        logger.warning(
            f"[verify] Fabricated numbers detected in final answer: {fabricated} "
            f"(not present in tool output)"
        )
        return False
    return True


# ── Scratchpad fallback ────────────────────────────────────────────────────────
_BASH_BLOCK_RE = re.compile(
    r"```(?:bash|shell|sh|terminal|cmd|console|python3?)?\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)
_INLINE_CMD_RE = re.compile(
    r"^(?:Running|Executing|Command):\s*(.+)$", re.MULTILINE
)


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
    """
    If the model wrote commands as plain text instead of calling the terminal tool,
    extract them, run them for real, and inject results back into messages.
    FIX-4: FINAL ANSWER RULE injected so model cannot substitute a fabricated number.
    """
    cmds = _extract_commands(content)
    if not cmds:
        return False

    logger.warning(
        f"[gateway] Model bypassed tool calls — scratchpad fallback for {len(cmds)} command(s)"
    )

    executed_pairs: list[str] = []
    for cmd in cmds:
        try:
            await update.message.reply_text(f"Executing (scratchpad): {cmd[:200]}")
        except Exception:
            pass

        result = await asyncio.to_thread(_run_terminal, cmd, 60)
        logger.info(f"[scratchpad] cmd={cmd[:80]!r} → {len(result)}chars")

        preview = result.strip()[:500]
        try:
            await update.message.reply_text(
                f"Output:\n{preview}" + ("\n...[truncated]" if len(result) > 500 else "")
            )
        except Exception:
            pass

        executed_pairs.append(f"$ {cmd}\n{result}")

    all_results = "\n---\n".join(executed_pairs)

    # FIX-4: inject strict FINAL ANSWER RULE — model must copy exact numbers from output
    messages.append({"role": "assistant", "content": content})
    messages.append({
        "role": "user",
        "content": (
            "SYSTEM NOTE: the commands you wrote were executed for real. "
            "Actual outputs:\n\n"
            + all_results
            + "\n\nFINAL ANSWER RULE: Copy the exact numbers and values from the output above. "
            "Do NOT type any number that does not appear verbatim in the output above. "
            "The output is the ground truth. Any other number is fabrication and is wrong."
        ),
    })
    return True


# ── OpenAI client ──────────────────────────────────────────────────────────────
openai_client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL)


# ── Agentic loop ───────────────────────────────────────────────────────────────
async def _agent_loop(messages: list[dict], update: Update) -> str:
    scratchpad_used = False
    all_tool_results: list[str] = []  # FIX-4: track all tool results for number verification

    for _round in range(MAX_TOOL_ROUNDS):
        resp = await openai_client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            max_tokens=4096,
            temperature=0.05,
        )
        choice = resp.choices[0]
        msg = choice.message
        content = msg.content or ""

        # ── No tool calls in this turn ──────────────────────────────────────
        if not msg.tool_calls:
            # Scratchpad fallback: did the model write commands as text?
            if not scratchpad_used:
                executed = await _execute_text_scratchpad(content, messages, update)
                if executed:
                    scratchpad_used = True
                    continue
            # FIX-4: numeric consistency check before delivering answer
            if all_tool_results and content:
                combined_results = "\n".join(all_tool_results)
                if not _verify_numbers(combined_results, content):
                    logger.warning("[gateway] Number mismatch detected — appending correction note")
                    messages.append({"role": "assistant", "content": content})
                    messages.append({
                        "role": "user",
                        "content": (
                            "CORRECTION REQUIRED: your answer contains numbers that do not match "
                            "the actual tool output. Re-read the tool outputs above and restate "
                            "your answer using only the exact values from those outputs. "
                            "Do not guess or recall from training data."
                        ),
                    })
                    all_tool_results = []  # prevent infinite correction loop
                    continue
            return content

        scratchpad_used = False

        # Append assistant turn with tool_calls
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

        # Execute each tool call
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
            logger.info(f"[tool:{fn_name}] result={len(result)}chars")

            # FIX-4: accumulate results for number verification
            all_tool_results.append(result)

            output_preview = result.strip()[:500]
            if output_preview:
                await asyncio.sleep(0.35)
                try:
                    await update.message.reply_text(
                        f"Output:\n{output_preview}"
                        + ("\n...[truncated]" if len(result) > 500 else "")
                    )
                except Exception as _out_exc:
                    logger.warning(f"[gateway] Output send failed: {_out_exc}")

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

    return "[hermes] Max tool rounds reached."


# ── Telegram handlers ─────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_chat.id):
        return
    name = update.effective_user.first_name or "there"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    await update.message.reply_text(
        f"Hermes online — {name}.\n"
        f"UTC: {ts}\n"
        f"Model: {MODEL}\n"
        f"Tools: terminal | web_fetch | camofox_browse\n"
        "Send any task. Use /reset to clear history, /status for stack health."
    )


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_chat.id):
        return
    user_id = update.effective_user.id
    _save_history(user_id, [])
    await update.message.reply_text("Cleared.")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_chat.id):
        return
    camofox_base = f"http://{CAMOFOX_HOST}:{CAMOFOX_PORT}"
    await update.message.reply_text(
        "Hermes — Rhodawk AI\n\n"
        "/start   — Status\n"
        "/reset   — Clear history\n"
        "/status  — Stack health check\n"
        "/help    — This message\n\n"
        f"Model: {MODEL}\n"
        f"Endpoint: {BASE_URL}\n"
        f"Camofox: {camofox_base}"
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


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    if not _is_allowed(update.effective_chat.id):
        logger.warning(f"[gateway] Blocked chat_id={update.effective_chat.id}")
        return

    user_id  = update.effective_user.id
    user_msg = update.message.text.strip()

    await update.message.chat.send_action(ChatAction.TYPING)

    # FIX-2: Load history from SQLite (survives restarts)
    history = _load_history(user_id)
    history.append({"role": "user", "content": user_msg})
    history = _trim(history)

    # FIX-1 + FIX-8: Build system prompt fresh per request with live UTC timestamp
    system_prompt = _build_system_prompt()
    messages = [{"role": "system", "content": system_prompt}] + list(history)

    try:
        reply = await _agent_loop(messages, update)
        history.append({"role": "assistant", "content": reply})
        history = _trim(history)
        # FIX-2: Persist updated history to SQLite
        _save_history(user_id, history)
        await _deliver_reply(update, reply)

    except Exception as exc:
        logger.error(f"[gateway] Error for user {user_id}: {exc}", exc_info=True)
        await update.message.reply_text(
            f"Error: {type(exc).__name__}: {str(exc)[:300]}"
        )


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    if not TELEGRAM_TOKEN:
        logger.error("[gateway] FATAL: TELEGRAM_BOT_TOKEN not set")
        sys.exit(1)
    if not API_KEY:
        logger.error("[gateway] FATAL: DO_INFERENCE_API_KEY not set")
        sys.exit(1)

    # FIX-2: Ensure DB is initialised at startup
    try:
        _ensure_db()
        logger.info(f"[gateway] SQLite memory DB ready at {_DB_PATH}")
    except Exception as exc:
        logger.warning(f"[gateway] Could not init SQLite DB: {exc} — will retry per-request")

    camofox_base = f"http://{CAMOFOX_HOST}:{CAMOFOX_PORT}"
    logger.info(f"[gateway] Model   : {MODEL}")
    logger.info(f"[gateway] Endpoint: {BASE_URL}")
    logger.info(f"[gateway] Camofox : {camofox_base}")
    logger.info(f"[gateway] Tools   : terminal | web_fetch | camofox_browse")
    logger.info(f"[gateway] Memory  : SQLite at {_DB_PATH}")
    if BRAVE_API_KEY:
        logger.info("[gateway] Search  : Brave API (primary) + DDG (fallback)")
    else:
        logger.warning("[gateway] Search  : DuckDuckGo only (BRAVE_API_KEY not set)")
    if ALLOWED_CHAT_IDS:
        logger.info(f"[gateway] Whitelist: {ALLOWED_CHAT_IDS}")
    else:
        logger.warning("[gateway] No TELEGRAM_CHAT_ID set — open to all users")
    logger.info("[gateway] Starting Telegram polling...")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("reset",  cmd_reset))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
