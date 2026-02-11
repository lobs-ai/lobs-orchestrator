"""Tests for AgentMetaBrain — especially the AI-powered proactive work advisor."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.services.agent_meta import (
    ADVISOR_SEED_GUIDANCE,
    DEFAULT_ADVISOR_INTERVAL,
    AgentMetaBrain,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_brain(
    *,
    ollama_available: bool = False,
    ollama_response: str | None = None,
    haiku_available: bool = False,
    haiku_response: str | None = None,
    advisor_interval: int = 0,
) -> AgentMetaBrain:
    """Create an AgentMetaBrain with mocked backends."""
    ollama = MagicMock()
    ollama.model = "test-model"
    ollama.is_available.return_value = ollama_available
    if ollama_response is not None:
        ollama.generate.return_value = {"response": ollama_response}
    else:
        ollama.generate.side_effect = RuntimeError("no model")

    tracker = MagicMock()
    memory = MagicMock()
    memory.get_agent_context.return_value = ""
    memory.load_memory.return_value = ""
    memory.load_evolved_traits.return_value = ""

    brain = AgentMetaBrain(
        ollama=ollama,
        agent_tracker=tracker,
        agent_memory=memory,
        advisor_interval=advisor_interval,
    )

    # Patch Haiku availability
    if not haiku_available:
        brain._haiku_response = None
    else:
        brain._haiku_response = haiku_response

    return brain


def _sample_project_summaries() -> list[dict]:
    return [
        {"id": "flock", "has_python": True, "has_node": False, "has_swift": False, "repo_exists": True},
        {"id": "lobs-dashboard", "has_python": False, "has_node": True, "has_swift": False, "repo_exists": True},
    ]


# ===================================================================
# Constants & configuration
# ===================================================================


class TestAdvisorConstants:
    def test_seed_guidance_covers_all_agent_types(self):
        expected = {"programmer", "researcher", "reviewer", "writer", "architect"}
        assert set(ADVISOR_SEED_GUIDANCE.keys()) == expected

    def test_seed_guidance_values_non_empty(self):
        for agent_type, guidance in ADVISOR_SEED_GUIDANCE.items():
            assert isinstance(guidance, str)
            assert len(guidance) > 20, f"Guidance for {agent_type} is too short"

    def test_default_advisor_interval(self):
        assert DEFAULT_ADVISOR_INTERVAL == 900


# ===================================================================
# _parse_advisor_suggestions
# ===================================================================


class TestParseAdvisorSuggestions:
    def _brain(self) -> AgentMetaBrain:
        return _make_brain()

    def test_parses_valid_single_line(self):
        brain = self._brain()
        raw = "KIND: ai_fix_auth | PRIORITY: HIGH | PROJECT: flock | TITLE: Fix auth module | DESCRIPTION: The auth module needs work"
        valid = {"flock", "lobs-dashboard"}

        result = brain._parse_advisor_suggestions(raw, "programmer", valid)

        assert len(result) == 1
        assert result[0]["kind"] == "ai_fix_auth"
        assert result[0]["priority"] == 2  # HIGH
        assert result[0]["project_id"] == "flock"
        assert result[0]["title"] == "Fix auth module"
        assert result[0]["description"] == "The auth module needs work"
        assert result[0]["agent_type"] == "programmer"

    def test_parses_multiple_lines(self):
        brain = self._brain()
        raw = (
            "KIND: ai_review_changes | PRIORITY: NORMAL | PROJECT: flock | TITLE: Review recent changes | DESCRIPTION: Check quality\n"
            "KIND: ai_add_tests | PRIORITY: HIGH | PROJECT: lobs-dashboard | TITLE: Add integration tests | DESCRIPTION: Missing coverage"
        )
        valid = {"flock", "lobs-dashboard"}

        result = brain._parse_advisor_suggestions(raw, "reviewer", valid)
        assert len(result) == 2

    def test_caps_at_three_suggestions(self):
        brain = self._brain()
        lines = []
        for i in range(5):
            lines.append(f"KIND: ai_task_{i} | PRIORITY: NORMAL | PROJECT: flock | TITLE: Task {i} | DESCRIPTION: Desc {i}")
        raw = "\n".join(lines)
        valid = {"flock"}

        result = brain._parse_advisor_suggestions(raw, "programmer", valid)
        assert len(result) == 3

    def test_returns_empty_for_none_response(self):
        brain = self._brain()
        assert brain._parse_advisor_suggestions("NONE", "programmer", {"flock"}) == []
        assert brain._parse_advisor_suggestions("none", "programmer", {"flock"}) == []
        assert brain._parse_advisor_suggestions("", "programmer", {"flock"}) == []
        assert brain._parse_advisor_suggestions(None, "programmer", {"flock"}) == []

    def test_skips_invalid_project(self):
        brain = self._brain()
        raw = "KIND: ai_task | PRIORITY: NORMAL | PROJECT: unknown-proj | TITLE: Do thing | DESCRIPTION: Desc"
        valid = {"flock"}

        result = brain._parse_advisor_suggestions(raw, "programmer", valid)
        assert result == []

    def test_skips_missing_required_fields(self):
        brain = self._brain()
        # Missing TITLE
        raw = "KIND: ai_task | PRIORITY: NORMAL | PROJECT: flock | DESCRIPTION: Desc"
        valid = {"flock"}

        result = brain._parse_advisor_suggestions(raw, "programmer", valid)
        assert result == []

    def test_adds_ai_prefix_if_missing(self):
        brain = self._brain()
        raw = "KIND: fix_bug | PRIORITY: NORMAL | PROJECT: flock | TITLE: Fix bug | DESCRIPTION: Desc"
        valid = {"flock"}

        result = brain._parse_advisor_suggestions(raw, "programmer", valid)
        assert len(result) == 1
        assert result[0]["kind"] == "ai_fix_bug"

    def test_preserves_existing_ai_prefix(self):
        brain = self._brain()
        raw = "KIND: ai_fix_bug | PRIORITY: NORMAL | PROJECT: flock | TITLE: Fix bug | DESCRIPTION: Desc"
        valid = {"flock"}

        result = brain._parse_advisor_suggestions(raw, "programmer", valid)
        assert result[0]["kind"] == "ai_fix_bug"

    def test_maps_priority_levels(self):
        brain = self._brain()
        valid = {"flock"}

        for prio_str, expected_val in [("URGENT", 1), ("HIGH", 2), ("NORMAL", 3), ("LOW", 4)]:
            raw = f"KIND: ai_t | PRIORITY: {prio_str} | PROJECT: flock | TITLE: T | DESCRIPTION: D"
            result = brain._parse_advisor_suggestions(raw, "programmer", valid)
            assert result[0]["priority"] == expected_val, f"Failed for {prio_str}"

    def test_defaults_to_normal_for_unknown_priority(self):
        brain = self._brain()
        raw = "KIND: ai_t | PRIORITY: MEGA | PROJECT: flock | TITLE: T | DESCRIPTION: D"
        result = brain._parse_advisor_suggestions(raw, "programmer", {"flock"})
        assert result[0]["priority"] == 3  # NORMAL

    def test_truncates_long_title_and_description(self):
        brain = self._brain()
        long_title = "A" * 500
        long_desc = "B" * 1000
        raw = f"KIND: ai_t | PRIORITY: NORMAL | PROJECT: flock | TITLE: {long_title} | DESCRIPTION: {long_desc}"
        result = brain._parse_advisor_suggestions(raw, "programmer", {"flock"})

        assert len(result[0]["title"]) <= 200
        assert len(result[0]["description"]) <= 500

    def test_skips_garbage_lines(self):
        brain = self._brain()
        raw = (
            "Here are my suggestions:\n"
            "KIND: ai_t | PRIORITY: NORMAL | PROJECT: flock | TITLE: Valid | DESCRIPTION: D\n"
            "Thank you for asking!\n"
        )
        result = brain._parse_advisor_suggestions(raw, "programmer", {"flock"})
        assert len(result) == 1
        assert result[0]["title"] == "Valid"

    def test_uses_title_as_fallback_description(self):
        brain = self._brain()
        raw = "KIND: ai_t | PRIORITY: NORMAL | PROJECT: flock | TITLE: Do the thing | DESCRIPTION: "
        result = brain._parse_advisor_suggestions(raw, "programmer", {"flock"})
        assert len(result) == 1
        # Empty description falls back to title
        assert result[0]["description"] == "Do the thing"


# ===================================================================
# _build_advisor_prompt
# ===================================================================


class TestBuildAdvisorPrompt:
    def test_prompt_contains_all_context_blocks(self):
        brain = _make_brain()
        brain._memory.get_agent_context.return_value = "Some memory content"

        prompt = brain._build_advisor_prompt(
            agent_type="programmer",
            awareness_context="System is idle, no active work.",
            agent_capabilities=["code", "debug"],
            agent_proactive=["scan_todos", "fix_warnings"],
            project_summaries=_sample_project_summaries(),
            existing_kinds={"todo_comment", "unpinned_dependencies"},
        )

        assert "## System State" in prompt
        assert "System is idle" in prompt
        assert "## Agent Memory" in prompt
        assert "## Projects" in prompt
        assert "flock" in prompt
        assert "lobs-dashboard" in prompt
        assert "## Agent Profile" in prompt
        assert "code" in prompt
        assert "scan_todos" in prompt

    def test_prompt_includes_seed_guidance(self):
        brain = _make_brain()

        for agent_type, seed in ADVISOR_SEED_GUIDANCE.items():
            prompt = brain._build_advisor_prompt(
                agent_type=agent_type,
                awareness_context="",
                agent_capabilities=[],
                agent_proactive=[],
                project_summaries=[{"id": "test", "has_python": True, "has_node": False, "has_swift": False}],
                existing_kinds=set(),
            )
            # At least part of the seed guidance should appear (it's truncated to 400 chars)
            assert "Look for:" in prompt

    def test_prompt_mentions_existing_kinds(self):
        brain = _make_brain()

        prompt = brain._build_advisor_prompt(
            agent_type="programmer",
            awareness_context="",
            agent_capabilities=[],
            agent_proactive=[],
            project_summaries=[{"id": "p", "has_python": True, "has_node": False, "has_swift": False}],
            existing_kinds={"todo_comment", "stale_docs"},
        )

        assert "todo_comment" in prompt
        assert "stale_docs" in prompt

    def test_prompt_includes_output_format(self):
        brain = _make_brain()

        prompt = brain._build_advisor_prompt(
            agent_type="programmer",
            awareness_context="",
            agent_capabilities=[],
            agent_proactive=[],
            project_summaries=[{"id": "p", "has_python": True, "has_node": False, "has_swift": False}],
            existing_kinds=set(),
        )

        assert "KIND:" in prompt
        assert "PRIORITY:" in prompt
        assert "PROJECT:" in prompt
        assert "TITLE:" in prompt
        assert "DESCRIPTION:" in prompt
        assert "NONE" in prompt

    def test_prompt_caps_awareness_context(self):
        brain = _make_brain()
        long_context = "X" * 2000

        prompt = brain._build_advisor_prompt(
            agent_type="programmer",
            awareness_context=long_context,
            agent_capabilities=[],
            agent_proactive=[],
            project_summaries=[{"id": "p", "has_python": True, "has_node": False, "has_swift": False}],
            existing_kinds=set(),
        )

        # The context block is capped at 800 chars
        # The full 2000-char string should NOT appear
        assert "X" * 2000 not in prompt
        assert "X" * 800 in prompt

    def test_prompt_language_detection(self):
        brain = _make_brain()

        prompt = brain._build_advisor_prompt(
            agent_type="programmer",
            awareness_context="",
            agent_capabilities=[],
            agent_proactive=[],
            project_summaries=[
                {"id": "py-proj", "has_python": True, "has_node": False, "has_swift": False},
                {"id": "node-proj", "has_python": False, "has_node": True, "has_swift": False},
                {"id": "swift-proj", "has_python": False, "has_node": False, "has_swift": True},
                {"id": "empty-proj", "has_python": False, "has_node": False, "has_swift": False},
            ],
            existing_kinds=set(),
        )

        assert "py-proj (Python)" in prompt
        assert "node-proj (Node)" in prompt
        assert "swift-proj (Swift)" in prompt
        assert "empty-proj (unknown)" in prompt


# ===================================================================
# suggest_proactive_work
# ===================================================================


class TestSuggestProactiveWork:
    def test_returns_empty_when_no_model(self):
        brain = _make_brain(ollama_available=False, haiku_available=False)

        result = brain.suggest_proactive_work(
            agent_type="programmer",
            awareness_context="idle",
            agent_capabilities=[],
            agent_proactive=[],
            project_summaries=_sample_project_summaries(),
            existing_kinds=set(),
        )

        assert result == []

    def test_returns_suggestions_from_ollama(self):
        response = "KIND: ai_fix_auth | PRIORITY: HIGH | PROJECT: flock | TITLE: Fix auth module | DESCRIPTION: Auth needs work"
        brain = _make_brain(ollama_available=True, ollama_response=response)

        result = brain.suggest_proactive_work(
            agent_type="programmer",
            awareness_context="idle",
            agent_capabilities=[],
            agent_proactive=[],
            project_summaries=_sample_project_summaries(),
            existing_kinds=set(),
        )

        assert len(result) == 1
        assert result[0]["kind"] == "ai_fix_auth"
        assert result[0]["project_id"] == "flock"

    def test_rate_limits_per_agent_type(self):
        response = "KIND: ai_t | PRIORITY: NORMAL | PROJECT: flock | TITLE: T | DESCRIPTION: D"
        brain = _make_brain(ollama_available=True, ollama_response=response, advisor_interval=300)

        kwargs = dict(
            awareness_context="idle",
            agent_capabilities=[],
            agent_proactive=[],
            project_summaries=_sample_project_summaries(),
            existing_kinds=set(),
        )

        # First call succeeds
        first = brain.suggest_proactive_work(agent_type="programmer", **kwargs)
        assert len(first) == 1

        # Second call for same agent type is rate-limited
        second = brain.suggest_proactive_work(agent_type="programmer", **kwargs)
        assert second == []

        # Different agent type is NOT rate-limited
        third = brain.suggest_proactive_work(agent_type="reviewer", **kwargs)
        assert len(third) == 1

    def test_rate_limit_expires(self):
        response = "KIND: ai_t | PRIORITY: NORMAL | PROJECT: flock | TITLE: T | DESCRIPTION: D"
        brain = _make_brain(ollama_available=True, ollama_response=response, advisor_interval=1)

        kwargs = dict(
            awareness_context="idle",
            agent_capabilities=[],
            agent_proactive=[],
            project_summaries=_sample_project_summaries(),
            existing_kinds=set(),
        )

        first = brain.suggest_proactive_work(agent_type="programmer", **kwargs)
        assert len(first) == 1

        # Force the rate limit to have expired
        brain._last_advisor_scan["programmer"] = time.time() - 2

        second = brain.suggest_proactive_work(agent_type="programmer", **kwargs)
        assert len(second) == 1

    def test_graceful_on_empty_model_response(self):
        brain = _make_brain(ollama_available=True, ollama_response="")

        result = brain.suggest_proactive_work(
            agent_type="programmer",
            awareness_context="idle",
            agent_capabilities=[],
            agent_proactive=[],
            project_summaries=_sample_project_summaries(),
            existing_kinds=set(),
        )

        assert result == []

    def test_graceful_on_none_response(self):
        """When Ollama returns None (model failure), _generate returns None."""
        brain = _make_brain(ollama_available=True)
        brain._ollama.generate.return_value = {"response": ""}

        result = brain.suggest_proactive_work(
            agent_type="programmer",
            awareness_context="idle",
            agent_capabilities=[],
            agent_proactive=[],
            project_summaries=_sample_project_summaries(),
            existing_kinds=set(),
        )

        assert result == []

    @patch("orchestrator.services.agent_meta._call_haiku")
    def test_falls_back_to_haiku(self, mock_haiku):
        """When Ollama is unavailable, falls back to Haiku."""
        haiku_response = "KIND: ai_review | PRIORITY: NORMAL | PROJECT: flock | TITLE: Review code | DESCRIPTION: Review recent"
        mock_haiku.return_value = haiku_response

        brain = _make_brain(ollama_available=False)

        with patch("orchestrator.services.agent_meta._haiku_available", return_value=True):
            result = brain.suggest_proactive_work(
                agent_type="reviewer",
                awareness_context="idle",
                agent_capabilities=[],
                agent_proactive=[],
                project_summaries=_sample_project_summaries(),
                existing_kinds=set(),
            )

        assert len(result) == 1
        assert result[0]["kind"] == "ai_review"
