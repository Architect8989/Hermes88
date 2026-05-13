#!/usr/bin/env python3
"""
Rhodawk Core Proactive Intelligence Engine.

Periodic scanning of GitHub events, system health, scheduled tasks,
financial alerts, competitive intel, and RSS feeds. Generates
INTEL/CONTEXT/ACTION/URGENCY formatted proactive notifications.

Copyright (c) 2024-2025 Rhodawk AI. All rights reserved.
"""
import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Dict, List, Any, Callable


# -- Notification Models ---------------------------------------------------------


@dataclass
class ProactiveNotification:
    """A proactive intelligence notification."""
    id: str = ""
    category: str = "INTEL"  # INTEL, CONTEXT, ACTION, URGENCY
    title: str = ""
    body: str = ""
    source: str = ""
    priority: str = "normal"  # low, normal, high, critical
    timestamp: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)
    actionable: bool = False
    action_suggestion: str = ""

    def format(self) -> str:
        """Format notification for display."""
        prefix = f"[{self.category}]"
        if self.priority in ("high", "critical"):
            prefix = f"[{self.category}/{self.priority.upper()}]"
        parts = [f"{prefix} {self.title}"]
        if self.body:
            parts.append(self.body)
        if self.action_suggestion:
            parts.append(f"Suggested action: {self.action_suggestion}")
        return "\n".join(parts)

    def to_dict(self) -> dict:
        """Serialize to dictionary."""
        return {
            "id": self.id,
            "category": self.category,
            "title": self.title,
            "body": self.body,
            "source": self.source,
            "priority": self.priority,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
            "actionable": self.actionable,
            "action_suggestion": self.action_suggestion,
        }


# -- Scanner Base ----------------------------------------------------------------


class BaseScanner:
    """Base class for proactive intelligence scanners."""

    name: str = "base"
    interval_seconds: int = 300  # 5 minutes default

    def __init__(self):
        self.last_run: float = 0
        self.error_count: int = 0
        self.enabled: bool = True

    async def scan(self) -> List[ProactiveNotification]:
        """Execute a scan. Override in subclasses."""
        return []

    def should_run(self) -> bool:
        """Check if this scanner should run based on interval."""
        if not self.enabled:
            return False
        return (time.time() - self.last_run) >= self.interval_seconds


# -- GitHub Scanner --------------------------------------------------------------


