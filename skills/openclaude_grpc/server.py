#!/usr/bin/env python3
"""
skills/openclaude_grpc/server.py — Full-protocol Python gRPC server for openclaude.

FIX Problem-2: This server now implements ALL message types defined in openclaude.proto:
  TextChunk      — streamed LLM tokens
  ToolCallStart  — emitted BEFORE executing a tool (bash, read_file, write_file)
  ToolCallResult — emitted AFTER tool returns real output
  ActionRequired — emitted for dangerous commands; waits for UserInput y/n
  FinalResponse  — emitted when the agentic loop completes
  ErrorResponse  — emitted on unrecoverable failure

FIX Problem-6: ActionRequired is fully implemented for dangerous commands.
  If the LLM wants to run rm -rf, sudo rm, chmod 777, mkfs, dd, etc., the server
  pauses the loop and sends ActionRequired to the client. The client must respond
  with UserInput(reply="y") to proceed or UserInput(reply="n") to abort.
  HERMES_YOLO_MODE=1 bypasses approval (same semantics as openclaude --permission-mode yolo).

Agentic loop per request:
  1. List files in working_directory
  2. Read up to 12 most relevant files for context
  3. Call LLM with bash/read_file/write_file/list_files tool definitions
  4. Execute tool calls (with ActionRequired gate for dangerous commands)
  5. Feed results back to LLM
  6. Repeat until LLM responds without tool calls or says TASK_COMPLETE
  7. Emit FinalResponse

Start:
    python3 /app/skills/openclaude_grpc/server.py

Env vars:
    GRPC_PORT            — listen port (default: 50051)
    GRPC_HOST            — bind host (default: 0.0.0.0)
    OPENAI_API_KEY       — DO Inference API key
    OPENAI_BASE_URL      — DO Inference base URL
    OPENCLAUDE_MODEL     — model to use
    HERMES_YOLO_MODE     — if "1", skip ActionRequired approval (dangerous)
"""
import json
import logging
import os
import pathlib
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from concurrent import futures

sys.path.insert(0, "/app/skills/openclaude_grpc")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("openclaude-grpc-server")

try:
    import grpc
    import openclaude_pb2
    import openclaude_pb2_grpc
except ImportError as exc:
    logger.error(
        "gRPC stubs not found. Run: python3 -m grpc_tools.protoc "
        "-I/app/openclaude_grpc --python_out=/app/skills/openclaude_grpc "
        "--grpc_python_out=/app/skills/openclaude_grpc "
        "/app/openclaude_grpc/openclaude.proto\n"
        f"ImportError: {exc}"
    )
    sys.exit(1)

try:
    import urllib.request
    import urllib.error
except ImportError:
    pass

GRPC_PORT   = int(os.environ.get("GRPC_PORT", "50051"))
GRPC_HOST   = os.environ.get("GRPC_HOST", "0.0.0.0")
API_KEY     = os.environ.get("OPENAI_API_KEY") or os.environ.get("DO_INFERENCE_API_KEY", "")
BASE_URL    = os.environ.get("OPENAI_BASE_URL") or os.environ.get("DO_INFERENCE_BASE_URL", "https://inference.do-ai.run/v1")
MODEL       = os.environ.get("OPENCLAUDE_MODEL") or os.environ.get("OPENAI_MODEL", "deepseek-r1-distill-llama-70b")
YOLO_MODE   = os.environ.get("HERMES_YOLO_MODE", "0").strip() == "1"

MAX_ITERATIONS = 20
MAX_FILE_SIZE  = 8000
MAX_FILES_CTX  = 12

# Patterns that indicate dangerous commands requiring human approval
_DANGEROUS_PATTERNS = re.compile(
    r"\b(rm\s+-[rRfF]*\s+[^;|&\n]{2,}|sudo\s+rm|mkfs|dd\s+if=|shred|"
    r"chmod\s+777|chown\s+-R\s+root|iptables\s+-F|"
    r":\s*\(\s*\)\s*\{.*\}|fork\s+bomb|"
    r"curl\s+.*\|\s*bash|wget\s+.*\|\s*bash|"
    r"shutdown|reboot|halt|poweroff)\b",
    re.IGNORECASE | re.DOTALL,
)

