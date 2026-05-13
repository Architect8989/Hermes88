#!/usr/bin/env python3
"""
rhodawk_core/orchestrator.py — hermes-agent model routing pass-through.

Routes all model calls through hermes-agent's built-in multi-provider system.
hermes-agent handles provider selection, failover, rate limiting, and
exponential backoff natively across 200+ models:

  - Nous Portal, OpenRouter (200+ models)
  - NVIDIA NIM (Nemotron)
  - DO Inference (DeepSeek)
  - Anthropic, OpenAI
  - Moonshot / Kimi, MiniMax, GLM, Hugging Face

Configure providers:  hermes model
Configure failover:   ~/.hermes/config.yaml  →  models.failover_chain
Full docs:            https://hermes-agent.nousresearch.com/docs/user-guide/configuration
"""

import shutil
import subprocess


class Orchestrator:
    """Routes requests through hermes-agent's multi-provider model system."""

    def route_request(self, messages: list[dict], task_type: str = "general") -> dict:
        """
        Route a model request through hermes-agent.

        hermes-agent selects the provider and handles failover automatically
        based on the config set by `hermes model`. No provider-specific code
        lives here — that is hermes-agent's responsibility.

        Returns dict with 'content' and 'provider' on success, or 'error'.
        """
        binary = shutil.which("hermes") or shutil.which("hermes-agent")
        if not binary:
            return {
                "error": (
                    "hermes-agent not installed. "
                    "Install: pip3 install 'hermes-agent[messaging,pty,mcp,acp]'"
                )
            }

        user_msgs = [m.get("content", "") for m in messages if m.get("role") == "user"]
        prompt = user_msgs[-1] if user_msgs else ""
        if not prompt:
            return {"error": "No user message in request"}

        try:
            result = subprocess.run(
                [binary, "--message", prompt],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0 and result.stdout.strip():
                return {
                    "content":  result.stdout.strip(),
                    "provider": "hermes-agent",
                    "model":    "routed",
                }
            err = (result.stderr or "empty response").strip()
            return {"error": err[:500]}
        except subprocess.TimeoutExpired:
            return {"error": "hermes-agent timed out (120s)"}
        except Exception as exc:
            return {"error": str(exc)}


def get_orchestrator() -> Orchestrator:
    return Orchestrator()
