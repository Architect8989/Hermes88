#!/usr/bin/env python3
"""
Rhodawk Core Tool Registry.

Implements the tool registry with terminal execution, web_fetch,
camofox_browse, file operations, and git operations via push-commit utility.
Each tool has rate limiting, audit logging, and error handling.

References modelcontextprotocol/servers patterns for tool definitions.

Copyright (c) 2024-2025 Rhodawk AI. All rights reserved.
"""
import asyncio
import hashlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Callable, Dict, List, Any


# -- Tool Definition Model -------------------------------------------------------


@dataclass
class ToolDefinition:
    """Definition of a tool in the MCP-compatible format."""
    name: str
    description: str
    parameters: dict = field(default_factory=dict)
    rate_limit_per_minute: int = 30
    requires_confirmation: bool = False
    category: str = "general"
    enabled: bool = True


@dataclass
class ToolResult:
    """Result from a tool execution."""
    success: bool
    output: str = ""
    error: str = ""
    duration_ms: float = 0.0
    tool_name: str = ""
    timestamp: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)


# -- Rate Limiter for Tools ------------------------------------------------------


class ToolRateLimiter:
    """Per-tool rate limiting with sliding window."""

    def __init__(self):
        self._windows: Dict[str, deque] = {}

    def check(self, tool_name: str, limit: int) -> bool:
        """Check if a tool call is allowed under the rate limit."""
        now = time.time()
        if tool_name not in self._windows:
            self._windows[tool_name] = deque()

        window = self._windows[tool_name]
        while window and window[0] < now - 60:
            window.popleft()

        if len(window) >= limit:
            return False

        window.append(now)
        return True

    def wait_time(self, tool_name: str, limit: int) -> float:
        """Time until next call is allowed."""
        now = time.time()
        if tool_name not in self._windows:
            return 0.0
        window = self._windows[tool_name]
        while window and window[0] < now - 60:
            window.popleft()
        if len(window) < limit:
            return 0.0
        return window[0] + 60 - now


# -- Tool Registry ---------------------------------------------------------------


