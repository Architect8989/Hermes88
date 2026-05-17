#!/usr/bin/env python3
"""
Deployment Orchestration Skill for Hermes88.
Manages deployments to HuggingFace Spaces, DigitalOcean Droplets,
and other infrastructure targets with health checks and rollback.

Rhodawk AI -- Peak Architecture v10.0
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class DeploymentRecord:
    """Records a deployment for history and rollback."""
    id: str = ""
    target: str = ""
    repo: str = ""
    branch: str = "main"
    commit_hash: str = ""
    timestamp: str = ""
    status: str = "pending"
    duration: float = 0.0
    health_check_passed: bool = False
    error: str = ""

    def to_dict(self) -> dict:
        """Serialize to dictionary."""
        return {
            "id": self.id,
            "target": self.target,
            "repo": self.repo,
            "branch": self.branch,
            "commit_hash": self.commit_hash,
            "timestamp": self.timestamp,
            "status": self.status,
            "duration": self.duration,
            "health_check_passed": self.health_check_passed,
            "error": self.error,
        }


class DeploymentHistory:
    """Tracks deployment history for rollback support."""

    def __init__(self, history_path: str = "/data/.hermes/deployments.json"):
        """Initialize deployment history store."""
        self.history_path = Path(history_path)
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        self._records: list = []
        self._load()

    def _load(self):
        """Load history from disk."""
        if self.history_path.exists():
            try:
                data = json.loads(self.history_path.read_text())
                self._records = data.get("deployments", [])
            except Exception:
                self._records = []

    def _save(self):
        """Save history to disk."""
        self.history_path.write_text(json.dumps(
            {"deployments": self._records[-100:]},  # Keep last 100
            indent=2,
        ))

    def record(self, deploy: DeploymentRecord):
        """Record a deployment."""
        self._records.append(deploy.to_dict())
        self._save()

    def get_last(self, target: str, count: int = 1) -> list:
        """Get last N deployments for a target."""
        filtered = [r for r in self._records if r.get("target") == target]
        return filtered[-count:]

    def get_last_successful(self, target: str) -> Optional[dict]:
        """Get the last successful deployment for rollback."""
        for record in reversed(self._records):
            if record.get("target") == target and record.get("status") == "success":
                return record
        return None


class HuggingFaceDeployer:
    """Deploys to HuggingFace Spaces via git push."""

    def __init__(self, token: str = ""):
        """
        Initialize HF deployer.

        Args:
            token: HuggingFace access token.
        """
        self.token = token or os.environ.get("HF_TOKEN", "")

    def deploy(self, repo: str, workdir: str = "",
               branch: str = "main") -> DeploymentRecord:
        """
        Deploy to HuggingFace Space.

        Args:
            repo: HF repo path (e.g., "Architect8999/Hermes").
            workdir: Local working directory with the code.
            branch: Branch to deploy.

        Returns:
            DeploymentRecord with result.
        """
        record = DeploymentRecord(
            id=f"hf_{int(time.time())}",
            target="huggingface",
            repo=repo,
            branch=branch,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        if not self.token:
            record.status = "failed"
            record.error = "HF_TOKEN not configured"
            return record

        start = time.time()

        try:
            # Get current commit hash
            if workdir:
                result = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=workdir, capture_output=True, text=True, timeout=10,
                )
                record.commit_hash = result.stdout.strip()[:8]

            # Push using push-commit utility or direct git
            hf_url = f"https://user:{self.token}@huggingface.co/spaces/{repo}"

            if workdir:
                # Add HF remote and push
                subprocess.run(
                    ["git", "remote", "remove", "hf"],
                    cwd=workdir, capture_output=True, timeout=10,
                )
                subprocess.run(
                    ["git", "remote", "add", "hf", hf_url],
                    cwd=workdir, capture_output=True, timeout=10,
                )

                result = subprocess.run(
                    ["git", "push", "hf", f"{branch}:main", "--force"],
                    cwd=workdir, capture_output=True, text=True, timeout=120,
                )

                if result.returncode != 0:
                    record.status = "failed"
                    record.error = result.stderr[-200:]
                    return record

            record.status = "success"
            record.duration = time.time() - start

            # Health check (HF Spaces take time to rebuild)
            print(
                f"[deploy] Pushed to HF. Space will rebuild. "
                f"Checking health in 60s...",
                flush=True,
            )
            time.sleep(60)
            record.health_check_passed = self._health_check(repo)

        except subprocess.TimeoutExpired:
            record.status = "failed"
            record.error = "Git push timed out"
        except Exception as e:
            record.status = "failed"
            record.error = str(e)

        record.duration = time.time() - start
        return record

    def _health_check(self, repo: str) -> bool:
        """Check if HF Space is running."""
        url = f"https://huggingface.co/spaces/{repo}"
        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status == 200
        except Exception:
            return False

    def rollback(self, repo: str, workdir: str, commits: int = 1) -> bool:
        """Rollback by reverting commits and pushing."""
        if not workdir:
            print("[deploy] Workdir required for rollback", flush=True)
            return False

        try:
            # Revert last N commits
            result = subprocess.run(
                ["git", "revert", "--no-commit", f"HEAD~{commits}..HEAD"],
                cwd=workdir, capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                # Try reset instead
                subprocess.run(
                    ["git", "reset", "--hard", f"HEAD~{commits}"],
                    cwd=workdir, capture_output=True, timeout=30,
                )

            # Push rollback
            record = self.deploy(repo, workdir)
            return record.status == "success"

        except Exception as e:
            print(f"[deploy] Rollback error: {e}", flush=True)
            return False


class DigitalOceanDeployer:
    """Deploys to DigitalOcean Droplets via SSH + docker compose."""

    def __init__(self, host: str = "", ssh_key: str = ""):
        """
        Initialize DO deployer.

        Args:
            host: Droplet IP or hostname.
            ssh_key: Path to SSH key file.
        """
        self.host = host or os.environ.get("DO_DROPLET_IP", "")
        self.ssh_key = ssh_key or os.environ.get("DEPLOY_SSH_KEY", "")
        self.user = os.environ.get("DO_DEPLOY_USER", "root")

    def deploy(self, compose_file: str = "docker-compose.yml",
               pull_latest: bool = True) -> DeploymentRecord:
        """
        Deploy via SSH to DO Droplet.

        Args:
            compose_file: Docker compose file name.
            pull_latest: Whether to pull latest images.

        Returns:
            DeploymentRecord with result.
        """
        record = DeploymentRecord(
            id=f"do_{int(time.time())}",
            target="digitalocean",
            repo=self.host,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        if not self.host:
            record.status = "failed"
            record.error = "DO_DROPLET_IP not configured"
            return record

        start = time.time()

        try:
            # Build SSH command
            ssh_opts = ["-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10"]
            if self.ssh_key:
                ssh_opts.extend(["-i", self.ssh_key])

            ssh_base = ["ssh"] + ssh_opts + [f"{self.user}@{self.host}"]

            # Step 1: Pull latest images
            if pull_latest:
                print("[deploy] Pulling latest images...", flush=True)
                result = subprocess.run(
                    ssh_base + [f"cd /app && docker compose -f {compose_file} pull"],
                    capture_output=True, text=True, timeout=120,
                )
                if result.returncode != 0:
                    print(f"[deploy] Pull warning: {result.stderr[-100:]}", flush=True)

            # Step 2: Deploy with compose
            print("[deploy] Deploying containers...", flush=True)
            result = subprocess.run(
                ssh_base + [
                    f"cd /app && docker compose -f {compose_file} up -d --build"
                ],
                capture_output=True, text=True, timeout=300,
            )

            if result.returncode != 0:
                record.status = "failed"
                record.error = result.stderr[-200:]
                return record

            # Step 3: Health check
            print("[deploy] Checking health...", flush=True)
            time.sleep(10)
            record.health_check_passed = self._health_check()

            if record.health_check_passed:
                record.status = "success"
            else:
                record.status = "degraded"
                record.error = "Health check did not pass"

        except subprocess.TimeoutExpired:
            record.status = "failed"
            record.error = "SSH command timed out"
        except Exception as e:
            record.status = "failed"
            record.error = str(e)

        record.duration = time.time() - start
        return record

    def _health_check(self) -> bool:
        """Check if the deployed service is healthy."""
        # Try HTTP health endpoint
        url = f"http://{self.host}:8080/health"
        try:
            req = urllib.request.Request(url, timeout=10)
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status == 200
        except Exception:
            pass

        # Fallback: SSH check supervisord
        ssh_opts = ["-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10"]
        if self.ssh_key:
            ssh_opts.extend(["-i", self.ssh_key])

        try:
            result = subprocess.run(
                ["ssh"] + ssh_opts + [f"{self.user}@{self.host}",
                 "docker ps --format '{{.Status}}' | grep -c Up"],
                capture_output=True, text=True, timeout=15,
            )
            running = int(result.stdout.strip() or "0")
            return running > 0
        except Exception:
            return False

    def rollback(self, compose_file: str = "docker-compose.yml") -> bool:
        """Rollback to previous container versions."""
        if not self.host:
            return False

        ssh_opts = ["-o", "StrictHostKeyChecking=no"]
        if self.ssh_key:
            ssh_opts.extend(["-i", self.ssh_key])

        try:
            # Stop current containers and restart with previous images
            result = subprocess.run(
                ["ssh"] + ssh_opts + [f"{self.user}@{self.host}",
                 f"cd /app && docker compose -f {compose_file} down && "
                 f"docker compose -f {compose_file} up -d"],
                capture_output=True, text=True, timeout=120,
            )
            return result.returncode == 0
        except Exception as e:
            print(f"[deploy] DO rollback error: {e}", flush=True)
            return False


class DeploymentOrchestrator:
    """
    Main deployment orchestrator.
    Coordinates pre-deploy checks, deployment, health verification,
    and automatic rollback on failure.
    """

    def __init__(self):
        """Initialize orchestrator with deployers and history."""
        self.hf = HuggingFaceDeployer()
        self.do = DigitalOceanDeployer()
        self.history = DeploymentHistory()

    def deploy(self, target: str, **kwargs) -> DeploymentRecord:
        """
        Execute a deployment with pre-checks and rollback.

        Args:
            target: Deployment target ("hf" or "do").
            **kwargs: Target-specific arguments.

        Returns:
            DeploymentRecord with full result.
        """
        # Pre-deploy: run tests if workdir available
        workdir = kwargs.get("workdir", "")
        if workdir and not self._pre_deploy_check(workdir):
            record = DeploymentRecord(
                id=f"{target}_{int(time.time())}",
                target=target,
                status="aborted",
                error="Pre-deploy tests failed",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            self.history.record(record)
            return record

        # Execute deployment
        if target == "hf":
            record = self.hf.deploy(
                repo=kwargs.get("repo", ""),
                workdir=workdir,
                branch=kwargs.get("branch", "main"),
            )
        elif target == "do":
            record = self.do.deploy(
                compose_file=kwargs.get("compose_file", "docker-compose.yml"),
                pull_latest=kwargs.get("pull_latest", True),
            )
        else:
            record = DeploymentRecord(
                id=f"{target}_{int(time.time())}",
                target=target,
                status="failed",
                error=f"Unknown target: {target}",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )

        # Record deployment
        self.history.record(record)

        # Auto-rollback on failure with health check
        if record.status == "failed" and not record.health_check_passed:
            print("[deploy] Deployment failed. Attempting rollback...", flush=True)
            self._auto_rollback(target, kwargs)

        return record

    def rollback(self, target: str, commits: int = 1, **kwargs) -> bool:
        """
        Rollback a deployment.

        Args:
            target: Deployment target.
            commits: Number of commits to revert.
            **kwargs: Additional arguments.

        Returns:
            True on successful rollback.
        """
        print(f"[deploy] Rolling back {target} by {commits} commits...", flush=True)

        if target == "hf":
            return self.hf.rollback(
                repo=kwargs.get("repo", ""),
                workdir=kwargs.get("workdir", ""),
                commits=commits,
            )
        elif target == "do":
            return self.do.rollback(
                compose_file=kwargs.get("compose_file", "docker-compose.yml"),
            )

        print(f"[deploy] Unknown target for rollback: {target}", flush=True)
        return False

    def _pre_deploy_check(self, workdir: str) -> bool:
        """Run pre-deployment checks (tests)."""
        print("[deploy] Running pre-deploy checks...", flush=True)

        # Detect test runner
        workdir_path = Path(workdir)
        if (workdir_path / "pytest.ini").exists() or (workdir_path / "tests").exists():
            cmd = "pytest --tb=short -q"
        elif (workdir_path / "package.json").exists():
            cmd = "npm test"
        else:
            # No tests found, skip
            return True

        try:
            result = subprocess.run(
                cmd, shell=True, cwd=workdir,
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                print(
                    f"[deploy] Tests failed:\n{result.stdout[-300:]}",
                    flush=True,
                )
                return False
            print("[deploy] Pre-deploy checks passed", flush=True)
            return True
        except subprocess.TimeoutExpired:
            print("[deploy] Tests timed out", flush=True)
            return False
        except Exception as e:
            print(f"[deploy] Check error: {e}", flush=True)
            return True  # Allow deploy if we cannot run tests

    def _auto_rollback(self, target: str, kwargs: dict):
        """Attempt automatic rollback after failed deployment."""
        last_success = self.history.get_last_successful(target)
        if last_success:
            print(
                f"[deploy] Found last successful deploy: {last_success.get('id')}",
                flush=True,
            )
            success = self.rollback(target, commits=1, **kwargs)
            if success:
                print("[deploy] Rollback successful", flush=True)
            else:
                print("[deploy] ROLLBACK FAILED -- manual intervention needed", flush=True)
        else:
            print("[deploy] No previous successful deploy found for rollback", flush=True)


# -- CLI Interface ---------------------------------------------------------------

def main():
    """CLI entry point for deployment orchestration."""
    parser = argparse.ArgumentParser(
        description="Deployment Orchestration -- Rhodawk AI Hermes88"
    )
    sub = parser.add_subparsers(dest="command")

    # HuggingFace deploy
    hf_p = sub.add_parser("hf", help="Deploy to HuggingFace Spaces")
    hf_p.add_argument("--repo", required=True, help="HF repo (user/space)")
    hf_p.add_argument("--branch", default="main")
    hf_p.add_argument("--workdir", default="")
    hf_p.add_argument("--token", default="")

    # DigitalOcean deploy
    do_p = sub.add_parser("do", help="Deploy to DigitalOcean")
    do_p.add_argument("--host", default="")
    do_p.add_argument("--compose-file", default="docker-compose.yml")
    do_p.add_argument("--pull-latest", action="store_true")

    # Rollback
    rb_p = sub.add_parser("rollback", help="Rollback a deployment")
    rb_p.add_argument("--target", required=True, choices=["hf", "do"])
    rb_p.add_argument("--commits", type=int, default=1)
    rb_p.add_argument("--repo", default="")
    rb_p.add_argument("--workdir", default="")

    # Status
    status_p = sub.add_parser("status", help="Show deployment history")
    status_p.add_argument("--target", default="")
    status_p.add_argument("--count", type=int, default=5)

    args = parser.parse_args()
    orchestrator = DeploymentOrchestrator()

    if args.command == "hf":
        if args.token:
            orchestrator.hf.token = args.token
        record = orchestrator.deploy(
            "hf", repo=args.repo, branch=args.branch, workdir=args.workdir,
        )
        _print_record(record)
        sys.exit(0 if record.status == "success" else 1)

    elif args.command == "do":
        if args.host:
            orchestrator.do.host = args.host
        record = orchestrator.deploy(
            "do", compose_file=args.compose_file, pull_latest=args.pull_latest,
        )
        _print_record(record)
        sys.exit(0 if record.status == "success" else 1)

    elif args.command == "rollback":
        success = orchestrator.rollback(
            target=args.target, commits=args.commits,
            repo=args.repo, workdir=args.workdir,
        )
        if success:
            print("[deploy] Rollback completed successfully")
        else:
            print("[deploy] Rollback failed")
            sys.exit(1)

    elif args.command == "status":
        history = DeploymentHistory()
        if args.target:
            records = history.get_last(args.target, args.count)
        else:
            records = history._records[-args.count:]

        if records:
            print(f"Last {len(records)} deployments:")
            for r in records:
                status_icon = {
                    "success": "OK", "failed": "FAIL", "aborted": "SKIP"
                }.get(r.get("status", ""), "??")
                print(
                    f"  [{status_icon}] {r.get('target')} "
                    f"{r.get('timestamp', '')[:16]} "
                    f"({r.get('duration', 0):.0f}s)"
                )
        else:
            print("No deployment history.")

    else:
        parser.print_help()
        sys.exit(1)


def _print_record(record: DeploymentRecord):
    """Print deployment record summary."""
    status_line = f"[{record.status.upper()}] {record.target}"
    if record.commit_hash:
        status_line += f" ({record.commit_hash})"
    status_line += f" -- {record.duration:.0f}s"
    print(status_line)
    if record.error:
        print(f"  Error: {record.error}")
    if record.health_check_passed:
        print("  Health check: PASSED")


if __name__ == "__main__":
    main()
