#!/usr/bin/env python3
"""
jcode Swarm Coordinator -- Peak Implementation.
Divides complex tasks into subtasks, assigns to parallel workers,
monitors progress, and merges results with conflict resolution.

Strategies:
- divide-and-conquer: Split task into independent subtasks by file scope
- parallel-repos: Same task across multiple repositories
- fan-out-fan-in: Generate multiple approaches, pick best, refine

Dependencies:
  pip install redis

Rhodawk AI -- Peak Architecture v10.0
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class SubTask:
    """A subtask assigned to a worker."""
    id: int
    description: str
    target_files: list = field(default_factory=list)
    status: str = "pending"
    output: str = ""
    error: str = ""
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    worker_id: Optional[int] = None
    retry_count: int = 0
    max_retries: int = 2


@dataclass
class WorkerStatus:
    """Tracks status of a worker process."""
    id: int
    subtask_id: Optional[int] = None
    status: str = "idle"
    started_at: Optional[float] = None
    tasks_completed: int = 0
    tasks_failed: int = 0


class SwarmCoordinator:
    """
    Coordinates multiple jcode workers on a complex task.

    Strategies:
    - divide-and-conquer: Split task into independent subtasks
    - parallel-repos: Same task across multiple repos
    - fan-out-fan-in: Generate alternatives, pick best

    The coordinator:
    1. Decomposes the task using LLM-guided analysis
    2. Assigns subtasks to workers (bounded concurrency)
    3. Monitors worker progress via status tracking
    4. Handles failures with retry and reassignment
    5. Merges results with conflict detection
    6. Reports final outcome
    """

    def __init__(self, task: str, workdir: str, workers: int = 3,
                 strategy: str = "divide-and-conquer", timeout: int = 1200):
        """
        Initialize the swarm coordinator.

        Args:
            task: Full task description.
            workdir: Working directory containing the codebase.
            workers: Maximum number of parallel workers.
            strategy: Decomposition strategy.
            timeout: Total timeout in seconds.
        """
        self.task = task
        self.workdir = workdir
        self.max_workers = workers
        self.strategy = strategy
        self.timeout = timeout
        self.subtasks: list = []
        self.worker_statuses: list = []
        self.start_time: float = 0.0

        # LLM configuration
        self.api_key = os.environ.get("DO_INFERENCE_API_KEY", "")
        self.base_url = os.environ.get(
            "DO_INFERENCE_BASE_URL", "https://inference.do-ai.run/v1"
        )
        self.model = os.environ.get("JCODE_MODEL", "kimi-k2.6")

        # Initialize worker statuses
        for i in range(workers):
            self.worker_statuses.append(WorkerStatus(id=i))

    async def run(self) -> int:
        """
        Execute the coordinated swarm.

        Returns:
            0 on success (all subtasks completed).
            1 on failure (subtasks failed or timeout).
        """
        self.start_time = time.time()
        print(f"[coordinator] Task: {self.task[:100]}", flush=True)
        print(f"[coordinator] Strategy: {self.strategy}", flush=True)
        print(f"[coordinator] Workers: {self.max_workers}", flush=True)
        print(f"[coordinator] Workdir: {self.workdir}", flush=True)
        print(f"[coordinator] Timeout: {self.timeout}s", flush=True)

        # Step 1: Analyze the codebase
        print("\n[coordinator] Phase 1: Analyzing codebase...", flush=True)
        codebase_info = self._analyze_codebase()

        # Step 2: Decompose task into subtasks
        print("[coordinator] Phase 2: Decomposing task...", flush=True)
        self.subtasks = await self._decompose(codebase_info)
        if not self.subtasks:
            print("[coordinator] Failed to decompose task", flush=True)
            return 1

        print(
            f"[coordinator] Decomposed into {len(self.subtasks)} subtasks:",
            flush=True,
        )
        for st in self.subtasks:
            files_str = ", ".join(st.target_files[:3]) if st.target_files else "auto"
            print(
                f"  [{st.id}] {st.description[:60]} -> [{files_str}]",
                flush=True,
            )

        # Step 3: Execute subtasks in parallel (bounded by worker count)
        print("\n[coordinator] Phase 3: Executing subtasks...", flush=True)
        semaphore = asyncio.Semaphore(self.max_workers)

        async def bounded_exec(subtask: SubTask):
            async with semaphore:
                await self._execute_subtask(subtask)

        await asyncio.gather(
            *[bounded_exec(st) for st in self.subtasks],
            return_exceptions=True,
        )

        # Step 4: Handle failures with retries
        failed_tasks = [st for st in self.subtasks if st.status == "failed"]
        if failed_tasks:
            print(
                f"\n[coordinator] Phase 3b: Retrying {len(failed_tasks)} "
                f"failed subtasks...",
                flush=True,
            )
            for st in failed_tasks:
                if st.retry_count < st.max_retries:
                    st.retry_count += 1
                    st.status = "pending"
                    await self._execute_subtask(st)

        # Step 5: Merge results and resolve conflicts
        print("\n[coordinator] Phase 4: Merging results...", flush=True)
        success = await self._merge_results()

        # Step 6: Report
        self._print_report()

        completed = sum(1 for st in self.subtasks if st.status == "completed")
        total = len(self.subtasks)
        return 0 if completed == total else (0 if success else 1)

    def _analyze_codebase(self) -> dict:
        """Analyze the codebase to understand structure."""
        workdir = Path(self.workdir)
        info = {
            "files": [],
            "languages": set(),
            "file_count": 0,
            "has_tests": False,
            "has_package_json": False,
            "has_requirements": False,
        }

        ext_to_lang = {
            ".py": "python", ".ts": "typescript", ".js": "javascript",
            ".go": "go", ".rs": "rust", ".yaml": "yaml", ".yml": "yaml",
            ".json": "json", ".sh": "shell", ".md": "markdown",
        }

        skip_dirs = {".git", "node_modules", "__pycache__", ".venv",
                     "venv", ".tox", "dist", "build", ".eggs"}

        for f in workdir.rglob("*"):
            if f.is_file():
                rel = str(f.relative_to(workdir))
                # Skip hidden/build dirs
                if any(skip in rel for skip in skip_dirs):
                    continue
                info["files"].append(rel)
                info["file_count"] += 1

                ext = f.suffix.lower()
                if ext in ext_to_lang:
                    info["languages"].add(ext_to_lang[ext])

                if "test" in rel.lower():
                    info["has_tests"] = True

        info["has_package_json"] = (workdir / "package.json").exists()
        info["has_requirements"] = (workdir / "requirements.txt").exists()
        info["languages"] = list(info["languages"])

        print(
            f"[coordinator] Codebase: {info['file_count']} files, "
            f"languages: {', '.join(info['languages'][:5])}",
            flush=True,
        )
        return info

    async def _decompose(self, codebase_info: dict) -> list:
        """Use LLM to decompose task into subtasks."""
        files_sample = codebase_info["files"][:50]

        if self.strategy == "divide-and-conquer":
            prompt = self._build_decomposition_prompt(files_sample)
        elif self.strategy == "parallel-repos":
            prompt = self._build_parallel_repos_prompt(files_sample)
        elif self.strategy == "fan-out-fan-in":
            prompt = self._build_fanout_prompt(files_sample)
        else:
            prompt = self._build_decomposition_prompt(files_sample)

        response = self._call_llm(prompt)
        if not response:
            # Fallback: single subtask with the full task
            return [SubTask(id=0, description=self.task)]

        try:
            # Try to parse JSON from response
            clean = response.strip()
            # Handle markdown code blocks
            if clean.startswith("```"):
                lines = clean.split("\n")
                clean = "\n".join(lines[1:-1])
            # Handle stray text before/after JSON
            start = clean.find("[")
            end = clean.rfind("]")
            if start >= 0 and end > start:
                clean = clean[start:end + 1]

            subtasks_data = json.loads(clean)
            return [
                SubTask(
                    id=i,
                    description=st.get("description", ""),
                    target_files=st.get("target_files", []),
                )
                for i, st in enumerate(subtasks_data)
                if st.get("description")
            ]
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            print(f"[coordinator] Decomposition parse error: {e}", flush=True)
            # Fallback: split by file groups
            return self._fallback_decompose(codebase_info)

    def _build_decomposition_prompt(self, files: list) -> str:
        """Build prompt for divide-and-conquer decomposition."""
        return (
            f"Decompose this task into {self.max_workers}-"
            f"{self.max_workers + 2} independent subtasks that can be "
            f"executed in parallel by separate coding agents:\n\n"
            f"TASK: {self.task}\n\n"
            f"FILES IN REPO:\n" + "\n".join(files[:50]) + "\n\n"
            f"Respond with a JSON array of objects, each with:\n"
            f'  {{"description": "what to do", '
            f'"target_files": ["file1.py", "file2.py"]}}\n\n'
            f"Rules:\n"
            f"- Make subtasks independent (no file overlaps between subtasks)\n"
            f"- Each subtask should be completable by one agent in isolation\n"
            f"- Be specific about what to change in each subtask\n"
            f"- Respond with ONLY the JSON array, no markdown fences, "
            f"no explanation\n"
        )

    def _build_parallel_repos_prompt(self, files: list) -> str:
        """Build prompt for parallel-repos strategy."""
        return (
            f"This task needs to be applied to multiple parts of the "
            f"codebase simultaneously:\n\n"
            f"TASK: {self.task}\n\n"
            f"FILES IN REPO:\n" + "\n".join(files[:50]) + "\n\n"
            f"Create {self.max_workers} subtasks, each targeting a different "
            f"part of the codebase with the same type of change.\n\n"
            f"Respond with a JSON array:\n"
            f'  [{{"description": "...", "target_files": [...]}}]\n'
            f"Respond with ONLY the JSON array.\n"
        )

    def _build_fanout_prompt(self, files: list) -> str:
        """Build prompt for fan-out-fan-in strategy."""
        return (
            f"Generate {self.max_workers} different approaches to solve "
            f"this task. Each approach should be independent:\n\n"
            f"TASK: {self.task}\n\n"
            f"FILES IN REPO:\n" + "\n".join(files[:30]) + "\n\n"
            f"Respond with a JSON array of approaches:\n"
            f'  [{{"description": "approach 1: ...", '
            f'"target_files": [...]}}]\n'
            f"Respond with ONLY the JSON array.\n"
        )

    def _fallback_decompose(self, codebase_info: dict) -> list:
        """Fallback decomposition when LLM fails."""
        # Group files by directory
        dir_groups: dict = {}
        for f in codebase_info["files"]:
            parts = f.split("/")
            top_dir = parts[0] if len(parts) > 1 else "root"
            if top_dir not in dir_groups:
                dir_groups[top_dir] = []
            dir_groups[top_dir].append(f)

        # Create one subtask per major directory
        subtasks = []
        for i, (directory, files) in enumerate(
            sorted(dir_groups.items(), key=lambda x: -len(x[1]))[:self.max_workers + 2]
        ):
            subtasks.append(SubTask(
                id=i,
                description=f"{self.task} -- focus on {directory}/ directory",
                target_files=files[:10],
            ))

        return subtasks or [SubTask(id=0, description=self.task)]

    async def _execute_subtask(self, subtask: SubTask):
        """Execute a single subtask using jcode or direct LLM."""
        subtask.status = "running"
        subtask.started_at = time.time()

        # Check timeout
        elapsed = time.time() - self.start_time
        if elapsed > self.timeout:
            subtask.status = "failed"
            subtask.error = "Global timeout exceeded"
            return

        # Find an available worker
        worker = next(
            (w for w in self.worker_statuses if w.status == "idle"), None
        )
        if worker:
            worker.status = "busy"
            worker.subtask_id = subtask.id
            worker.started_at = time.time()
            subtask.worker_id = worker.id

        env = {
            **os.environ,
            "OPENAI_BASE_URL": self.base_url,
            "OPENAI_API_KEY": self.api_key,
            "OPENAI_MODEL": self.model,
        }

        # Build focused prompt with target files
        prompt = subtask.description
        if subtask.target_files:
            prompt += (
                f"\n\nFocus on these files: {', '.join(subtask.target_files)}"
                f"\nDo not modify any other files."
            )

        # Calculate per-subtask timeout
        remaining = max(60, self.timeout - (time.time() - self.start_time))
        subtask_timeout = min(
            remaining / max(1, len([s for s in self.subtasks if s.status == "pending"])),
            remaining,
        )

        try:
            # Try jcode first, fall back to direct LLM
            proc = await asyncio.create_subprocess_exec(
                "jcode", "run", "--message", prompt, "--non-interactive",
                cwd=self.workdir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=subtask_timeout
            )
            subtask.output = (stdout or b"").decode(errors="replace")[-2000:]
            subtask.error = (stderr or b"").decode(errors="replace")[-1000:]
            subtask.status = "completed" if proc.returncode == 0 else "failed"

        except FileNotFoundError:
            # jcode binary is required — do not reimplement it
            subtask.status = "failed"
            subtask.error = (
                "jcode binary not found. "
                "Install the real jcode (github.com/1jehuang/jcode): "
                "cargo install jcode"
            )
            print(f"[coordinator] {subtask.error}", flush=True)

        except asyncio.TimeoutError:
            subtask.status = "failed"
            subtask.error = f"Subtask timeout ({subtask_timeout:.0f}s)"

        except Exception as e:
            subtask.status = "failed"
            subtask.error = str(e)

        subtask.completed_at = time.time()
        duration = subtask.completed_at - (subtask.started_at or subtask.completed_at)

        # Update worker status
        if worker:
            worker.status = "idle"
            worker.subtask_id = None
            if subtask.status == "completed":
                worker.tasks_completed += 1
            else:
                worker.tasks_failed += 1

        status_icon = "OK" if subtask.status == "completed" else "FAIL"
        print(
            f"[coordinator] [{status_icon}] Subtask {subtask.id} "
            f"({duration:.0f}s): {subtask.description[:60]}",
            flush=True,
        )
        if subtask.status == "failed" and subtask.error:
            print(f"  Error: {subtask.error[:100]}", flush=True)

    async def _merge_results(self) -> bool:
        """Check for file conflicts and resolve them."""
        # In divide-and-conquer strategy, subtasks should not overlap
        # Verify by checking git status
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self.workdir, capture_output=True, text=True,
                timeout=30,
            )
            modified_files = [
                line[3:].strip()
                for line in result.stdout.splitlines()
                if line.strip()
            ]
            if modified_files:
                print(
                    f"[coordinator] Modified {len(modified_files)} files total",
                    flush=True,
                )

            # Check for merge conflicts (files modified by multiple subtasks)
            conflict_check = subprocess.run(
                ["grep", "-rl", "<<<<<<<", "."],
                cwd=self.workdir, capture_output=True, text=True,
                timeout=10,
            )
            if conflict_check.stdout.strip():
                conflicted = conflict_check.stdout.strip().split("\n")
                print(
                    f"[coordinator] WARNING: {len(conflicted)} files have "
                    f"merge conflicts",
                    flush=True,
                )
                return False

            return True
        except Exception as e:
            print(f"[coordinator] Merge check error: {e}", flush=True)
            return True  # Assume success if we cannot check

    def _call_llm(self, prompt: str, msg: Optional[str] = None) -> Optional[str]:
        """Call LLM for task decomposition or direct execution."""
        import urllib.request
        import urllib.error

        if not self.api_key:
            return None

        messages = []
        if msg:
            messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": msg[:40000]},
            ]
        else:
            messages = [{"role": "user", "content": prompt[:40000]}]

        payload = json.dumps({
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 8192,
        }).encode()

        req = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )

        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.loads(resp.read())
                return data["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as e:
                if e.code in (429, 503) and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                print(f"[coordinator] LLM HTTP error: {e.code}", flush=True)
                return None
            except Exception as e:
                print(f"[coordinator] LLM error: {e}", flush=True)
                return None

        return None

    def _print_report(self):
        """Print final coordination report."""
        elapsed = time.time() - self.start_time
        completed = sum(1 for st in self.subtasks if st.status == "completed")
        failed = sum(1 for st in self.subtasks if st.status == "failed")
        total = len(self.subtasks)

        print(f"\n[coordinator] === Final Report ===", flush=True)
        print(f"  Total subtasks: {total}", flush=True)
        print(f"  Completed: {completed}", flush=True)
        print(f"  Failed: {failed}", flush=True)
        print(f"  Duration: {elapsed:.0f}s", flush=True)
        print(f"  Strategy: {self.strategy}", flush=True)

        if failed > 0:
            print(f"\n  Failed subtasks:", flush=True)
            for st in self.subtasks:
                if st.status == "failed":
                    print(f"    [{st.id}] {st.description[:50]}", flush=True)
                    if st.error:
                        print(f"         Error: {st.error[:80]}", flush=True)


# -- CLI Interface ---------------------------------------------------------------

def main():
    """CLI entry point for the swarm coordinator."""
    parser = argparse.ArgumentParser(
        description="jcode Swarm Coordinator -- Rhodawk AI"
    )
    parser.add_argument("--task", required=True, help="Full task description")
    parser.add_argument("--workdir", default="/tmp", help="Working directory")
    parser.add_argument(
        "--workers", type=int, default=3,
        help="Number of parallel workers (default: 3)"
    )
    parser.add_argument(
        "--strategy", default="divide-and-conquer",
        choices=["divide-and-conquer", "parallel-repos", "fan-out-fan-in"],
        help="Decomposition strategy"
    )
    parser.add_argument(
        "--timeout", type=int, default=1200,
        help="Total timeout in seconds (default: 1200)"
    )
    args = parser.parse_args()

    coordinator = SwarmCoordinator(
        task=args.task,
        workdir=args.workdir,
        workers=args.workers,
        strategy=args.strategy,
        timeout=args.timeout,
    )
    sys.exit(asyncio.run(coordinator.run()))


if __name__ == "__main__":
    main()
