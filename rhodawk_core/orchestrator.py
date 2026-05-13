#!/usr/bin/env python3
"""
Rhodawk Core Orchestrator.

LLM orchestration with multi-model routing, automatic failover chain,
rate limiting with exponential backoff, and request/response logging.

FIX Problem-7: Multi-provider failover is now REAL multi-provider, not just
different model names behind the same DO Inference base URL.

Provider chain:
  Tier 1: DO Inference  (deepseek-v4-pro, kimi-k2.6)   — primary
  Tier 2: DO Inference  (lighter fallback model)        — same provider, rate-limit escape
  Tier 3: Anthropic     (claude-3-5-haiku-20241022)     — genuinely different provider
  Tier 4: OpenRouter    (qwen/qwen-2.5-72b-instruct)    — aggregator, 10+ backends

Each ModelConfig now carries its own base_url and api_key, so _call_model()
uses the correct endpoint for each tier rather than hard-coding self.base_url.

Copyright (c) 2024-2025 Rhodawk AI. All rights reserved.
"""
import hashlib
import json
import os
import time
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Callable


# -- Configuration ---------------------------------------------------------------


@dataclass
class ModelConfig:
    """Configuration for a single LLM model."""
    name: str
    provider: str = "do_inference"
    base_url: str = ""
    api_key: str = ""
    max_tokens: int = 16384
    temperature: float = 0.1
    cost_per_1k_input: float = 0.0
    cost_per_1k_output: float = 0.0
    requests_per_minute: int = 60
    timeout: int = 120
    supports_streaming: bool = True
    supports_function_calling: bool = False
    context_window: int = 128000
    # Provider-specific extra headers (e.g. Anthropic-Version)
    extra_headers: dict = field(default_factory=dict)


@dataclass
class RequestRecord:
    """Record of a single LLM request for logging and analytics."""
    id: str = ""
    model: str = ""
    provider: str = ""
    task_type: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    success: bool = True
    error: str = ""
    timestamp: float = field(default_factory=time.time)
    cost: float = 0.0


# -- Rate Limiter ----------------------------------------------------------------


class RateLimiter:
    """Token bucket rate limiter with per-model tracking."""

    def __init__(self):
        self._buckets: Dict[str, deque] = {}
        self._lock = threading.Lock()

    def acquire(self, model_name: str, rpm_limit: int) -> bool:
        """Try to acquire a rate limit token. Returns True if allowed."""
        with self._lock:
            now = time.time()
            if model_name not in self._buckets:
                self._buckets[model_name] = deque()

            bucket = self._buckets[model_name]

            # Remove requests older than 60 seconds
            while bucket and bucket[0] < now - 60:
                bucket.popleft()

            if len(bucket) >= rpm_limit:
                return False

            bucket.append(now)
            return True

    def wait_time(self, model_name: str, rpm_limit: int) -> float:
        """Calculate seconds to wait before next request is allowed."""
        with self._lock:
            now = time.time()
            if model_name not in self._buckets:
                return 0.0

            bucket = self._buckets[model_name]
            while bucket and bucket[0] < now - 60:
                bucket.popleft()

            if len(bucket) < rpm_limit:
                return 0.0

            return bucket[0] + 60 - now


# -- Orchestrator ----------------------------------------------------------------


