#!/usr/bin/env python3
"""
rate_limit_proxy.py — Rhodawk AI Model-Switching Fallback Proxy v2.0

Sits between hermes-agent and DO Inference on localhost:11434.
hermes-agent sends ALL calls here with model=deepseek-v4-pro + DO_KEY_DEEPSEEK.

This proxy:
  1. Enforces per-model sliding-window rate cap (RPM_CAP per key)
  2. On persistent 429 from deepseek: rewrites request to kimi-k2.6 + DO_KEY_KIMI
  3. On persistent 429 from kimi: rewrites to qwen3.5-397b-a17b + DO_KEY_QWEN
  4. Returns the winning model's response to hermes-agent transparently
  5. hermes-agent never sees a 429 and never needs to know about model switching

Stdlib only. No external dependencies.
"""

import http.server
import http.client
import json
import os
import random
import ssl
import threading
import time
import urllib.parse
from collections import deque

# ── Config ────────────────────────────────────────────────────────────────────

PROXY_PORT    = int(os.environ.get("PROXY_PORT", "11434"))
UPSTREAM_HOST = "inference.do-ai.run"
UPSTREAM_PORT = 443
RPM_CAP       = int(os.environ.get("PROXY_RPM_CAP", "270"))
MAX_RETRIES   = int(os.environ.get("PROXY_MAX_RETRIES", "5"))
BASE_DELAY    = float(os.environ.get("PROXY_BASE_DELAY", "10.0"))
MAX_DELAY     = float(os.environ.get("PROXY_MAX_DELAY", "120.0"))
JITTER_PCT    = float(os.environ.get("PROXY_JITTER_PCT", "0.2"))

# ── Model chain — proxy owns this, hermes-agent knows nothing about it ────────
# Each entry: (model_name, api_key_env_var)
# The proxy rewrites the request body and Authorization header as it walks the chain.
_MODEL_CHAIN = [
    {
        "model":   os.environ.get("PROXY_MODEL_PRIMARY", "deepseek-v4-pro"),
        "api_key": os.environ.get("DO_KEY_DEEPSEEK", os.environ.get("DO_INFERENCE_API_KEY", "")),
        "label":   "deepseek-v4-pro (primary)",
    },
    {
        "model":   os.environ.get("PROXY_MODEL_FALLBACK1", "kimi-k2.6"),
        # FIX-BUG1: NO fallback to DO_INFERENCE_API_KEY — using deepseek key against kimi
        # endpoint returns 403. An empty key here triggers the skip path below correctly.
        "api_key": os.environ.get("DO_KEY_KIMI", ""),
        "label":   "kimi-k2.6 (fallback-1)",
    },
    {
        "model":   os.environ.get("PROXY_MODEL_FALLBACK2", "qwen3.5-397b-a17b"),
        # FIX-BUG1: NO fallback to DO_INFERENCE_API_KEY — same reason as kimi above.
        "api_key": os.environ.get("DO_KEY_QWEN", ""),
        "label":   "qwen3.5-397b-a17b (fallback-2)",
    },
]

# ── Per-model rate limiters ───────────────────────────────────────────────────

# FIX-BUG3: Daily request cap — deepseek-v4-pro has a 300 req/day hard limit.
# Using 290 to leave 10 requests as safety buffer.
RPD_CAP_DEEPSEEK = int(os.environ.get("PROXY_RPD_CAP_DEEPSEEK", "290"))


class RateLimiter:
    """Sliding-window RPM limiter with optional daily (RPD) quota guard."""

    def __init__(self, rpm_cap: int, rpd_cap: int = 0):
        self._rpm_cap = rpm_cap
        self._rpd_cap = rpd_cap          # 0 = no daily limit enforced
        self._calls: deque = deque()
        self._day_count = 0
        self._day_reset = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> float:
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                # Reset daily counter every 24 h (approximate — monotonic clock)
                if now - self._day_reset >= 86400:
                    self._day_count = 0
                    self._day_reset = now
                # Daily quota hard stop — raise so caller skips to next tier
                if self._rpd_cap and self._day_count >= self._rpd_cap:
                    raise RuntimeError(
                        f"Daily quota ({self._rpd_cap} req/day) exhausted — escalating to next tier"
                    )
                # Sliding-window RPM
                while self._calls and now - self._calls[0] >= 60.0:
                    self._calls.popleft()
                if len(self._calls) < self._rpm_cap:
                    self._calls.append(now)
                    self._day_count += 1
                    return waited
                sleep_for = 60.0 - (now - self._calls[0]) + 0.05
            print(f"[proxy] RPM cap ({self._rpm_cap}/min) reached — waiting {sleep_for:.1f}s", flush=True)
            time.sleep(sleep_for)
            waited += sleep_for

    @property
    def current_rpm(self) -> int:
        with self._lock:
            now = time.monotonic()
            return sum(1 for t in self._calls if now - t < 60.0)

    @property
    def day_count(self) -> int:
        with self._lock:
            return self._day_count


