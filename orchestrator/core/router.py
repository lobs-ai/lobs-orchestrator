"""orchestrator.core.router

Task Router

Rule-based agent selection for tasks.

This module inspects a task's title/notes to infer an agent "type" (e.g.
programmer, researcher). If the task explicitly specifies an agent, routing is
skipped.

Routing rules are sourced from docs/ARCHITECTURE.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from orchestrator.core.registry import AgentRegistry


@dataclass(frozen=True, slots=True)
class _Rule:
    agent_type: str
    pattern: re.Pattern[str]


def _compile_keywords(words: Iterable[str]) -> re.Pattern[str]:
    # Word-boundary match on any keyword, case-insensitive.
    # NOTE: We intentionally keep this simple/transparent (no stemming).
    escaped = [re.escape(w) for w in words]
    return re.compile(r"\b(?:" + "|".join(escaped) + r")\b", re.IGNORECASE)


# Order matters: first match wins.
_DEFAULT_RULES: tuple[_Rule, ...] = (
    _Rule(
        agent_type="programmer",
        pattern=_compile_keywords(["fix", "bug", "implement", "add", "update", "refactor"]),
    ),
    _Rule(
        agent_type="researcher",
        pattern=_compile_keywords(["research", "investigate", "explore", "analyze"]),
    ),
    _Rule(
        agent_type="reviewer",
        pattern=_compile_keywords(["review", "check", "audit"]),
    ),
    _Rule(
        agent_type="writer",
        pattern=_compile_keywords(["write", "draft", "document", "doc"]),
    ),
    _Rule(
        agent_type="architect",
        pattern=_compile_keywords(["design", "architect", "rework", "restructure"]),
    ),
)


class Router:
    """Rule-based router that selects an agent type for a task."""

    def __init__(
        self,
        registry: Optional[AgentRegistry] = None,
        rules: Optional[Iterable[tuple[str, re.Pattern[str]]]] = None,
        default_agent_type: str = "programmer",
    ):
        self.registry = registry or AgentRegistry()
        self.default_agent_type = default_agent_type

        if rules is None:
            self._rules: tuple[_Rule, ...] = _DEFAULT_RULES
        else:
            self._rules = tuple(_Rule(agent_type=a, pattern=p) for a, p in rules)

        # Validate the configured default exists.
        self._validate_agent_type(self.default_agent_type)

    def route(self, task: dict[str, Any]) -> str:
        """Return an agent type for the given task.

        Behavior:
        - If task contains a non-empty explicit `agent` field, that is used.
        - Otherwise, rules are matched against task title + notes.
        - Defaults to 'programmer' when no rule matches.

        The returned agent type is validated against AgentRegistry.
        """

        explicit = (task.get("agent") or "").strip()
        if explicit:
            return self._validate_agent_type(explicit)

        # Check inbox response messages for explicit agent mentions
        # e.g. "architect needs to do it", "send to researcher"
        mentioned_agent = self._extract_explicit_agent_from_messages(task)
        if mentioned_agent:
            return self._validate_agent_type(mentioned_agent)

        haystack = self._task_text(task)
        for rule in self._rules:
            if rule.pattern.search(haystack):
                return self._validate_agent_type(rule.agent_type)

        return self._validate_agent_type(self.default_agent_type)

    def _validate_agent_type(self, agent_type: str) -> str:
        normalized = agent_type.strip().lower()
        if not normalized:
            raise ValueError("Agent type must be a non-empty string")

        # Uses registry as the source of truth.
        self.registry.get_agent(normalized)
        return normalized

    # Explicit agent mention pattern: "architect should", "send to researcher", etc.
    _AGENT_MENTION_RE = re.compile(
        r"\b(programmer|researcher|reviewer|writer|architect)\b\s+"
        r"(?:should|needs?\s+to|can|will|do|handle|take)",
        re.IGNORECASE,
    )

    def _extract_explicit_agent_from_messages(self, task: dict[str, Any]) -> str | None:
        """Check inbox response messages for explicit agent mentions like 'architect needs to do it'."""
        messages = task.get("messages", [])
        for msg in messages:
            text = msg.get("text", "")
            m = self._AGENT_MENTION_RE.search(text)
            if m:
                return m.group(1).lower()
        return None

    @staticmethod
    def _task_text(task: dict[str, Any]) -> str:
        title = task.get("title") or task.get("prompt") or ""
        notes = task.get("notes") or ""
        # Include inbox response messages in routing text
        messages = task.get("messages", [])
        msg_text = "\n".join(m.get("text", "") for m in messages)
        return f"{title}\n{notes}\n{msg_text}".strip()


# Convenience singleton for call-sites that don't want to manage an instance.
_default_router = Router()


def route(task: dict[str, Any]) -> str:
    """Module-level convenience wrapper around the default Router."""

    return _default_router.route(task)
