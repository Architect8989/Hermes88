#!/usr/bin/env python3
"""
rhodawk_core/operator_model.py — Honcho-Grade User Modeling (Layer F)

Builds a deepening behavioral model of the operator across sessions.
Infers preferences, working patterns, communication style, and goals
from observed task executions and conversation history.

Unlike a flat profile, this is a probabilistic behavioral model that:
- Infers preferences from behavior, not just explicit statements
- Weights recent observations more than old ones
- Builds confidence over time (more confirmations = higher confidence)
- Automatically surfaces high-confidence observations into the system prompt

Architecture inspired by plastic-labs/honcho dialectic user modeling
but implemented with SQLite (no external dependencies).

Copyright (c) 2024-2025 Rhodawk AI. All rights reserved.
"""

import json
import os
import re
import sqlite3
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Dict, Any


# ── Config ────────────────────────────────────────────────────────────────────

DB_PATH = Path(os.environ.get("HERMES_HOME", "/data/.hermes")) / "sessions" / "conversations.db"

# Categories of observations
OBSERVATION_CATEGORIES = [
    "work_pattern",        # when they work, how they chunk tasks
    "communication",       # preferred verbosity, format preferences
    "decision_style",      # risk tolerance, delegate vs own
    "technical_preferences",  # language/tool/stack choices
    "goals",               # explicit objectives
    "workflow",            # recurring task patterns
    "trust",               # what they delegate vs want to approve
]

# Minimum confidence to include in system prompt
PROMPT_CONFIDENCE_THRESHOLD = 0.65


# ── Schema ────────────────────────────────────────────────────────────────────


