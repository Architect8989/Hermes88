#!/usr/bin/env python3
"""
skills/openclaude_grpc/client.py — openclaude gRPC client for Hermes.

Sends tasks to the AgentService.Chat bidirectional stream (openclaude.proto).

Priority order:
  1. gRPC → localhost:50051  (routes to real openclaude CLI via server.py)
  2. openclaude CLI directly  (when gRPC server is unavailable)

All agentic logic — tool calling, file writing, bash, grep, glob, MCP —
is handled by openclaude itself. This client only drives the transport.

Install openclaude: npm install -g @gitlawb/openclaude
"""

import argparse
import os
import shutil
import subprocess
import sys
import uuid

import pathlib as _pathlib
sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent))

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
    """Stream openclaude output via gRPC. Falls back to direct CLI if gRPC is down."""
    if not GRPC_AVAILABLE:
        print("[grpc] gRPC stubs unavailable — using openclaude CLI directly", file=sys.stderr)
        return _cli_fallback(prompt, workdir, model, timeout)

    channel = grpc.insecure_channel("localhost:50051")
    stub    = openclaude_pb2_grpc.AgentServiceStub(channel)

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
                    f"\n[grpc] approval required: {server_msg.action_required.question[:100]}",
                    file=sys.stderr,
                )
            elif event == "done":
                print("", flush=True)
                break
            elif event == "error":
                print(
                    f"\n[grpc] error: {server_msg.error.message}",
                    file=sys.stderr,
                )
                return 1
        return 0
    except grpc.RpcError as exc:
        print(f"\n[grpc] RPC error: {exc.code()}: {exc.details()}", file=sys.stderr)
        print("[grpc] gRPC server down — falling back to openclaude CLI", file=sys.stderr)
        return _cli_fallback(prompt, workdir, model, timeout)


def _cli_fallback(prompt: str, workdir: str, model: str, timeout: int) -> int:
    """
    Call openclaude CLI directly when the gRPC server is unavailable.

    openclaude handles all agentic logic: file reading, tool calling, writing,
    bash execution, and self-correction. No reimplementation needed here.
    """
    binary = shutil.which("openclaude")
    if not binary:
        print(
            "[grpc] openclaude not found. Install: npm install -g @gitlawb/openclaude",
            file=sys.stderr,
        )
        return 1

    cmd = [binary, "--print", "--prompt", prompt, "--workdir", workdir]
    if model:
        cmd += ["--model", model]

    try:
        result = subprocess.run(cmd, timeout=timeout)
        return result.returncode
    except subprocess.TimeoutExpired:
        print(f"[grpc] openclaude timed out after {timeout}s", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"[grpc] openclaude error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OpenClaude gRPC client for Hermes")
    parser.add_argument("--prompt",  required=True)
    parser.add_argument("--workdir", default="/tmp")
    parser.add_argument("--model",   default="")
    parser.add_argument("--timeout", type=int, default=480)
    args = parser.parse_args()
    sys.exit(run_openclaude_grpc(args.prompt, args.workdir, args.model, args.timeout))
