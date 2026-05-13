#!/usr/bin/env python3
"""
rhodawk_core/skill_engine.py — Hermes Learning Loop (Layer E)

After every complex task completion, evaluates whether a reusable skill
was exercised. If yes, generates or updates a SKILL.md file in the
skill index. Before every task, searches the index for matching skills
and returns a proven procedure to prepend to the system prompt.

This is the single biggest gap between a static assistant and an
actual digital twin: the system learns from every completed task.

Architecture follows hermes-agent's skill_manage pattern but adapted
for Rhodawk's Python gateway stack.

Copyright (c) 2024-2025 Rhodawk AI. All rights reserved.
"""

import json
import os
import re
import sqlite3
import time
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any


# ── Configuration ─────────────────────────────────────────────────────────────

SKILL_DIR   = Path(os.environ.get("HERMES_HOME", "/data/.hermes")) / "skills" / "_learned"
SKILL_INDEX = Path(os.environ.get("HERMES_HOME", "/data/.hermes")) / "skills" / "INDEX.json"
SKILL_DB    = Path(os.environ.get("HERMES_HOME", "/data/.hermes")) / "sessions" / "conversations.db"

# Minimum tool rounds to consider a task "non-trivial" enough to warrant skill creation
MIN_TOOL_ROUNDS_FOR_SKILL = 3

# Similarity threshold for skill matching (simple keyword overlap)
SKILL_MATCH_THRESHOLD = 0.30


# ── Data Models ───────────────────────────────────────────────────────────────


