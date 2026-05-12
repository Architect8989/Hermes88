#!/usr/bin/env python3
"""
OpenClaude gRPC client skill for Hermes.
Sends tasks to the AgentService.Chat bidirectional stream (openclaude.proto).

Priority order:
  1. gRPC → localhost:50051  (openclaude Node/Bun server OR Python fallback server)
  2. Direct DO Inference API (when gRPC is unavailable — same API as gateway/run.py and jcode)

The openclaude binary and @gitlawb/openclaude npm package are NOT used here;
that package is not reliably available. All fallback logic calls DO Inference
directly via urllib (zero extra dependencies).
"""
import argparse
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, "/app/skills/openclaude_grpc")

try:
    import grpc
    import openclaude_pb2
    import openclaude_pb2_grpc
    GRPC_AVAILABLE = True
except ImportError:
    GRPC_AVAILABLE = False
    grpc = None  # type: ignore


def _request_iterator(prompt: str, workdir: str, model: str):
    """Yield the initial ChatRequest as the first message in the bidirectional stream."""
    req = openclaude_pb2.ChatRequest(
        message=prompt,
        working_directory=workdir,
        session_id=str(uuid.uuid4()),
    )
    if model:
        req.model = model
    yield openclaude_pb2.ClientMessage(request=req)


def run_openclaude_grpc(prompt: str, workdir: str, model: str, timeout: int = 480) -> int:
    """Stream openclaude output via gRPC AgentService.Chat. Returns 0 on success, 1 on failure."""
    if not GRPC_AVAILABLE:
        print("[grpc] gRPC stubs unavailable — using direct API fallback", file=sys.stderr)
        return _api_fallback(prompt, workdir, timeout)

    channel = grpc.insecure_channel("localhost:50051")
    stub = openclaude_pb2_grpc.AgentServiceStub(channel)

    try:
        for server_msg in stub.Chat(
            _request_iterator(prompt, workdir, model),
            timeout=timeout,
        ):
            event = server_msg.WhichOneof("event")
            if event == "text_chunk":
                print(server_msg.text_chunk.text, end="", flush=True)
            elif event == "tool_start":
                print(
                    f"\n[grpc] tool: {server_msg.tool_start.tool_name}",
                    file=sys.stderr,
                )
            elif event == "action_required":
                print(
                    f"\n[grpc] auto-approving: {server_msg.action_required.question[:100]}",
                    file=sys.stderr,
                )
            elif event == "done":
                print("", flush=True)
                break
            elif event == "error":
                print(
                    f"\n[grpc] agent error: {server_msg.error.message}",
                    file=sys.stderr,
                )
                return 1
        return 0
    except grpc.RpcError as e:
        print(f"\n[grpc] RPC error: {e.code()}: {e.details()}", file=sys.stderr)
        print("[grpc] falling back to direct API", file=sys.stderr)
        return _api_fallback(prompt, workdir, timeout)


def _api_fallback(prompt: str, workdir: str, timeout: int) -> int:
    """
    Direct DO Inference API fallback when gRPC server is unavailable.
    Uses the same OpenAI-compatible endpoint as gateway/run.py and scripts/jcode.
    Writes any # FILE: <path> blocks from the response directly to workdir.
    """
    api_key  = os.environ.get("OPENAI_API_KEY") or os.environ.get("DO_INFERENCE_API_KEY", "")
    base_url = (
        os.environ.get("OPENAI_BASE_URL")
        or os.environ.get("DO_INFERENCE_BASE_URL", "https://inference.do-ai.run/v1")
    )
    model = (
        os.environ.get("OPENCLAUDE_MODEL")
        or os.environ.get("OPENAI_MODEL", "deepseek-v4-pro")
    )

    if not api_key:
        print(
            "[grpc-fallback] OPENAI_API_KEY / DO_INFERENCE_API_KEY not set",
            file=sys.stderr,
        )
        return 1

    system = (
        "You are an expert software engineer. You receive a task and file context. "
        "Read files, apply fixes, write all changes.\n"
        "When writing a file output it exactly as:\n"
        "# FILE: relative/path/to/file.ext\n"
        "<full file contents>\n"
        "Write every changed file in full. No explanations. No markdown fences."
    )

    ctx_parts: list[str] = []
    try:
        for fp in sorted(pathlib.Path(workdir).rglob("*")):
            if fp.is_file() and fp.suffix in (
                ".py", ".ts", ".js", ".json", ".yaml", ".yml",
                ".toml", ".md", ".sh", ".txt",
            ):
                try:
                    ctx_parts.append(
                        f"# FILE: {fp.relative_to(workdir)}\n"
                        f"{fp.read_text(errors='replace')[:3000]}"
                    )
                except Exception:
                    pass
            if len(ctx_parts) >= 20:
                break
    except Exception:
        pass

    context   = "\n\n".join(ctx_parts)
    full_msg  = f"{prompt}\n\nWorkdir: {workdir}"
    if context:
        full_msg += f"\n\nContext files:\n{context}"

    payload = json.dumps({
        "model":    model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": full_msg},
        ],
        "temperature": 0.05,
        "max_tokens":  8192,
    }).encode()

    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=payload,
        headers={
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(
            f"[grpc-fallback] HTTP {e.code}: {e.read().decode()[:200]}",
            file=sys.stderr,
        )
        return 1
    except Exception as e:
        print(f"[grpc-fallback] API error: {e}", file=sys.stderr)
        return 1

    content = (
        data.get("choices", [{}])[0].get("message", {}).get("content", "")
    )
    if not content:
        print("[grpc-fallback] empty response from model", file=sys.stderr)
        return 1

    print(content, flush=True)

    file_pattern  = re.compile(r"^#\s*FILE:\s*(.+)$")
    current_file: str | None = None
    current_lines: list[str] = []

    def _flush(path: str | None, lines: list[str]) -> None:
        if not path or not lines:
            return
        fpath = pathlib.Path(workdir) / path.lstrip("/")
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text("\n".join(lines).strip() + "\n")
        print(f"[grpc-fallback] wrote {fpath} ({len(lines)} lines)", file=sys.stderr)

    for line in content.split("\n"):
        m = file_pattern.match(line)
        if m:
            _flush(current_file, current_lines)
            current_file  = m.group(1).strip()
            current_lines = []
        elif current_file is not None:
            current_lines.append(line)
    _flush(current_file, current_lines)

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OpenClaude gRPC client for Hermes")
    parser.add_argument("--prompt",  required=True)
    parser.add_argument("--workdir", default="/tmp")
    parser.add_argument("--model",   default="")
    parser.add_argument("--timeout", type=int, default=480)
    args = parser.parse_args()
    sys.exit(run_openclaude_grpc(args.prompt, args.workdir, args.model, args.timeout))