class GitHubScanner(BaseScanner):
    """Scan GitHub for relevant events across monitored repositories."""

    name = "github"
    interval_seconds = 300  # 5 minutes

    def __init__(self, repos: Optional[List[str]] = None):
        super().__init__()
        self.github_pat = os.environ.get("GITHUB_PAT", "")
        self.repos = repos or ["Architect8989/Hermes88"]
        self._seen_events: set = set()
        self._max_seen = 200

    async def scan(self) -> List[ProactiveNotification]:
        """Scan GitHub events for monitored repos."""
        if not self.github_pat:
            return []

        notifications = []
        for repo in self.repos:
            try:
                events = self._fetch_events(repo)
                for event in events:
                    event_id = event.get("id", "")
                    if event_id in self._seen_events:
                        continue
                    self._seen_events.add(event_id)

                    notification = self._process_event(event, repo)
                    if notification:
                        notifications.append(notification)

                # Cap seen events set
                if len(self._seen_events) > self._max_seen:
                    self._seen_events = set(list(self._seen_events)[-self._max_seen:])

            except Exception as e:
                self.error_count += 1

        self.last_run = time.time()
        return notifications

    def _fetch_events(self, repo: str) -> List[dict]:
        """Fetch recent events from GitHub API."""
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/events?per_page=10",
            headers={
                "Authorization": f"Bearer {self.github_pat}",
                "Accept": "application/vnd.github+json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except Exception:
            return []

    def _process_event(self, event: dict, repo: str) -> Optional[ProactiveNotification]:
        """Process a GitHub event into a notification if relevant."""
        event_type = event.get("type", "")
        payload = event.get("payload", {})
        actor = event.get("actor", {}).get("login", "unknown")

        if event_type == "PushEvent":
            commits = payload.get("commits", [])
            branch = payload.get("ref", "").replace("refs/heads/", "")
            if branch == "main" and commits:
                return ProactiveNotification(
                    id=f"gh_{event.get('id', '')}",
                    category="CONTEXT",
                    title=f"Push to {repo}/{branch} by {actor}",
                    body=f"{len(commits)} commit(s). Latest: {commits[-1].get('message', '')[:80]}",
                    source="github",
                    priority="normal",
                    metadata={"repo": repo, "branch": branch, "commits": len(commits)},
                )

        elif event_type == "IssuesEvent":
            action = payload.get("action", "")
            issue = payload.get("issue", {})
            if action == "opened":
                return ProactiveNotification(
                    id=f"gh_{event.get('id', '')}",
                    category="INTEL",
                    title=f"New issue in {repo}: {issue.get('title', '')[:60]}",
                    body=f"#{issue.get('number', 0)} by {actor}",
                    source="github",
                    priority="normal",
                    actionable=True,
                    action_suggestion="Review and triage the issue",
                    metadata={"repo": repo, "issue_number": issue.get("number", 0)},
                )

        elif event_type == "PullRequestEvent":
            action = payload.get("action", "")
            pr = payload.get("pull_request", {})
            if action == "opened":
                return ProactiveNotification(
                    id=f"gh_{event.get('id', '')}",
                    category="CONTEXT",
                    title=f"New PR in {repo}: {pr.get('title', '')[:60]}",
                    body=f"#{pr.get('number', 0)} by {actor}",
                    source="github",
                    priority="normal",
                    metadata={"repo": repo, "pr_number": pr.get("number", 0)},
                )

        elif event_type == "ReleaseEvent":
            release = payload.get("release", {})
            return ProactiveNotification(
                id=f"gh_{event.get('id', '')}",
                category="INTEL",
                title=f"New release in {repo}: {release.get('tag_name', '')}",
                body=release.get("name", "")[:100],
                source="github",
                priority="normal",
                metadata={"repo": repo, "tag": release.get("tag_name", "")},
            )

        return None


# -- System Health Scanner -------------------------------------------------------


class SystemHealthScanner(BaseScanner):
    """Monitor system health: disk, memory, processes."""

    name = "system_health"
    interval_seconds = 60  # Every minute

    def __init__(self):
        super().__init__()
        self._last_disk_alert: float = 0
        self._last_memory_alert: float = 0

    async def scan(self) -> List[ProactiveNotification]:
        """Scan system health metrics."""
        notifications = []

        # Disk usage
        disk_notification = self._check_disk()
        if disk_notification:
            notifications.append(disk_notification)

        # Memory usage
        mem_notification = self._check_memory()
        if mem_notification:
            notifications.append(mem_notification)

        # Process health
        proc_notifications = self._check_processes()
        notifications.extend(proc_notifications)

        self.last_run = time.time()
        return notifications

    def _check_disk(self) -> Optional[ProactiveNotification]:
        """Check disk usage."""
        try:
            import shutil
            disk = shutil.disk_usage("/data")
            pct = (disk.used / disk.total) * 100
            free_gb = disk.free / (1024 ** 3)

            if pct > 90 and (time.time() - self._last_disk_alert) > 300:
                self._last_disk_alert = time.time()
                return ProactiveNotification(
                    id=f"disk_{int(time.time())}",
                    category="URGENCY",
                    title=f"Disk usage critical: {pct:.1f}%",
                    body=f"Only {free_gb:.1f}GB free",
                    source="system_health",
                    priority="critical",
                    actionable=True,
                    action_suggestion="Run cleanup: docker system prune, clear old logs",
                    metadata={"disk_percent": pct, "free_gb": free_gb},
                )
            elif pct > 80 and (time.time() - self._last_disk_alert) > 3600:
                self._last_disk_alert = time.time()
                return ProactiveNotification(
                    id=f"disk_{int(time.time())}",
                    category="ACTION",
                    title=f"Disk usage warning: {pct:.1f}%",
                    body=f"{free_gb:.1f}GB remaining",
                    source="system_health",
                    priority="high",
                    actionable=True,
                    action_suggestion="Consider cleanup soon",
                    metadata={"disk_percent": pct, "free_gb": free_gb},
                )
        except Exception:
            pass
        return None

    def _check_memory(self) -> Optional[ProactiveNotification]:
        """Check memory usage from /proc/meminfo."""
        try:
            with open("/proc/meminfo") as f:
                lines = f.read().splitlines()
            meminfo = {}
            for line in lines:
                if ":" in line:
                    key, val = line.split(":", 1)
                    meminfo[key.strip()] = val.strip()

            total_kb = int(meminfo.get("MemTotal", "0 kB").split()[0])
            available_kb = int(meminfo.get("MemAvailable", "0 kB").split()[0])

            if total_kb > 0:
                pct = ((total_kb - available_kb) / total_kb) * 100
                if pct > 90 and (time.time() - self._last_memory_alert) > 300:
                    self._last_memory_alert = time.time()
                    return ProactiveNotification(
                        id=f"mem_{int(time.time())}",
                        category="URGENCY",
                        title=f"Memory usage critical: {pct:.1f}%",
                        body=f"Available: {available_kb // 1024}MB",
                        source="system_health",
                        priority="critical",
                        actionable=True,
                        action_suggestion="Restart heavy services or add swap",
                        metadata={"memory_percent": pct},
                    )
        except Exception:
            pass
        return None

    def _check_processes(self) -> List[ProactiveNotification]:
        """Check process health via supervisorctl."""
        import subprocess
        notifications = []
        try:
            result = subprocess.run(
                ["supervisorctl", "status"],
                capture_output=True, text=True, timeout=10,
            )
            for line in result.stdout.splitlines():
                if "FATAL" in line or "EXITED" in line:
                    process_name = line.split()[0] if line.split() else "unknown"
                    notifications.append(ProactiveNotification(
                        id=f"proc_{process_name}_{int(time.time())}",
                        category="URGENCY",
                        title=f"Process crashed: {process_name}",
                        body=line.strip(),
                        source="system_health",
                        priority="high",
                        actionable=True,
                        action_suggestion=f"Restart with: supervisorctl restart {process_name}",
                        metadata={"process": process_name},
                    ))
        except Exception:
            pass
        return notifications


# -- Scheduled Task Scanner ------------------------------------------------------


class ScheduledTaskScanner(BaseScanner):
    """Monitor scheduled/cron tasks for missed executions."""

    name = "scheduled_tasks"
    interval_seconds = 600  # Every 10 minutes

    def __init__(self, cron_dir: str = "/app/hermes_config/cron"):
        super().__init__()
        self.cron_dir = cron_dir
        self._last_executions: Dict[str, float] = {}

    async def scan(self) -> List[ProactiveNotification]:
        """Check if any scheduled tasks are overdue."""
        from pathlib import Path
        notifications = []

        cron_path = Path(self.cron_dir)
        if not cron_path.exists():
            self.last_run = time.time()
            return []

        for yaml_file in cron_path.glob("*.yaml"):
            try:
                content = yaml_file.read_text()
                # Simple check: if a nightly task was not logged recently
                if "nightly" in yaml_file.name:
                    last_exec = self._last_executions.get(yaml_file.name, 0)
                    hours_since = (time.time() - last_exec) / 3600 if last_exec else 999
                    if hours_since > 26:  # More than 26 hours since last nightly
                        notifications.append(ProactiveNotification(
                            id=f"cron_{yaml_file.name}_{int(time.time())}",
                            category="ACTION",
                            title=f"Scheduled task may be overdue: {yaml_file.name}",
                            body=f"Last execution: {hours_since:.0f}h ago" if last_exec else "Never executed",
                            source="scheduled_tasks",
                            priority="normal",
                            actionable=True,
                            action_suggestion=f"Check and manually trigger if needed",
                            metadata={"task_file": yaml_file.name},
                        ))
            except Exception:
                pass

        self.last_run = time.time()
        return notifications


# -- Financial Scanner -----------------------------------------------------------


class FinancialScanner(BaseScanner):
    """Monitor financial alerts (Stripe, revenue metrics)."""

    name = "financial"
    interval_seconds = 3600  # Hourly

    def __init__(self):
        super().__init__()
        self.stripe_key = os.environ.get("STRIPE_SECRET_KEY", "")

    async def scan(self) -> List[ProactiveNotification]:
        """Check financial metrics and alert on anomalies."""
        notifications = []

        if not self.stripe_key:
            self.last_run = time.time()
            return []

        # Check for recent payment failures
        try:
            req = urllib.request.Request(
                "https://api.stripe.com/v1/events?type=invoice.payment_failed&limit=5",
                headers={"Authorization": f"Bearer {self.stripe_key}"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())

            events = data.get("data", [])
            recent_failures = [
                e for e in events
                if e.get("created", 0) > time.time() - 3600
            ]

            if recent_failures:
                notifications.append(ProactiveNotification(
                    id=f"fin_fail_{int(time.time())}",
                    category="URGENCY",
                    title=f"Payment failures detected: {len(recent_failures)} in last hour",
                    body="Check Stripe dashboard for details",
                    source="financial",
                    priority="critical",
                    actionable=True,
                    action_suggestion="Review failed payments in Stripe dashboard",
                    metadata={"failure_count": len(recent_failures)},
                ))
        except Exception:
            pass

        self.last_run = time.time()
        return notifications


# -- Competitive Intel Scanner ---------------------------------------------------


class CompetitiveIntelScanner(BaseScanner):
    """Monitor competitive intelligence sources."""

    name = "competitive_intel"
    interval_seconds = 7200  # Every 2 hours

    def __init__(self, targets_file: str = "/app/data/target_list.json"):
        super().__init__()
        self.targets_file = targets_file
        self._targets: List[dict] = []
        self._load_targets()

    def _load_targets(self):
        """Load target list from JSON file."""
        from pathlib import Path
        try:
            path = Path(self.targets_file)
            if path.exists():
                self._targets = json.loads(path.read_text())
        except Exception:
            self._targets = []

    async def scan(self) -> List[ProactiveNotification]:
        """Scan competitive intelligence targets."""
        notifications = []

        github_pat = os.environ.get("GITHUB_PAT", "")
        if not github_pat or not self._targets:
            self.last_run = time.time()
            return []

        for target in self._targets[:5]:  # Limit to 5 targets per scan
            repo = target.get("repo", "")
            if not repo:
                continue

            try:
                req = urllib.request.Request(
                    f"https://api.github.com/repos/{repo}/releases?per_page=1",
                    headers={
                        "Authorization": f"Bearer {github_pat}",
                        "Accept": "application/vnd.github+json",
                    },
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    releases = json.loads(resp.read())

                if releases:
                    latest = releases[0]
                    published = latest.get("published_at", "")
                    # Check if release is recent (within 24 hours)
                    if published:
                        pub_time = datetime.fromisoformat(
                            published.replace("Z", "+00:00")
                        ).timestamp()
                        if time.time() - pub_time < 86400:
                            notifications.append(ProactiveNotification(
                                id=f"comp_{repo}_{int(time.time())}",
                                category="INTEL",
                                title=f"Competitor release: {repo} {latest.get('tag_name', '')}",
                                body=latest.get("name", "")[:100],
                                source="competitive_intel",
                                priority="normal",
                                metadata={"repo": repo, "tag": latest.get("tag_name", "")},
                            ))
            except Exception:
                pass

        self.last_run = time.time()
        return notifications


# -- RSS Feed Scanner ------------------------------------------------------------


class RSSFeedScanner(BaseScanner):
    """Monitor RSS feeds for relevant news."""

    name = "rss_feeds"
    interval_seconds = 1800  # Every 30 minutes

    def __init__(self, feeds: Optional[List[str]] = None):
        super().__init__()
        self.feeds = feeds or []
        self._seen_ids: set = set()

    async def scan(self) -> List[ProactiveNotification]:
        """Scan RSS feeds for new entries."""
        notifications = []

        for feed_url in self.feeds:
            try:
                req = urllib.request.Request(
                    feed_url,
                    headers={"User-Agent": "Rhodawk-Hermes/1.0"},
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    content = resp.read().decode(errors="replace")

                # Simple RSS/Atom parsing (basic XML extraction)
                items = self._extract_items(content)
                for item in items[:5]:
                    item_id = item.get("link", "") or item.get("title", "")
                    if item_id and item_id not in self._seen_ids:
                        self._seen_ids.add(item_id)
                        notifications.append(ProactiveNotification(
                            id=f"rss_{hash(item_id) % 100000}",
                            category="INTEL",
                            title=item.get("title", "New feed item")[:80],
                            body=item.get("description", "")[:200],
                            source="rss_feeds",
                            priority="low",
                            metadata={"link": item.get("link", ""), "feed": feed_url},
                        ))
            except Exception:
                pass

        # Cap seen IDs
        if len(self._seen_ids) > 500:
            self._seen_ids = set(list(self._seen_ids)[-500:])

        self.last_run = time.time()
        return notifications

    def _extract_items(self, xml_content: str) -> List[dict]:
        """Extract items from RSS/Atom feed (basic regex-based parsing)."""
        import re
        items = []

        # Try RSS format
        item_blocks = re.findall(r'<item>(.*?)</item>', xml_content, re.DOTALL)
        if not item_blocks:
            # Try Atom format
            item_blocks = re.findall(r'<entry>(.*?)</entry>', xml_content, re.DOTALL)

        for block in item_blocks[:10]:
            title = re.search(r'<title[^>]*>(.*?)</title>', block, re.DOTALL)
            link = re.search(r'<link[^>]*>(.*?)</link>', block, re.DOTALL)
            if not link:
                link = re.search(r'<link[^>]*href="([^"]*)"', block)
            desc = re.search(r'<description[^>]*>(.*?)</description>', block, re.DOTALL)
            if not desc:
                desc = re.search(r'<summary[^>]*>(.*?)</summary>', block, re.DOTALL)

            items.append({
                "title": (title.group(1).strip() if title else ""),
                "link": (link.group(1).strip() if link else ""),
                "description": (desc.group(1).strip() if desc else ""),
            })

        return items


# -- Proactive Engine (Main) -----------------------------------------------------


class ProactiveEngine:
    """
    Main proactive intelligence engine.

    Coordinates all scanners, manages scan scheduling, collects notifications,
    and dispatches them to the notification callback (typically Telegram).
    """

    def __init__(self, config: Optional[dict] = None):
        config = config or {}

        self._scanners: List[BaseScanner] = []
        self._running = False
        self._notification_callback: Optional[Callable] = None
        self._notification_history: deque = deque(maxlen=200)
        self._stats = {
            "total_scans": 0,
            "total_notifications": 0,
            "errors": 0,
        }

        # Initialize default scanners
        self._init_scanners(config)

    def _init_scanners(self, config: dict):
        """Initialize all scanner instances."""
        repos = config.get("github_repos", ["Architect8989/Hermes88"])
        self._scanners.append(GitHubScanner(repos=repos))
        self._scanners.append(SystemHealthScanner())
        self._scanners.append(ScheduledTaskScanner(
            cron_dir=config.get("cron_dir", "/app/hermes_config/cron")
        ))
        self._scanners.append(FinancialScanner())
        self._scanners.append(CompetitiveIntelScanner(
            targets_file=config.get("targets_file", "/app/data/target_list.json")
        ))

        feeds = config.get("rss_feeds", [])
        if feeds:
            self._scanners.append(RSSFeedScanner(feeds=feeds))

    def set_notification_callback(self, callback: Callable):
        """
        Set the callback for sending notifications.

        The callback should be an async callable that accepts a
        ProactiveNotification and delivers it to the operator.
        """
        self._notification_callback = callback

    async def start(self):
        """Start the proactive engine scan loop."""
        self._running = True
        print("[proactive] Engine started", flush=True)

        while self._running:
            try:
                await self._run_cycle()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._stats["errors"] += 1
                print(f"[proactive] Cycle error: {e}", flush=True)

            # Sleep between cycles
            await asyncio.sleep(30)

        print("[proactive] Engine stopped", flush=True)

    async def stop(self):
        """Stop the proactive engine."""
        self._running = False

    async def _run_cycle(self):
        """Run one cycle: check all scanners and collect notifications."""
        for scanner in self._scanners:
            if not scanner.should_run():
                continue

            try:
                notifications = await scanner.scan()
                self._stats["total_scans"] += 1

                for notification in notifications:
                    self._notification_history.append(notification)
                    self._stats["total_notifications"] += 1

                    if self._notification_callback:
                        try:
                            if asyncio.iscoroutinefunction(self._notification_callback):
                                await self._notification_callback(notification)
                            else:
                                self._notification_callback(notification)
                        except Exception as e:
                            print(
                                f"[proactive] Notification delivery failed: {e}",
                                flush=True,
                            )

            except Exception as e:
                self._stats["errors"] += 1
                scanner.error_count += 1
                print(
                    f"[proactive] Scanner {scanner.name} error: {e}",
                    flush=True,
                )

    async def scan_now(self, scanner_name: Optional[str] = None) -> List[ProactiveNotification]:
        """
        Force an immediate scan (all scanners or a specific one).

        Returns collected notifications without dispatching.
        """
        notifications = []
        for scanner in self._scanners:
            if scanner_name and scanner.name != scanner_name:
                continue
            try:
                result = await scanner.scan()
                notifications.extend(result)
            except Exception as e:
                print(f"[proactive] Scan error ({scanner.name}): {e}", flush=True)
        return notifications

    def get_history(self, limit: int = 50,
                    category: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get notification history with optional filtering."""
        history = list(self._notification_history)
        if category:
            history = [n for n in history if n.category == category]
        return [n.to_dict() for n in history[-limit:]]

    def get_stats(self) -> Dict[str, Any]:
        """Get proactive engine statistics."""
        scanner_stats = {}
        for scanner in self._scanners:
            scanner_stats[scanner.name] = {
                "enabled": scanner.enabled,
                "interval_seconds": scanner.interval_seconds,
                "last_run": scanner.last_run,
                "error_count": scanner.error_count,
            }

        return {
            **self._stats,
            "running": self._running,
            "scanners": scanner_stats,
            "history_size": len(self._notification_history),
        }

    def enable_scanner(self, name: str):
        """Enable a specific scanner."""
        for scanner in self._scanners:
            if scanner.name == name:
                scanner.enabled = True
                return

    def disable_scanner(self, name: str):
        """Disable a specific scanner."""
        for scanner in self._scanners:
            if scanner.name == name:
                scanner.enabled = False
                return

    def add_scanner(self, scanner: BaseScanner):
        """Add a custom scanner to the engine."""
        self._scanners.append(scanner)
