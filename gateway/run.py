#!/usr/bin/env python3
"""
gateway/run.py — Rhodawk AI Telegram Gateway v3.0 (Full Agentic Loop)

Real tool execution via OpenAI function calling:
  terminal        — bash commands inside the container (openclaude, jcode, git, curl, etc.)
  web_fetch       — lightweight HTTP fetch + HTML strip (public APIs, raw files)
  camofox_browse  — stealth browser session (JS SPAs, Cloudflare, LinkedIn, YouTube)

The model (deepseek-v4-pro) gets SOUL.md as its persona + routing guide.
Each tool call is executed for real. Results feed back into the loop.
This replaces the previous plain-chat-loop that had no execution capability.

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
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
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
CAMOFOX_BASE_URL = f"http://{CAMOFOX_HOST}:{CAMOFOX_PORT}"
CAMOFOX_KEY      = os.environ.get("CAMOFOX_ACCESS_KEY", "")
MAX_MSG_LENGTH   = 4000
MAX_HISTORY      = 20
MAX_TOOL_ROUNDS  = 20

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
    The nested " inside the double-quoted -c string breaks bash:
    bash closes the outer string at the FIRST inner " and Python gets
    an incomplete, broken script — then the model hallucinates the answer.

    Fix: find the outer double-quoted -c argument, switch outer " to ',
    and escape any ' inside the script as '\\'' (bash single-quote escape).

    Works for: python3 -c "SCRIPT" anywhere in a pipeline.
    """
    marker = 'python3 -c "'
    idx = cmd.find(marker)
    if idx == -1:
        return cmd

    # Split: everything before the marker, then the argument content
    pre   = cmd[:idx] + "python3 -c "
    after = cmd[idx + len(marker):]   # everything after the opening "

    # The intended closing " is the LAST " in the remaining string.
    # (bash stops at the FIRST one, which is the bug; we use LAST as the intent.)
    last_q = after.rfind('"')
    if last_q == -1:
        return cmd  # no closing quote found — leave as-is

    script    = after[:last_q]      # the intended Python script content
    remainder = after[last_q + 1:]  # anything after the closing " (e.g. " 2>&1")

    # If the script has no nested " it's fine as-is — no need to touch it
    if '"' not in script:
        return cmd

    # Escape any single quotes in the script, then wrap in single quotes
    script_escaped = script.replace("'", "'\\''")
    logger.debug(f"[sanitizer] Rewrote python3 -c quoting for: {script[:60]!r}")
    return pre + "'" + script_escaped + "'" + remainder


# ── Real tool execution ────────────────────────────────────────────────────────

def _run_terminal(command: str, timeout: int = 60) -> str:
    timeout = max(1, min(int(timeout), 600))
    # Auto-repair the common broken python3 -c "...{data["key"]}..." quoting
    command = _fix_python_c_quotes(command)
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=timeout, executable="/bin/bash",
        )
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        if not output:
            return f"(exit {result.returncode}, no output)"
        # Prefix non-zero exits so the model knows the command failed
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

    wait_seconds = max(1, min(int(wait_seconds), 10))
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if CAMOFOX_KEY:
        headers["Authorization"] = f"Bearer {CAMOFOX_KEY}"

    # Health check
    try:
        h = req_lib.get(f"{CAMOFOX_BASE_URL}/health", headers=headers, timeout=5)
        if h.status_code != 200:
            return (
                f"[camofox] DOWN at {CAMOFOX_BASE_URL} (HTTP {h.status_code}). "
                "Falling back to plain curl — web_fetch may return empty for JS pages."
            )
    except Exception as exc:
        return (
            f"[camofox] Unreachable at {CAMOFOX_BASE_URL}: {exc}. "
            "Use web_fetch as fallback."
        )

    session_id = f"hermes-{uuid.uuid4().hex[:8]}"
    tab_id = None
    try:
        # Create tab
        resp = req_lib.post(
            f"{CAMOFOX_BASE_URL}/sessions/{session_id}/tabs",
            json={"url": url},
            headers=headers,
            timeout=10,
        )
        data = resp.json()
        tab_id = data.get("tabId") or data.get("id") or data.get("tab_id")
        if not tab_id:
            return f"[camofox] Tab creation failed: {json.dumps(data)[:300]}"

        # Wait for JS render
        time.sleep(wait_seconds)

        # Snapshot
        snap_resp = req_lib.get(
            f"{CAMOFOX_BASE_URL}/tabs/{tab_id}/snapshot",
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
                    f"{CAMOFOX_BASE_URL}/tabs/{tab_id}",
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


# ── System prompt ──────────────────────────────────────────────────────────────
_GATEWAY_ADDENDUM = f"""

## Runtime Environment

- Container: Ubuntu 22.04, Python 3.11, Node 24, Bun, ripgrep, git
- Tool: terminal  — real bash execution. Use for EVERYTHING executable.
- Tool: web_fetch — curl + HTML strip. For plain APIs and raw files.
- Tool: camofox_browse — headless Chromium at {CAMOFOX_BASE_URL}. For JS/Cloudflare sites.
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
"""


def _load_soul() -> str:
    for path in [Path(HERMES_HOME) / "SOUL.md", Path("/app/hermes_config/SOUL.md")]:
        if path.exists():
            logger.info(f"[gateway] Soul loaded from {path}")
            return path.read_text()
    logger.warning("[gateway] SOUL.md not found — using minimal persona")
    return "You are Hermes, Rhodawk AI's autonomous executive intelligence. Direct. No hedging. Execute first."


SYSTEM_PROMPT = _load_soul() + _GATEWAY_ADDENDUM

# ── OpenAI client + conversation history ──────────────────────────────────────
openai_client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL)
_conversations: dict[int, list[dict]] = {}


