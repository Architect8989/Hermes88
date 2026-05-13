#!/usr/bin/env python3
"""
OpenClaude Agentic Client -- Peak Implementation.
Transforms the single-shot gRPC client into a true agentic coding loop
that iterates: plan -> edit -> verify -> self-correct.

This replaces the simple client.py for complex tasks that require
multiple iterations to achieve a verifiable goal.

Dependencies:
  pip install grpc protobuf

Rhodawk AI -- Peak Architecture v10.0
"""
import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Optional

sys.path.insert(0, "/app/skills/openclaude_grpc")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

try:
    import grpc
    GRPC_AVAILABLE = True
except ImportError:
    GRPC_AVAILABLE = False

try:
    if GRPC_AVAILABLE:
        import openclaude_pb2
        import openclaude_pb2_grpc
        PROTO_AVAILABLE = True
    else:
        PROTO_AVAILABLE = False
except ImportError:
    PROTO_AVAILABLE = False


class AgenticLoop:
    """
    Multi-iteration agentic coding loop.

    Each iteration:
    1. Assess current state (read files, run tests)
    2. Plan next action
    3. Execute action (edit files via gRPC or direct API)
    4. Verify result (run verify command)
    5. If verify fails and iterations remain: loop back to step 1
    6. If verify passes: report success

    The loop continues until:
    - Verification passes (success)
    - Max iterations exhausted (failure)
    - Timeout reached (failure)
    - LLM reports DONE (success without verification)
    """

    def __init__(self, task: str, workdir: str, model: str = "",
                 max_iterations: int = 10, verify_cmd: str = "",
                 timeout: int = 900):
        """
        Initialize the agentic loop.

        Args:
            task: Description of the coding task to complete.
            workdir: Working directory containing the code.
            model: LLM model to use (default: from env).
            max_iterations: Maximum number of plan/edit/verify cycles.
            verify_cmd: Shell command to verify success (e.g., pytest).
            timeout: Total timeout in seconds for the entire loop.
        """
        self.task = task
        self.workdir = workdir
        self.model = model or os.environ.get(
            "OPENCLAUDE_MODEL", "deepseek-r1-distill-llama-70b"
        )
        self.max_iterations = max_iterations
        self.verify_cmd = verify_cmd
        self.timeout = timeout
        self.iteration = 0
        self.history: list = []
        self.start_time = time.time()

        # API config
        self.api_key = os.environ.get("DO_INFERENCE_API_KEY", "")
        self.base_url = os.environ.get(
            "DO_INFERENCE_BASE_URL", "https://inference.do-ai.run/v1"
        )

        # File tracking
        self._files_modified: set = set()
        self._last_verify_output: str = ""

    def run(self) -> int:
        """
        Execute the agentic loop.

        Returns:
            0 on success (verification passed or LLM reported DONE).
            1 on failure (max iterations or timeout).
        """
        print(f"[agentic] Starting loop: {self.task}", flush=True)
        print(f"[agentic] Workdir: {self.workdir}", flush=True)
        print(f"[agentic] Model: {self.model}", flush=True)
        print(f"[agentic] Max iterations: {self.max_iterations}", flush=True)
        print(f"[agentic] Verify cmd: {self.verify_cmd or 'none'}", flush=True)
        print(f"[agentic] Timeout: {self.timeout}s", flush=True)

        while self.iteration < self.max_iterations:
            self.iteration += 1
            elapsed = time.time() - self.start_time

            if elapsed > self.timeout:
                print(
                    f"[agentic] Timeout ({self.timeout}s) reached "
                    f"after {self.iteration - 1} iterations",
                    flush=True,
                )
                return 1

            print(
                f"\n[agentic] === Iteration {self.iteration}/{self.max_iterations} "
                f"(elapsed: {elapsed:.0f}s) ===",
                flush=True,
            )

            # Step 1: Assess current state
            state = self._assess_state()

            # Step 2: Plan and execute
            success = self._plan_and_execute(state)
            if not success:
                print(
                    f"[agentic] Execution failed in iteration {self.iteration}",
                    flush=True,
                )
                continue

            # Step 3: Verify
            if self.verify_cmd:
                verified = self._verify()
                if verified:
                    elapsed = time.time() - self.start_time
                    print(
                        f"\n[agentic] SUCCESS after {self.iteration} iterations "
                        f"({elapsed:.0f}s)",
                        flush=True,
                    )
                    self._print_summary()
                    return 0
                else:
                    print(
                        f"[agentic] Verification failed, continuing...",
                        flush=True,
                    )
            else:
                # No verify command -- assume success after execution
                elapsed = time.time() - self.start_time
                print(
                    f"\n[agentic] Completed (no verify cmd) after "
                    f"{self.iteration} iterations ({elapsed:.0f}s)",
                    flush=True,
                )
                self._print_summary()
                return 0

        print(
            f"\n[agentic] FAILED: max iterations ({self.max_iterations}) exhausted",
            flush=True,
        )
        self._print_summary()
        return 1

    def _assess_state(self) -> dict:
        """Assess current state of the workdir."""
        state = {"files": [], "test_output": "", "errors": []}

        # List relevant files
        workdir = pathlib.Path(self.workdir)
        if not workdir.exists():
            state["errors"].append(f"Workdir does not exist: {self.workdir}")
            return state

        for ext in (".py", ".ts", ".js", ".yaml", ".yml", ".json", ".toml",
                    ".rs", ".go", ".sh", ".md"):
            for f in workdir.rglob(f"*{ext}"):
                rel = str(f.relative_to(workdir))
                if any(skip in rel for skip in [".git", "node_modules",
                                                "__pycache__", ".venv",
                                                "venv", ".tox"]):
                    continue
                state["files"].append(rel)

        # Run verify command to see current state (only after first iteration)
        if self.verify_cmd and self.iteration > 1:
            try:
                result = subprocess.run(
                    self.verify_cmd, shell=True, cwd=self.workdir,
                    capture_output=True, text=True, timeout=120,
                )
                state["test_output"] = (result.stdout + result.stderr)[-3000:]
                if result.returncode != 0:
                    state["errors"].append(
                        f"Verify command failed (rc={result.returncode})"
                    )
            except subprocess.TimeoutExpired:
                state["errors"].append("Verify command timed out")
            except Exception as e:
                state["errors"].append(f"Verify error: {e}")

        return state

    def _plan_and_execute(self, state: dict) -> bool:
        """Send task + state to LLM, get plan, execute edits."""
        # Build context from relevant files
        context_parts = []
        workdir = pathlib.Path(self.workdir)

        # Prioritize recently modified files and target files
        files_to_read = sorted(state["files"])[:15]
        if self._files_modified:
            # Put modified files first
            modified_first = [f for f in files_to_read if f in self._files_modified]
            others = [f for f in files_to_read if f not in self._files_modified]
            files_to_read = modified_first + others

        for fname in files_to_read[:15]:
            fpath = workdir / fname
            try:
                content = fpath.read_text(errors="replace")[:4000]
                context_parts.append(f"# FILE: {fname}\n{content}")
            except Exception:
                pass

        # Build the prompt
        system_prompt = (
            "You are an expert software engineer executing a coding task "
            "iteratively.\n"
            "You will receive:\n"
            "1. The task description\n"
            "2. Current file contents\n"
            "3. Any test/verification output from the previous iteration\n"
            "4. History of what you have already tried\n\n"
            "Your response MUST be ONLY file edits in this format:\n"
            "# FILE: relative/path/to/file.ext\n"
            "<complete file contents>\n\n"
            "Rules:\n"
            "- Write every changed file in FULL (not patches)\n"
            "- Do NOT include explanations or markdown fences\n"
            "- Do NOT write files that have not changed\n"
            "- If you believe the task is already complete, respond with "
            "exactly: DONE\n"
            "- Focus on making the verify command pass\n"
            "- If a previous attempt failed, analyze the error and try a "
            "different approach\n"
        )

        user_msg_parts = [f"TASK: {self.task}"]

        if state.get("test_output"):
            user_msg_parts.append(
                f"\nVERIFICATION OUTPUT (iteration {self.iteration - 1}):\n"
                f"{state['test_output']}"
            )

        if state.get("errors"):
            user_msg_parts.append(
                f"\nERRORS:\n" + "\n".join(state["errors"])
            )

        if self.history:
            history_summary = "\n".join(
                f"  Iteration {h['iteration']}: {h['action']}"
                for h in self.history[-5:]
            )
            user_msg_parts.append(f"\nHISTORY:\n{history_summary}")

        if context_parts:
            user_msg_parts.append(
                f"\nCONTEXT FILES:\n" + "\n\n".join(context_parts[:10])
            )

        user_msg = "\n".join(user_msg_parts)

        # Try gRPC first, then API fallback
        response = self._call_llm(system_prompt, user_msg)
        if not response:
            self.history.append({
                "iteration": self.iteration,
                "action": "LLM call failed (no response)",
            })
            return False

        if response.strip() == "DONE":
            self.history.append({
                "iteration": self.iteration,
                "action": "reported DONE",
            })
            return True

        # Parse and write files from response
        files_written = self._write_files(response)
        self.history.append({
            "iteration": self.iteration,
            "action": (
                f"wrote {len(files_written)} files: "
                f"{', '.join(files_written[:5])}"
            ),
        })

        return len(files_written) > 0

    def _call_llm(self, system: str, user: str) -> Optional[str]:
        """Call the LLM (gRPC or API fallback)."""
        # Try gRPC first
        if GRPC_AVAILABLE and PROTO_AVAILABLE:
            try:
                response = self._call_grpc(user)
                if response:
                    return response
            except Exception as e:
                print(
                    f"[agentic] gRPC failed: {e}, using API fallback",
                    flush=True,
                )

        # API fallback
        return self._call_api(system, user)

    def _call_grpc(self, prompt: str) -> Optional[str]:
        """Call openclaude via gRPC."""
        import uuid

        channel = grpc.insecure_channel("localhost:50051")

        # Check if server is available
        try:
            grpc.channel_ready_future(channel).result(timeout=5)
        except grpc.FutureTimeoutError:
            print("[agentic] gRPC server not ready", flush=True)
            return None

        stub = openclaude_pb2_grpc.AgentServiceStub(channel)

        def request_iter():
            req = openclaude_pb2.ChatRequest(
                message=prompt,
                working_directory=self.workdir,
                session_id=str(uuid.uuid4()),
            )
            if self.model:
                req.model = self.model
            yield openclaude_pb2.ClientMessage(request=req)

        output_parts = []
        try:
            for msg in stub.Chat(request_iter(), timeout=300):
                event = msg.WhichOneof("event")
                if event == "text_chunk":
                    output_parts.append(msg.text_chunk.text)
                elif event == "done":
                    break
                elif event == "error":
                    error_msg = getattr(msg.error, "message", "unknown error")
                    print(f"[agentic] gRPC error: {error_msg}", flush=True)
                    return None
        except grpc.RpcError as e:
            print(f"[agentic] gRPC RPC error: {e.code()}", flush=True)
            return None

        return "".join(output_parts) if output_parts else None

    def _call_api(self, system: str, user: str) -> Optional[str]:
        """Call LLM via DO Inference API (OpenAI-compatible)."""
        if not self.api_key:
            print("[agentic] No API key available", flush=True)
            return None

        # Truncate user message to fit within context limits
        max_user_len = 60000
        if len(user) > max_user_len:
            user = user[:max_user_len] + "\n\n[TRUNCATED]"

        payload = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.02,
            "max_tokens": 16384,
        }).encode()

        req = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        # Retry with exponential backoff
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=300) as resp:
                    data = json.loads(resp.read())
                return data["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as e:
                if e.code in (429, 503) and attempt < 2:
                    wait = (2 ** attempt) * 2
                    print(
                        f"[agentic] API {e.code}, retry in {wait}s",
                        flush=True,
                    )
                    time.sleep(wait)
                    continue
                print(f"[agentic] API HTTP error: {e.code}", flush=True)
                return None
            except Exception as e:
                print(f"[agentic] API error: {e}", flush=True)
                return None

        return None

    def _write_files(self, response: str) -> list:
        """Parse FILE: blocks from response and write to workdir."""
        file_pattern = re.compile(r"^#\s*FILE:\s*(.+)$")
        files_written = []
        current_file = None
        current_lines = []

        for line in response.split("\n"):
            m = file_pattern.match(line)
            if m:
                if current_file:
                    self._flush_file(current_file, current_lines)
                    files_written.append(current_file)
                current_file = m.group(1).strip()
                current_lines = []
            elif current_file is not None:
                current_lines.append(line)

        if current_file:
            self._flush_file(current_file, current_lines)
            files_written.append(current_file)

        self._files_modified.update(files_written)
        return files_written

    def _flush_file(self, path: str, lines: list):
        """Write a file to the workdir."""
        # Sanitize path to prevent directory traversal
        clean_path = path.lstrip("/").lstrip("./")
        if ".." in clean_path:
            print(f"[agentic] Rejected path with '..': {path}", flush=True)
            return

        fpath = pathlib.Path(self.workdir) / clean_path
        fpath.parent.mkdir(parents=True, exist_ok=True)

        # Strip trailing empty lines but preserve content
        content = "\n".join(lines).rstrip() + "\n"
        fpath.write_text(content)
        print(f"[agentic] Wrote: {fpath} ({len(lines)} lines)", flush=True)

    def _verify(self) -> bool:
        """Run verification command and check result."""
        if not self.verify_cmd:
            return True

        try:
            result = subprocess.run(
                self.verify_cmd, shell=True, cwd=self.workdir,
                capture_output=True, text=True, timeout=120,
            )
            output = (result.stdout + result.stderr)[-2000:]
            self._last_verify_output = output

            if result.returncode == 0:
                print("[agentic] Verify: PASSED", flush=True)
            else:
                print(f"[agentic] Verify: FAILED (rc={result.returncode})", flush=True)
                # Print relevant error lines
                error_lines = [
                    l for l in output.split("\n")
                    if any(kw in l.lower() for kw in ["error", "fail", "assert"])
                ]
                for line in error_lines[:5]:
                    print(f"  {line.strip()}", flush=True)

            return result.returncode == 0

        except subprocess.TimeoutExpired:
            print("[agentic] Verify command timed out", flush=True)
            self._last_verify_output = "TIMEOUT: verify command exceeded 120s"
            return False
        except Exception as e:
            print(f"[agentic] Verify error: {e}", flush=True)
            self._last_verify_output = f"ERROR: {e}"
            return False

    def _print_summary(self):
        """Print a summary of what was done."""
        elapsed = time.time() - self.start_time
        print(f"\n[agentic] --- Summary ---", flush=True)
        print(f"  Task: {self.task[:80]}", flush=True)
        print(f"  Iterations: {self.iteration}", flush=True)
        print(f"  Duration: {elapsed:.0f}s", flush=True)
        print(f"  Files modified: {len(self._files_modified)}", flush=True)
        for f in sorted(self._files_modified)[:10]:
            print(f"    - {f}", flush=True)
        if len(self._files_modified) > 10:
            print(
                f"    ... and {len(self._files_modified) - 10} more",
                flush=True,
            )


# -- CLI Interface ---------------------------------------------------------------

def main():
    """CLI entry point for the agentic client."""
    parser = argparse.ArgumentParser(
        description="OpenClaude Agentic Loop Client -- Rhodawk AI"
    )
    parser.add_argument("--task", required=True, help="Task description")
    parser.add_argument("--workdir", default="/tmp", help="Working directory")
    parser.add_argument("--model", default="", help="Model override")
    parser.add_argument(
        "--max-iterations", type=int, default=10,
        help="Maximum iterations (default: 10)"
    )
    parser.add_argument(
        "--verify-cmd", default="",
        help="Command to verify success (e.g., 'pytest tests/ -q')"
    )
    parser.add_argument(
        "--timeout", type=int, default=900,
        help="Total timeout in seconds (default: 900)"
    )
    args = parser.parse_args()

    loop = AgenticLoop(
        task=args.task,
        workdir=args.workdir,
        model=args.model,
        max_iterations=args.max_iterations,
        verify_cmd=args.verify_cmd,
        timeout=args.timeout,
    )
    sys.exit(loop.run())


if __name__ == "__main__":
    main()
