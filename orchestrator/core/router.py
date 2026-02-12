"""orchestrator.core.router

Task Router

LLM-based agent selection with regex fallback.

Uses a cheap LLM call (haiku via the 'suggester' agent) to determine which
agent should handle a task. Falls back to keyword-based regex matching if
the LLM call fails or times out.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from orchestrator.core.registry import AgentRegistry
from orchestrator.utils.settings import get_setting

logger = logging.getLogger(__name__)

VALID_AGENTS = {"programmer", "researcher", "reviewer", "writer", "architect"}

LLM_ROUTER_PROMPT = """\
You are a task router for a multi-agent system. Given a task description, pick which agent should handle it.

Agents:
- programmer: Code implementation, bug fixes, testing, refactoring
- researcher: Investigation, analysis, comparisons, information gathering
- reviewer: Code review, quality assurance, audits
- writer: Documentation, write-ups, summaries, content creation
- architect: System design, technical strategy, planning, restructuring

Task:
---
{task_text}
---

Respond with ONLY valid JSON, nothing else: {{"agent": "<type>", "reason": "<10 words max>"}}"""


@dataclass(frozen=True, slots=True)
class _Rule:
    agent_type: str
    pattern: re.Pattern[str]


def _compile_keywords(words: Iterable[str]) -> re.Pattern[str]:
    escaped = [re.escape(w) for w in words]
    return re.compile(r"\b(?:" + "|".join(escaped) + r")\b", re.IGNORECASE)


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
    """LLM-based router with regex fallback."""

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

        self._validate_agent_type(self.default_agent_type)

    def route(self, task: dict[str, Any]) -> str:
        """Return an agent type for the given task.

        Priority:
        1. Explicit `agent` field on the task
        2. LLM-based routing (cheap haiku call via suggester agent)
        3. Regex keyword fallback
        4. Default (programmer)
        """
        # 1. Explicit agent field
        explicit = (task.get("agent") or "").strip()
        if explicit:
            logger.info(f"[ROUTER] Explicit agent field: {explicit}")
            return self._validate_agent_type(explicit)

        task_text = self._task_text(task)

        # 2. LLM-based routing
        llm_result = self._route_via_llm(task_text)
        if llm_result:
            return self._validate_agent_type(llm_result)

        # 3. Regex fallback
        for rule in self._rules:
            if rule.pattern.search(task_text):
                agent = self._validate_agent_type(rule.agent_type)
                logger.info(f"[ROUTER] Regex fallback matched: {agent}")
                return agent

        # 4. Default
        logger.info(f"[ROUTER] No match, defaulting to {self.default_agent_type}")
        return self._validate_agent_type(self.default_agent_type)

    def _route_via_llm(self, task_text: str) -> str | None:
        """Use the suggester agent (haiku) to determine the right agent."""
        try:
            executable = get_setting("openclaw_executable", "openclaw")
            prompt = LLM_ROUTER_PROMPT.format(task_text=task_text[:2000])

            result = subprocess.run(
                [executable, "agent", "--agent", "suggester", "-m", prompt, "--json"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

            output = (result.stdout or "").strip()
            if not output:
                logger.warning("[ROUTER] LLM returned empty output")
                return None

            # If --json flag worked, parse the wrapper
            try:
                wrapper = json.loads(output)
                if isinstance(wrapper, dict) and "reply" in wrapper:
                    output = wrapper["reply"]
            except json.JSONDecodeError:
                pass  # output is raw text, that's fine

            # Extract JSON from response (may have surrounding text)
            json_match = re.search(r'\{[^}]+\}', output)
            if not json_match:
                logger.warning(f"[ROUTER] No JSON in LLM output: {output[:200]}")
                return None

            parsed = json.loads(json_match.group())
            agent = parsed.get("agent", "").strip().lower()
            reason = parsed.get("reason", "")

            if agent not in VALID_AGENTS:
                logger.warning(f"[ROUTER] LLM returned invalid agent '{agent}'")
                return None

            logger.info(f"[ROUTER] LLM selected '{agent}': {reason}")
            return agent

        except subprocess.TimeoutExpired:
            logger.warning("[ROUTER] LLM routing timed out, falling back to regex")
            return None
        except Exception as e:
            logger.warning(f"[ROUTER] LLM routing failed: {e}")
            return None

    def _validate_agent_type(self, agent_type: str) -> str:
        normalized = agent_type.strip().lower()
        if not normalized:
            raise ValueError("Agent type must be a non-empty string")
        self.registry.get_agent(normalized)
        return normalized

    @staticmethod
    def _task_text(task: dict[str, Any]) -> str:
        """Build searchable text from all task fields including inbox messages."""
        title = task.get("title") or task.get("prompt") or ""
        notes = task.get("notes") or ""
        doc_id = task.get("docId", "")
        messages = task.get("messages", [])
        msg_text = "\n".join(
            f"{m.get('author', 'unknown')}: {m.get('text', '')}"
            for m in messages
        )
        parts = [p for p in [title, notes, doc_id, msg_text] if p]
        return "\n".join(parts).strip()


_default_router = Router()


def route(task: dict[str, Any]) -> str:
    """Module-level convenience wrapper around the default Router."""
    return _default_router.route(task)
