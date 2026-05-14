# ─────────────────────────────────────────────────────────────────────────────
# Rhodawk AI — Autonomous Architect  v6.1  (General-Purpose)
#
# Architecture:
#   hermes-agent (NousResearch) — orchestration engine, skills, provider routing
#     └─ gateway/platforms/telegram.py — Telegram adapter (built-in)
#   openclaude (Gitlawb/openclaude) — precision coder via gRPC server at :50051
#   jcode (1jehuang/jcode) — parallel scaffolding swarm server at :7865
#   bot/telegram_bot.py — push-commit / bounded-run / ingest-media utilities
#   MCP shared layer — filesystem + github servers for all three agents
#
# Changes from v6.0 → v6.1:
#   FIX-A  Use requirements.txt instead of inline pip install for reproducibility
#   FIX-B  Node.js pinned to 24.x via NodeSource (resolves ?? operator SyntaxError
#          that occurred when system fell back to Node.js 12)
#   FIX-C  Explicit pip upgrade before requirements install avoids resolver bugs
#   FIX-D  chmod +x main.py added so it can be run directly
# ─────────────────────────────────────────────────────────────────────────────

FROM ubuntu:26.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# ── System packages ────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 python3.11-dev python3-pip python3.11-venv \
    curl wget git supervisor \
    build-essential ca-certificates \
    libmupdf-dev poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# ── ripgrep 14.1.0 (required by openclaude for codebase search) ────────────────
RUN curl -LO https://github.com/BurntSushi/ripgrep/releases/download/14.1.0/ripgrep_14.1.0-1_amd64.deb \
    && dpkg -i ripgrep_14.1.0-1_amd64.deb \
    && rm ripgrep_14.1.0-1_amd64.deb

# ── Node.js 24 (required by openclaude + push Layer 3 + MCP servers) ──────────
# FIX-B: Explicitly install Node.js 24.x from NodeSource. This prevents the
# system from falling back to Ubuntu's default Node.js 12.x package, which
# does not support the Nullish Coalescing operator (??) used by MCP servers
# and openclaude. Node.js 14+ is required; 24.x is recommended.
RUN curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
    && apt-get install -y nodejs \
    && node --version \
    && npm --version \
    && echo "[node] Version: $(node --version) — ?? operator supported"

# ── Bun runtime (required by @gitlawb/openclaude for gRPC TypeScript scripts) ─
# openclaude's dev:grpc script calls `bun run scripts/start-grpc.ts`.
# Without bun, openclaude-grpc crashes with "bun: not found" (exit 127).
RUN npm install -g bun \
    && bun --version && echo "[bun] installed OK"

# ── Python 3.11 aliases ────────────────────────────────────────────────────────
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 \
    && update-alternatives --install /usr/bin/python  python  /usr/bin/python3.11 1 \
    && pip3 install --upgrade pip setuptools wheel

# ─────────────────────────────────────────────────────────────────────────────
# ── Agent 1: HERMES-AGENT ────────────────────────────────────────────────────
# Install with [messaging] extra to ensure python-telegram-bot is included in
# hermes-agent's own dependency set (fixes "python-telegram-bot not installed"
# warning that prevents the built-in Telegram adapter from loading).
# FIX-E  hermes-agent now installed with [messaging] extra so its internal
#         Telegram adapter can import python-telegram-bot successfully.
RUN pip3 install --no-cache-dir "hermes-agent[messaging]>=0.10.0" \
    || pip3 install --no-cache-dir "hermes-agent>=0.10.0" \
    || echo "[hermes-agent] PyPI package not available — built-in gateway.run will be used"

# ─────────────────────────────────────────────────────────────────────────────
# Remaining Python dependencies — install from requirements.txt
# hermes-agent base is already installed above; this adds grpcio, requests, etc.
# ─────────────────────────────────────────────────────────────────────────────
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

# ─────────────────────────────────────────────────────────────────────────────
# Agent 2: OPENCLAUDE — cloned from GitHub source
#   Role: Precision coder — surgical edits, bug fixes via gRPC server at :50051
#   LLM:  DO Inference deepseek-v4-pro (CLAUDE_CODE_USE_OPENAI=1)
#   Mode: gRPC server (bun run dev:grpc from source) — bidirectional streaming
# NOTE: npm @gitlawb/openclaude@0.9.2 does NOT include scripts/start-grpc.ts.
#       Must clone from GitHub and run from source.
# ─────────────────────────────────────────────────────────────────────────────
RUN git clone --depth=1 https://github.com/Gitlawb/openclaude.git /opt/openclaude \
    && cd /opt/openclaude \
    && bun install \
    && python3 -c "import re,sys; f='src/entrypoints/sdk/index.ts'; t=open(f).read(); p=re.sub(r'^detectStubLeaks\(\);?$','// detectStubLeaks() // patched: Bun v1.3 TDZ bug',t,flags=re.MULTILINE); (sys.exit('PATCH FAILED') if p==t else None); open(f,'w').write(p); print('Patched:',f)" \
    && echo "[openclaude] source ready — $(ls scripts/start-grpc.ts) present" \
    || echo "[openclaude] source unavailable — Python gRPC server fallback will be used"

