#!/usr/bin/env python3
"""
jcode swarm spawner — launches parallel jcode workers, one per repo.
jcode serve (running on :7865) coordinates conflict resolution automatically.
"""
import argparse
import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path


async def spawn_worker(repo: str, task: str, idx: int) -> tuple[int, str]:
    """Spawn one jcode worker for a single repo. Returns (returncode, output)."""
    import tempfile
    import shutil
    workdir = tempfile.mkdtemp(prefix=f"swarm_{idx}_")

    try:
        # Clone the repo with --no-hardlinks to prevent inode sharing between workers.
        # Without this flag, local file:// clones share inodes and workers corrupt each other.
        clone = await asyncio.create_subprocess_exec(
            "git", "clone", "--depth", "1", "--no-hardlinks", repo, workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, clone_stderr = await clone.communicate()
        if clone.returncode != 0:
            return clone.returncode, f"git clone failed: {clone_stderr.decode()}"

        # Spawn jcode worker (connects to running jcode server at :7865)
        proc = await asyncio.create_subprocess_exec(
            "jcode", "run",
            "--message", f"{task}\nRepo: {repo}\nWorkdir: {workdir}",
            "--non-interactive",
            cwd=workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=1800)
        output = (stdout or b"").decode() + (stderr or b"").decode()
        return proc.returncode, output
    finally:
        # Always clean up the temporary workdir to prevent disk exhaustion
        shutil.rmtree(workdir, ignore_errors=True)


async def main(repos: list[str], task: str, max_workers: int):
    semaphore = asyncio.Semaphore(max_workers)

    async def bounded_worker(repo: str, idx: int):
        async with semaphore:
            print(f"[swarm] Worker {idx} starting: {repo}", flush=True)
            rc, out = await spawn_worker(repo, task, idx)
            print(f"[swarm] Worker {idx} done (rc={rc}): {repo}", flush=True)
            if out.strip():
                print(f"[swarm] Worker {idx} output:\n{out[-500:]}", flush=True)
            return rc, repo

    tasks = [bounded_worker(repo, i) for i, repo in enumerate(repos)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    success = sum(1 for r in results if not isinstance(r, Exception) and r[0] == 0)
    print(f"\n[swarm] Complete: {success}/{len(repos)} workers succeeded")
    return 0 if success > 0 else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="jcode swarm spawner")
    parser.add_argument("--repos",   required=True, help="JSON file with list of repo URLs")
    parser.add_argument("--task",    required=True)
    parser.add_argument("--workers", type=int, default=5)
    args = parser.parse_args()
    repos = json.loads(Path(args.repos).read_text())
    sys.exit(asyncio.run(main(repos, args.task, args.workers)))
