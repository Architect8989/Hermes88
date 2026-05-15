#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# init_and_start.sh — v4.3 Full Integration Bootstrap
#
# IMPORTANT — Error Handling Policy:
#   This script uses `set -e` (exit on first error) combined with `|| true`
#   and `|| echo` fallbacks on NON-CRITICAL commands. This is intentional:
#   the system is designed for graceful degradation. If openclaw, jcode, or
#   MCP servers fail to configure, Hermes still starts with hermes-agent alone.
#
#   Commands that use `|| true` or `|| echo` are explicitly non-fatal:
#     - openclaw config set (openclaw is optional, disabled by default)
#     - openclaw doctor --fix (cosmetic)
#     - jcode session prewarm (optimization, not required)
#     - MCP YAML injection (hermes-agent works without MCP)
#
#   Commands that MUST succeed (no fallback):
#     - Secret validation (TELEGRAM_BOT_TOKEN, DO_INFERENCE_API_KEY, GITHUB_PAT)
#     - /data/.hermes/.env creation
#     - config.yaml and gateway.yaml template expansion
#     - supervisord exec (final CMD)
#
# What changed from v4.2 → v4.3:
#   FIX-8  HERMES_YOLO_MODE now defaults to 0 (safe). Override via env var.
#   FIX-9  Documented || true patterns and error handling policy (this header).
#
# What changed from v4.1 → v4.2:
#   FIX-5  gateway.yaml and config.yaml were copied verbatim with cp, leaving
#          ${DO_INFERENCE_API_KEY}, ${DO_INFERENCE_BASE_URL}, ${HERMES_MODEL}
#          etc. as literal strings. hermes-agent read these unresolved strings
#          as the actual credentials, producing HTTP 401 (Missing Authentication
#          header) on every LLM call. Now uses Python string.Template to
#          expand all ${VAR} references before writing to /data/.hermes/.
#   FIX-6  mcp_shared.json uses the "servers" key; init_and_start.sh was
#          reading "mcpServers" (empty dict) so zero MCP tools were configured
#          for hermes-agent. Now falls back to "servers" when "mcpServers" is
#          absent, so filesystem/github/fetch MCP servers reach hermes-agent.
#   FIX-7  TELEGRAM_CHAT_ID and TELEGRAM_ALLOWED_USERS now written to
#          /data/.hermes/.env so hermes-agent's allowlist is populated and the
#          "No user allowlists configured" warning is resolved.
#
# What changed from v4.0 → v4.1:
#   FIX-1  DO_BASE_URL was not exported to env, causing KeyError in Python
#          heredocs at lines that called os.environ['DO_BASE_URL'].
#          Now export DO_BASE_URL alongside DO_INFERENCE_BASE_URL so both
#          names are available in subprocess environments.
#   FIX-2  hermes-agent module check was `import hermes_agent` (wrong — that module
#          does not exist). hermes-agent installs modules: hermes_cli, hermes_constants,
#          run_agent, gateway. Check is now: import hermes_cli || hermes-agent --version
#   FIX-3  gateway.yaml path passed explicitly via HERMES_GATEWAY_CONFIG so
#          hermes-agent picks it up regardless of working directory.
#   FIX-4  jcode config heredoc also referenced DO_BASE_URL (same KeyError);
#          fixed to use DO_INFERENCE_BASE_URL consistently.
# ─────────────────────────────────────────────────────────────────────────────
set -e

echo ""
echo "╔══════════════════════════════════════════════════════════════════════╗"
echo "║  Rhodawk AI — Autonomous Architect  v7.1  (CEO-Grade Second Brain)        ║"
echo "║  Engine:  NousResearch/hermes-agent (orchestration + gateway)       ║"
echo "║  Coder:   Gitlawb/openclaude  (precision edits via gRPC)            ║"
echo "║  Swarm:   1jehuang/jcode      (parallel scaffolding)                ║"
echo "║  Relay:   openclaw/openclaw   (20+ channels: WhatsApp/Discord/Slack) ║"
echo "║  Push:    bot/telegram_bot.py (3-layer resilient push utility)      ║"
echo "╚══════════════════════════════════════════════════════════════════════╝"
echo ""

