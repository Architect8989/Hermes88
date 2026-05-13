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
EXA_API_KEY    = os.environ.get("EXA_API_KEY", "")
FAL_API_KEY    = os.environ.get("FAL_API_KEY", "")
GITHUB_PAT     = os.environ.get("GITHUB_PAT", "")
MAX_MSG_LENGTH  = 4000
MAX_HISTORY     = 20
MAX_TOOL_ROUNDS = 25

if not BRAVE_API_KEY:
    logger.warning("[gateway] BRAVE_API_KEY not set — DuckDuckGo+Exa+camofox fallback active")
if not GITHUB_PAT:
    logger.warning("[gateway] GITHUB_PAT not set — git push via push-commit will fail")
if not FAL_API_KEY:
    logger.warning("[gateway] FAL_API_KEY not set — image generation disabled")

# ── Rhodawk Core module imports ───────────────────────────────────────────────
import sys as _sys
_sys.path.insert(0, "/app")
try:
    from rhodawk_core.skill_engine import get_skill_engine, TaskExecution
    _skill_engine_available = True
except ImportError:
    _skill_engine_available = False
    logger.warning("[gateway] rhodawk_core.skill_engine not available — skill learning disabled")

try:
    from rhodawk_core.operator_model import get_operator_model
    _operator_model_available = True
except ImportError:
    _operator_model_available = False
    logger.warning("[gateway] rhodawk_core.operator_model not available")

try:
    from rhodawk_core.image_gen import generate_image as _fal_generate, format_result as _fal_format_result
    _image_gen_available = True
except ImportError:
    _image_gen_available = False
    logger.warning("[gateway] rhodawk_core.image_gen not available — FAL.ai disabled")

# FIX Problem-5: Wire rhodawk_core.Orchestrator for real multi-provider failover routing.
# The Orchestrator adds: rate-limit tracking, exponential backoff, multi-model chains,
# and (Problem-7) Anthropic / OpenRouter as genuinely different tier-3/tier-4 providers.
# The primary agent loop still uses AsyncOpenAI with streaming for real-time delivery;
# the Orchestrator is used when the primary provider fails (429, 503, timeout).
try:
    from rhodawk_core.orchestrator import Orchestrator as _Orchestrator
    _orchestrator = _Orchestrator()
    _orchestrator_available = True
    logger.info("[gateway] rhodawk_core.Orchestrator loaded — multi-provider failover active")
except Exception as _orch_exc:
    _orchestrator = None
    _orchestrator_available = False
    logger.warning(f"[gateway] rhodawk_core.Orchestrator not available: {_orch_exc}")

# FIX Problem-5: Wire rhodawk_core.TaskQueue for background task execution.
# Background tasks submitted by the LLM (long-running ops, scheduled work) go into
# the queue and are processed by WorkerPool workers. Falls back to fire-and-forget
# asyncio.create_task when Redis is unavailable (Problem-9 graceful degradation).
try:
    from rhodawk_core.task_engine import TaskQueue as _TaskQueue, Task as _Task, TaskPriority as _TaskPriority
    _task_queue_available = True
    logger.info("[gateway] rhodawk_core.TaskQueue loaded — background task queue active")
except Exception as _tq_exc:
    _TaskQueue = None
    _task_queue_available = False
    logger.warning(f"[gateway] rhodawk_core.TaskQueue not available: {_tq_exc}")

# FIX Problem-6: Dangerous command patterns that require operator approval
# before execution (terminal tool). HERMES_YOLO_MODE=1 bypasses this gate.
# Matches: rm -rf, sudo rm, mkfs, dd if=, shred, fork bomb, pipe-to-bash.
import re as _re_danger
_DANGEROUS_CMD_RE = _re_danger.compile(
    r"\b(rm\s+-[rRfF]*\s+[^;|&\n]{2,}|sudo\s+rm|mkfs|dd\s+if=|shred|"
    r":\s*\(\s*\)\s*\{.*?\}|chmod\s+777|chown\s+-R\s+root|"
    r"iptables\s+-F|shutdown|reboot|halt|poweroff|"
    r"curl\s+.*\|\s*bash|wget\s+.*\|\s*bash)\b",
    _re_danger.IGNORECASE | _re_danger.DOTALL,
)
_YOLO_MODE = os.environ.get("HERMES_YOLO_MODE", "1").strip() == "1"