@dataclass
class Skill:
    """A learned skill — stored as SKILL.md and indexed in INDEX.json."""
    id: str                          # slug: "deploy-to-digitalocean"
    title: str
    trigger: str                     # when to activate this skill
    procedure: str                   # step-by-step what works
    verification: str = ""           # how to confirm success
    failure_modes: str = ""          # what goes wrong
    model_preference: str = ""       # which model worked best
    success_count: int = 0
    attempt_count: int = 0
    avg_duration_s: float = 0.0
    created_at: str = ""
    last_used: str = ""
    pinned: bool = False             # pinned skills are never auto-archived

    @property
    def success_rate(self) -> float:
        if self.attempt_count == 0:
            return 0.0
        return self.success_count / self.attempt_count

    def to_markdown(self) -> str:
        sr = f"{self.success_rate:.0%}"
        return (
            f"# Skill: {self.title}\n"
            f"**ID:** {self.id}\n"
            f"**Trigger:** {self.trigger}\n\n"
            f"## Procedure\n{self.procedure}\n\n"
            f"## Verification\n{self.verification or '(not specified)'}\n\n"
            f"## Failure Modes\n{self.failure_modes or '(none recorded)'}\n\n"
            f"## Metadata\n"
            f"- Model preference: {self.model_preference or 'any'}\n"
            f"- Success rate: {sr} ({self.success_count}/{self.attempt_count})\n"
            f"- Avg duration: {self.avg_duration_s:.1f}s\n"
            f"- Created: {self.created_at}\n"
            f"- Last used: {self.last_used}\n"
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TaskExecution:
    """Record of a completed task execution for skill evaluation."""
    task_description: str
    tool_calls_made: List[Dict[str, Any]]
    result_summary: str
    duration_seconds: float
    tool_round_count: int
    succeeded: bool
    model_used: str = ""


# ── Skill Index ───────────────────────────────────────────────────────────────


class SkillIndex:
    """JSON-backed index of all learned skills."""

    def __init__(self) -> None:
        SKILL_DIR.mkdir(parents=True, exist_ok=True)
        self._index: Dict[str, dict] = self._load()

    def _load(self) -> Dict[str, dict]:
        if SKILL_INDEX.exists():
            try:
                return json.loads(SKILL_INDEX.read_text())
            except Exception:
                return {}
        return {}

    def _save(self) -> None:
        SKILL_INDEX.write_text(json.dumps(self._index, indent=2))

    def get(self, skill_id: str) -> Optional[Skill]:
        d = self._index.get(skill_id)
        if not d:
            return None
        try:
            return Skill(**{k: v for k, v in d.items() if k in Skill.__dataclass_fields__})
        except Exception:
            return None

    def all(self) -> List[Skill]:
        skills = []
        for d in self._index.values():
            try:
                skills.append(Skill(**{k: v for k, v in d.items() if k in Skill.__dataclass_fields__}))
            except Exception:
                continue
        return skills

    def save_skill(self, skill: Skill) -> None:
        self._index[skill.id] = skill.to_dict()
        self._save()
        # Write SKILL.md file
        skill_path = SKILL_DIR / f"{skill.id}.md"
        skill_path.write_text(skill.to_markdown(), encoding="utf-8")

    def update_usage(self, skill_id: str, succeeded: bool, duration_s: float) -> None:
        d = self._index.get(skill_id)
        if not d:
            return
        d["attempt_count"] = d.get("attempt_count", 0) + 1
        if succeeded:
            d["success_count"] = d.get("success_count", 0) + 1
        # Rolling average duration
        prev_avg = d.get("avg_duration_s", 0.0)
        n = d["attempt_count"]
        d["avg_duration_s"] = ((prev_avg * (n - 1)) + duration_s) / n
        d["last_used"] = datetime.now(timezone.utc).isoformat()
        self._save()


# ── Skill Matcher ─────────────────────────────────────────────────────────────


def _keyword_overlap(text_a: str, text_b: str) -> float:
    """Simple keyword overlap similarity (Jaccard on word sets)."""
    _stop = {"the", "a", "an", "to", "for", "with", "and", "or", "in", "on",
             "is", "it", "of", "how", "can", "i", "me", "my", "this", "that"}
    words_a = set(re.findall(r'\b\w{3,}\b', text_a.lower())) - _stop
    words_b = set(re.findall(r'\b\w{3,}\b', text_b.lower())) - _stop
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


# ── Skill Engine ──────────────────────────────────────────────────────────────


class SkillEngine:
    """
    Learning loop implementation for the Rhodawk gateway.

    After every complex task completion:
      1. evaluate_for_skill_creation() checks if the task warrants a skill
      2. If yes, calls the LLM to generate a SKILL.md
      3. Saves the skill to the index

    Before every task:
      1. find_relevant_skill() searches the index for matching patterns
      2. If found above threshold, returns the skill procedure as a prompt prefix
    """

    def __init__(self, openai_client=None, model: str = "") -> None:
        self._index = SkillIndex()
        self._client = openai_client
        self._model = model or os.environ.get("HERMES_MODEL", "deepseek-v4-pro")

    def find_relevant_skill(self, task_description: str) -> Optional[Skill]:
        """
        Search skill index for a skill that matches this task description.
        Returns the best match above SKILL_MATCH_THRESHOLD, or None.
        """
        best_skill: Optional[Skill] = None
        best_score: float = 0.0

        for skill in self._index.all():
            # Score against title + trigger
            combined = f"{skill.title} {skill.trigger}"
            score = _keyword_overlap(task_description, combined)
            if score > best_score:
                best_score = score
                best_skill = skill

        if best_score >= SKILL_MATCH_THRESHOLD and best_skill:
            return best_skill
        return None

    def build_skill_context(self, task_description: str) -> str:
        """Return a system prompt prefix with a matching skill, or empty string."""
        skill = self.find_relevant_skill(task_description)
        if not skill:
            return ""
        return (
            f"[PROVEN SKILL — {skill.title}]\n"
            f"This task matches a skill you have mastered. Follow the proven procedure:\n\n"
            f"{skill.procedure}\n\n"
            f"Verification: {skill.verification}\n"
            f"Success rate: {skill.success_rate:.0%} ({skill.success_count}/{skill.attempt_count} attempts)\n"
            f"[END PROVEN SKILL]\n\n"
        )

    def evaluate_for_skill_creation(self, execution: TaskExecution) -> Optional[Skill]:
        """
        Evaluate whether a completed task warrants skill creation.

        Criteria:
        - Task took >= MIN_TOOL_ROUNDS_FOR_SKILL tool rounds (non-trivial)
        - Task succeeded
        - A similar skill doesn't already exist (or score < threshold)
        - Client is configured (async LLM call not possible here — uses sync urllib)

        Returns a new Skill if criteria are met, else None.
        """
        if not execution.succeeded:
            return None
        if execution.tool_round_count < MIN_TOOL_ROUNDS_FOR_SKILL:
            return None

        # Check if similar skill already exists
        existing = self.find_relevant_skill(execution.task_description)
        if existing and _keyword_overlap(execution.task_description, f"{existing.title} {existing.trigger}") > 0.6:
            # Update existing skill usage stats instead
            self._index.update_usage(existing.id, True, execution.duration_seconds)
            return None

        # Generate skill via LLM (sync path for gateway compatibility)
        if self._client is None:
            return self._heuristic_skill(execution)

        return self._llm_skill(execution)

    def _heuristic_skill(self, execution: TaskExecution) -> Optional[Skill]:
        """
        Heuristic skill creation without LLM. Extracts procedure from tool calls.
        Less polished than LLM-generated but always available.
        """
        tool_names = [tc.get("name", "") for tc in execution.tool_calls_made]
        commands = [
            tc.get("arguments", {}).get("command", "")
            for tc in execution.tool_calls_made
            if tc.get("name") == "terminal" and tc.get("arguments", {}).get("command")
        ]

        if not commands:
            return None

        # Build slug from task description
        slug_words = re.findall(r'\b\w{4,}\b', execution.task_description.lower())[:4]
        skill_id = "-".join(slug_words[:3]) or f"skill-{int(time.time())}"
        skill_id = re.sub(r'[^a-z0-9-]', '', skill_id)

        procedure_steps = []
        for i, cmd in enumerate(commands[:8], 1):
            procedure_steps.append(f"{i}. `{cmd[:200]}`")

        now = datetime.now(timezone.utc).isoformat()
        skill = Skill(
            id=skill_id,
            title=execution.task_description[:80],
            trigger=f"When asked to: {execution.task_description[:100]}",
            procedure="\n".join(procedure_steps),
            verification=f"Task completed in {execution.duration_seconds:.0f}s with result: {execution.result_summary[:200]}",
            failure_modes="(heuristic-generated — update after failures)",
            model_preference=execution.model_used,
            success_count=1,
            attempt_count=1,
            avg_duration_s=execution.duration_seconds,
            created_at=now,
            last_used=now,
        )
        return skill

    def _llm_skill(self, execution: TaskExecution) -> Optional[Skill]:
        """
        LLM-generated skill creation (sync urllib path).
        Calls the DO Inference API to generate a structured SKILL.md.
        """
        api_key  = os.environ.get("DO_INFERENCE_API_KEY", "")
        base_url = os.environ.get("DO_INFERENCE_BASE_URL", "https://inference.do-ai.run/v1")
        if not api_key:
            return self._heuristic_skill(execution)

        tool_summary = "\n".join([
            f"  {tc.get('name','?')}: {json.dumps(tc.get('arguments',{}))[:200]}"
            for tc in execution.tool_calls_made[:10]
        ])

        prompt = (
            f"You are analyzing a completed AI agent task to extract a reusable skill.\n\n"
            f"TASK: {execution.task_description}\n"
            f"RESULT: {execution.result_summary[:300]}\n"
            f"TOOL CALLS ({execution.tool_round_count} rounds):\n{tool_summary}\n"
            f"DURATION: {execution.duration_seconds:.0f}s\n\n"
            "Generate a skill entry as JSON with these exact fields:\n"
            '{"id": "slug-kebab-case", "title": "Short title", "trigger": "When to use this skill", '
            '"procedure": "Step-by-step procedure", "verification": "How to verify success", '
            '"failure_modes": "Common failure patterns to watch for"}\n\n'
            "id must be 2-4 words kebab-case. procedure must be numbered steps. JSON only, no prose."
        )

        try:
            payload = json.dumps({
                "model": self._model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 600,
                "temperature": 0.1,
            }).encode()
            req = urllib.request.Request(
                f"{base_url}/chat/completions",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            content = data["choices"][0]["message"]["content"].strip()

            # Extract JSON
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if not json_match:
                return self._heuristic_skill(execution)
            skill_data = json.loads(json_match.group(0))

            now = datetime.now(timezone.utc).isoformat()
            skill = Skill(
                id=re.sub(r'[^a-z0-9-]', '', skill_data.get("id", f"skill-{int(time.time())}"))[:50],
                title=skill_data.get("title", execution.task_description[:80]),
                trigger=skill_data.get("trigger", f"When asked to: {execution.task_description[:100]}"),
                procedure=skill_data.get("procedure", ""),
                verification=skill_data.get("verification", ""),
                failure_modes=skill_data.get("failure_modes", ""),
                model_preference=execution.model_used,
                success_count=1,
                attempt_count=1,
                avg_duration_s=execution.duration_seconds,
                created_at=now,
                last_used=now,
            )
            return skill

        except Exception as exc:
            print(f"[skill_engine] LLM skill creation failed: {exc}", flush=True)
            return self._heuristic_skill(execution)

    def save_skill(self, skill: Skill) -> str:
        """Save skill to index and write SKILL.md. Returns the skill path."""
        self._index.save_skill(skill)
        skill_path = SKILL_DIR / f"{skill.id}.md"
        print(f"[skill_engine] Learned skill: {skill.title} ({skill_path})", flush=True)
        return str(skill_path)

    def get_skill_index_summary(self, max_skills: int = 20) -> str:
        """Return a compact index of learned skills for the system prompt."""
        skills = self._index.all()
        if not skills:
            return "(no learned skills yet)"
        lines = [f"LEARNED SKILLS ({len(skills)} total):"]
        for skill in sorted(skills, key=lambda s: s.last_used, reverse=True)[:max_skills]:
            sr = f"{skill.success_rate:.0%}"
            lines.append(f"  [{skill.id}] {skill.title} — {sr} success, trigger: {skill.trigger[:60]}")
        return "\n".join(lines)


# ── Singleton ─────────────────────────────────────────────────────────────────

_engine: Optional[SkillEngine] = None

def get_skill_engine(openai_client=None, model: str = "") -> SkillEngine:
    global _engine
    if _engine is None:
        _engine = SkillEngine(openai_client=openai_client, model=model)
    return _engine