class Orchestrator:
    """
    Unified LLM orchestrator with multi-provider routing and automatic failover.

    FIX Problem-7: Each model now carries its own (base_url, api_key, extra_headers)
    so requests to Anthropic go to api.anthropic.com and requests to OpenRouter
    go to openrouter.ai — not everything to DO Inference.

    Provider selection:
      - DO Inference is tried first (best tool calling + context window).
      - If DO Inference returns 429/503 or times out N times, Anthropic is tried.
      - If Anthropic is unavailable (no API key or fails), OpenRouter is tried.
      - OpenRouter is the last resort — it aggregates 10+ providers.

    Task-type routing maps task types to ordered model lists. Each model in the
    list can be on a different provider; the Orchestrator picks the right
    base_url automatically.
    """

    # Task type to model mapping (primary -> fallback chain across providers)
    DEFAULT_MODEL_CHAINS = {
        "reasoning":   ["deepseek-r1", "kimi-k2.6", "claude-3-5-haiku", "qwen-openrouter"],
        "coding":      ["deepseek-v4-0324", "kimi-k2.6", "claude-3-5-haiku", "qwen-openrouter"],
        "scaffolding": ["kimi-k2.6", "deepseek-v4-0324", "claude-3-5-haiku", "qwen-openrouter"],
        "fast":        ["kimi-k2.6", "deepseek-v4-0324", "claude-3-5-haiku"],
        "embedding":   ["text-embedding-3-small"],
        "general":     ["kimi-k2.6", "deepseek-v4-0324", "claude-3-5-haiku", "qwen-openrouter"],
    }

    def __init__(self, config: Optional[dict] = None):
        config = config or {}

        # Default DO Inference credentials (used by _init_default_models)
        self._do_base_url = config.get("base_url", os.environ.get(
            "DO_INFERENCE_BASE_URL", "https://inference.do-ai.run/v1"
        ))
        self._do_api_key = config.get("api_key", os.environ.get(
            "DO_INFERENCE_API_KEY", ""
        ))

        # FIX Problem-7: True alternative provider credentials
        self._anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self._openrouter_api_key = os.environ.get("OPENROUTER_API_KEY", "")

        # Model chains (task_type -> ordered list of model keys to try)
        self.model_chains: Dict[str, List[str]] = config.get(
            "model_chains", self.DEFAULT_MODEL_CHAINS
        )

        # Model configurations
        self.models: Dict[str, ModelConfig] = {}
        self._init_default_models()

        # Rate limiting
        self.rate_limiter = RateLimiter()

        # Request logging
        self._request_log: deque = deque(maxlen=1000)
        self._lock = threading.Lock()

        # Backoff tracking per model
        self._backoff: Dict[str, float] = {}
        self._consecutive_failures: Dict[str, int] = {}

        # Response cache (simple TTL cache)
        self._cache: Dict[str, tuple] = {}
        self._cache_ttl = config.get("cache_ttl", 300)

        # Callbacks
        self._on_request: Optional[Callable] = None
        self._on_response: Optional[Callable] = None

    def _init_default_models(self):
        """
        Initialize model configurations for ALL providers.

        FIX Problem-7: Tier 3 (Anthropic) and Tier 4 (OpenRouter) now use their
        own base_url and api_key, not DO Inference. This is what makes the
        failover genuinely multi-provider instead of just multi-model-name.
        """
        defaults = [
            # ── Tier 1 & 2: DO Inference ─────────────────────────────────────
            ModelConfig(
                name="deepseek-r1",
                provider="do_inference",
                base_url=self._do_base_url,
                api_key=self._do_api_key,
                max_tokens=16384,
                temperature=0.1,
                requests_per_minute=30,
                timeout=180,
                context_window=64000,
            ),
            ModelConfig(
                name="deepseek-v4-0324",
                provider="do_inference",
                base_url=self._do_base_url,
                api_key=self._do_api_key,
                max_tokens=16384,
                temperature=0.02,
                requests_per_minute=60,
                timeout=120,
                context_window=128000,
                supports_function_calling=True,
            ),
            ModelConfig(
                name="kimi-k2.6",
                provider="do_inference",
                base_url=self._do_base_url,
                api_key=self._do_api_key,
                max_tokens=16384,
                temperature=0.1,
                requests_per_minute=60,
                timeout=120,
                context_window=128000,
                supports_function_calling=True,
            ),
            ModelConfig(
                name="qwen3-235b-a22b",
                provider="do_inference",
                base_url=self._do_base_url,
                api_key=self._do_api_key,
                max_tokens=8192,
                temperature=0.1,
                requests_per_minute=30,
                timeout=180,
                context_window=128000,
            ),
            ModelConfig(
                name="text-embedding-3-small",
                provider="do_inference",
                base_url=self._do_base_url,
                api_key=self._do_api_key,
                max_tokens=8192,
                timeout=30,
                context_window=8000,
            ),
            # ── Tier 3: Anthropic (GENUINELY DIFFERENT PROVIDER) ─────────────
            # FIX Problem-7: This is the actual fix. Previously "tier 3" was just
            # another DO Inference model. Now it calls api.anthropic.com with the
            # ANTHROPIC_API_KEY env var. If that key is not set, this model is
            # skipped during failover (api_key == "" → HTTP 401 → failure).
            ModelConfig(
                name="claude-3-5-haiku",
                provider="anthropic",
                base_url="https://api.anthropic.com/v1",
                api_key=self._anthropic_api_key,
                max_tokens=8192,
                temperature=0.1,
                requests_per_minute=50,
                timeout=120,
                context_window=200000,
                supports_function_calling=True,
                extra_headers={
                    "anthropic-version": "2023-06-01",
                    "x-api-key": self._anthropic_api_key,
                },
            ),
            # ── Tier 4: OpenRouter (aggregator — last resort) ─────────────────
            # FIX Problem-7: OpenRouter aggregates 10+ providers. If DO Inference
            # AND Anthropic are both down, OpenRouter routes to whichever backend
            # is available. Requires OPENROUTER_API_KEY env var.
            ModelConfig(
                name="qwen-openrouter",
                provider="openrouter",
                base_url="https://openrouter.ai/api/v1",
                api_key=self._openrouter_api_key,
                max_tokens=8192,
                temperature=0.1,
                requests_per_minute=20,
                timeout=120,
                context_window=128000,
                supports_function_calling=True,
                extra_headers={
                    "HTTP-Referer": "https://github.com/Architect8989/Hermes88",
                    "X-Title": "Rhodawk Hermes",
                },
            ),
        ]
        for m in defaults:
            self.models[m.name] = m

    def route_request(self, messages: List[Dict[str, str]],
                      task_type: str = "general",
                      model_override: str = "",
                      max_tokens: Optional[int] = None,
                      temperature: Optional[float] = None,
                      stream: bool = False,
                      cache_key: Optional[str] = None) -> Dict[str, Any]:
        """
        Route a request to the appropriate model with automatic failover.

        FIX Problem-7: The failover chain now includes Anthropic (tier 3) and
        OpenRouter (tier 4) as genuinely different providers. When DO Inference
        fails, the Orchestrator calls a completely different API endpoint.

        Args:
            messages: Chat messages in OpenAI format
            task_type: Type of task (reasoning, coding, scaffolding, fast, general)
            model_override: Force a specific model (bypasses routing)
            max_tokens: Override max tokens
            temperature: Override temperature
            stream: Whether to stream the response
            cache_key: Optional cache key for response caching

        Returns:
            Dict with keys: content, model, provider, tokens_used, latency_ms, cached
        """
        # Check cache first
        if cache_key:
            cached = self._get_cached(cache_key)
            if cached:
                return {**cached, "cached": True}

        # Determine model chain
        if model_override:
            chain = [model_override]
        else:
            chain = self.model_chains.get(task_type, self.model_chains["general"])

        # Try each model in the chain
        last_error = ""
        for model_name in chain:
            # Skip models with no API key configured
            model_cfg = self.models.get(model_name)
            if model_cfg and not model_cfg.api_key:
                continue

            # Check backoff
            if self._is_backed_off(model_name):
                continue

            # Check rate limit
            rpm = model_cfg.requests_per_minute if model_cfg else 60
            if not self.rate_limiter.acquire(model_name, rpm):
                wait = self.rate_limiter.wait_time(model_name, rpm)
                if wait < 5:
                    time.sleep(wait)
                    if not self.rate_limiter.acquire(model_name, rpm):
                        continue
                else:
                    continue

            # Make the request (using model-specific base_url + api_key)
            result = self._call_model(
                model_name=model_name,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                stream=stream,
                task_type=task_type,
            )

            if result.get("success"):
                # Reset backoff on success
                self._consecutive_failures[model_name] = 0
                self._backoff.pop(model_name, None)

                provider = model_cfg.provider if model_cfg else "unknown"
                if model_cfg and model_cfg.provider != "do_inference":
                    print(
                        f"[orchestrator] Failover active — using {provider} "
                        f"({model_name}) instead of DO Inference",
                        flush=True,
                    )

                response = {
                    "content": result["content"],
                    "model": model_name,
                    "provider": provider,
                    "tokens_used": result.get("tokens_used", 0),
                    "latency_ms": result.get("latency_ms", 0),
                    "cached": False,
                }

                if cache_key:
                    self._set_cached(cache_key, response)

                return response
            else:
                last_error = result.get("error", "Unknown error")
                self._record_failure(model_name)

        # All models failed
        return {
            "content": "",
            "model": "",
            "provider": "",
            "tokens_used": 0,
            "latency_ms": 0,
            "cached": False,
            "error": f"All models in chain failed. Last error: {last_error}",
        }

    def complete(self, prompt: str, task_type: str = "general",
                 system_prompt: str = "", **kwargs) -> str:
        """Simple completion interface. Returns just the content string."""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        result = self.route_request(messages, task_type=task_type, **kwargs)
        return result.get("content", "")

    def _call_model(self, model_name: str, messages: List[Dict[str, str]],
                    max_tokens: Optional[int] = None,
                    temperature: Optional[float] = None,
                    stream: bool = False,
                    task_type: str = "general") -> Dict[str, Any]:
        """
        Make an API call to a specific model.

        FIX Problem-7: Uses model_cfg.base_url and model_cfg.api_key
        (not self.base_url which was always DO Inference). Also adds
        model_cfg.extra_headers for providers like Anthropic that require
        non-standard headers (anthropic-version, x-api-key).
        """
        import urllib.request
        import urllib.error

        model_cfg = self.models.get(model_name)
        effective_base_url  = model_cfg.base_url if model_cfg else self._do_base_url
        effective_api_key   = model_cfg.api_key  if model_cfg else self._do_api_key
        effective_max_tokens = max_tokens or (model_cfg.max_tokens if model_cfg else 16384)
        effective_temperature = temperature if temperature is not None else (
            model_cfg.temperature if model_cfg else 0.1
        )
        effective_timeout = model_cfg.timeout if model_cfg else 120

        # Resolve actual model name to send to the API
        # "claude-3-5-haiku" is our internal key; the Anthropic API name is different
        api_model_name = {
            "claude-3-5-haiku": "claude-3-5-haiku-20241022",
            "qwen-openrouter": "qwen/qwen-2.5-72b-instruct",
        }.get(model_name, model_name)

        payload = json.dumps({
            "model": api_model_name,
            "messages": messages,
            "max_tokens": effective_max_tokens,
            "temperature": effective_temperature,
            "stream": False,
        }).encode()

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {effective_api_key}",
        }
        # Add provider-specific extra headers
        if model_cfg and model_cfg.extra_headers:
            headers.update(model_cfg.extra_headers)

        req = urllib.request.Request(
            f"{effective_base_url.rstrip('/')}/chat/completions",
            data=payload,
            headers=headers,
            method="POST",
        )

        start_time = time.time()
        try:
            with urllib.request.urlopen(req, timeout=effective_timeout) as resp:
                data = json.loads(resp.read())

            latency_ms = (time.time() - start_time) * 1000
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            tokens_used = usage.get("total_tokens", 0)

            record = RequestRecord(
                id=f"req_{int(time.time() * 1000)}",
                model=model_name,
                provider=model_cfg.provider if model_cfg else "unknown",
                task_type=task_type,
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
                latency_ms=latency_ms,
                success=True,
            )
            self._log_request(record)

            if self._on_response:
                self._on_response(record)

            return {
                "success": True,
                "content": content,
                "tokens_used": tokens_used,
                "latency_ms": latency_ms,
            }

        except urllib.error.HTTPError as e:
            latency_ms = (time.time() - start_time) * 1000
            error_body = ""
            try:
                error_body = e.read().decode()[:500]
            except Exception:
                pass
            error_msg = f"HTTP {e.code}: {error_body}"

            record = RequestRecord(
                id=f"req_{int(time.time() * 1000)}",
                model=model_name,
                provider=model_cfg.provider if model_cfg else "unknown",
                task_type=task_type,
                latency_ms=latency_ms,
                success=False,
                error=error_msg,
            )
            self._log_request(record)

            return {"success": False, "error": error_msg}

        except Exception as e:
            latency_ms = (time.time() - start_time) * 1000
            error_msg = f"{type(e).__name__}: {str(e)}"

            record = RequestRecord(
                id=f"req_{int(time.time() * 1000)}",
                model=model_name,
                provider=model_cfg.provider if model_cfg else "unknown",
                task_type=task_type,
                latency_ms=latency_ms,
                success=False,
                error=error_msg,
            )
            self._log_request(record)

            return {"success": False, "error": error_msg}

    def _record_failure(self, model_name: str):
        """Record a failure and update backoff timing."""
        failures = self._consecutive_failures.get(model_name, 0) + 1
        self._consecutive_failures[model_name] = failures
        # Exponential backoff: 2^failures seconds, capped at 300s
        backoff_seconds = min(300, 2 ** failures)
        self._backoff[model_name] = time.time() + backoff_seconds

    def _is_backed_off(self, model_name: str) -> bool:
        """Check if a model is currently in backoff period."""
        backoff_until = self._backoff.get(model_name, 0)
        if backoff_until and time.time() < backoff_until:
            return True
        return False

    def _get_cached(self, key: str) -> Optional[dict]:
        """Get a cached response if it exists and is not expired."""
        if key in self._cache:
            response, expires_at = self._cache[key]
            if time.time() < expires_at:
                return response
            else:
                del self._cache[key]
        return None

    def _set_cached(self, key: str, response: dict):
        """Cache a response with TTL."""
        self._cache[key] = (response, time.time() + self._cache_ttl)

    def _log_request(self, record: RequestRecord):
        """Log a request record."""
        with self._lock:
            self._request_log.append(record)

    def get_request_log(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get recent request log entries."""
        with self._lock:
            entries = list(self._request_log)[-limit:]
        return [
            {
                "id": r.id,
                "model": r.model,
                "provider": r.provider,
                "task_type": r.task_type,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "latency_ms": r.latency_ms,
                "success": r.success,
                "error": r.error,
                "timestamp": r.timestamp,
            }
            for r in entries
        ]

    def get_stats(self) -> Dict[str, Any]:
        """Get orchestrator statistics."""
        with self._lock:
            entries = list(self._request_log)

        if not entries:
            return {"total_requests": 0}

        total = len(entries)
        successes = sum(1 for r in entries if r.success)
        total_tokens = sum(r.input_tokens + r.output_tokens for r in entries)
        avg_latency = sum(r.latency_ms for r in entries) / total if total else 0

        model_usage = {}
        for r in entries:
            if r.model not in model_usage:
                model_usage[r.model] = {
                    "requests": 0, "tokens": 0, "failures": 0, "provider": r.provider
                }
            model_usage[r.model]["requests"] += 1
            model_usage[r.model]["tokens"] += r.input_tokens + r.output_tokens
            if not r.success:
                model_usage[r.model]["failures"] += 1

        return {
            "total_requests": total,
            "success_rate": successes / total if total else 0,
            "total_tokens": total_tokens,
            "avg_latency_ms": avg_latency,
            "model_usage": model_usage,
            "cache_size": len(self._cache),
            "providers_configured": {
                "do_inference": bool(self._do_api_key),
                "anthropic": bool(self._anthropic_api_key),
                "openrouter": bool(self._openrouter_api_key),
            },
        }

    def set_callbacks(self, on_request: Optional[Callable] = None,
                      on_response: Optional[Callable] = None):
        """Set callback functions for request/response events."""
        self._on_request = on_request
        self._on_response = on_response

    def clear_cache(self):
        """Clear the response cache."""
        self._cache.clear()

    def reset_backoff(self, model_name: Optional[str] = None):
        """Reset backoff for a specific model or all models."""
        if model_name:
            self._backoff.pop(model_name, None)
            self._consecutive_failures.pop(model_name, None)
        else:
            self._backoff.clear()
            self._consecutive_failures.clear()