# One limiter per model — independent quotas
# deepseek gets an RPD cap because it has a 300 req/day hard limit on DO Inference
_LIMITERS = {
    entry["model"]: RateLimiter(
        rpm_cap=RPM_CAP,
        rpd_cap=RPD_CAP_DEEPSEEK if "deepseek-v4-pro" in entry["model"] else 0,
    )
    for entry in _MODEL_CHAIN
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _jitter(delay: float) -> float:
    spread = delay * JITTER_PCT
    return delay + random.uniform(-spread, spread)

def _backoff_delay(attempt: int) -> float:
    return _jitter(min(BASE_DELAY * (2 ** attempt), MAX_DELAY))

def _make_conn():
    ctx = ssl.create_default_context()
    return http.client.HTTPSConnection(UPSTREAM_HOST, UPSTREAM_PORT, context=ctx, timeout=300)

def _telegram_notify(text: str) -> None:
    """Fire-and-forget Telegram message for model switch events."""
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        import urllib.request
        payload = json.dumps({
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=6)
    except Exception:
        pass  # never block on notification failure

# ── Core: single request to upstream with a specific model + key ──────────────

def _forward_once(path: str, method: str, original_headers: dict,
                  body_dict: dict, model_entry: dict) -> tuple:
    """
    Forward one request using model_entry's model name and api_key.
    Returns (status_code, response_headers, response_body_bytes).
    Raises on network failure.
    """
    patched_body = dict(body_dict)
    patched_body["model"] = model_entry["model"]
    body_bytes = json.dumps(patched_body).encode()

    fwd_headers = {}
    skip = {"connection", "transfer-encoding", "te", "trailers",
            "upgrade", "proxy-authorization", "keep-alive", "host",
            "authorization", "content-length"}
    for k, v in original_headers.items():
        if k.lower() not in skip:
            fwd_headers[k] = v
    fwd_headers["Host"]           = UPSTREAM_HOST
    fwd_headers["Authorization"]  = f"Bearer {model_entry['api_key']}"
    fwd_headers["Content-Length"] = str(len(body_bytes))

    conn = _make_conn()
    conn.request(method, path, body=body_bytes, headers=fwd_headers)
    resp = conn.getresponse()
    resp_body    = resp.read()
    resp_headers = dict(resp.getheaders())
    conn.close()
    return resp.status, resp_headers, resp_body


# ── Request handler ───────────────────────────────────────────────────────────

class ModelSwitchingProxyHandler(http.server.BaseHTTPRequestHandler):
    server_version   = "RhodawkProxy/2.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def _send_response(self, status: int, headers: dict, body: bytes):
        self.send_response(status)
        skip = {"transfer-encoding", "connection"}
        for k, v in headers.items():
            if k.lower() not in skip:
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def _is_completion_call(self) -> bool:
        return "/chat/completions" in self.path or (
            "/completions" in self.path and "/chat/" not in self.path
        )

    def do_GET(self):     self._handle()
    def do_POST(self):    self._handle()
    def do_OPTIONS(self): self._handle()

    def _handle(self):
        raw_body = self._read_body()

        if self.path == "/proxy/health":
            self._serve_health()
            return

        if not self._is_completion_call():
            self._passthrough(raw_body)
            return

        try:
            body_dict = json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError:
            self._send_response(400, {}, b'{"error":"invalid JSON body"}')
            return

        original_model = body_dict.get("model", "unknown")

        last_status   = 429
        last_headers  = {}
        last_body     = b""
        switched_from = None

        for chain_idx, model_entry in enumerate(_MODEL_CHAIN):
            label   = model_entry["label"]
            limiter = _LIMITERS[model_entry["model"]]

            if not model_entry["api_key"]:
                print(f"[proxy] Skipping {label} — no API key configured", flush=True)
                continue

            if chain_idx > 0:
                msg = (
                    f"⚠️ <b>Model switched</b>\n"
                    f"<b>From:</b> {switched_from}\n"
                    f"<b>To:</b>   {label}\n"
                    f"Task continues with full context."
                )
                threading.Thread(target=_telegram_notify, args=(msg,), daemon=True).start()
                print(f"[proxy] ⟳ Switched to {label}", flush=True)

            for attempt in range(MAX_RETRIES):
                try:
                    limiter.acquire()
                except RuntimeError as quota_err:
                    # Daily quota exhausted — skip entire tier immediately
                    print(f"[proxy] {label}: {quota_err}", flush=True)
                    switched_from = label
                    break

                try:
                    status, resp_headers, resp_body = _forward_once(
                        path=self.path,
                        method=self.command,
                        original_headers=self.headers,
                        body_dict=body_dict,
                        model_entry=model_entry,
                    )
                except Exception as exc:
                    delay = _backoff_delay(attempt)
                    print(
                        f"[proxy] {label}: connection error (attempt {attempt+1}/{MAX_RETRIES}) "
                        f"— retry in {delay:.1f}s: {exc}",
                        flush=True,
                    )
                    time.sleep(delay)
                    continue

                if status == 200:
                    print(
                        f"[proxy] ← 200 | {label} | "
                        f"RPM={limiter.current_rpm}/{RPM_CAP} | "
                        f"{len(resp_body)}b",
                        flush=True,
                    )
                    self._send_response(200, resp_headers, resp_body)
                    return

                if status in (429, 503, 500):
                    last_status  = status
                    last_headers = resp_headers
                    last_body    = resp_body
                    if attempt < MAX_RETRIES - 1:
                        delay = _backoff_delay(attempt)
                        print(
                            f"[proxy] {label}: HTTP {status} "
                            f"(attempt {attempt+1}/{MAX_RETRIES}) — "
                            f"retry in {delay:.1f}s",
                            flush=True,
                        )
                        time.sleep(delay)
                        continue
                    print(
                        f"[proxy] {label}: {MAX_RETRIES} retries exhausted "
                        f"(HTTP {status}) — escalating to next tier",
                        flush=True,
                    )
                    switched_from = label
                    break

                # Non-retryable (4xx other than 429)
                print(f"[proxy] {label}: HTTP {status} — passing through", flush=True)
                self._send_response(status, resp_headers, resp_body)
                return

        # All tiers exhausted
        print(
            f"[proxy] All {len(_MODEL_CHAIN)} model tiers exhausted — "
            f"returning 503. original_model={original_model}",
            flush=True,
        )
        threading.Thread(
            target=_telegram_notify,
            args=(
                "❌ <b>All model tiers exhausted</b>\n"
                "deepseek → kimi → qwen all returned 429.\n"
                "Request a quota increase at cloud.digitalocean.com/gen-ai",
            ),
            daemon=True,
        ).start()
        error_body = json.dumps({
            "error": {
                "message": (
                    f"All {len(_MODEL_CHAIN)} model tiers exhausted after "
                    f"{MAX_RETRIES} retries each. "
                    "Quota increase needed at cloud.digitalocean.com/gen-ai"
                ),
                "type": "all_tiers_exhausted",
                "code": 503,
            }
        }).encode()
        self._send_response(503, {"Content-Type": "application/json"}, error_body)

    def _passthrough(self, body: bytes):
        """Pass non-completion requests through with primary model's key."""
        primary = _MODEL_CHAIN[0]
        fwd_headers = {}
        skip = {"connection", "transfer-encoding", "host", "authorization", "content-length"}
        for k, v in self.headers.items():
            if k.lower() not in skip:
                fwd_headers[k] = v
        fwd_headers["Host"]          = UPSTREAM_HOST
        fwd_headers["Authorization"] = f"Bearer {primary['api_key']}"
        if body:
            fwd_headers["Content-Length"] = str(len(body))
        try:
            conn = _make_conn()
            conn.request(self.command, self.path, body=body, headers=fwd_headers)
            resp = conn.getresponse()
            resp_body    = resp.read()
            resp_headers = dict(resp.getheaders())
            conn.close()
            self._send_response(resp.status, resp_headers, resp_body)
        except Exception as exc:
            self._send_response(502, {}, json.dumps({"error": str(exc)}).encode())

    def _serve_health(self):
        body = json.dumps({
            "status": "ok",
            "proxy_version": "2.0",
            "model_chain": [
                {
                    "label":     e["label"],
                    "model":     e["model"],
                    "rpm":       _LIMITERS[e["model"]].current_rpm,
                    "rpm_cap":   RPM_CAP,
                    "rpd":       _LIMITERS[e["model"]].day_count,
                    "rpd_cap":   RPD_CAP_DEEPSEEK if "deepseek-v4-pro" in e["model"] else 0,
                    "key_set":   bool(e["api_key"]),
                }
                for e in _MODEL_CHAIN
            ],
            "upstream": f"https://{UPSTREAM_HOST}",
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ThreadedHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads      = True
    allow_reuse_address = True


def main():
    print(f"\n  Rhodawk AI — Model-Switching Fallback Proxy v2.0", flush=True)
    print(f"  Listening : localhost:{PROXY_PORT}", flush=True)
    print(f"  Upstream  : https://{UPSTREAM_HOST}", flush=True)
    print(f"  RPM cap   : {RPM_CAP}/min per model key", flush=True)
    print(f"  Max retry : {MAX_RETRIES} per tier", flush=True)
    print(f"  Chain     :", flush=True)
    for i, e in enumerate(_MODEL_CHAIN):
        key_status = "SET" if e["api_key"] else "MISSING — will skip"
        print(f"    Tier {i+1}  {e['label']} — key {key_status}", flush=True)
    print(flush=True)

    for e in _MODEL_CHAIN:
        if not e["api_key"]:
            print(
                f"[proxy] WARNING: {e['label']} has no API key. "
                f"Set DO_KEY_DEEPSEEK / DO_KEY_KIMI / DO_KEY_QWEN in supervisord environment.",
                flush=True,
            )

    server = ThreadedHTTPServer(("127.0.0.1", PROXY_PORT), ModelSwitchingProxyHandler)
    print(f"[proxy] Ready on http://127.0.0.1:{PROXY_PORT}/v1", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[proxy] Shutting down.", flush=True)
        server.shutdown()


if __name__ == "__main__":
    main()