class ToolRegistry:
    """
    Central registry for all Hermes tools.

    Provides terminal execution, web fetching, camofox browser integration,
    file system operations, and git operations. Each tool has rate limiting,
    audit logging, and comprehensive error handling.
    """

    def __init__(self, config: Optional[dict] = None):
        config = config or {}

        self._tools: Dict[str, ToolDefinition] = {}
        self._handlers: Dict[str, Callable] = {}
        self._rate_limiter = ToolRateLimiter()
        self._audit_log: deque = deque(maxlen=500)
        self._stats: Dict[str, Dict[str, int]] = {}

        # Configuration
        self.workdir = config.get("workdir", "/app")
        self.camofox_host = config.get("camofox_host", os.environ.get("CAMOFOX_HOST", "camofox"))
        self.camofox_port = config.get("camofox_port", os.environ.get("CAMOFOX_PORT", "9377"))
        self.camofox_key = config.get("camofox_key", os.environ.get("CAMOFOX_ACCESS_KEY", ""))
        self.max_output_size = config.get("max_output_size", 8000)

        # Register all built-in tools
        self._register_builtins()

    def _register_builtins(self):
        """Register all built-in tool definitions and handlers."""
        # Terminal execution
        self.register(
            ToolDefinition(
                name="terminal",
                description="Execute a shell command with timeout and output capture.",
                parameters={
                    "command": {"type": "string", "description": "Shell command to execute"},
                    "workdir": {"type": "string", "description": "Working directory", "default": "/app"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds", "default": 120},
                },
                rate_limit_per_minute=20,
                category="execution",
            ),
            self._handle_terminal,
        )

        # Web fetch
        self.register(
            ToolDefinition(
                name="web_fetch",
                description="Fetch content from a URL using urllib with User-Agent spoofing.",
                parameters={
                    "url": {"type": "string", "description": "URL to fetch"},
                    "method": {"type": "string", "description": "HTTP method", "default": "GET"},
                    "headers": {"type": "object", "description": "Custom headers", "default": {}},
                    "max_length": {"type": "integer", "description": "Max response length", "default": 8000},
                },
                rate_limit_per_minute=30,
                category="network",
            ),
            self._handle_web_fetch,
        )

        # Camofox browse
        self.register(
            ToolDefinition(
                name="camofox_browse",
                description="Browse a URL using the camofox headless browser with session management.",
                parameters={
                    "url": {"type": "string", "description": "URL to browse"},
                    "session_id": {"type": "string", "description": "Session ID", "default": "default"},
                    "wait_seconds": {"type": "integer", "description": "Seconds to wait for page load", "default": 3},
                    "extract": {"type": "string", "description": "Content type: text or html", "default": "text"},
                },
                rate_limit_per_minute=10,
                category="browser",
            ),
            self._handle_camofox_browse,
        )

        # File read
        self.register(
            ToolDefinition(
                name="file_read",
                description="Read content from a file on the filesystem.",
                parameters={
                    "path": {"type": "string", "description": "File path to read"},
                    "max_lines": {"type": "integer", "description": "Max lines to return", "default": 500},
                    "offset": {"type": "integer", "description": "Line offset to start from", "default": 0},
                },
                rate_limit_per_minute=60,
                category="filesystem",
            ),
            self._handle_file_read,
        )

        # File write
        self.register(
            ToolDefinition(
                name="file_write",
                description="Write content to a file on the filesystem.",
                parameters={
                    "path": {"type": "string", "description": "File path to write"},
                    "content": {"type": "string", "description": "Content to write"},
                    "mode": {"type": "string", "description": "Write mode: overwrite or append", "default": "overwrite"},
                },
                rate_limit_per_minute=30,
                requires_confirmation=False,
                category="filesystem",
            ),
            self._handle_file_write,
        )

        # File list
        self.register(
            ToolDefinition(
                name="file_list",
                description="List files in a directory with optional pattern matching.",
                parameters={
                    "path": {"type": "string", "description": "Directory path"},
                    "pattern": {"type": "string", "description": "Glob pattern", "default": "*"},
                    "recursive": {"type": "boolean", "description": "Recursive listing", "default": False},
                },
                rate_limit_per_minute=60,
                category="filesystem",
            ),
            self._handle_file_list,
        )

        # Git operations
        self.register(
            ToolDefinition(
                name="git_status",
                description="Get git status for a repository.",
                parameters={
                    "repo_path": {"type": "string", "description": "Repository path", "default": "/app"},
                },
                rate_limit_per_minute=30,
                category="git",
            ),
            self._handle_git_status,
        )

        # Git push-commit
        self.register(
            ToolDefinition(
                name="git_push_commit",
                description="Stage, commit, and push changes using the push-commit utility.",
                parameters={
                    "repo_path": {"type": "string", "description": "Repository path"},
                    "message": {"type": "string", "description": "Commit message"},
                    "files": {"type": "array", "description": "Files to stage (empty = all)", "default": []},
                    "branch": {"type": "string", "description": "Branch to push to", "default": ""},
                },
                rate_limit_per_minute=5,
                requires_confirmation=True,
                category="git",
            ),
            self._handle_git_push_commit,
        )

        # Git diff
        self.register(
            ToolDefinition(
                name="git_diff",
                description="Get git diff for staged or unstaged changes.",
                parameters={
                    "repo_path": {"type": "string", "description": "Repository path", "default": "/app"},
                    "staged": {"type": "boolean", "description": "Show staged changes", "default": False},
                    "file": {"type": "string", "description": "Specific file to diff", "default": ""},
                },
                rate_limit_per_minute=30,
                category="git",
            ),
            self._handle_git_diff,
        )

    def register(self, definition: ToolDefinition, handler: Callable):
        """Register a tool with its definition and handler."""
        self._tools[definition.name] = definition
        self._handlers[definition.name] = handler
        self._stats[definition.name] = {"calls": 0, "errors": 0, "total_ms": 0}

    async def execute(self, tool_name: str, params: dict) -> ToolResult:
        """
        Execute a tool by name with the given parameters.

        Applies rate limiting, calls the handler, logs the result,
        and returns a ToolResult.
        """
        # Check tool exists
        definition = self._tools.get(tool_name)
        if not definition:
            return ToolResult(
                success=False,
                error=f"Tool not found: {tool_name}",
                tool_name=tool_name,
            )

        # Check enabled
        if not definition.enabled:
            return ToolResult(
                success=False,
                error=f"Tool is disabled: {tool_name}",
                tool_name=tool_name,
            )

        # Check rate limit
        if not self._rate_limiter.check(tool_name, definition.rate_limit_per_minute):
            wait = self._rate_limiter.wait_time(tool_name, definition.rate_limit_per_minute)
            return ToolResult(
                success=False,
                error=f"Rate limited. Try again in {wait:.1f}s",
                tool_name=tool_name,
            )

        # Execute handler
        handler = self._handlers[tool_name]
        start_time = time.time()

        try:
            if asyncio.iscoroutinefunction(handler):
                result = await handler(params)
            else:
                result = handler(params)

            duration_ms = (time.time() - start_time) * 1000

            if isinstance(result, ToolResult):
                result.duration_ms = duration_ms
                result.tool_name = tool_name
            else:
                result = ToolResult(
                    success=True,
                    output=str(result)[:self.max_output_size],
                    tool_name=tool_name,
                    duration_ms=duration_ms,
                )

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            result = ToolResult(
                success=False,
                error=f"{type(e).__name__}: {str(e)}",
                tool_name=tool_name,
                duration_ms=duration_ms,
            )

        # Update stats
        self._stats[tool_name]["calls"] += 1
        self._stats[tool_name]["total_ms"] += int(result.duration_ms)
        if not result.success:
            self._stats[tool_name]["errors"] += 1

        # Audit log
        self._audit_log.append({
            "tool": tool_name,
            "params": {k: str(v)[:200] for k, v in params.items()},
            "success": result.success,
            "duration_ms": result.duration_ms,
            "timestamp": time.time(),
            "error": result.error[:200] if result.error else "",
        })

        return result

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        """Get all tool definitions in MCP-compatible format."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
                "category": t.category,
                "enabled": t.enabled,
            }
            for t in self._tools.values()
        ]

    def get_stats(self) -> Dict[str, Any]:
        """Get tool usage statistics."""
        return {
            "tools": dict(self._stats),
            "total_calls": sum(s["calls"] for s in self._stats.values()),
            "total_errors": sum(s["errors"] for s in self._stats.values()),
            "audit_log_size": len(self._audit_log),
        }

    def get_audit_log(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get recent audit log entries."""
        return list(self._audit_log)[-limit:]

    # -- Tool Handlers ---------------------------------------------------------------

    def _handle_terminal(self, params: dict) -> ToolResult:
        """Execute a shell command."""
        command = params.get("command", "")
        workdir = params.get("workdir", self.workdir)
        timeout = params.get("timeout", 120)

        if not command:
            return ToolResult(success=False, error="No command provided")

        # Security: block dangerous commands
        blocked = ["rm -rf /", "mkfs", "dd if=/dev/zero", ":(){:|:&};:"]
        for b in blocked:
            if b in command:
                return ToolResult(success=False, error=f"Blocked dangerous command pattern")

        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = result.stdout + result.stderr
            output = output[-self.max_output_size:]

            return ToolResult(
                success=result.returncode == 0,
                output=output,
                error="" if result.returncode == 0 else f"Exit code: {result.returncode}",
                metadata={"returncode": result.returncode},
            )
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, error=f"Command timed out after {timeout}s")
        except Exception as e:
            return ToolResult(success=False, error=f"{type(e).__name__}: {str(e)}")

    def _handle_web_fetch(self, params: dict) -> ToolResult:
        """Fetch content from a URL."""
        url = params.get("url", "")
        method = params.get("method", "GET")
        headers = params.get("headers", {})
        max_length = params.get("max_length", 8000)

        if not url:
            return ToolResult(success=False, error="No URL provided")

        default_headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/115.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        default_headers.update(headers)

        req = urllib.request.Request(url, headers=default_headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                content_type = resp.headers.get("Content-Type", "")
                raw = resp.read()
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    text = raw.decode("latin-1")
                text = text[:max_length]

            return ToolResult(
                success=True,
                output=text,
                metadata={"content_type": content_type, "status": resp.status},
            )
        except urllib.error.HTTPError as e:
            return ToolResult(
                success=False,
                error=f"HTTP {e.code}: {e.reason}",
                metadata={"status": e.code},
            )
        except urllib.error.URLError as e:
            return ToolResult(success=False, error=f"URL error: {str(e.reason)}")
        except Exception as e:
            return ToolResult(success=False, error=f"{type(e).__name__}: {str(e)}")

    def _handle_camofox_browse(self, params: dict) -> ToolResult:
        """Browse a URL using camofox headless browser."""
        url = params.get("url", "")
        session_id = params.get("session_id", "default")
        wait_seconds = params.get("wait_seconds", 3)

        if not url:
            return ToolResult(success=False, error="No URL provided")

        base_url = f"http://{self.camofox_host}:{self.camofox_port}"

        # Health check
        try:
            health_req = urllib.request.Request(f"{base_url}/health")
            with urllib.request.urlopen(health_req, timeout=5) as resp:
                if resp.status != 200:
                    raise Exception("Camofox unhealthy")
        except Exception:
            # Fallback to plain fetch
            return self._handle_web_fetch({"url": url, "max_length": 8000})

        # Create tab
        try:
            payload = json.dumps({"url": url}).encode()
            tab_req = urllib.request.Request(
                f"{base_url}/sessions/{session_id}/tabs",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.camofox_key}",
                },
            )
            with urllib.request.urlopen(tab_req, timeout=15) as resp:
                tab_data = json.loads(resp.read())
            tab_id = tab_data.get("tabId") or tab_data.get("id", "")
        except Exception as e:
            return ToolResult(success=False, error=f"Camofox tab creation failed: {e}")

        # Wait for page load
        time.sleep(wait_seconds)

        # Get snapshot
        try:
            snap_req = urllib.request.Request(
                f"{base_url}/tabs/{tab_id}/snapshot",
                headers={"Authorization": f"Bearer {self.camofox_key}"},
            )
            with urllib.request.urlopen(snap_req, timeout=15) as resp:
                snap_data = json.loads(resp.read())
            content = snap_data.get("text") or snap_data.get("content", "")
        except Exception as e:
            content = ""

        # Close tab
        try:
            close_req = urllib.request.Request(
                f"{base_url}/tabs/{tab_id}",
                headers={"Authorization": f"Bearer {self.camofox_key}"},
                method="DELETE",
            )
            urllib.request.urlopen(close_req, timeout=5)
        except Exception:
            pass

        if content:
            return ToolResult(
                success=True,
                output=content[:self.max_output_size],
                metadata={"tab_id": tab_id, "session_id": session_id},
            )
        else:
            return ToolResult(success=False, error="No content retrieved from page")

    def _handle_file_read(self, params: dict) -> ToolResult:
        """Read content from a file."""
        path = params.get("path", "")
        max_lines = params.get("max_lines", 500)
        offset = params.get("offset", 0)

        if not path:
            return ToolResult(success=False, error="No path provided")

        filepath = Path(path)
        if not filepath.exists():
            return ToolResult(success=False, error=f"File not found: {path}")

        if not filepath.is_file():
            return ToolResult(success=False, error=f"Not a file: {path}")

        try:
            with open(filepath, "r", errors="replace") as f:
                lines = f.readlines()

            total_lines = len(lines)
            selected = lines[offset:offset + max_lines]
            content = "".join(selected)

            return ToolResult(
                success=True,
                output=content[:self.max_output_size],
                metadata={"total_lines": total_lines, "offset": offset, "returned": len(selected)},
            )
        except Exception as e:
            return ToolResult(success=False, error=f"{type(e).__name__}: {str(e)}")

    def _handle_file_write(self, params: dict) -> ToolResult:
        """Write content to a file."""
        path = params.get("path", "")
        content = params.get("content", "")
        mode = params.get("mode", "overwrite")

        if not path:
            return ToolResult(success=False, error="No path provided")

        filepath = Path(path)

        try:
            filepath.parent.mkdir(parents=True, exist_ok=True)
            write_mode = "a" if mode == "append" else "w"
            with open(filepath, write_mode) as f:
                f.write(content)

            return ToolResult(
                success=True,
                output=f"Written {len(content)} bytes to {path}",
                metadata={"path": path, "bytes": len(content), "mode": mode},
            )
        except Exception as e:
            return ToolResult(success=False, error=f"{type(e).__name__}: {str(e)}")

    def _handle_file_list(self, params: dict) -> ToolResult:
        """List files in a directory."""
        path = params.get("path", ".")
        pattern = params.get("pattern", "*")
        recursive = params.get("recursive", False)

        dirpath = Path(path)
        if not dirpath.exists():
            return ToolResult(success=False, error=f"Directory not found: {path}")

        try:
            if recursive:
                files = sorted(str(f.relative_to(dirpath)) for f in dirpath.rglob(pattern)
                              if ".git" not in str(f))
            else:
                files = sorted(str(f.relative_to(dirpath)) for f in dirpath.glob(pattern))

            files = files[:200]  # Cap at 200 entries
            output = "\n".join(files)

            return ToolResult(
                success=True,
                output=output,
                metadata={"count": len(files), "path": path, "pattern": pattern},
            )
        except Exception as e:
            return ToolResult(success=False, error=f"{type(e).__name__}: {str(e)}")

    def _handle_git_status(self, params: dict) -> ToolResult:
        """Get git status for a repository."""
        repo_path = params.get("repo_path", self.workdir)

        try:
            result = subprocess.run(
                ["git", "status", "--porcelain", "-b"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=30,
            )
            return ToolResult(
                success=result.returncode == 0,
                output=result.stdout[:self.max_output_size],
                error=result.stderr if result.returncode != 0 else "",
            )
        except Exception as e:
            return ToolResult(success=False, error=f"{type(e).__name__}: {str(e)}")

    def _handle_git_push_commit(self, params: dict) -> ToolResult:
        """Stage, commit, and push changes."""
        repo_path = params.get("repo_path", self.workdir)
        message = params.get("message", "")
        files = params.get("files", [])
        branch = params.get("branch", "")

        if not message:
            return ToolResult(success=False, error="No commit message provided")

        try:
            # Stage files
            if files:
                for f in files:
                    subprocess.run(
                        ["git", "add", f], cwd=repo_path,
                        capture_output=True, timeout=30,
                    )
            else:
                subprocess.run(
                    ["git", "add", "-A"], cwd=repo_path,
                    capture_output=True, timeout=30,
                )

            # Commit
            commit_result = subprocess.run(
                ["git", "commit", "-m", message],
                cwd=repo_path, capture_output=True, text=True, timeout=30,
            )
            if commit_result.returncode != 0:
                if "nothing to commit" in commit_result.stdout:
                    return ToolResult(success=True, output="Nothing to commit")
                return ToolResult(
                    success=False,
                    error=f"Commit failed: {commit_result.stderr}",
                )

            # Push
            push_cmd = ["git", "push"]
            if branch:
                push_cmd.extend(["origin", branch])

            push_result = subprocess.run(
                push_cmd, cwd=repo_path,
                capture_output=True, text=True, timeout=60,
            )

            output = commit_result.stdout + "\n" + push_result.stdout
            return ToolResult(
                success=push_result.returncode == 0,
                output=output[:self.max_output_size],
                error=push_result.stderr if push_result.returncode != 0 else "",
            )
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, error="Git operation timed out")
        except Exception as e:
            return ToolResult(success=False, error=f"{type(e).__name__}: {str(e)}")

    def _handle_git_diff(self, params: dict) -> ToolResult:
        """Get git diff."""
        repo_path = params.get("repo_path", self.workdir)
        staged = params.get("staged", False)
        file_path = params.get("file", "")

        cmd = ["git", "diff"]
        if staged:
            cmd.append("--staged")
        if file_path:
            cmd.extend(["--", file_path])

        try:
            result = subprocess.run(
                cmd, cwd=repo_path,
                capture_output=True, text=True, timeout=30,
            )
            return ToolResult(
                success=result.returncode == 0,
                output=result.stdout[:self.max_output_size],
                error=result.stderr if result.returncode != 0 else "",
            )
        except Exception as e:
            return ToolResult(success=False, error=f"{type(e).__name__}: {str(e)}")
