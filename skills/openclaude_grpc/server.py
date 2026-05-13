#!/usr/bin/env python3
"""
skills/openclaude_grpc/server.py — openclaude gRPC pass-through server.

Wraps the real openclaude CLI (github.com/Gitlawb/openclaude) as a gRPC
AgentService. All agentic logic — tool calling, bash execution, file writing,
multi-step planning, grep/glob, MCP, and self-correction — is handled by
openclaude itself. This server only translates between the gRPC proto contract
and the subprocess.

Install openclaude:
  npm install -g @gitlawb/openclaude

Env vars:
  GRPC_PORT         — listen port (default: 50051)
  OPENCLAUDE_MODEL  — model passed to openclaude --model
  HERMES_YOLO_MODE  — "1" → pass --dangerously-skip-permissions to openclaude
"""

import logging
import os
import shutil
import subprocess
import sys
from concurrent import futures

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger("openclaude-grpc")

try:
    import grpc
    import openclaude_pb2
    import openclaude_pb2_grpc
except ImportError as exc:
    logger.error(f"[server] gRPC stubs not found: {exc}")
    sys.exit(1)


class AgentServiceServicer(openclaude_pb2_grpc.AgentServiceServicer):

    def Chat(self, request_iterator, context):
        # Read the first (and only required) client message
        req = None
        for client_msg in request_iterator:
            if client_msg.HasField("request"):
                req = client_msg.request
                break

        if req is None:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "No ChatRequest received")
            return

        binary = shutil.which("openclaude")
        if not binary:
            msg = (
                "openclaude not found. "
                "Install: npm install -g @gitlawb/openclaude"
            )
            logger.error(f"[server] {msg}")
            yield openclaude_pb2.ServerMessage(
                error=openclaude_pb2.ErrorResponse(message=msg)
            )
            return

        model   = req.model or os.environ.get("OPENCLAUDE_MODEL", "")
        workdir = req.working_directory or "/tmp"
        yolo    = os.environ.get("HERMES_YOLO_MODE", "0") == "1"

        # Pass 100% through to the real openclaude binary
        cmd = [binary, "--print", "--prompt", req.message, "--workdir", workdir]
        if model:
            cmd += ["--model", model]
        if yolo:
            cmd += ["--dangerously-skip-permissions"]

        logger.info(
            f"[server] openclaude session={req.session_id[:8] if req.session_id else '?'} "
            f"workdir={workdir}"
        )

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,   # merge stderr into stdout stream
                text=True,
                bufsize=1,
            )
            for line in proc.stdout:
                yield openclaude_pb2.ServerMessage(
                    text_chunk=openclaude_pb2.TextChunk(text=line)
                )
            proc.wait()
            if proc.returncode != 0:
                yield openclaude_pb2.ServerMessage(
                    error=openclaude_pb2.ErrorResponse(
                        message=f"openclaude exited with code {proc.returncode}"
                    )
                )
                return
        except Exception as exc:
            yield openclaude_pb2.ServerMessage(
                error=openclaude_pb2.ErrorResponse(message=str(exc))
            )
            return

        yield openclaude_pb2.ServerMessage(
            done=openclaude_pb2.FinalResponse(full_text="openclaude completed")
        )


def serve(port: int = 50051) -> None:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    openclaude_pb2_grpc.add_AgentServiceServicer_to_server(AgentServiceServicer(), server)
    server.add_insecure_port(f"[::]:{port}")
    server.start()
    logger.info(f"[server] Listening on :{port} — routing to openclaude CLI")
    server.wait_for_termination()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=int(os.environ.get("GRPC_PORT", "50051")))
    args = p.parse_args()
    serve(args.port)
