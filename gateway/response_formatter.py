#!/usr/bin/env python3
"""
gateway/response_formatter.py — DEAD CODE — NOT CALLED BY ANY RUNNING PROCESS.

gateway/run.py does os.execvpe(hermes | openclaw). This file is never imported.
Response formatting in the running system is handled natively by the chosen
gateway binary:
  - hermes-agent: formats responses per-channel based on gateway.yaml
  - openclaw: applies its own channel adapters (Telegram MarkdownV2, etc.)

rhodawk_core.SynthesisEngine (which this file wraps) is also unreachable.
See rhodawk_core/__init__.py for the full dead-code explanation.

Rhodawk AI -- Peak Architecture v10.0
"""
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

# Ensure rhodawk_core is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from rhodawk_core.synthesis import SynthesisEngine
    SYNTHESIS_AVAILABLE = True
except ImportError:
    SYNTHESIS_AVAILABLE = False

try:
    from rhodawk_core.audit import AuditLogger
    AUDIT_AVAILABLE = True
except ImportError:
    AUDIT_AVAILABLE = False


# -- Channel Format Definitions --------------------------------------------------

CHANNEL_FORMATS = {
    "telegram": {
        "max_length": 4000,
        "markdown": False,
        "code_blocks": False,
        "bullets": False,
        "style": "dense prose, no formatting, plain text only",
        "file_delivery": "%%FILE:name%% ... %%/FILE%%",
        "line_separator": "\n",
        "truncation_message": "\n\n[truncated -- full report available on request]",
    },
    "discord": {
        "max_length": 2000,
        "markdown": True,
        "code_blocks": True,
        "bullets": True,
        "style": "discord markdown with embeds for structured data",
        "file_delivery": "attachment upload",
        "line_separator": "\n",
        "truncation_message": "\n\n*[truncated -- use `/full` for complete output]*",
    },
    "slack": {
        "max_length": 3000,
        "markdown": False,
        "code_blocks": True,
        "bullets": True,
        "style": "slack Block Kit for structured, mrkdwn for inline",
        "file_delivery": "files.upload API",
        "line_separator": "\n",
        "truncation_message": "\n\n_[truncated -- thread reply has full output]_",
    },
    "email": {
        "max_length": 50000,
        "markdown": True,
        "code_blocks": True,
        "bullets": True,
        "style": "professional email with HTML formatting",
        "file_delivery": "MIME attachment",
        "line_separator": "\n",
        "truncation_message": "",
    },
    "whatsapp": {
        "max_length": 4096,
        "markdown": False,
        "code_blocks": False,
        "bullets": False,
        "style": "plain text, no formatting symbols",
        "file_delivery": "media attachment",
        "line_separator": "\n",
        "truncation_message": "\n\n[message truncated]",
    },
    "api": {
        "max_length": 100000,
        "markdown": True,
        "code_blocks": True,
        "bullets": True,
        "style": "raw markdown, no length restrictions",
        "file_delivery": "base64 in JSON response",
        "line_separator": "\n",
        "truncation_message": "",
    },
}