def _get_history(user_id: int) -> list[dict]:
    return _conversations.setdefault(user_id, [])


def _trim(history: list[dict]) -> list[dict]:
    return history[-MAX_HISTORY:] if len(history) > MAX_HISTORY else history


# ── Scratchpad fallback: execute bash blocks the model wrote as plain text ──────
_BASH_BLOCK_RE = re.compile(
    r"```(?:bash|shell|sh|terminal|cmd|console|python3?)?\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)
# Also catch single-line commands the model writes without fences
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
    Returns True if any commands were found and executed.
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

    # Inject real results — model MUST use these in its answer
    messages.append({"role": "assistant", "content": content})
    messages.append({
        "role": "user",
        "content": (
            "SYSTEM NOTE: the commands you wrote were executed for real. "
            "Actual outputs:\n\n"
            + "\n---\n".join(executed_pairs)
            + "\n\nNow write your final answer using ONLY the actual outputs above. "
            "Do NOT invent or guess any numbers or facts."
        ),
    })
    return True


# ── Agentic loop ───────────────────────────────────────────────────────────────
async def _agent_loop(messages: list[dict], update: Update) -> str:
    scratchpad_used = False

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
                    continue  # re-query with real results injected
            # Genuine final answer
            return content

        scratchpad_used = False  # reset if real tool calls are being used

        # Append assistant turn with proper tool_calls
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

            # Live progress
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

            # Show real raw output — makes fabrication impossible to hide
            output_preview = result.strip()[:500]
            if output_preview:
                await asyncio.sleep(0.35)   # Telegram rate-limit buffer
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
    await update.message.reply_text(
        f"Hermes online — {name}.\n"
        f"Model: {MODEL}\n"
        f"Tools: terminal | web_fetch | camofox_browse\n"
        "Send any task. Use /reset to clear history, /status for stack health."
    )


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_chat.id):
        return
    _conversations.pop(update.effective_user.id, None)
    await update.message.reply_text("Cleared.")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_chat.id):
        return
    await update.message.reply_text(
        "Hermes — Rhodawk AI\n\n"
        "/start   — Status\n"
        "/reset   — Clear history\n"
        "/status  — Stack health check\n"
        "/help    — This message\n\n"
        f"Model: {MODEL}\n"
        f"Endpoint: {BASE_URL}\n"
        f"Camofox: {CAMOFOX_BASE_URL}"
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

    history = _get_history(user_id)
    history.append({"role": "user", "content": user_msg})
    history = _trim(history)
    _conversations[user_id] = history

    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + list(history)

    try:
        reply = await _agent_loop(messages, update)
        history.append({"role": "assistant", "content": reply})
        _conversations[user_id] = _trim(history)
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

    logger.info(f"[gateway] Model   : {MODEL}")
    logger.info(f"[gateway] Endpoint: {BASE_URL}")
    logger.info(f"[gateway] Camofox : {CAMOFOX_BASE_URL}")
    logger.info(f"[gateway] Tools   : terminal | web_fetch | camofox_browse")
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
