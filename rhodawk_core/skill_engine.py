#!/usr/bin/env python3
"""
rhodawk_core/skill_engine.py — hermes-agent skills pass-through.

Delegates all skill management to hermes-agent's built-in skill system.
hermes-agent provides:
  - Autonomous skill creation after complex tasks (learning loop)
  - Skill search and retrieval before tasks (procedural memory)
  - Compatible with agentskills.io open standard for sharing across agents
  - Skills self-improve during use without any custom code here

Browse installed skills:  hermes skills
Community skills hub:     https://agentskills.io
Docs: https://hermes-agent.nousresearch.com/docs/user-guide/features/skills
"""

import shutil
import subprocess
from typing import Optional


class SkillEngine:
    """Routes skill operations through hermes-agent's skill system."""

    def search(self, query: str) -> Optional[str]:
        """Search hermes-agent's skill index for a matching procedure."""
        binary = shutil.which("hermes") or shutil.which("hermes-agent")
        if not binary:
            return None
        try:
            result = subprocess.run(
                [binary, "skills", "search", query],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.stdout.strip() or None
        except Exception:
            return None

    def record(
        self,
        task_description: str,
        outcome: str,
        tool_calls: int = 0,
    ) -> None:
        """hermes-agent's learning loop records skills autonomously after complex tasks."""
        pass


# Type alias used by callers that import TaskExecution from this module
TaskExecution = dict


def get_skill_engine() -> SkillEngine:
    return SkillEngine()