class ResponseFormatter:
    """
    Formats Hermes responses for specific delivery channels.

    Responsibilities:
    - Strip markdown for plain-text channels (Telegram, WhatsApp)
    - Enforce message length limits with intelligent truncation
    - Apply synthesis templates for structured output types
    - Handle multi-part message splitting for long responses
    - Inject channel-specific formatting (Slack Block Kit, Discord embeds)
    """

    def __init__(self, config: Optional[dict] = None):
        """
        Initialize the response formatter.

        Args:
            config: Optional configuration dict with channel overrides.
        """
        self.config = config or {}
        self._synthesis: Optional["SynthesisEngine"] = None
        self._audit: Optional["AuditLogger"] = None

        # Override default channel formats with config
        self._formats = dict(CHANNEL_FORMATS)
        for channel, overrides in self.config.get("channel_overrides", {}).items():
            if channel in self._formats:
                self._formats[channel].update(overrides)

        # Initialize synthesis engine
        self._init_synthesis()
        self._init_audit()

    def _init_synthesis(self):
        """Initialize the synthesis engine."""
        if not SYNTHESIS_AVAILABLE:
            return
        try:
            self._synthesis = SynthesisEngine()
        except Exception as e:
            print(f"[response_formatter] Synthesis init failed: {e}", flush=True)
            self._synthesis = None

    def _init_audit(self):
        """Initialize audit logger."""
        if not AUDIT_AVAILABLE:
            return
        try:
            self._audit = AuditLogger()
        except Exception:
            self._audit = None

    def format_for_channel(self, content: str, channel: str = "telegram",
                           content_type: str = "general",
                           metadata: Optional[dict] = None) -> str:
        """
        Format response content for a specific channel.

        This is the primary entry point called by the gateway.

        Args:
            content: Raw response content from the LLM.
            channel: Target delivery channel.
            content_type: Type of content (general, code, research, error, financial).
            metadata: Optional metadata about the response context.

        Returns:
            Formatted content string ready for delivery.
        """
        fmt = self._formats.get(channel, self._formats["telegram"])
        metadata = metadata or {}

        # Step 1: Apply synthesis template if available and content_type matches
        if self._synthesis and content_type != "general":
            content = self._apply_synthesis(content, content_type, channel)

        # Step 2: Strip or convert formatting based on channel
        if not fmt["markdown"]:
            content = self._strip_markdown(content)

        if not fmt["code_blocks"]:
            content = self._strip_code_blocks(content)

        if not fmt["bullets"]:
            content = self._convert_bullets(content)

        # Step 3: Enforce length limits
        content = self._enforce_length(content, fmt)

        # Step 4: Channel-specific post-processing
        content = self._channel_post_process(content, channel, metadata)

        # Audit log
        if self._audit:
            self._audit.log("response_format", {
                "channel": channel,
                "content_type": content_type,
                "input_length": len(content),
                "output_length": len(content),
            })

        return content

    def format_multi_part(self, content: str, channel: str = "telegram") -> list:
        """
        Split a long response into multiple messages for delivery.

        Used when a response exceeds the channel's max_length and needs
        to be sent as multiple messages.

        Args:
            content: The full response content.
            channel: Target channel.

        Returns:
            List of message parts, each within channel length limits.
        """
        fmt = self._formats.get(channel, self._formats["telegram"])
        max_len = fmt["max_length"]

        if len(content) <= max_len:
            return [content]

        parts = []
        remaining = content

        while remaining:
            if len(remaining) <= max_len:
                parts.append(remaining)
                break

            # Find a good split point (paragraph break, sentence end)
            split_at = self._find_split_point(remaining, max_len)
            parts.append(remaining[:split_at].rstrip())
            remaining = remaining[split_at:].lstrip()

        return parts

    def format_error(self, error: str, channel: str = "telegram",
                     context: str = "") -> str:
        """
        Format an error message for delivery.

        Args:
            error: The error text.
            channel: Target channel.
            context: Additional context about what was being attempted.

        Returns:
            Formatted error message.
        """
        if context:
            formatted = f"Failed: {context}\nError: {error}"
        else:
            formatted = f"Error: {error}"

        return self.format_for_channel(formatted, channel, content_type="error")

    def format_proactive(self, intel: str, context: str, action: str,
                         urgency: str = "FYI",
                         channel: str = "telegram") -> str:
        """
        Format a proactive intelligence notification.

        Args:
            intel: One-line summary.
            context: Why it matters now.
            action: Recommended or completed action.
            urgency: act now / today / this week / FYI
            channel: Target channel.

        Returns:
            Formatted proactive notification.
        """
        content = (
            f"INTEL: {intel}\n"
            f"CONTEXT: {context}\n"
            f"ACTION: {action}\n"
            f"URGENCY: {urgency}"
        )
        return self.format_for_channel(content, channel, content_type="proactive")

    def format_task_status(self, task_name: str, status: str,
                           duration: float = 0.0,
                           channel: str = "telegram") -> str:
        """
        Format a task status update.

        Args:
            task_name: Name of the task.
            status: Current status (queued, running, completed, failed).
            duration: Duration in seconds (if completed).
            channel: Target channel.

        Returns:
            Formatted status message.
        """
        status_icons = {
            "queued": "[QUEUED]",
            "running": "[RUNNING]",
            "completed": "[DONE]",
            "failed": "[FAILED]",
            "retrying": "[RETRY]",
        }
        icon = status_icons.get(status, f"[{status.upper()}]")

        if duration > 0:
            msg = f"{icon} {task_name} ({duration:.0f}s)"
        else:
            msg = f"{icon} {task_name}"

        return self.format_for_channel(msg, channel)

    def _apply_synthesis(self, content: str, content_type: str,
                         channel: str) -> str:
        """Apply synthesis engine formatting for structured content types."""
        if not self._synthesis:
            return content

        try:
            return self._synthesis.format_response(
                content=content,
                response_type=content_type,
                channel=channel,
            )
        except Exception as e:
            print(f"[response_formatter] Synthesis error: {e}", flush=True)
            return content

    def _strip_markdown(self, content: str) -> str:
        """Remove markdown formatting for plain-text channels."""
        # Remove bold
        content = re.sub(r'\*\*(.+?)\*\*', r'\1', content)
        content = re.sub(r'__(.+?)__', r'\1', content)

        # Remove italic
        content = re.sub(r'\*(.+?)\*', r'\1', content)
        content = re.sub(r'_(.+?)_', r'\1', content)

        # Remove headers (## Header -> Header)
        content = re.sub(r'^#{1,6}\s+', '', content, flags=re.MULTILINE)

        # Remove horizontal rules
        content = re.sub(r'^---+$', '', content, flags=re.MULTILINE)
        content = re.sub(r'^\*\*\*+$', '', content, flags=re.MULTILINE)

        # Remove link formatting [text](url) -> text (url)
        content = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1 (\2)', content)

        # Remove inline code backticks (preserve content)
        content = re.sub(r'`([^`]+)`', r'\1', content)

        return content

    def _strip_code_blocks(self, content: str) -> str:
        """Remove code block fences, preserving the code content."""
        # Remove triple backtick fences
        content = re.sub(r'^```\w*\n?', '', content, flags=re.MULTILINE)
        content = re.sub(r'^```$', '', content, flags=re.MULTILINE)
        return content

    def _convert_bullets(self, content: str) -> str:
        """Convert bullet-point lists to prose-style for plain-text channels."""
        lines = content.split("\n")
        result = []
        bullet_group = []

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("- ") or stripped.startswith("* "):
                bullet_group.append(stripped[2:])
            else:
                if bullet_group:
                    # Join bullet items with semicolons for density
                    result.append("; ".join(bullet_group) + ".")
                    bullet_group = []
                result.append(line)

        if bullet_group:
            result.append("; ".join(bullet_group) + ".")

        return "\n".join(result)

    def _enforce_length(self, content: str, fmt: dict) -> str:
        """Enforce channel max length with intelligent truncation."""
        max_len = fmt["max_length"]
        truncation_msg = fmt.get("truncation_message", "")

        if len(content) <= max_len:
            return content

        # Reserve space for truncation message
        available = max_len - len(truncation_msg)

        # Try to truncate at a paragraph or sentence boundary
        truncated = content[:available]
        last_para = truncated.rfind("\n\n")
        last_sentence = max(truncated.rfind(". "), truncated.rfind(".\n"))

        if last_para > available * 0.7:
            truncated = truncated[:last_para]
        elif last_sentence > available * 0.7:
            truncated = truncated[:last_sentence + 1]

        return truncated + truncation_msg

    def _channel_post_process(self, content: str, channel: str,
                              metadata: dict) -> str:
        """Apply channel-specific post-processing."""
        if channel == "telegram":
            # Clean up any remaining empty lines (Telegram renders them)
            content = re.sub(r'\n{3,}', '\n\n', content)

        elif channel == "discord":
            # Wrap long code sections in code blocks if not already
            pass

        elif channel == "slack":
            # Convert markdown bold to Slack bold (*text* instead of **text**)
            content = re.sub(r'\*\*(.+?)\*\*', r'*\1*', content)

        return content.strip()

    def _find_split_point(self, text: str, max_len: int) -> int:
        """Find the best point to split a message."""
        # Prefer paragraph break
        para_break = text.rfind("\n\n", 0, max_len)
        if para_break > max_len * 0.5:
            return para_break

        # Prefer line break
        line_break = text.rfind("\n", 0, max_len)
        if line_break > max_len * 0.5:
            return line_break

        # Prefer sentence end
        sentence_end = text.rfind(". ", 0, max_len)
        if sentence_end > max_len * 0.5:
            return sentence_end + 1

        # Last resort: split at max_len
        return max_len


# -- Module-level convenience functions ------------------------------------------

_default_formatter: Optional[ResponseFormatter] = None


def get_formatter(config: Optional[dict] = None) -> ResponseFormatter:
    """Get or create the default ResponseFormatter instance."""
    global _default_formatter
    if _default_formatter is None:
        _default_formatter = ResponseFormatter(config)
    return _default_formatter


def format_for_channel(content: str, channel: str = "telegram",
                       content_type: str = "general",
                       metadata: Optional[dict] = None) -> str:
    """
    Convenience function: format response for a channel.

    This is the primary function called by gateway/run.py.

    Args:
        content: Raw response content.
        channel: Target channel (telegram, discord, slack, email, etc.)
        content_type: Content type for synthesis (general, code, research, error).
        metadata: Optional response metadata.

    Returns:
        Formatted content string.
    """
    formatter = get_formatter()
    return formatter.format_for_channel(content, channel, content_type, metadata)
