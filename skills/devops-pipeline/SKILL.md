# Skill: devops-pipeline

## Purpose
End-to-end DevOps pipeline: clone → install → test → fix → push.
Uses hermes-agent's terminal tool to orchestrate openclaude (precision edits)
and jcode (parallel scaffolding) as sub-agents.

## When This Skill Applies
- User provides a GitHub or HuggingFace URL with a task description
- User asks to fix failing tests in a repo
- User asks to implement a feature in an existing codebase
- User asks to scaffold a new service or module

## Step-by-Step Pipeline

### Step 1 — Clone
```bash
mkdir -p /tmp/repos/$CHAT_ID
git clone --depth 1 $REPO_URL /tmp/repos/$CHAT_ID/$REPO_NAME
# For private repos with GitHub PAT:
git clone --depth 1 https://x-token-auth:$GITHUB_PAT@github.com/owner/repo /tmp/repos/$CHAT_ID/repo
```

### Step 2 — Pre-flight (ALWAYS before bounded-run)
```bash
cd /tmp/repos/$CHAT_ID/$REPO_NAME
[ -f requirements.txt ] && pip install -r requirements.txt -q 2>&1 | tail -5
[ -f pyproject.toml ]   && pip install -e . -q 2>&1 | tail -5
[ -f package.json ]     && npm install --silent 2>&1 | tail -3
[ -f Cargo.toml ]       && cargo fetch -q 2>&1 | tail -3
pytest --collect-only -q 2>&1 | tail -30
pytest --tb=no -q 2>&1 | tail -40
```
Report to user: "X tests collected, Y failing" before proceeding.

### Step 3 — Fix with bounded-run (Python/JS/Rust repos with tests)
```bash
python3 /app/bot/telegram_bot.py bounded-run \
    --cmd "pytest --tb=short -q" \
    --workdir /tmp/repos/$CHAT_ID/$REPO_NAME \
    --strikes 3 \
    --timeout 1200 \
    --api-key $DO_INFERENCE_API_KEY \
    --base-url $DO_INFERENCE_BASE_URL \
    --model deepseek-ai/DeepSeek-V4-Pro
```
Shell tool timeout: 3600 (mandatory — never lower).

### Step 3 alt — Fix with openclaude (no test suite, or targeted patch)
```bash
CLAUDE_CODE_USE_OPENAI=1 \
OPENAI_BASE_URL=$DO_INFERENCE_BASE_URL \
OPENAI_API_KEY=$DO_INFERENCE_API_KEY \
OPENAI_MODEL=$DO_PRIMARY_MODEL \
openclaude --print "Fix the bug described by the user. Read every file you touch. Write all fixes to disk now. Do not ask for confirmation."
```

### Step 3 alt — Scaffold with jcode (new files / new service)
```bash
OPENAI_BASE_URL=$DO_INFERENCE_BASE_URL \
OPENAI_API_KEY=$DO_INFERENCE_API_KEY \
OPENAI_MODEL=$DO_PRIMARY_MODEL \
jcode run --message "Scaffold a FastAPI service with JWT auth and Postgres. Write all files to /tmp/repos/$CHAT_ID/$REPO_NAME/. Do not ask questions."
```

### Step 4 — Escalation (bounded-run exhausted 3 strikes)
```bash
FAILURES=$(pytest --tb=no -q 2>&1 | grep "^FAILED" | awk '{print $1}')
for TEST in $FAILURES; do
    pytest "$TEST" --tb=short -q 2>&1
    CLAUDE_CODE_USE_OPENAI=1 \
    OPENAI_API_KEY=$DO_INFERENCE_API_KEY \
    OPENAI_BASE_URL=$DO_INFERENCE_BASE_URL \
    OPENAI_MODEL=$DO_PRIMARY_MODEL \
    openclaude --print "Fix ONLY this failing test: $TEST. Read the test file and every source file it imports. Write all fixes to disk now."
done
```

### Step 5 — Push to GitHub
```bash
python3 /app/bot/telegram_bot.py push-commit \
    --repo $REPO_URL \
    --token $GITHUB_PAT \
    --branch main \
    --message "fix: resolve failing tests via Rhodawk Hermes" \
    --workdir /tmp/repos/$CHAT_ID/$REPO_NAME
```
Output JSON: `{"success": true, "commit_hash": "abc1234...", "platform": "github"}`

### Step 5 alt — Push to HuggingFace Space
```bash
python3 /app/bot/telegram_bot.py push-commit \
    --repo $HF_SPACE_URL \
    --token $HF_TOKEN \
    --branch main \
    --message "feat: implement feature via Rhodawk Hermes" \
    --workdir /tmp/repos/$CHAT_ID/$REPO_NAME \
    --repo-type space
```

## Routing Decision Tree

```
Task received
    │
    ├─ Has test suite? (pytest/jest/cargo test)
    │       └─ YES → bounded-run → push
    │
    ├─ Targeted bug fix in existing code?
    │       └─ YES → openclaude --print → push
    │
    ├─ New service / scaffold multiple files?
    │       └─ YES → jcode run → push
    │
    └─ Redesign from image/PDF?
            └─ ingest-media → jcode (structure) → openclaude (precision) → push
```

## Environment Variables Available to All Sub-Agents

| Variable | Value |
|---|---|
| `GITHUB_PAT` | GitHub personal access token with repo+workflow scopes |
| `HF_TOKEN` | HuggingFace write token (optional) |
| `DO_INFERENCE_API_KEY` | DigitalOcean Inference key |
| `DO_INFERENCE_BASE_URL` | `https://inference.do-ai.run/v1` |
| `DO_PRIMARY_MODEL` | `deepseek-ai/DeepSeek-V4-Pro` |
| `NIM_API_KEY` | NVIDIA NIM key |
| `NIM_BASE_URL` | `https://integrate.api.nvidia.com/v1` |
| `NIM_PRIMARY_MODEL` | `deepseek-ai/deepseek-r1` |
| `CLAUDE_CODE_USE_OPENAI` | `1` (openclaude OpenAI-compat mode) |
| `HERMES_YOLO_MODE` | `1` (no approval prompts) |
