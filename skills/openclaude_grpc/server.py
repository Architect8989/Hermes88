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
  HERMES_YOLO_MODE  — "1" → dangerously-skip-permissions (NO approval gate)
                      default "0" → --permission-mode plan (read-only, safe)

SAFETY MODEL — ActionRequired:
  The proto defines ActionRequired for operator approval of dangerous commands
  (CONFIRM_COMMAND, REQUEST_INFORMATION). This requires a full bidirectional
  gRPC stream: server sends ActionRequired, client sends UserInput, server
  resumes. That loop requires:
    1. A real interactive channel (Telegram inline buttons, Slack block actions)
       to surface the approval request to a human operator.
    2. The gRPC client to hold the stream open and forward UserInput messages.
    3. The subprocess stdin to be wired so that openclaude can receive the
       approval answer and continue.

  None of these are wired in the current implementation because this server
  runs headlessly (no interactive channel available at the gRPC layer).

  CURRENT SAFE DEFAULT (HERMES_YOLO_MODE != "1"):
    --permission-mode plan is passed to openclaude.
    openclaude will read files and produce a plan but will NOT execute shell
    commands or write files. This eliminates the rm -rf class of risk.

  TO ENABLE FULL EXECUTION:
    Set HERMES_YOLO_MODE=1 only if:
      a) You fully trust the LLM's judgment on the target repo, OR
      b) You have implemented the ActionRequired bidirectional loop with
         a real approval channel (Telegram, Slack, etc.)

  Do NOT set HERMES_YOLO_MODE=1 in production against untrusted repos.
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

        # Build the command passed to the real openclaude binary.
        # Safety policy is enforced here:
        #   HERMES_YOLO_MODE=0 (default): --permission-mode plan
        #     openclaude reads and plans but does NOT execute shell commands
        #     or write files. Safe for untrusted / arbitrary repos.
        #   HERMES_YOLO_MODE=1: --dangerously-skip-permissions
        #     openclaude executes all tool calls without approval.
        #     Use ONLY on trusted repos with human oversight.
        cmd = [binary, "--print", "--prompt", req.message, "--workdir", workdir]
        if model:
            cmd += ["--model", model]

        if yolo:
            cmd += ["--dangerously-skip-permissions"]
            logger.warning(
                "[server] HERMES_YOLO_MODE=1: dangerous commands will execute "
                "without operator approval. Ensure you trust the target repo."
            )
        else:
            # Safe default: read-only plan mode.
            # openclaude will not execute shell commands or write files.
            # To enable execution, set HERMES_YOLO_MODE=1 or implement the
            # ActionRequired approval loop (see module docstring).
            cmd += ["--permission-mode", "plan"]
            logger.info(
                "[server] permission-mode=plan (read-only). "
                "Set HERMES_YOLO_MODE=1 to enable execution."
            )

        logger.info(
            f"[server] openclaude session={req.session_id[:8] if req.session_id else '?'} "
            f"workdir={workdir} yolo={yolo}"
        )

        proc = None
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            for line in proc.stdout:
                # Check if client has disconnected — kill subprocess if so
                if not context.is_active():
                    logger.info("[server] Client disconnected — killing openclaude subprocess")
                    proc.kill()
                    proc.wait(timeout=5)
                    return
                yield openclaude_pb2.ServerMessage(
                    text_chunk=openclaude_pb2.TextChunk(text=line)
                )
            proc.wait(timeout=30)
            if proc.returncode != 0:
                yield openclaude_pb2.ServerMessage(
                    error=openclaude_pb2.ErrorResponse(
                        message=f"openclaude exited with code {proc.returncode}"
                    )
                )
                return
        except Exception as exc:
            if proc and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
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