# ── Validate required secrets ──────────────────────────────────────────────────
MISSING=()
[ -z "${TELEGRAM_BOT_TOKEN}" ]   && MISSING+=("TELEGRAM_BOT_TOKEN")
[ -z "${DO_INFERENCE_API_KEY}" ] && MISSING+=("DO_INFERENCE_API_KEY")
[ -z "${GITHUB_PAT}" ]           && MISSING+=("GITHUB_PAT")

if [ ${#MISSING[@]} -gt 0 ]; then
    echo "[FATAL] Missing required secrets: ${MISSING[*]}"
    echo "        HF Spaces: https://huggingface.co/spaces/Architect8999/Hermes/settings"
    echo "        VPS:       edit .env and re-run: bash install.sh"
    exit 1
fi
echo "[init] All required secrets present."

# Warn if TELEGRAM_ALLOWED_USERS is unset — bot will respond to ALL users.
if [ -z "${TELEGRAM_ALLOWED_USERS:-}" ] && [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
    echo "[WARN] TELEGRAM_ALLOWED_USERS and TELEGRAM_CHAT_ID are both unset."
    echo "       The bot will respond to ALL Telegram users."
    echo "       Set TELEGRAM_CHAT_ID (or TELEGRAM_ALLOWED_USERS) to restrict access."
fi

if [ -n "${HF_TOKEN}" ]; then
    echo "[init] HF_TOKEN present — HuggingFace push enabled."
else
    echo "[init] HF_TOKEN not set — HuggingFace push disabled."
fi
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# LLM routing — all agents use DigitalOcean Inference
# ─────────────────────────────────────────────────────────────────────────────
DO_BASE_URL="https://inference.do-ai.run/v1"
HERMES_MODEL_VAL="deepseek-v4-pro"
OPENCLAUDE_MODEL_VAL="deepseek-r1-distill-llama-70b"
JCODE_MODEL_VAL="kimi-k2.6"
DO_FALLBACK_MODEL_VAL="deepseek-r1-distill-llama-70b"

echo "[routing] hermes-agent:  DO (${HERMES_MODEL_VAL})"
echo "[routing] openclaude:    DO (${OPENCLAUDE_MODEL_VAL})"
echo "[routing] jcode:         DO (${JCODE_MODEL_VAL})"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Export all env vars NOW so supervisord child processes inherit them.
# FIX-1: Export BOTH DO_BASE_URL and DO_INFERENCE_BASE_URL so Python
#        subprocesses can reference either name without KeyError.
# ─────────────────────────────────────────────────────────────────────────────
export DO_BASE_URL="${DO_BASE_URL}"
export DO_INFERENCE_BASE_URL="${DO_BASE_URL}"
export DO_INFERENCE_API_KEY="${DO_INFERENCE_API_KEY}"
export HERMES_MODEL="${HERMES_MODEL_VAL}"
export OPENCLAUDE_MODEL="${OPENCLAUDE_MODEL_VAL}"
export JCODE_MODEL="${JCODE_MODEL_VAL}"
export DO_FALLBACK_MODEL="${DO_FALLBACK_MODEL_VAL}"
export OPENAI_API_KEY="${DO_INFERENCE_API_KEY}"
export OPENAI_BASE_URL="${DO_BASE_URL}"
export OPENAI_MODEL="${OPENCLAUDE_MODEL_VAL}"
export CLAUDE_CODE_USE_OPENAI=1
# SECURITY: HERMES_YOLO_MODE=0 (safe default) — openclaude uses --permission-mode plan (read-only).
# Set to 1 ONLY if you have implemented the ActionRequired approval loop via Telegram
# inline buttons, or you fully trust the LLM's judgment on the target repos.
# See skills/openclaude_grpc/server.py for the full safety model.
export HERMES_YOLO_MODE="${HERMES_YOLO_MODE:-0}"
export HERMES_HOME=/data/.hermes
export HERMES_GATEWAY_CONFIG="/data/.hermes/gateway.yaml"
export HF_TOKEN="${HF_TOKEN:-}"
export GITHUB_PAT="${GITHUB_PAT}"
export TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN}"
# FIX-7: Export TELEGRAM_CHAT_ID so hermes-agent's allowlist is populated.
export TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"
# Camofox container hostname (Docker Compose service name — NOT localhost)
export CAMOFOX_HOST="camofox"
export CAMOFOX_PORT="9377"
echo "[env] All LLM + token env vars exported (DO_BASE_URL and DO_INFERENCE_BASE_URL both set)."

# ─────────────────────────────────────────────────────────────────────────────
# Configure hermes-agent
# ─────────────────────────────────────────────────────────────────────────────
echo "[hermes-agent] Configuring /data/.hermes/ ..."
mkdir -p /data/.hermes/skills/devops-pipeline \
         /data/.hermes/memories \
         /data/.hermes/sessions \
         /data/.hermes/logs \
         /data/.hermes/cron \
         /data/.hermes/plugins

# Write .env for hermes-agent's own env_loader (it reads $HERMES_HOME/.env)
# FIX-1: All os.environ references use the exported var names.
#         DO_INFERENCE_BASE_URL is used (not DO_BASE_URL) for clarity,
#         though both are now exported and either would work.
python3 - << 'PYEOF'
import os, pathlib
p = pathlib.Path("/data/.hermes/.env")
# FIX-7: Include TELEGRAM_CHAT_ID and TELEGRAM_ALLOWED_USERS so hermes-agent's
#         platform allowlist is populated (fixes "No user allowlists configured").
telegram_chat_id = os.environ.get('TELEGRAM_CHAT_ID', '')
lines = [
    f"HERMES_HOME=/data/.hermes",
    f"DO_INFERENCE_API_KEY={os.environ['DO_INFERENCE_API_KEY']}",
    f"DO_INFERENCE_BASE_URL={os.environ['DO_INFERENCE_BASE_URL']}",
    f"DO_BASE_URL={os.environ['DO_INFERENCE_BASE_URL']}",
    f"HERMES_MODEL={os.environ['HERMES_MODEL']}",
    f"OPENCLAUDE_MODEL={os.environ['OPENCLAUDE_MODEL']}",
    f"JCODE_MODEL={os.environ['JCODE_MODEL']}",
    f"DO_FALLBACK_MODEL={os.environ['DO_FALLBACK_MODEL']}",
    f"OPENAI_API_KEY={os.environ['DO_INFERENCE_API_KEY']}",
    f"OPENAI_BASE_URL={os.environ['DO_INFERENCE_BASE_URL']}",
    f"OPENAI_MODEL={os.environ['OPENCLAUDE_MODEL']}",
    f"CLAUDE_CODE_USE_OPENAI=1",
    f"HERMES_YOLO_MODE={os.environ.get('HERMES_YOLO_MODE', '0')}",
    f"HERMES_GATEWAY_CONFIG=/data/.hermes/gateway.yaml",
    f"TELEGRAM_BOT_TOKEN={os.environ['TELEGRAM_BOT_TOKEN']}",
    f"TELEGRAM_CHAT_ID={telegram_chat_id}",
    f"TELEGRAM_ALLOWED_USERS={telegram_chat_id}",
    f"GITHUB_PAT={os.environ['GITHUB_PAT']}",
    f"HF_TOKEN={os.environ.get('HF_TOKEN', '')}",
    f"BRAVE_API_KEY={os.environ.get('BRAVE_API_KEY', '')}",
    f"CAMOFOX_ACCESS_KEY={os.environ.get('CAMOFOX_ACCESS_KEY', '')}",
]
p.write_text("\n".join(lines) + "\n")
print(f"[hermes-agent] .env written to /data/.hermes/.env (TELEGRAM_ALLOWED_USERS={'set' if telegram_chat_id else 'not set — bot open to all'})")
PYEOF

# Copy persona and skills from app bundle (plain copy — no template vars)
cp /app/hermes_config/SOUL.md        /data/.hermes/SOUL.md
cp /app/skills/devops-pipeline/SKILL.md /data/.hermes/skills/devops-pipeline/SKILL.md
cp /app/skills/devops-pipeline/skill.md /data/.hermes/skills/devops-pipeline/skill.md 2>/dev/null || true

# FIX-5: Expand ${VAR} placeholders in config.yaml and gateway.yaml before
# writing to /data/.hermes/. Previously these were copied verbatim with `cp`,
# leaving ${DO_INFERENCE_API_KEY}, ${DO_INFERENCE_BASE_URL}, ${HERMES_MODEL}
# etc. as literal strings. hermes-agent tried to use "${DO_INFERENCE_API_KEY}"
# as the actual API key, which produced HTTP 401 on every LLM call.
python3 - << 'PYEOF'
import string, pathlib, os

def expand_yaml(src, dst):
    """Expand ${VAR} references in a YAML file using the current environment."""
    text = pathlib.Path(src).read_text()
    expanded = string.Template(text).safe_substitute(os.environ)
    pathlib.Path(dst).write_text(expanded)
    print(f"[hermes-agent] {dst} written (env vars expanded)")

expand_yaml("/app/hermes_config/config.yaml",  "/data/.hermes/config.yaml")
expand_yaml("/app/hermes_config/gateway.yaml", "/data/.hermes/gateway.yaml")
PYEOF

echo "[hermes-agent] Config ready."

# ─────────────────────────────────────────────────────────────────────────────
# Configure jcode
# FIX-4: Python heredoc now uses DO_INFERENCE_BASE_URL (exported) instead of
#        DO_BASE_URL which was only a local shell variable in v4.0.
# ─────────────────────────────────────────────────────────────────────────────
echo "[jcode] Configuring ~/.jcode/ (real config path from jcode source: jcode_dir() = ~/.jcode) ..."
mkdir -p /root/.jcode

python3 - << 'PYEOF'
import os, pathlib
# jcode reads config from ~/.jcode/config.toml (src/config/config_file.rs: jcode_dir().join("config.toml"))
# Config section names and keys come from jcode-config-types/src/lib.rs.
# Provider config: [provider] block with openai-compatible API keys.
# Env overrides (JCODE_API_KEY, JCODE_BASE_URL, JCODE_MODEL) take precedence over config file.
cfg = f"""# jcode configuration — Rhodawk integration
# Location: ~/.jcode/config.toml
# Provider: DigitalOcean Inference (OpenAI-compatible)

[provider]
default_model = "{os.environ['JCODE_MODEL']}"

[provider.openai]
api_key = "{os.environ['DO_INFERENCE_API_KEY']}"
base_url = "{os.environ['DO_INFERENCE_BASE_URL']}"

[display]
diff_mode = "inline"
"""
pathlib.Path("/root/.jcode/config.toml").write_text(cfg)
print("[jcode] ~/.jcode/config.toml written")
PYEOF

echo "[jcode] Config ready."

# ─────────────────────────────────────────────────────────────────────────────
# Configure openclaude (env vars already exported above — no config file needed)
# ─────────────────────────────────────────────────────────────────────────────
echo "[openclaude] Using exported env vars (CLAUDE_CODE_USE_OPENAI=1, OPENAI_* → DO Inference)"

# ─────────────────────────────────────────────────────────────────────────────
# Configure openclaw gateway (multi-channel relay on port 18789)
# Uses models.providers.do-inference — DO Inference OpenAI-compatible endpoint
# No channels configured here — add via `openclaw config set channels.<name>...`
# ─────────────────────────────────────────────────────────────────────────────
echo "[openclaw] Configuring ~/.openclaw/ ..."
mkdir -p /data/.openclaw/workspace /root/.openclaw

# Write openclaw.json using only keys recognised by openclaw 2026.5.x.
# REMOVED: 'identity' and 'agent' — both cause startup crash:
#   "Invalid config: Unrecognized keys: identity, agent"
# VALID root keys: models, gateway, tools, logging, channels
python3 -c "
import os, json, pathlib
config = {
    'models': {
        'mode': 'merge',
        'providers': {
            'do-inference': {
                'baseUrl': 'https://inference.do-ai.run/v1',
                'apiKey': os.environ['DO_INFERENCE_API_KEY'],
                'api': 'openai-completions',
                'models': [
                    {
                        'id': 'deepseek-r1-distill-llama-70b',
                        'name': 'DeepSeek R1 70B (DO)',
                        'reasoning': True,
                        'input': ['text'],
                        'cost': {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0},
                        'contextWindow': 131072,
                        'contextTokens': 65536,
                        'maxTokens': 16384,
                    },
                    {
                        'id': 'deepseek-v4-pro',
                        'name': 'DeepSeek V4 Pro (DO)',
                        'reasoning': False,
                        'input': ['text'],
                        'cost': {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0},
                        'contextWindow': 131072,
                        'contextTokens': 65536,
                        'maxTokens': 16384,
                    },
                ],
            },
        },
    },
    'gateway': {
        'mode': 'local',
        'port': 18789,
        'bind': 'loopback',
    },
    'tools': {'profile': 'coding'},
    'logging': {'level': 'info', 'consoleLevel': 'info', 'consoleStyle': 'pretty'},
}
pathlib.Path('/root/.openclaw/openclaw.json').write_text(json.dumps(config, indent=2))
print('[openclaw] openclaw.json written — provider: do-inference → https://inference.do-ai.run/v1')
"

# Export openclaw startup optimisations into the current shell so they are
# inherited by `openclaw doctor --fix` AND by all later openclaw calls.
# (supervisord.conf also sets these for the long-running gateway process.)
export OPENCLAW_NO_RESPAWN=1
export NODE_COMPILE_CACHE=/var/tmp/openclaw-compile-cache
mkdir -p /var/tmp/openclaw-compile-cache

if command -v openclaw &>/dev/null; then
    # Set the three keys the doctor complains about via the CLI config store.
    # These are persisted to openclaw's own internal config (separate from
    # openclaw.json) so the doctor no longer reports them as unset.
    openclaw config set gateway.mode local              2>&1 || true
    openclaw config set channels.telegram.enabled false 2>&1 || true
    echo "[openclaw] gateway.mode=local set; Telegram channel DISABLED (hermes-gateway owns that token)."
    # NOTE: commandOwner is NOT a valid openclaw config key (schema validation
    # rejects it). The 'No command owner' doctor warning is cosmetic only —
    # it does not block gateway startup. Leaving it unset is correct.

    echo "[openclaw] Running doctor --fix to repair/validate config schema..."
    openclaw doctor --fix 2>&1 || echo "[openclaw] doctor --fix returned non-zero (non-critical)"
fi

echo "[openclaw] Config ready."

# ─────────────────────────────────────────────────────────────────────────────
# Tool availability check
# FIX-D: hermes-agent installs as the `hermes` binary (not `hermes-agent`).
#   pyproject.toml entry_point: hermes = "hermes_cli.main:main"
#   Previous check: hermes-agent --version  → always failed (wrong binary name)
#   Corrected check: hermes --version       → succeeds when installed correctly
#
# Also checks openclaude binary (FIX-C: now in PATH via /usr/local/bin symlink)
# and jcode (FIX-B: correct tarball installed the real binary).
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "[check] Agent and tool availability:"
# hermes-agent: installs as `hermes` CLI (github.com/NousResearch/hermes-agent)
if command -v hermes &>/dev/null; then
    hermes --version 2>/dev/null | head -1 | sed 's/^/  hermes:         /'
    python3 -c "import hermes_cli; print('  hermes_cli module: OK')" 2>/dev/null \
        || echo "  hermes_cli module: import failed (check install)"
else
    echo "  hermes: NOT INSTALLED — gateway/run.py fallback will run (check Dockerfile FIX-A)"
fi
# openclaude: CLI coding agent (github.com/Gitlawb/openclaude)
if command -v openclaude &>/dev/null; then
    openclaude --version 2>/dev/null | head -1 | sed 's/^/  openclaude:     /'
else
    echo "  openclaude: NOT IN PATH — gRPC server cannot spawn it (check Dockerfile FIX-C)"
fi
# openclaude gRPC stubs: generated by Dockerfile into /app/skills/openclaude_grpc/
python3 -c "
import sys; sys.path.insert(0,'/app/skills/openclaude_grpc')
try:
    import grpc, openclaude_pb2
    print('  openclaude-grpc stubs: OK')
except ImportError as e:
    print('  openclaude-grpc stubs: NOT READY —', e)
" 2>/dev/null || echo "  openclaude-grpc stubs: import check failed"
# openclaw: multi-channel gateway (github.com/openclaw/openclaw) — non-critical
openclaw   --version 2>/dev/null | head -1 | sed 's/^/  openclaw:       /' || echo "  openclaw:       not installed (non-critical)"
# jcode: coding agent harness (github.com/1jehuang/jcode)
jcode      --version 2>/dev/null | head -1 | sed 's/^/  jcode:          /' || echo "  jcode:          NOT INSTALLED — check Dockerfile FIX-B"
rg         --version 2>/dev/null | head -1 | sed 's/^/  ripgrep:        /'
git        --version             | head -1 | sed 's/^/  git:            /'
node       --version             | head -1 | sed 's/^/  node:           /'
python3    --version             | head -1 | sed 's/^/  python3:        /'
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Neutralize any stale OpenRouter override
# ─────────────────────────────────────────────────────────────────────────────
unset OPENROUTER_API_KEY
unset OPENROUTER_BASE_URL

# ─────────────────────────────────────────────────────────────────────────────
# Deploy shared MCP config to all three agents
# mcp_shared.json uses ${GITHUB_PAT} and ${BRAVE_API_KEY} as template strings.
# These are NOT expanded by the shell when the file is read as JSON — we must
# substitute them explicitly before writing to each agent's config location.
# Also adds 'mcpServers' key (Claude Code / openclaude format) alongside
# 'servers' (hermes-agent format) for full compatibility.
# ─────────────────────────────────────────────────────────────────────────────
echo "[mcp] Deploying shared MCP config to all three agents (env vars expanded)..."
python3 - << 'PYEOF'
import json, os, pathlib, copy

template = json.loads(pathlib.Path("/app/mcp_shared.json").read_text())
github_pat   = os.environ.get("GITHUB_PAT", "")
brave_key    = os.environ.get("BRAVE_API_KEY", "")

def expand_env(obj):
    """Recursively expand ${VAR} template strings in a JSON structure."""
    if isinstance(obj, dict):
        return {k: expand_env(v) for k, v in obj.items() if not k.startswith("_comment")}
    if isinstance(obj, list):
        return [expand_env(i) for i in obj]
    if isinstance(obj, str):
        return (obj
                .replace("${GITHUB_PAT}", github_pat)
                .replace("${BRAVE_API_KEY}", brave_key))
    return obj

expanded = expand_env(copy.deepcopy(template))

# Omit brave-search section entirely when BRAVE_API_KEY is not configured
# (the server will fail to start without a valid key)
for key in ("mcpServers", "servers"):
    if key in expanded and "brave-search" in expanded[key] and not brave_key:
        del expanded[key]["brave-search"]

out = json.dumps(expanded, indent=2)

pathlib.Path("/root/.jcode").mkdir(parents=True, exist_ok=True)
pathlib.Path("/root/.jcode/mcp.json").write_text(out)
pathlib.Path("/root/.claude").mkdir(parents=True, exist_ok=True)
pathlib.Path("/root/.claude/mcp.json").write_text(out)

# Inject mcp_servers into hermes-agent config.yaml.
# hermes-agent reads MCP config from $HERMES_HOME/config.yaml under the
# `mcp_servers` key (hermes_cli/mcp_config.py: config.get("mcp_servers")).
# Writing a separate mcp.json is NOT read by hermes-agent.
try:
    import yaml
    cfg_path = pathlib.Path("/data/.hermes/config.yaml")
    cfg = yaml.safe_load(cfg_path.read_text()) or {}
    # Build mcp_servers dict in hermes-agent format (same as mcpServers but snake_case key)
    mcp_servers = {}
    # FIX-6: mcp_shared.json uses "servers" key; fall back to it when "mcpServers"
    # is absent so hermes-agent actually receives MCP tool configuration.
    source_key = "mcpServers" if "mcpServers" in expanded else "servers"
    for name, server in expanded.get(source_key, {}).items():
        entry = {"command": server["command"], "args": server.get("args", [])}
        if "env" in server:
            entry["env"] = server["env"]
        mcp_servers[name] = entry
    cfg["mcp_servers"] = mcp_servers
    cfg_path.write_text(yaml.dump(cfg, default_flow_style=False, allow_unicode=True))
    print(f"[mcp] Injected {len(mcp_servers)} MCP servers (from '{source_key}') into /data/.hermes/config.yaml")
except Exception as e:
    print(f"[mcp] WARNING: Could not inject mcp_servers into config.yaml: {e}")

print(f"[mcp]   github MCP server: {'enabled (PAT set)' if github_pat else 'DISABLED (GITHUB_PAT not set)'}")
print(f"[mcp]   brave-search MCP:  {'enabled' if brave_key else 'DISABLED (BRAVE_API_KEY not set — section omitted)'}")
PYEOF

echo "[mcp] Shared MCP config deployed."

# Deploy openclaude agent routing (per-task model routing)
# openclaude_settings.json uses ${DO_INFERENCE_API_KEY} as a template string.
# Expand it before writing to /root/.claude/settings.json.
python3 - << 'PYEOF'
import json, os, pathlib

template = json.loads(pathlib.Path("/app/openclaude_settings.json").read_text())
api_key  = os.environ.get("DO_INFERENCE_API_KEY", "")

def expand_env(obj):
    if isinstance(obj, dict):
        return {k: expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand_env(i) for i in obj]
    if isinstance(obj, str):
        return obj.replace("${DO_INFERENCE_API_KEY}", api_key)
    return obj

expanded = expand_env(template)
out = json.dumps(expanded, indent=2)
pathlib.Path("/root/.claude/settings.json").write_text(out)
print(f"[openclaude] settings.json written with DO_INFERENCE_API_KEY expanded ({len(api_key)} chars)")
PYEOF
echo "[openclaude] Agent routing settings deployed."

# ─────────────────────────────────────────────────────────────────────────────
# Copy memory and cron templates to runtime directories
# ─────────────────────────────────────────────────────────────────────────────
echo "[memory] Initializing Hermes native memory files..."
mkdir -p /data/.hermes/memories /data/.hermes/cron

# Only write if not already present (preserve any live state)
[ -f /data/.hermes/memories/MEMORY.md ] || cp /app/hermes_config/memories/MEMORY.md /data/.hermes/memories/MEMORY.md
[ -f /data/.hermes/memories/USER.md ]   || cp /app/hermes_config/memories/USER.md   /data/.hermes/memories/USER.md

# Cron templates always refreshed from app bundle
cp /app/hermes_config/cron/nightly_sweep.yaml  /data/.hermes/cron/nightly_sweep.yaml
cp /app/hermes_config/cron/weekly_traction.yaml /data/.hermes/cron/weekly_traction.yaml

# Target repo list
mkdir -p /data
[ -f /data/target_list.json ] || cp /app/data/target_list.json /data/target_list.json

echo "[memory] Memory and cron templates ready."

# ─────────────────────────────────────────────────────────────────────────────
# Copy peak skills to hermes-agent skills directory
# ─────────────────────────────────────────────────────────────────────────────
echo "[skills] Deploying peak skills to hermes-agent..."
mkdir -p /data/.hermes/skills/openclaude_grpc /data/.hermes/skills/jcode_swarm
cp /app/skills/openclaude_grpc/SKILL.md /data/.hermes/skills/openclaude_grpc/SKILL.md
cp /app/skills/jcode_swarm/SKILL.md     /data/.hermes/skills/jcode_swarm/SKILL.md
mkdir -p /data/.hermes/skills/openclaw_channel
cp /app/skills/openclaw_channel/SKILL.md /data/.hermes/skills/openclaw_channel/SKILL.md
mkdir -p /data/.hermes/skills/research-deep /data/.hermes/skills/competitive-intel /data/.hermes/skills/security-audit /data/.hermes/skills/stealth-browse /data/.hermes/audit_reports
cp /app/skills/research-deep/SKILL.md /data/.hermes/skills/research-deep/SKILL.md
cp /app/skills/competitive-intel/SKILL.md /data/.hermes/skills/competitive-intel/SKILL.md
cp /app/skills/security-audit/SKILL.md /data/.hermes/skills/security-audit/SKILL.md
cp /app/skills/security-audit/aggregate.py /data/.hermes/skills/security-audit/aggregate.py
cp /app/skills/stealth-browse/SKILL.md /data/.hermes/skills/stealth-browse/SKILL.md
echo "[skills] Peak skills deployed: openclaude_grpc, jcode_swarm, openclaw_channel, research-deep, competitive-intel, security-audit, stealth-browse."

# ─────────────────────────────────────────────────────────────────────────────
# Layer D: Inject SOUL.md into openclaw workspace
# openclaw uses the workspace as context for all channel messages.
# Copying SOUL.md ensures Hermes's persona, rules, and capabilities are
# preserved when messages are relayed through Discord/Slack/WhatsApp.
# ─────────────────────────────────────────────────────────────────────────────
echo "[layer-d] Injecting SOUL.md into openclaw workspace..."
mkdir -p /data/.openclaw/workspace
cp /app/hermes_config/SOUL.md /data/.openclaw/workspace/SOUL.md
# Also write a CONTEXT.md summarising the channel integration setup
python3 - << 'PYEOF'
import pathlib, os, datetime
ctx_path = pathlib.Path("/data/.openclaw/workspace/CONTEXT.md")
ctx_path.write_text(f"""# Hermes Channel Context
Generated: {datetime.datetime.utcnow().isoformat()}Z

This workspace is the Hermes intelligent relay. Messages arriving through
any channel (Telegram, Discord, Slack, WhatsApp) are processed by the same
Hermes AI with the same SOUL.md persona and rules.

## Active Channels
- Telegram: primary operator interface (hermes-gateway owns the token)
- Other channels: relayed via openclaw gateway on port 18789

## Key Facts
- Agent: Rhodawk AI Hermes v8.0 (Jarvis + 8-Layer Architecture)
- Operator: Solo founder, YOLO mode always on
- Model: deepseek-v4-pro via DigitalOcean Inference
- Memory: /data/.hermes/memories/MEMORY.md (auto-injected every session)
- SOUL: See SOUL.md in this directory
- Skills: /data/.hermes/skills/_learned/ (learned from completed tasks)
""")
print("[layer-d] /data/.openclaw/workspace/CONTEXT.md written")
PYEOF

# ─────────────────────────────────────────────────────────────────────────────
# Layer B: Pre-warm jcode session for primary repo
# jcode's memory system only activates when the same session is reused.
# Pre-warming creates the session entry so the first task benefits immediately.
# ─────────────────────────────────────────────────────────────────────────────
echo "[layer-b] Pre-warming jcode session for primary repo..."
mkdir -p /data/.hermes/jcode_sessions /tmp/repos/Hermes88
python3 - << 'PYEOF'
import sys, pathlib
sys.path.insert(0, '/app')
try:
    from skills.jcode_swarm.session_manager import get_session_manager
    manager = get_session_manager()
    manager.prewarm_sessions([
        "Architect8989/Hermes88",
    ])
    print("[layer-b] jcode sessions pre-warmed")
except Exception as exc:
    print(f"[layer-b] Pre-warm skipped (jcode not installed): {exc}")
PYEOF

# ─────────────────────────────────────────────────────────────────────────────
# Layer E: Initialize skill engine directories
# ─────────────────────────────────────────────────────────────────────────────
echo "[layer-e] Initializing skill engine directories..."
mkdir -p /data/.hermes/skills/_learned
# If no INDEX.json exists yet, create an empty one
[ -f /data/.hermes/skills/INDEX.json ] || echo '{}' > /data/.hermes/skills/INDEX.json
echo "[layer-e] Skill engine ready: /data/.hermes/skills/_learned/"

# ─────────────────────────────────────────────────────────────────────────────
# Layer H: Create image output directory
# ─────────────────────────────────────────────────────────────────────────────
mkdir -p /data/.hermes/images /data/.hermes/screenshots
echo "[layer-h] FAL.ai image output directories created"

echo ""
echo "[supervisord] Starting hermes-gateway + openclaude-grpc + jcode + openclaw-gateway..."
exec supervisord -c /etc/supervisor/conf.d/rhodawk.conf