# Tool definitions for the LLM (OpenAI function calling format)
_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Execute a bash command in the working directory. "
                "Returns stdout + stderr. Use for: running tests, installing packages, "
                "checking file existence, running linters, building projects. "
                "Do NOT use for file reads (use read_file instead — it's cheaper)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"},
                    "timeout": {"type": "integer", "default": 60, "description": "Timeout in seconds"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the working directory. Returns full content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to file"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write complete file content to disk. Overwrites existing file. "
                "Always write the ENTIRE file (no patches or diffs). "
                "Create parent directories automatically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to file"},
                    "content": {"type": "string", "description": "Complete file content"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files in the working directory (or a subdirectory).",
            "parameters": {
                "type": "object",
                "properties": {
                    "subdir": {"type": "string", "default": ".", "description": "Subdirectory to list"},
                    "pattern": {"type": "string", "default": "*", "description": "Glob pattern"},
                },
            },
        },
    },
]

SYSTEM_PROMPT = """\
You are an expert software engineer executing coding tasks iteratively.
You have access to bash, read_file, write_file, and list_files tools.

Rules:
- ALWAYS call list_files first to understand the directory structure.
- ALWAYS read relevant files before editing them.
- write_file ALWAYS writes the COMPLETE file content (no diffs, no patches).
- After writing files, verify your changes by running tests or build commands via bash.
- If verification fails, read the error, fix the files, and verify again.
- When the task is fully complete and verified, respond with exactly: TASK_COMPLETE
- Never fabricate file contents or command output — always use tools.
- Never ask for clarification — infer intent and execute.
"""


def _is_dangerous(command: str) -> bool:
    return bool(_DANGEROUS_PATTERNS.search(command))


def _exec_bash(command: str, workdir: str, timeout: int = 60) -> tuple[str, bool]:
    """Execute a bash command. Returns (output, is_error)."""
    timeout = max(1, min(int(timeout), 600))
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout,
            executable="/bin/bash",
        )
        out = ((result.stdout or "") + (result.stderr or "")).strip()
        if not out:
            out = f"(exit {result.returncode}, no output)"
        elif result.returncode != 0:
            out = f"[exit {result.returncode}] {out}"
        return out[:6000], result.returncode != 0
    except subprocess.TimeoutExpired:
        return f"[bash] Timed out after {timeout}s", True
    except Exception as e:
        return f"[bash] Error: {e}", True


def _read_file(path: str, workdir: str) -> tuple[str, bool]:
    """Read a file from workdir. Returns (content, is_error)."""
    clean = path.lstrip("/").lstrip("./")
    if ".." in clean:
        return f"[read_file] Rejected path: {path}", True
    fpath = pathlib.Path(workdir) / clean
    try:
        content = fpath.read_text(errors="replace")
        if len(content) > MAX_FILE_SIZE:
            content = content[:MAX_FILE_SIZE] + "\n...[truncated]"
        return content, False
    except FileNotFoundError:
        return f"[read_file] Not found: {path}", True
    except Exception as e:
        return f"[read_file] Error: {e}", True


def _write_file(path: str, content: str, workdir: str) -> tuple[str, bool]:
    """Write a file to workdir. Returns (message, is_error)."""
    clean = path.lstrip("/").lstrip("./")
    if ".." in clean:
        return f"[write_file] Rejected path: {path}", True
    fpath = pathlib.Path(workdir) / clean
    try:
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(content)
        return f"[write_file] Wrote {fpath} ({len(content.splitlines())} lines)", False
    except Exception as e:
        return f"[write_file] Error: {e}", True


def _list_files(subdir: str, pattern: str, workdir: str) -> tuple[str, bool]:
    """List files in workdir. Returns (listing, is_error)."""
    base = pathlib.Path(workdir)
    target = base / (subdir or ".")
    try:
        skip = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", "dist", "build"}
        results = []
        for f in sorted(target.rglob(pattern or "*")):
            rel = str(f.relative_to(base))
            if any(s in rel for s in skip):
                continue
            if f.is_file():
                results.append(rel)
        listing = "\n".join(results[:200])
        return listing or "(empty)", False
    except Exception as e:
        return f"[list_files] Error: {e}", True