# Install grpc Python client for openclaude gRPC seam (already in requirements.txt
# but listed here for clarity — pip install is idempotent)
RUN pip3 install --no-cache-dir grpcio grpcio-tools protobuf

# ─────────────────────────────────────────────────────────────────────────────
# Agent 3: JCODE (1jehuang/jcode)
#   Role: Parallel scaffolding swarm — multi-file generation, server at :7865
#   Mode: `jcode serve` in background + `jcode run --message` workers
# ─────────────────────────────────────────────────────────────────────────────
RUN curl -fsSL https://raw.githubusercontent.com/1jehuang/jcode/master/scripts/install.sh \
    | bash 2>/dev/null \
    && jcode --version 2>/dev/null \
    || echo "[jcode] install via script failed — trying cargo fallback" \
    && (which cargo && cargo install jcode 2>/dev/null || true)

# ─────────────────────────────────────────────────────────────────────────────
# Shared MCP layer — filesystem + github servers for all three agents
# Install each server individually with retry + graceful failure so a single
# transient npm registry error does not abort the entire build.
# filesystem + github are critical; fetch + brave-search are optional.
# ─────────────────────────────────────────────────────────────────────────────
RUN npm config set fetch-retry-mintimeout 20000 \
    && npm config set fetch-retry-maxtimeout 120000 \
    && npm config set fetch-retries 5
RUN npm install -g @modelcontextprotocol/server-filesystem \
    || (sleep 10 && npm install -g @modelcontextprotocol/server-filesystem) \
    && echo "[mcp] server-filesystem OK"
RUN npm install -g @modelcontextprotocol/server-github \
    || (sleep 10 && npm install -g @modelcontextprotocol/server-github) \
    && echo "[mcp] server-github OK"
RUN npm install -g @modelcontextprotocol/server-fetch \
    || (sleep 10 && npm install -g @modelcontextprotocol/server-fetch) \
    || echo "[mcp] server-fetch install failed (non-critical — skipping)"
RUN npm install -g @modelcontextprotocol/server-brave-search \
    || (sleep 10 && npm install -g @modelcontextprotocol/server-brave-search) \
    || echo "[mcp] server-brave-search install failed (non-critical — skipping)"

# ── App structure ───────────────────────────────────────────────────────────────
WORKDIR /app
COPY . /app/

# ── Generate openclaude gRPC Python stubs from real proto ──────────────────────
# Proto source: skills/openclaude_grpc/openclaude.proto (shipped in-repo)
# Stubs output: same directory so client.py + server.py can find them via
#   sys.path.insert(0, "/app/skills/openclaude_grpc")
RUN python3 -m grpc_tools.protoc \
       -I/app/skills/openclaude_grpc \
       --python_out=/app/skills/openclaude_grpc \
       --grpc_python_out=/app/skills/openclaude_grpc \
       /app/skills/openclaude_grpc/openclaude.proto \
    && echo "[grpc-stubs] Generated openclaude_pb2.py and openclaude_pb2_grpc.py in /app/skills/openclaude_grpc/"

# ── Git global config ───────────────────────────────────────────────────────────
RUN git config --global user.email "hermes@rhodawk.ai" \
    && git config --global user.name  "Hermes Bot" \
    && git config --global safe.directory "*"

# ── Runtime directories ─────────────────────────────────────────────────────────
RUN mkdir -p /tmp/repos /var/log \
    && mkdir -p /data/.hermes/skills/devops-pipeline \
                /data/.hermes/memories \
                /data/.hermes/sessions \
                /data/.hermes/logs \
                /data/.hermes/cron \
                /data/.hermes/plugins \
    && mkdir -p /root/.jcode \
                /root/.claude \
    && chmod -R 755 /data

# ── Script permissions ──────────────────────────────────────────────────────────
RUN chmod +x /app/scripts/init_and_start.sh \
    && chmod +x /app/bot/telegram_bot.py \
    && chmod +x /app/main.py \
    && chmod +x /app/skills/openclaude_grpc/client.py \
    && chmod +x /app/skills/jcode_swarm/spawn.py

# ── Force-reinstall python-telegram-bot LAST so it is never shadowed ───────────
# hermes-agent or its transitive deps may install a bare `telegram` package that
# shadows the python-telegram-bot `telegram` namespace, causing hermes-agent to
# log "python-telegram-bot not installed" even though it IS installed. Running
# pip install --force-reinstall after all other packages ensures the correct
# python-telegram-bot is the one Python resolves `import telegram` to.
RUN pip3 install --no-cache-dir --force-reinstall "python-telegram-bot>=20.7" \
    && python3 -c "from telegram.ext import Application; print('[ptb] python-telegram-bot OK')"

COPY supervisord.conf /etc/supervisor/conf.d/rhodawk.conf
EXPOSE 7860

CMD ["/app/scripts/init_and_start.sh"]
