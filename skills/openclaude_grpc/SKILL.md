# openclaude gRPC Skill

## When to use
Surgical single-file edits. One bug, one function, one patch.
Use instead of `openclaude --print` for all coding work — faster startup,
bidirectional streaming, action_required callbacks handled automatically.
The gRPC server is always running (supervisord program:openclaude-grpc).

## Pre-check: verify gRPC server is healthy before invocation
python3 -c "
import grpc, sys
sys.path.insert(0, '/app/skills/openclaude_grpc')
try:
    channel = grpc.insecure_channel('localhost:50051')
    grpc.channel_ready_future(channel).result(timeout=5)
    print('gRPC OK')
except Exception as e:
    print(f'gRPC FAIL: {e}')
"
If gRPC is down, check supervisord: supervisorctl status openclaude-grpc
If still down, fall back to jcode for the same task.

## Prompt format that produces execution (not text output)
1. State the exact file path
2. State the exact location (function name or line range)
3. State the exact change using before/after blocks
4. End with: "Write the corrected file to disk now."

## Correct invocation pattern
python3 /app/skills/openclaude_grpc/client.py \
  --prompt "FILE: /tmp/repos/myrepo/src/auth.py
TASK: Replace the verify_token function starting at line 47 with this exact implementation:
def verify_token(token: str) -> dict:
    ...
Do not change any other code. Do not add imports. Write the corrected file to disk now." \
  --workdir /tmp/repos/myrepo \
  --model deepseek-v4-pro \
  --timeout 480

## Prompt format to avoid (produces code block output, not execution)
- "Fix the auth module" (too vague)
- "Improve the code" (no target)
- "Make it work" (no specification)

## Verification after openclaude run
Always verify the edit landed:
grep -n "target_function_name" /tmp/repos/myrepo/src/target.py | head -5

## Provider routing via OpenClaude settings.json
Explore tasks  → groq/llama-3.3-70b-instruct (free tier)
Planning tasks → deepseek-v4-pro (DO Inference)
Code tasks     → deepseek-v4-pro (DO Inference)
Review tasks   → kimi-k2.6 (NIM)

## Usage
python3 /app/skills/openclaude_grpc/client.py \
  --prompt "Fix the failing test: tests/test_auth.py::test_login" \
  --workdir /tmp/repos/myrepo \
  --model deepseek-v4-pro