def _ensure_schema(db_path: Path = DB_PATH) -> None:
    """Ensure operator_model and related tables exist."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.executescript("""
        CREATE TABLE IF NOT EXISTS operator_model (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id          INTEGER NOT NULL,
            category         TEXT NOT NULL,
            observation      TEXT NOT NULL,
            evidence         TEXT DEFAULT '',
            confidence       REAL DEFAULT 0.7,
            times_confirmed  INTEGER DEFAULT 1,
            created_at       REAL DEFAULT (unixepoch()),
            updated_at       REAL DEFAULT (unixepoch()),
            archived         INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_operator_model_user ON operator_model(user_id, category);

        CREATE TABLE IF NOT EXISTS skill_usage_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            skill_id    TEXT NOT NULL,
            task        TEXT,
            succeeded   INTEGER DEFAULT 1,
            duration_s  REAL DEFAULT 0,
            logged_at   REAL DEFAULT (unixepoch())
        );
        CREATE INDEX IF NOT EXISTS idx_skill_usage ON skill_usage_log(skill_id);
    """)
    con.commit()
    con.close()


# ── Data Models ───────────────────────────────────────────────────────────────


@dataclass
class Observation:
    """A single behavioral observation about the operator."""
    id: Optional[int]
    user_id: int
    category: str
    observation: str
    evidence: str = ""
    confidence: float = 0.7
    times_confirmed: int = 1
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


# ── Operator Model ────────────────────────────────────────────────────────────


class OperatorModel:
    """
    Persistent behavioral model of the operator.

    Stores observations with confidence scores in SQLite.
    Automatically degrades confidence on old unconfirmed observations.
    Surfaces high-confidence observations into the system prompt.
    """

    def __init__(self, user_id: int, db_path: Path = DB_PATH) -> None:
        self.user_id = user_id
        self.db_path = db_path
        _ensure_schema(db_path)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.db_path))

    def add_observation(
        self,
        category: str,
        observation: str,
        evidence: str = "",
        confidence: float = 0.7,
    ) -> None:
        """
        Add a new observation or confirm an existing similar one.
        If a very similar observation already exists, boosts its confidence
        instead of creating a duplicate.
        """
        con = self._connect()
        try:
            # Check for similar existing observation
            rows = con.execute(
                "SELECT id, observation, confidence, times_confirmed FROM operator_model "
                "WHERE user_id=? AND category=? AND archived=0",
                (self.user_id, category),
            ).fetchall()

            # Simple similarity: check if 60%+ of words overlap
            obs_words = set(re.findall(r'\b\w{4,}\b', observation.lower()))
            for row_id, row_obs, row_conf, row_count in rows:
                row_words = set(re.findall(r'\b\w{4,}\b', row_obs.lower()))
                if obs_words and row_words:
                    overlap = len(obs_words & row_words) / len(obs_words | row_words)
                    if overlap >= 0.50:
                        # Boost existing observation
                        new_conf = min(0.98, row_conf + (confidence * 0.15))
                        con.execute(
                            "UPDATE operator_model SET confidence=?, times_confirmed=?, "
                            "updated_at=?, evidence=? WHERE id=?",
                            (new_conf, row_count + 1, time.time(), evidence[:500], row_id),
                        )
                        con.commit()
                        return

            # Insert new observation
            con.execute(
                "INSERT INTO operator_model "
                "(user_id, category, observation, evidence, confidence, times_confirmed, "
                "created_at, updated_at) VALUES (?,?,?,?,?,1,?,?)",
                (self.user_id, category, observation[:500], evidence[:500],
                 confidence, time.time(), time.time()),
            )
            con.commit()
        finally:
            con.close()

    def get_observations(
        self,
        min_confidence: float = 0.0,
        categories: Optional[List[str]] = None,
        limit: int = 50,
    ) -> List[Observation]:
        """Get observations filtered by confidence and category."""
        con = self._connect()
        try:
            query = (
                "SELECT id, user_id, category, observation, evidence, confidence, "
                "times_confirmed, created_at, updated_at FROM operator_model "
                "WHERE user_id=? AND archived=0 AND confidence>=?"
            )
            params: list = [self.user_id, min_confidence]
            if categories:
                placeholders = ",".join("?" * len(categories))
                query += f" AND category IN ({placeholders})"
                params.extend(categories)
            query += " ORDER BY confidence DESC, updated_at DESC LIMIT ?"
            params.append(limit)

            rows = con.execute(query, params).fetchall()
            return [
                Observation(
                    id=r[0], user_id=r[1], category=r[2],
                    observation=r[3], evidence=r[4], confidence=r[5],
                    times_confirmed=r[6], created_at=r[7], updated_at=r[8],
                )
                for r in rows
            ]
        finally:
            con.close()

    def build_prompt_context(self, max_observations: int = 10) -> str:
        """
        Build a compact operator profile for the system prompt.
        Only includes observations above PROMPT_CONFIDENCE_THRESHOLD.
        """
        observations = self.get_observations(
            min_confidence=PROMPT_CONFIDENCE_THRESHOLD,
            limit=max_observations,
        )
        if not observations:
            return "(no operator model built yet — model learns from your interactions)"

        lines = ["OPERATOR BEHAVIORAL MODEL (inferred from sessions):"]
        by_category: Dict[str, List[str]] = {}
        for obs in observations:
            cat = obs.category.replace("_", " ").title()
            by_category.setdefault(cat, []).append(
                f"  • {obs.observation} (confidence: {obs.confidence:.0%}, confirmed {obs.times_confirmed}x)"
            )

        for cat, items in by_category.items():
            lines.append(f"\n{cat}:")
            lines.extend(items[:3])  # Max 3 per category

        return "\n".join(lines)

    def infer_from_task(
        self,
        task: str,
        result: str,
        tool_calls: List[Dict],
        duration_s: float,
        timestamp: float,
    ) -> None:
        """
        Infer behavioral observations from a completed task.
        Called automatically after every task completion.
        """
        # Work pattern: time of day
        hour = datetime.fromtimestamp(timestamp, tz=timezone.utc).hour
        if 21 <= hour or hour <= 5:
            self.add_observation(
                "work_pattern",
                f"Works late at night (task at {hour:02d}:00 UTC)",
                evidence=task[:100],
                confidence=0.5,
            )
        elif 6 <= hour <= 11:
            self.add_observation(
                "work_pattern",
                f"Works in the morning ({hour:02d}:00 UTC)",
                evidence=task[:100],
                confidence=0.5,
            )

        # Technical preferences: detected from commands
        tool_commands = [
            tc.get("arguments", {}).get("command", "")
            for tc in tool_calls
            if tc.get("name") == "terminal"
        ]
        all_commands = " ".join(tool_commands)

        if "typescript" in all_commands.lower() or ".ts" in all_commands:
            self.add_observation(
                "technical_preferences",
                "Prefers TypeScript for new code",
                evidence=task[:100],
                confidence=0.6,
            )
        if "python3" in all_commands or ".py" in all_commands:
            self.add_observation(
                "technical_preferences",
                "Works in Python regularly",
                evidence=task[:100],
                confidence=0.6,
            )
        if "pytest" in all_commands or "unittest" in all_commands:
            self.add_observation(
                "technical_preferences",
                "Expects tests alongside code changes",
                evidence=task[:100],
                confidence=0.7,
            )

        # Task complexity
        if len(tool_calls) >= 5:
            self.add_observation(
                "workflow",
                "Delegates complex multi-step tasks to Hermes",
                evidence=task[:100],
                confidence=0.6,
            )

        # Git usage
        if any("git" in c.lower() for c in tool_commands):
            self.add_observation(
                "workflow",
                "Manages code through git — uses Hermes for commits and pushes",
                evidence=task[:100],
                confidence=0.7,
            )

    def infer_from_explicit(self, message: str) -> None:
        """
        Infer preferences from explicit operator statements.
        E.g. "always include tests", "I prefer concise output", "never ask for confirmation"
        """
        msg_lower = message.lower()

        patterns = [
            (r"always include tests?",          "technical_preferences", "Always include tests with code changes", 0.9),
            (r"never ask.{0,20}confirm",         "trust",                 "Does not want confirmation prompts — YOLO mode", 0.9),
            (r"prefer.{0,20}concise",            "communication",         "Prefers concise output over detailed explanations", 0.85),
            (r"prefer.{0,20}detailed?",          "communication",         "Prefers detailed explanations", 0.85),
            (r"don.t explain.{0,20}code",        "communication",         "Prefers code without explanations", 0.85),
            (r"use python",                      "technical_preferences", "Prefers Python", 0.8),
            (r"use typescript",                  "technical_preferences", "Prefers TypeScript", 0.8),
            (r"push.{0,20}without.{0,20}asking", "trust",                "Push to git without asking for approval", 0.9),
            (r"(most|highest|top).{0,20}priority.{0,30}(is|are)", "goals", message[:150], 0.8),
        ]

        for pattern, category, observation, confidence in patterns:
            if re.search(pattern, msg_lower):
                self.add_observation(category, observation, evidence=message[:200], confidence=confidence)


# ── Singleton Factory ─────────────────────────────────────────────────────────


def get_operator_model(user_id: int) -> OperatorModel:
    """Get or create an OperatorModel for the given user."""
    return OperatorModel(user_id=user_id)