def _call_llm(messages: list, model: str) -> dict | None:
    """Call LLM via OpenAI-compatible API. Returns parsed response dict or None."""
    if not API_KEY:
        return None

    payload = json.dumps({
        "model": model,
        "messages": messages,
        "tools": _TOOLS,
        "tool_choice": "auto",
        "temperature": 0.02,
        "max_tokens": 16384,
    }).encode()

    req = urllib.request.Request(
        f"{BASE_URL.rstrip('/')}/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
        },
        method="POST",
    )

    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < 2:
                time.sleep(2 ** attempt * 2)
                continue
            logger.error(f"[grpc-server] LLM HTTP {e.code}")
            return None
        except Exception as e:
            logger.error(f"[grpc-server] LLM error: {e}")
            if attempt < 2:
                time.sleep(2)
                continue
            return None
    return None


class AgentServiceServicer(openclaude_pb2_grpc.AgentServiceServicer):
    """
    Full bidirectional streaming implementation of AgentService.Chat.

    Protocol correctness (Problem-2 fix):
      - Every tool call emits ToolCallStart BEFORE execution
      - Every tool call emits ToolCallResult AFTER execution
      - Dangerous commands pause loop and emit ActionRequired (Problem-6 fix)
      - Final response emits FinalResponse with full text + token counts
      - Errors emit ErrorResponse
    """

    def Chat(self, request_iterator, context):
        """Bidirectional streaming Chat handler."""
        # Build a queue for incoming client messages after the initial request
        input_q: queue.Queue = queue.Queue()

        def _read_inputs():
            try:
                for msg in request_iterator:
                    input_q.put(msg)
            except Exception:
                pass
            input_q.put(None)  # sentinel

        reader = threading.Thread(target=_read_inputs, daemon=True)
        reader.start()

        # Wait for the initial ChatRequest
        first = input_q.get(timeout=30)
        if first is None:
            return
        event_type = first.WhichOneof("payload")
        if event_type != "request":
            return

        req     = first.request
        task    = req.message
        workdir = req.working_directory or "/tmp"
        model   = req.model or MODEL

        logger.info(f"[grpc-server] Chat — model={model} workdir={workdir} task={task[:80]}")

        if not API_KEY:
            yield openclaude_pb2.ServerMessage(
                error=openclaude_pb2.ErrorResponse(
                    message="OPENAI_API_KEY / DO_INFERENCE_API_KEY not set",
                    code="no_api_key",
                )
            )
            return

        # Ensure workdir exists
        pathlib.Path(workdir).mkdir(parents=True, exist_ok=True)

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": f"TASK: {task}\nWORKDIR: {workdir}"},
        ]

        full_text_parts = []
        prompt_tokens = 0
        completion_tokens = 0

        for iteration in range(MAX_ITERATIONS):
            logger.info(f"[grpc-server] Iteration {iteration + 1}/{MAX_ITERATIONS}")

            # Call LLM
            response = _call_llm(messages, model)
            if not response:
                yield openclaude_pb2.ServerMessage(
                    error=openclaude_pb2.ErrorResponse(
                        message="LLM call failed",
                        code="llm_error",
                    )
                )
                return

            # Extract usage
            usage = response.get("usage", {})
            prompt_tokens     = usage.get("prompt_tokens", prompt_tokens)
            completion_tokens = usage.get("completion_tokens", completion_tokens)

            choice  = response.get("choices", [{}])[0]
            msg     = choice.get("message", {})
            content = msg.get("content") or ""
            tool_calls = msg.get("tool_calls") or []

            # Stream text content if any
            if content:
                full_text_parts.append(content)
                # Stream in chunks for progressive display
                chunk_size = 200
                for i in range(0, len(content), chunk_size):
                    yield openclaude_pb2.ServerMessage(
                        text_chunk=openclaude_pb2.TextChunk(text=content[i:i + chunk_size])
                    )

                # Check for completion signal
                if "TASK_COMPLETE" in content:
                    logger.info("[grpc-server] Task complete signal received")
                    break

            # No tool calls = agentic loop is done
            if not tool_calls:
                logger.info("[grpc-server] No tool calls — loop complete")
                break

            # Process tool calls
            messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
            tool_results = []

            for tc in tool_calls:
                tool_id   = tc.get("id", str(uuid.uuid4()))
                fn_name   = tc.get("function", {}).get("name", "")
                fn_args_s = tc.get("function", {}).get("arguments", "{}")

                try:
                    fn_args = json.loads(fn_args_s)
                except json.JSONDecodeError:
                    fn_args = {}

                # Emit ToolCallStart BEFORE execution
                yield openclaude_pb2.ServerMessage(
                    tool_start=openclaude_pb2.ToolCallStart(
                        tool_name=fn_name,
                        arguments_json=fn_args_s,
                        tool_use_id=tool_id,
                    )
                )

                output    = ""
                is_error  = False

                if fn_name == "bash":
                    command = fn_args.get("command", "")
                    timeout = int(fn_args.get("timeout", 60))

                    # Problem-6 fix: ActionRequired gate for dangerous commands
                    if _is_dangerous(command) and not YOLO_MODE:
                        prompt_id = str(uuid.uuid4())
                        logger.warning(f"[grpc-server] Dangerous command — ActionRequired: {command[:80]}")

                        yield openclaude_pb2.ServerMessage(
                            action_required=openclaude_pb2.ActionRequired(
                                prompt_id=prompt_id,
                                question=(
                                    f"The agent wants to execute a potentially destructive command:\n\n"
                                    f"  {command}\n\n"
                                    f"Reply y to execute, n to abort."
                                ),
                                type=openclaude_pb2.ActionRequired.CONFIRM_COMMAND,
                            )
                        )

                        # Wait for UserInput from client
                        approved = False
                        try:
                            user_msg = input_q.get(timeout=300)
                            if user_msg is not None:
                                ui_type = user_msg.WhichOneof("payload")
                                if ui_type == "input":
                                    reply = user_msg.input.reply.strip().lower()
                                    approved = reply in ("y", "yes", "ok", "approve", "1")
                        except queue.Empty:
                            pass

                        if not approved:
                            output   = f"[ActionRequired] Command aborted by operator: {command[:120]}"
                            is_error = True
                            logger.info("[grpc-server] Dangerous command aborted by operator")
                        else:
                            logger.info("[grpc-server] Dangerous command approved — executing")
                            output, is_error = _exec_bash(command, workdir, timeout)
                    else:
                        output, is_error = _exec_bash(command, workdir, timeout)

                elif fn_name == "read_file":
                    output, is_error = _read_file(fn_args.get("path", ""), workdir)

                elif fn_name == "write_file":
                    output, is_error = _write_file(
                        fn_args.get("path", ""),
                        fn_args.get("content", ""),
                        workdir,
                    )

                elif fn_name == "list_files":
                    output, is_error = _list_files(
                        fn_args.get("subdir", "."),
                        fn_args.get("pattern", "*"),
                        workdir,
                    )

                else:
                    output   = f"[grpc-server] Unknown tool: {fn_name}"
                    is_error = True

                logger.info(f"[grpc-server] Tool {fn_name}: {len(output)} chars, error={is_error}")

                # Emit ToolCallResult AFTER execution
                yield openclaude_pb2.ServerMessage(
                    tool_result=openclaude_pb2.ToolCallResult(
                        tool_name=fn_name,
                        output=output,
                        is_error=is_error,
                        tool_use_id=tool_id,
                    )
                )

                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "content": output,
                })

            # Add all tool results to message history
            messages.extend(tool_results)

        # Emit FinalResponse
        full_text = "".join(full_text_parts)
        yield openclaude_pb2.ServerMessage(
            done=openclaude_pb2.FinalResponse(
                full_text=full_text,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
        )
        logger.info(
            f"[grpc-server] Complete — iterations={iteration + 1} "
            f"tokens={prompt_tokens + completion_tokens}"
        )


def serve():
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=4),
        options=[
            ("grpc.max_send_message_length",    128 * 1024 * 1024),
            ("grpc.max_receive_message_length", 128 * 1024 * 1024),
        ],
    )
    openclaude_pb2_grpc.add_AgentServiceServicer_to_server(
        AgentServiceServicer(), server
    )
    server.add_insecure_port(f"{GRPC_HOST}:{GRPC_PORT}")
    server.start()
    logger.info(
        f"[grpc-server] Full-protocol Python gRPC server started on {GRPC_HOST}:{GRPC_PORT} "
        f"model={MODEL} yolo={YOLO_MODE} "
        f"implements: TextChunk ToolCallStart ToolCallResult ActionRequired FinalResponse"
    )
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
