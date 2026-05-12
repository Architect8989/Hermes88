#!/usr/bin/env python3
"""
skills/openclaude_grpc/server.py — Python gRPC server fallback for openclaude.

Used by supervisord when the Node.js/Bun openclaude gRPC binary is unavailable.
Implements the AgentService.Chat bidirectional stream defined in openclaude.proto.
Delegates to the DO Inference OpenAI-compatible API (CLAUDE_CODE_USE_OPENAI=1).

Start:
    python3 /app/skills/openclaude_grpc/server.py

Env vars:
    GRPC_PORT            — listen port (default: 50051)
    OPENAI_API_KEY       — DO Inference API key
    OPENAI_BASE_URL      — DO Inference base URL
    OPENAI_MODEL         — model to use (default: deepseek-v4-pro)
"""
import logging
import os
import sys
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
        "-I/app/skills/openclaude_grpc --python_out=/app/skills/openclaude_grpc "
        "--grpc_python_out=/app/skills/openclaude_grpc /app/skills/openclaude_grpc/openclaude.proto\n"
        f"ImportError: {exc}"
    )
    sys.exit(1)

try:
    from openai import OpenAI
except ImportError:
    logger.error("openai package not installed. Run: pip3 install openai")
    sys.exit(1)

GRPC_PORT   = int(os.environ.get("GRPC_PORT", "50051"))
API_KEY     = os.environ.get("OPENAI_API_KEY") or os.environ.get("DO_INFERENCE_API_KEY", "")
BASE_URL    = os.environ.get("OPENAI_BASE_URL") or os.environ.get("DO_INFERENCE_BASE_URL", "https://inference.do-ai.run/v1")
MODEL       = os.environ.get("OPENAI_MODEL") or os.environ.get("OPENCLAUDE_MODEL", "deepseek-v4-pro")

SYSTEM_PROMPT = (
    "You are an expert software engineer. You receive a task, read the relevant files "
    "in the working directory, and write all fixes directly to disk. "
    "Do not ask for confirmation. Do not explain. Fix and exit."
)


class AgentServiceServicer(openclaude_pb2_grpc.AgentServiceServicer):
    """Python fallback implementation of the openclaude AgentService gRPC server."""

    def Chat(self, request_iterator, context):
        client = OpenAI(api_key=API_KEY, base_url=BASE_URL)

        for client_msg in request_iterator:
            event_type = client_msg.WhichOneof("payload")
            if event_type != "request":
                continue

            req = client_msg.request
            prompt  = req.message
            workdir = req.working_directory or "/tmp"
            model   = req.model or MODEL

            logger.info(
                f"[grpc-server] Chat request — model={model} workdir={workdir} "
                f"prompt_chars={len(prompt)}"
            )

            if not API_KEY:
                yield openclaude_pb2.ServerMessage(
                    error=openclaude_pb2.ErrorResponse(
                        message="OPENAI_API_KEY / DO_INFERENCE_API_KEY not set",
                        code="no_api_key",
                    )
                )
                return

            try:
                full_text = ""
                with client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user",   "content": prompt},
                    ],
                    max_tokens=8192,
                    temperature=0.05,
                    stream=True,
                ) as stream:
                    for chunk in stream:
                        delta = chunk.choices[0].delta.content or ""
                        if delta:
                            full_text += delta
                            yield openclaude_pb2.ServerMessage(
                                text_chunk=openclaude_pb2.TextChunk(text=delta)
                            )

                yield openclaude_pb2.ServerMessage(
                    done=openclaude_pb2.FinalResponse(full_text=full_text)
                )
                logger.info(
                    f"[grpc-server] Chat complete — chars={len(full_text)}"
                )

            except Exception as exc:
                logger.error(f"[grpc-server] LLM error: {exc}")
                yield openclaude_pb2.ServerMessage(
                    error=openclaude_pb2.ErrorResponse(
                        message=str(exc),
                        code="llm_error",
                    )
                )
            return


def serve():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    openclaude_pb2_grpc.add_AgentServiceServicer_to_server(
        AgentServiceServicer(), server
    )
    server.add_insecure_port(f"[::]:{GRPC_PORT}")
    server.start()
    logger.info(
        f"[grpc-server] Python fallback gRPC server started on port {GRPC_PORT} "
        f"— model={MODEL} base_url={BASE_URL}"
    )
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