# Per-chat pending approval registry: chat_id -> {command, future}
_pending_approvals: dict[int, dict] = {}
_approvals_lock = asyncio.Lock() if False else None  # created lazily

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
            "description": "Headless Chromium stealth browser. JS SPAs, Cloudflare, LinkedIn, YouTube. For interactive pages use camofox_act instead.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "wait_seconds": {"type": "integer", "default": 3}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "camofox_act",
            "description": (
                "Interact with a page already open in camofox: click, type, scroll, press keys. "
                "Use after camofox_browse to fill forms, click buttons, navigate SPAs. "
                "tab_id required — returned by camofox_browse_tab (use terminal + camofox API if needed)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tab_id": {"type": "string", "description": "Active tab ID from a prior camofox session"},
                    "action": {"type": "string", "enum": ["click", "type", "scroll", "press", "navigate", "back", "forward", "refresh"]},
                    "selector": {"type": "string", "description": "CSS selector or element ref for click/type"},
                    "text": {"type": "string", "description": "Text to type (for action=type)"},
                    "key": {"type": "string", "description": "Key name for action=press (e.g. Enter, Tab, Escape)"},
                    "direction": {"type": "string", "enum": ["up", "down", "left", "right"], "description": "Scroll direction"},
                    "amount": {"type": "integer", "default": 500, "description": "Scroll amount in pixels"},
                    "url": {"type": "string", "description": "URL for action=navigate"},
                    "user_id": {"type": "string", "default": "hermes"}
                },
                "required": ["tab_id", "action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "camofox_extract",
            "description": (
                "Extract structured data from a page using a JSON schema. "
                "Use for price scraping, table extraction, contact lists, job postings. "
                "Returns JSON matching your schema. More reliable than regex on HTML."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to browse and extract from"},
                    "schema": {
                        "type": "object",
                        "description": "JSON schema describing the structure to extract. Example: {\"price\": \"string\", \"title\": \"string\"}",
                    },
                    "wait_seconds": {"type": "integer", "default": 3},
                    "user_id": {"type": "string", "default": "hermes"}
                },
                "required": ["url", "schema"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "camofox_screenshot",
            "description": (
                "Take a screenshot of a live webpage for visual verification. "
                "Returns local path to PNG file. Use to verify UI changes, dashboards, paywalled content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "wait_seconds": {"type": "integer", "default": 2},
                    "user_id": {"type": "string", "default": "hermes"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "camofox_auth",
            "description": (
                "Inject authentication cookies into a camofox browser session. "
                "Use to access sites that require login without exposing credentials in URLs. "
                "Pair with camofox_browse to access authenticated content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "Session user ID to inject cookies into"},
                    "cookies": {
                        "type": "array",
                        "description": "List of cookie objects with name, value, domain fields",
                        "items": {"type": "object"}
                    }
                },
                "required": ["user_id", "cookies"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "camofox_youtube",
            "description": (
                "Fetch the full transcript of any YouTube video. "
                "Use for summarizing talks, extracting technical content, research. "
                "Much faster than watching the video."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "video_url": {"type": "string", "description": "Full YouTube URL or video ID"},
                    "language": {"type": "string", "default": "en", "description": "Transcript language code"}
                },
                "required": ["video_url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": (
                "Generate an image using FAL.ai (FLUX Schnell — fastest model, free tier). "
                "Use for creating logos, mockups, diagrams, marketing assets, or any visual. "
                "Returns local path and public URL. Requires FAL_API_KEY secret."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Detailed image description"},
                    "model": {
                        "type": "string",
                        "default": "fal-ai/flux/schnell",
                        "description": "FAL model: fal-ai/flux/schnell (fast), fal-ai/flux/dev (quality), fal-ai/imagen4/preview"
                    },
                    "width": {"type": "integer", "default": 1024},
                    "height": {"type": "integer", "default": 1024},
                    "num_inference_steps": {"type": "integer", "default": 4, "description": "4=fast/schnell, 20-50=quality models"}
                },
                "required": ["prompt"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "find_skill",
            "description": (
                "Search the learned skill index for a proven procedure matching this task. "
                "Call this FIRST before tackling any complex multi-step task — "
                "you may have already mastered this exact workflow."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Task description to search for"}
                },
                "required": ["query"]
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


# ── Layer A: Complete camofox toolkit ────────────────────────────────────────

def _camofox_headers() -> dict:
    h = {"Content-Type": "application/json"}
    if CAMOFOX_KEY:
        h["Authorization"] = f"Bearer {CAMOFOX_KEY}"
    return h

def _camofox_health_check() -> bool:
    try:
        import requests as rlib
        resp = rlib.get(f"http://{CAMOFOX_HOST}:{CAMOFOX_PORT}/health",
                        headers=_camofox_headers(), timeout=5)
        return resp.status_code == 200
    except Exception:
        return False

def _run_camofox_act(tab_id: str, action: str, **kwargs) -> str:
    """Interact with an open camofox tab: click, type, scroll, press, navigate."""
    try:
        import requests as rlib
    except ImportError:
        return "[camofox_act] requests not installed"
    if not _camofox_health_check():
        return "[camofox_act] camofox DOWN"
    base = f"http://{CAMOFOX_HOST}:{CAMOFOX_PORT}"
    headers = _camofox_headers()
    user_id = kwargs.get("user_id", "hermes")
    try:
        if action == "click":
            payload = {"userId": user_id, "selector": kwargs.get("selector", ""), "ref": kwargs.get("ref", "")}
            r = rlib.post(f"{base}/tabs/{tab_id}/click", json=payload, headers=headers, timeout=10)
        elif action == "type":
            payload = {"userId": user_id, "text": kwargs.get("text", ""), "selector": kwargs.get("selector", "")}
            r = rlib.post(f"{base}/tabs/{tab_id}/type", json=payload, headers=headers, timeout=10)
        elif action == "scroll":
            payload = {"userId": user_id, "direction": kwargs.get("direction", "down"), "amount": kwargs.get("amount", 500)}
            r = rlib.post(f"{base}/tabs/{tab_id}/scroll", json=payload, headers=headers, timeout=10)
        elif action == "press":
            payload = {"userId": user_id, "key": kwargs.get("key", "Enter")}
            r = rlib.post(f"{base}/tabs/{tab_id}/press", json=payload, headers=headers, timeout=10)
        elif action == "navigate":
            payload = {"userId": user_id, "url": kwargs.get("url", "")}
            r = rlib.post(f"{base}/tabs/{tab_id}/navigate", json=payload, headers=headers, timeout=10)
        elif action in ("back", "forward", "refresh"):
            payload = {"userId": user_id}
            r = rlib.post(f"{base}/tabs/{tab_id}/{action}", json=payload, headers=headers, timeout=10)
        else:
            return f"[camofox_act] Unknown action: {action}"
        if r.status_code == 200:
            data = r.json()
            # Get snapshot after action
            import time as _t
            _t.sleep(1)
            snap = rlib.get(f"{base}/tabs/{tab_id}/snapshot", headers=headers, timeout=10).json()
            text = snap.get("text", "")[:4000]
            return f"[camofox_act:{action}] OK\nPage state:\n{text}"
        return f"[camofox_act:{action}] HTTP {r.status_code}: {r.text[:300]}"
    except Exception as exc:
        return f"[camofox_act:{action}] Error: {exc}"

def _run_camofox_extract(url: str, schema: dict, wait_seconds: int = 3, user_id: str = "hermes") -> str:
    """Extract structured data from a page using a JSON schema via camofox POST /tabs/{id}/extract."""
    try:
        import requests as rlib
    except ImportError:
        return "[camofox_extract] requests not installed"
    if not _camofox_health_check():
        return "[camofox_extract] camofox DOWN — use web_fetch + regex fallback"
    base = f"http://{CAMOFOX_HOST}:{CAMOFOX_PORT}"
    headers = _camofox_headers()
    session_id = f"hermes-ext-{uuid.uuid4().hex[:8]}"
    tab_id = None
    try:
        resp = rlib.post(f"{base}/tabs",
                         json={"userId": session_id, "sessionKey": "extract", "url": url},
                         headers=headers, timeout=12)
        data = resp.json()
        tab_id = data.get("tabId") or data.get("id") or data.get("tab_id")
        if not tab_id:
            return f"[camofox_extract] Tab creation failed: {data}"
        import time as _t
        _t.sleep(wait_seconds)
        extract_resp = rlib.post(
            f"{base}/tabs/{tab_id}/extract",
            json={"userId": session_id, "schema": schema},
            headers=headers, timeout=20,
        )
        extracted = extract_resp.json()
        return json.dumps(extracted, indent=2)[:6000]
    except Exception as exc:
        return f"[camofox_extract] Error: {exc}"
    finally:
        if tab_id:
            try:
                rlib.delete(f"{base}/tabs/{tab_id}", headers=headers, timeout=5)
            except Exception:
                pass

def _run_camofox_screenshot(url: str, wait_seconds: int = 2, user_id: str = "hermes") -> str:
    """Take a screenshot of a URL. Returns local path to PNG."""
    try:
        import requests as rlib
    except ImportError:
        return "[camofox_screenshot] requests not installed"
    if not _camofox_health_check():
        return "[camofox_screenshot] camofox DOWN"
    base = f"http://{CAMOFOX_HOST}:{CAMOFOX_PORT}"
    headers = _camofox_headers()
    session_id = f"hermes-ss-{uuid.uuid4().hex[:8]}"
    tab_id = None
    try:
        resp = rlib.post(f"{base}/tabs",
                         json={"userId": session_id, "sessionKey": "screenshot", "url": url},
                         headers=headers, timeout=12)
        data = resp.json()
        tab_id = data.get("tabId") or data.get("id") or data.get("tab_id")
        if not tab_id:
            return f"[camofox_screenshot] Tab creation failed: {data}"
        import time as _t
        _t.sleep(wait_seconds)
        ss_resp = rlib.get(f"{base}/tabs/{tab_id}/screenshot", headers=headers, timeout=20)
        if ss_resp.status_code != 200:
            return f"[camofox_screenshot] HTTP {ss_resp.status_code}: {ss_resp.text[:200]}"
        # Save PNG to hermes home
        img_dir = Path(HERMES_HOME) / "screenshots"
        img_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        safe_url = re.sub(r'[^a-z0-9]', '_', url.lower())[:40]
        out_path = img_dir / f"{ts}_{safe_url}.png"
        out_path.write_bytes(ss_resp.content)
        return f"[camofox_screenshot] Screenshot saved: {out_path} ({len(ss_resp.content)} bytes)"
    except Exception as exc:
        return f"[camofox_screenshot] Error: {exc}"
    finally:
        if tab_id:
            try:
                rlib.delete(f"{base}/tabs/{tab_id}", headers=headers, timeout=5)
            except Exception:
                pass

def _run_camofox_auth(user_id: str, cookies: list) -> str:
    """Inject cookies into a camofox session for authenticated browsing."""
    try:
        import requests as rlib
    except ImportError:
        return "[camofox_auth] requests not installed"
    if not _camofox_health_check():
        return "[camofox_auth] camofox DOWN"
    base = f"http://{CAMOFOX_HOST}:{CAMOFOX_PORT}"
    headers = _camofox_headers()
    try:
        resp = rlib.post(
            f"{base}/sessions/{user_id}/cookies",
            json={"cookies": cookies},
            headers=headers,
            timeout=10,
        )
        if resp.status_code == 200:
            return f"[camofox_auth] {len(cookies)} cookies injected into session '{user_id}'. Now use camofox_browse with the same user_id."
        return f"[camofox_auth] HTTP {resp.status_code}: {resp.text[:300]}"
    except Exception as exc:
        return f"[camofox_auth] Error: {exc}"

def _run_camofox_youtube(video_url: str, language: str = "en") -> str:
    """Fetch a YouTube video transcript via camofox /youtube/transcript."""
    try:
        import requests as rlib
    except ImportError:
        return "[camofox_youtube] requests not installed"
    if not _camofox_health_check():
        # Fallback: yt-dlp
        return _youtube_fallback(video_url, language)
    base = f"http://{CAMOFOX_HOST}:{CAMOFOX_PORT}"
    headers = _camofox_headers()
    # Extract video ID
    vid_match = re.search(r'(?:v=|youtu\.be/|/embed/)([a-zA-Z0-9_-]{11})', video_url)
    video_id = vid_match.group(1) if vid_match else video_url.strip()
    try:
        resp = rlib.get(
            f"{base}/youtube/transcript",
            params={"videoId": video_id, "lang": language},
            headers=headers,
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            transcript = data.get("transcript") or data.get("text") or json.dumps(data)[:6000]
            return transcript[:6000] + ("\n...[truncated]" if len(str(transcript)) > 6000 else "")
        return f"[camofox_youtube] HTTP {resp.status_code}: {resp.text[:300]}"
    except Exception as exc:
        return _youtube_fallback(video_url, language)

def _youtube_fallback(video_url: str, language: str = "en") -> str:
    """yt-dlp fallback for YouTube transcripts."""
    try:
        result = subprocess.run(
            ["yt-dlp", "--skip-download", "--write-auto-sub", "--sub-lang", language,
             "--sub-format", "vtt", "-o", "/tmp/hermes_yt_%(id)s", video_url],
            capture_output=True, text=True, timeout=60,
        )
        import glob
        vtt_files = glob.glob("/tmp/hermes_yt_*.vtt")
        if vtt_files:
            content = Path(vtt_files[0]).read_text()
            # Strip VTT header/timestamps
            lines = [l for l in content.splitlines()
                     if l and not l.startswith("WEBVTT") and "-->" not in l
                     and not re.match(r'^\d{2}:', l)]
            return "\n".join(lines)[:6000]
        return f"[camofox_youtube] yt-dlp: {result.stderr[:300]}"
    except FileNotFoundError:
        return "[camofox_youtube] Neither camofox /youtube/transcript nor yt-dlp available"
    except Exception as exc:
        return f"[camofox_youtube] Fallback error: {exc}"

def _run_generate_image(prompt: str, model: str = "fal-ai/flux/schnell",
                         width: int = 1024, height: int = 1024,
                         num_inference_steps: int = 4) -> str:
    """Generate image via FAL.ai."""
    if not _image_gen_available:
        return "[generate_image] rhodawk_core.image_gen not loaded — check /app/rhodawk_core/image_gen.py"
    if not FAL_API_KEY:
        return ("[generate_image] FAL_API_KEY not set. "
                "Get a free key at https://fal.ai and add it as FAL_API_KEY secret.")
    result = _fal_generate(
        prompt=prompt,
        model=model,
        width=width,
        height=height,
        num_inference_steps=num_inference_steps,
    )
    msg = _fal_format_result(result)
    if result.success and result.local_path:
        # Signal Telegram to send the image as a photo
        return f"[IMAGE_GENERATED]\npath={result.local_path}\nurl={result.image_url}\n{msg}"
    return msg

def _run_find_skill(query: str) -> str:
    """Search the learned skill index."""
    if not _skill_engine_available:
        return "[find_skill] skill_engine not available — check /app/rhodawk_core/skill_engine.py"
    try:
        engine = get_skill_engine()
        context = engine.build_skill_context(query)
        if context:
            return context
        index_summary = engine.get_skill_index_summary()
        return f"[find_skill] No matching skill found.\n\n{index_summary}"
    except Exception as exc:
        return f"[find_skill] Error: {exc}"


async def _execute_tool(name: str, args: dict, update: Update = None) -> str:
    if name == "terminal":
        command = args.get("command", "")
        timeout = args.get("timeout", 60)

        # FIX Problem-6: Dangerous command approval gate.
        # If HERMES_YOLO_MODE != 1 and the command matches a destructive pattern,
        # send a Telegram approval request and wait up to 5 minutes for y/n.
        # This mirrors openclaude's ActionRequired mechanism (Problem-6 fix).
        if not _YOLO_MODE and command and _DANGEROUS_CMD_RE.search(command):
            logger.warning(f"[dangerous-cmd] ActionRequired: {command[:120]}")
            if update is not None:
                chat_id = update.effective_chat.id
                try:
                    await update.message.reply_text(
                        f"⚠️ DANGEROUS COMMAND REQUIRES APPROVAL\n\n"
                        f"Command: `{command[:300]}`\n\n"
                        f"Reply y to approve, n to abort. Timeout: 5 minutes.",
                        parse_mode="Markdown",
                    )
                    # Store a future that the /approve or next message will resolve
                    loop = asyncio.get_event_loop()
                    approval_future: asyncio.Future = loop.create_future()
                    _pending_approvals[chat_id] = {
                        "command": command,
                        "future": approval_future,
                        "created_at": time.time(),
                    }
                    try:
                        # Wait up to 300s for operator to reply y/n
                        reply = await asyncio.wait_for(
                            asyncio.shield(approval_future), timeout=300
                        )
                        if not reply:
                            return f"[terminal] Command aborted by operator: {command[:120]}"
                    except asyncio.TimeoutError:
                        _pending_approvals.pop(chat_id, None)
                        return f"[terminal] Approval timed out — command NOT executed: {command[:120]}"
                    finally:
                        _pending_approvals.pop(chat_id, None)
                except Exception as gate_exc:
                    logger.error(f"[dangerous-cmd] Approval gate error: {gate_exc}")
                    return f"[terminal] Could not request approval — command NOT executed for safety."
            else:
                # No Telegram context (e.g., internal call) — block if not YOLO
                return (
                    f"[terminal] Blocked dangerous command (HERMES_YOLO_MODE not set): "
                    f"{command[:120]}"
                )

        return await asyncio.to_thread(_run_terminal, command, timeout)
    elif name == "web_fetch":
        return await asyncio.to_thread(_run_web_fetch, args.get("url", ""), args.get("timeout", 15))
    elif name == "camofox_browse":
        return await asyncio.to_thread(_run_camofox_browse_sync, args.get("url", ""), args.get("wait_seconds", 3))
    elif name == "camofox_act":
        return await asyncio.to_thread(
            _run_camofox_act,
            args.get("tab_id", ""), args.get("action", ""),
            **{k: v for k, v in args.items() if k not in ("tab_id", "action")}
        )
    elif name == "camofox_extract":
        return await asyncio.to_thread(
            _run_camofox_extract,
            args.get("url", ""), args.get("schema", {}),
            args.get("wait_seconds", 3), args.get("user_id", "hermes"),
        )
    elif name == "camofox_screenshot":
        return await asyncio.to_thread(
            _run_camofox_screenshot,
            args.get("url", ""), args.get("wait_seconds", 2), args.get("user_id", "hermes"),
        )
    elif name == "camofox_auth":
        return await asyncio.to_thread(
            _run_camofox_auth,
            args.get("user_id", "hermes"), args.get("cookies", []),
        )
    elif name == "camofox_youtube":
        return await asyncio.to_thread(
            _run_camofox_youtube,
            args.get("video_url", ""), args.get("language", "en"),
        )
    elif name == "generate_image":
        return await asyncio.to_thread(
            _run_generate_image,
            args.get("prompt", ""),
            args.get("model", "fal-ai/flux/schnell"),
            args.get("width", 1024),
            args.get("height", 1024),
            args.get("num_inference_steps", 4),
        )
    elif name == "find_skill":
        return await asyncio.to_thread(_run_find_skill, args.get("query", ""))
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


# Layers A/E/F/G/H: Updated runtime block with full tool roster + 4-tier search + skill index
_RUNTIME_BLOCK = """

═══════════════════════════════════════════════════════
HERMES RUNTIME v8.0 — READ BEFORE EVERY RESPONSE
═══════════════════════════════════════════════════════

UTC NOW:       {utc_ts}
OPERATOR:      {operator_name}
TELEGRAM:      {telegram_username}
FIRST SEEN:    {first_seen}
NOTES:         {operator_notes}
LAST TASK:     {last_task}
MODEL:         {model}
GITHUB PAT:    {github_pat_status}
SEARCH STACK:  {search_status}
IMAGE GEN:     {image_status}

──── LONG-TERM MEMORY (auto-loaded every session) ────
{memory_block}
──── END MEMORY ────

──── OPERATOR BEHAVIORAL MODEL ────
{operator_model_block}
──── END OPERATOR MODEL ────

──── LEARNED SKILLS ────
{skill_index_block}
──── END SKILLS ────

──── ENVIRONMENT STATUS ────
{briefing_block}
──── END STATUS ────

══════════════════════════════════════════════════════
TOOLS — call these, never narrate them:

CORE:
  terminal              — real bash. Use for EVERYTHING executable.
  web_fetch             — curl + HTML strip. Public APIs, raw files.
  find_skill            — search learned skill index BEFORE any complex task.

STEALTH BROWSER (Layer A — complete camofox toolkit):
  camofox_browse        — headless Chromium. JS SPAs, Cloudflare, LinkedIn, YouTube.
  camofox_act           — interact with open tab: click/type/scroll/press/navigate.
  camofox_extract       — extract structured JSON from page using schema.
  camofox_screenshot    — screenshot URL → local PNG file.
  camofox_auth          — inject cookies into session for authenticated browsing.
  camofox_youtube       — full transcript of any YouTube video.

BROWSER SEARCH MACROS (use inside camofox_browse url field):
  @google_search?q=QUERY          → Google search via stealth browser
  @bing_search?q=QUERY            → Bing search
  @duckduckgo_search?q=QUERY      → DDG search
  @linkedin_search?q=QUERY        → LinkedIn people/company search

IMAGE GENERATION (Layer H — FAL.ai):
  generate_image        — create images via FLUX Schnell (free tier, fast). Sends as Telegram photo.

WEB SEARCH — 4-TIER CASCADE (Layer G). NEVER say "search unavailable":

  Tier 1 — DDG (default, no key, always works):
    python3 -c '
from duckduckgo_search import DDGS
for r in DDGS().text("QUERY HERE", max_results=5):
    print(r["title"]); print(r["href"]); print(r["body"][:300]); print()
'

  Tier 2 — Brave (higher quality, BRAVE_API_KEY required):
    curl -s "https://api.search.brave.com/res/v1/web/search?q=QUERY&count=5" \
      -H "Accept: application/json" \
      -H "X-Subscription-Token: $BRAVE_API_KEY" | \
      jq -r '.web.results[] | "\(.title)\n\(.url)\n\(.description)\n"'

  Tier 3 — Exa AI (semantic, EXA_API_KEY required):
    curl -s "https://api.exa.ai/search" \
      -H "x-api-key: $EXA_API_KEY" -H "Content-Type: application/json" \
      -d '{{"query":"QUERY","numResults":5,"useAutoprompt":true}}' | jq '.results[]'

  Tier 4 — camofox Google (always works, cannot be blocked):
    Use camofox_browse with url="@google_search?q=QUERY"

  RULE: If tier N fails, immediately try tier N+1. Never declare search failed.

JCODE PERSISTENT SESSIONS (Layer B):
  python3 /app/skills/jcode_swarm/session_manager.py \
    --project "OWNER/REPO" --task "task description" --workdir /tmp/repos/REPO

SUB-AGENTS:
  openclaude: python3 /app/skills/openclaude_grpc/client.py --prompt "..." --workdir DIR
  jcode:      OPENAI_BASE_URL=$DO_INFERENCE_BASE_URL OPENAI_API_KEY=$DO_INFERENCE_API_KEY \
              jcode run --message "..." --session SESSION_KEY
  health:     python3 /app/bot/telegram_bot.py health-check

VERSION CHECKS — always call terminal, never write from memory:
  python3 --version && pip show openai | grep Version

API LISTS — always call terminal:
  curl -s "$DO_INFERENCE_BASE_URL/models" \
    -H "Authorization: Bearer $DO_INFERENCE_API_KEY" | jq -r '.data[].id'

FILE DELIVERY — THE ONLY WAY TO SEND A FILE IS WITH %%FILE%% TAGS IN YOUR REPLY:

  %%FILE:check.sh%%
  #!/bin/bash
  echo hello world
  %%/FILE%%

  THE GATEWAY INTERCEPTS %%FILE%% TAGS AND SENDS THEM AS REAL TELEGRAM DOCUMENTS.
  If you don't use the tags, no file is ever sent. Period.

GIT PUSH — use push-commit utility. NEVER bare git push (no credentials):
  python3 /app/bot/telegram_bot.py push-commit \
    --repo https://github.com/Architect8989/Hermes88 \
    --token $GITHUB_PAT \
    --workdir /tmp/repos/REPONAME \
    --message "feat: description"

ABSOLUTE RULES — breaking any = Jarvis failure:
  1. TOOLS BEFORE TEXT. For any data/action query: call terminal first.
  2. SKILL FIRST. Before any complex multi-step task: call find_skill.
  3. ZERO FABRICATION. Search results, versions, stats, file content = tool call required.
  4. FILE TAG. Any file goes in %%FILE:%% tags. "Here it is:" with inline text = FAILURE.
  5. PUSH-COMMIT. Never bare git push. Always push-commit.
  6. NUMBERS from tool output only. Verbatim. No rounding.
  7. DATE = UTC timestamp above. Not training data.
  8. ADDRESS OPERATOR BY NAME. Use "{operator_name}" naturally.
  9. SEARCH CASCADE. DDG → Brave → Exa → camofox. Never declare search failed.
  10. JCODE SESSIONS. Use --session flag to persist jcode memory across tasks.
══════════════════════════════════════════════════════
"""

def _build_system_prompt(user_id: int | None = None, is_fresh_session: bool = False,
                          task_description: str = "") -> str:
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

    # Layer F: Operator behavioral model
    operator_model_block = "(not yet built)"
    if _operator_model_available and user_id:
        try:
            op_model = get_operator_model(user_id)
            operator_model_block = op_model.build_prompt_context()
        except Exception as _exc:
            logger.debug(f"[operator_model] Build failed: {_exc}")

    # Layer E: Skill index
    skill_index_block = "(no skills learned yet)"
    if _skill_engine_available:
        try:
            engine = get_skill_engine(model=MODEL)
            skill_index_block = engine.get_skill_index_summary()
            # If we have a task description, prepend matching skill context
            if task_description:
                skill_ctx = engine.build_skill_context(task_description)
                if skill_ctx:
                    skill_index_block = skill_ctx + "\n" + skill_index_block
        except Exception as _exc:
            logger.debug(f"[skill_engine] Context build failed: {_exc}")

    # Layer G: Search status
    search_tiers = ["DDG(always)"]
    if BRAVE_API_KEY:
        search_tiers.append("Brave")
    if EXA_API_KEY:
        search_tiers.append("Exa-AI")
    search_tiers.append("camofox-Google(stealth)")
    search_status = " → ".join(search_tiers)

    # Layer H: Image generation status
    image_status = f"FAL.ai FLUX/Schnell (SET)" if FAL_API_KEY else "NOT SET — add FAL_API_KEY secret"

    runtime = _RUNTIME_BLOCK.format(
        utc_ts=utc_ts,
        operator_name=operator_name,
        telegram_username=telegram_username,
        first_seen=first_seen,
        operator_notes=operator_notes,
        last_task=last_task,
        model=MODEL,
        github_pat_status="SET" if GITHUB_PAT else "NOT SET — pushes will fail",
        search_status=search_status,
        image_status=image_status,
        memory_block=memory_block,
        operator_model_block=operator_model_block,
        skill_index_block=skill_index_block,
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

        # FIX Problem-5: Wire rhodawk_core.Orchestrator as failover.
        # Primary path: AsyncOpenAI with streaming tool calling (real-time delivery).
        # Fallback path: Orchestrator.route_request() — tries Anthropic, then OpenRouter
        # when DO Inference returns 429/503. We run the fallback in a thread since
        # the Orchestrator uses synchronous urllib.request.
        resp = None
        try:
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
        except Exception as _primary_exc:
            # Primary DO Inference call failed — try Orchestrator fallback
            _err_str = str(_primary_exc).lower()
            _is_recoverable = any(x in _err_str for x in [
                "429", "503", "rate limit", "timeout", "connection", "overloaded"
            ])
            if _orchestrator_available and _is_recoverable:
                logger.warning(
                    f"[failover] Primary model error ({_primary_exc}) — "
                    f"trying Orchestrator multi-provider chain"
                )
                try:
                    _orch_result = await asyncio.to_thread(
                        _orchestrator.route_request,
                        messages,
                        "general",
                    )
                    if _orch_result.get("content"):
                        logger.info(
                            f"[failover] Orchestrator succeeded via "
                            f"{_orch_result.get('provider', '?')} / "
                            f"{_orch_result.get('model', '?')}"
                        )
                        # Wrap result to look like an OpenAI response with no tool calls
                        content = _orch_result["content"]
                        # Create a mock message object using a simple namespace
                        from types import SimpleNamespace
                        msg = SimpleNamespace(content=content, tool_calls=None)
                    else:
                        logger.error(
                            f"[failover] Orchestrator also failed: "
                            f"{_orch_result.get('error')}"
                        )
                        raise _primary_exc
                except Exception as _orch_exc:
                    logger.error(f"[failover] Orchestrator exception: {_orch_exc}")
                    raise _primary_exc
            else:
                raise

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

            result = await _execute_tool(fn_name, fn_args, update=update)
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


async def _post_task_hooks(
    user_id: int,
    user_msg: str,
    reply: str,
    tool_calls_log: list,
    tool_round_count: int,
    task_start_time: float,
    succeeded: bool,
) -> None:
    """
    Layer E + F: Post-task hooks.
    - Skill engine: evaluate task for skill creation
    - Operator model: infer behavioral patterns from task execution
    """
    duration_s = time.time() - task_start_time

    # Layer E: Skill engine
    if _skill_engine_available and tool_round_count >= 2:
        try:
            engine = get_skill_engine(model=MODEL)
            execution = TaskExecution(
                task_description=user_msg[:500],
                tool_calls_made=tool_calls_log,
                result_summary=reply[:300],
                duration_seconds=duration_s,
                tool_round_count=tool_round_count,
                succeeded=succeeded,
                model_used=MODEL,
            )
            skill = engine.evaluate_for_skill_creation(execution)
            if skill:
                engine.save_skill(skill)
                logger.info(f"[skill_engine] Learned new skill: {skill.title}")
        except Exception as exc:
            logger.debug(f"[skill_engine] Post-task hook failed: {exc}")

    # Layer F: Operator model inference
    if _operator_model_available:
        try:
            op_model = get_operator_model(user_id)
            op_model.infer_from_task(
                task=user_msg,
                result=reply[:300],
                tool_calls=tool_calls_log,
                duration_s=duration_s,
                timestamp=task_start_time,
            )
            op_model.infer_from_explicit(user_msg)
        except Exception as exc:
            logger.debug(f"[operator_model] Post-task hook failed: {exc}")


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    if not _is_allowed(update.effective_chat.id):
        logger.warning(f"[gateway] Blocked chat_id={update.effective_chat.id}")
        return

    user_id  = update.effective_user.id
    user_msg = update.message.text.strip()
    task_start = time.time()

    # Sync Telegram identity + update last_seen on every message
    _sync_profile_from_telegram(update)

    await update.message.chat.send_action(ChatAction.TYPING)

    history       = _load_history(user_id)
    fresh_session = (len(history) == 0)

    history.append({"role": "user", "content": user_msg})
    history = _trim(history)

    # JARVIS-1+2+3 + Layer E/F: system prompt with memory + profile + briefing + skills + operator model
    system_prompt = _build_system_prompt(
        user_id,
        is_fresh_session=fresh_session,
        task_description=user_msg,
    )
    messages = [{"role": "system", "content": system_prompt}] + list(history)

    try:
        reply = await _agent_loop(messages, update, user_msg)
        history.append({"role": "assistant", "content": reply})
        _save_history(user_id, _trim(history))

        # JARVIS-4: Auto-write memory after every non-trivial exchange
        if len(user_msg) > 15 and len(reply) > 20:
            _auto_write_memory(user_msg, reply)
            _upsert_profile(user_id, last_task=user_msg.strip()[:200])

        # Layer E + F: Post-task hooks (skill learning + operator modeling)
        # Collect tool round count from message history
        tool_rounds = sum(
            1 for m in messages if m.get("role") == "tool"
        )
        tool_calls_log = [
            {"name": m.get("name", ""), "arguments": {}}
            for m in messages if m.get("role") == "tool"
        ]
        asyncio.get_event_loop().call_soon(
            lambda: asyncio.ensure_future(
                _post_task_hooks(
                    user_id=user_id,
                    user_msg=user_msg,
                    reply=reply,
                    tool_calls_log=tool_calls_log,
                    tool_round_count=tool_rounds,
                    task_start_time=task_start,
                    succeeded=not reply.startswith("[hermes] Max tool rounds"),
                )
            )
        )

        # Layer H: If reply contains [IMAGE_GENERATED], send as Telegram photo
        if "[IMAGE_GENERATED]" in reply:
            import re as _re
            img_path_match = _re.search(r'path=([^\n]+)', reply)
            img_url_match  = _re.search(r'url=([^\n]+)', reply)
            img_path = img_path_match.group(1).strip() if img_path_match else ""
            img_url  = img_url_match.group(1).strip() if img_url_match else ""
            try:
                if img_path and Path(img_path).exists():
                    with open(img_path, "rb") as fh:
                        await update.message.reply_photo(
                            photo=fh,
                            caption=reply.split("\n", 3)[-1][:900],
                        )
                elif img_url:
                    await update.message.reply_photo(
                        photo=img_url,
                        caption=reply.split("\n", 3)[-1][:900],
                    )
                else:
                    await _deliver_reply(update, reply.replace("[IMAGE_GENERATED]\n", ""))
            except Exception as img_exc:
                logger.warning(f"[gateway] Image send failed: {img_exc}")
                await _deliver_reply(update, reply)
        else:
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
