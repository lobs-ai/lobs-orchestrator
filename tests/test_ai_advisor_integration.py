"""Tests for the AI advisor integration in the orchestrator engine."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import orchestrator.core.engine as engine_mod
from orchestrator.core.observer import Opportunity, OpportunityPriority
from orchestrator.providers.base import TaskProvider


# ---------------------------------------------------------------------------
# Shared stub provider (matches the TaskProvider ABC)
# ---------------------------------------------------------------------------


@dataclass
class _StubProvider(TaskProvider):
    _projects: list[dict] | None = None

    def get_tasks(self) -> list[dict[str, Any]]:
        return []

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        return None

    def get_projects(self) -> list[dict[str, Any]]:
        return self._projects or []

    def update_task(self, task_id: str, updates: dict[str, Any]) -> None:
        pass

    def update_worker_status(self, updates: dict[str, Any]) -> None:
        pass

    def check_pending_request(self) -> bool:
        return False

    def consume_request(self) -> None:
        pass

    def sync(self) -> list[dict[str, Any]]:
        return []

    def get_engineering_rules(self) -> str:
        return ""

    def update_project(self, project_id: str, updates: dict[str, Any]) -> None:
        pass

    def add_inbox_item(self, item: dict[str, Any]) -> None:
        pass

    def get_inbox_items(self) -> list[dict[str, Any]]:
        return []

    def update_inbox_item(self, item_id: str, updates: dict[str, Any]) -> None:
        pass

    def get_active_alerts(self) -> list[dict[str, Any]]:
        return []

    def update_alert(self, alert_id: str, updates: dict[str, Any]) -> None:
        pass


# Stub WorkerManager that doesn't need real subprocess infrastructure.
class _WM:
    def __init__(self, state_dir, provider, **kwargs):
        self.active_workers = {}
        self.pending_workers = set()

    def check_workers(self):
        pass

    def get_worker_status(self):
        return {"busy": False, "current_task": None, "state": "idle"}

    def spawn_worker(self, *args, **kwargs):
        return False

    def shutdown(self, **kwargs):
        pass


class _StubMonitor:
    """Stub Monitor that avoids real filesystem dependencies."""

    def __init__(self, provider):
        pass

    def tick(self):
        pass


class _StubReconciler:
    """Stub Reconciler that does nothing."""

    def __init__(self, provider):
        pass

    def reconcile(self, project_ids):
        pass


def _make_orchestrator(monkeypatch, tmp_path, projects=None, settings=None):
    """Create an Orchestrator with common test mocks."""
    monkeypatch.setattr(engine_mod, "WorkerManager", _WM)
    monkeypatch.setattr(engine_mod, "Monitor", _StubMonitor)
    monkeypatch.setattr(engine_mod, "Reconciler", _StubReconciler)
    monkeypatch.setattr(engine_mod, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(engine_mod, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(engine_mod, "CONTROL_REPO_PATH", tmp_path / "control")

    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tasks").mkdir(parents=True, exist_ok=True)
    (tmp_path / "control" / "state" / "agents").mkdir(parents=True, exist_ok=True)
    (tmp_path / "control" / "memory").mkdir(parents=True, exist_ok=True)

    if settings:
        _mock_get_setting = lambda key, default=None: settings.get(key, default)
        monkeypatch.setattr("orchestrator.utils.settings.get_setting", _mock_get_setting)
        # Also patch the imported name in the engine module
        monkeypatch.setattr(engine_mod, "get_setting", _mock_get_setting)

    provider = _StubProvider(_projects=projects or [])
    orch = engine_mod.Orchestrator(provider)
    return orch


# ===================================================================
# _pick_advisor_agent_type — round-robin
# ===================================================================


class TestPickAdvisorAgentType:
    def test_round_robin_cycles_through_all_types(self, monkeypatch, tmp_path):
        orch = _make_orchestrator(monkeypatch, tmp_path)
        expected = ["programmer", "researcher", "reviewer", "writer", "architect"]

        seen = []
        for _ in range(len(expected)):
            seen.append(orch._pick_advisor_agent_type())

        assert seen == expected

    def test_round_robin_wraps_around(self, monkeypatch, tmp_path):
        orch = _make_orchestrator(monkeypatch, tmp_path)

        # Cycle through all 5 types
        for _ in range(5):
            orch._pick_advisor_agent_type()

        # Next should start over at programmer
        assert orch._pick_advisor_agent_type() == "programmer"

    def test_index_persists_across_calls(self, monkeypatch, tmp_path):
        orch = _make_orchestrator(monkeypatch, tmp_path)

        first = orch._pick_advisor_agent_type()
        second = orch._pick_advisor_agent_type()

        assert first == "programmer"
        assert second == "researcher"


# ===================================================================
# _build_project_summaries
# ===================================================================


class TestBuildProjectSummaries:
    def test_basic_summaries(self, monkeypatch, tmp_path):
        # Create a repo with Python indicator
        repo = tmp_path / "my-project"
        repo.mkdir()
        (repo / "requirements.txt").write_text("flask\n")

        projects = [{"id": "my-project", "repoPath": str(repo)}]
        orch = _make_orchestrator(monkeypatch, tmp_path, projects=projects)

        summaries = orch._build_project_summaries(projects)

        assert len(summaries) == 1
        assert summaries[0]["id"] == "my-project"
        assert summaries[0]["has_python"] is True
        assert summaries[0]["has_node"] is False
        assert summaries[0]["has_swift"] is False
        assert summaries[0]["repo_exists"] is True

    def test_node_project(self, monkeypatch, tmp_path):
        repo = tmp_path / "web-app"
        repo.mkdir()
        (repo / "package.json").write_text("{}\n")

        projects = [{"id": "web-app", "repoPath": str(repo)}]
        orch = _make_orchestrator(monkeypatch, tmp_path, projects=projects)

        summaries = orch._build_project_summaries(projects)

        assert summaries[0]["has_node"] is True
        assert summaries[0]["has_python"] is False

    def test_skips_archived_projects(self, monkeypatch, tmp_path):
        projects = [{"id": "old", "archived": True}]
        orch = _make_orchestrator(monkeypatch, tmp_path, projects=projects)

        summaries = orch._build_project_summaries(projects)
        assert summaries == []

    def test_nonexistent_repo(self, monkeypatch, tmp_path):
        projects = [{"id": "ghost", "repoPath": str(tmp_path / "does-not-exist")}]
        orch = _make_orchestrator(monkeypatch, tmp_path, projects=projects)

        summaries = orch._build_project_summaries(projects)

        assert len(summaries) == 1
        assert summaries[0]["repo_exists"] is False
        assert summaries[0]["has_python"] is False


# ===================================================================
# _get_ai_suggestions — config checks and wiring
# ===================================================================


class TestGetAiSuggestions:
    def test_disabled_returns_empty(self, monkeypatch, tmp_path):
        settings = {"proactive": {"enabled": True, "ai_advisor_enabled": False}}
        orch = _make_orchestrator(monkeypatch, tmp_path, settings=settings)

        result = orch._get_ai_suggestions([], [])
        assert result == []

    def test_no_proactive_config_dict_returns_empty(self, monkeypatch, tmp_path):
        settings = {"proactive": "not a dict"}
        orch = _make_orchestrator(monkeypatch, tmp_path, settings=settings)

        result = orch._get_ai_suggestions([], [])
        assert result == []

    def test_daily_limit_enforcement(self, monkeypatch, tmp_path):
        settings = {"proactive": {"ai_advisor_enabled": True, "ai_advisor_max_daily": 2}}
        orch = _make_orchestrator(monkeypatch, tmp_path, settings=settings)

        # Simulate already at daily limit
        orch._ai_advisor_daily_count = 2
        orch._ai_advisor_daily_date = datetime.now(timezone.utc).date().isoformat()

        result = orch._get_ai_suggestions([], [])
        assert result == []

    def test_daily_count_resets_on_new_day(self, monkeypatch, tmp_path):
        settings = {"proactive": {"ai_advisor_enabled": True, "ai_advisor_max_daily": 10}}
        orch = _make_orchestrator(monkeypatch, tmp_path, settings=settings)

        # Set count from yesterday
        orch._ai_advisor_daily_count = 10
        orch._ai_advisor_daily_date = "1999-01-01"

        # Mock the agent_meta to return suggestions
        orch.agent_meta.suggest_proactive_work = MagicMock(return_value=[
            {"project_id": "p", "agent_type": "programmer", "kind": "ai_t", "title": "T", "description": "D", "priority": 3}
        ])
        orch.awareness.format_context_for_agent = MagicMock(return_value="")

        projects = [{"id": "p", "repoPath": str(tmp_path)}]
        result = orch._get_ai_suggestions(projects, [])

        # Daily count should have reset
        assert orch._ai_advisor_daily_count == 1

    def test_converts_dicts_to_opportunity_objects(self, monkeypatch, tmp_path):
        settings = {"proactive": {"ai_advisor_enabled": True}}
        orch = _make_orchestrator(monkeypatch, tmp_path, settings=settings)

        orch.agent_meta.suggest_proactive_work = MagicMock(return_value=[
            {"project_id": "proj", "agent_type": "programmer", "kind": "ai_fix", "title": "Fix stuff", "description": "Desc", "priority": 2}
        ])
        orch.awareness.format_context_for_agent = MagicMock(return_value="context")

        projects = [{"id": "proj", "repoPath": str(tmp_path)}]
        result = orch._get_ai_suggestions(projects, [])

        assert len(result) == 1
        assert isinstance(result[0], Opportunity)
        assert result[0].kind == "ai_fix"
        assert result[0].priority == OpportunityPriority.HIGH
        assert result[0].project_id == "proj"

    def test_passes_existing_kinds_to_advisor(self, monkeypatch, tmp_path):
        settings = {"proactive": {"ai_advisor_enabled": True}}
        orch = _make_orchestrator(monkeypatch, tmp_path, settings=settings)

        orch.agent_meta.suggest_proactive_work = MagicMock(return_value=[])
        orch.awareness.format_context_for_agent = MagicMock(return_value="")

        existing = [
            Opportunity(project_id="p", agent_type="programmer", kind="todo_comment", title="T", description="D", priority=OpportunityPriority.NORMAL),
        ]
        projects = [{"id": "p", "repoPath": str(tmp_path)}]
        orch._get_ai_suggestions(projects, existing)

        # Verify existing_kinds was passed through
        call_kwargs = orch.agent_meta.suggest_proactive_work.call_args
        assert "todo_comment" in call_kwargs.kwargs["existing_kinds"]

    def test_respects_max_per_scan(self, monkeypatch, tmp_path):
        settings = {"proactive": {"ai_advisor_enabled": True, "ai_advisor_max_per_scan": 1}}
        orch = _make_orchestrator(monkeypatch, tmp_path, settings=settings)

        orch.agent_meta.suggest_proactive_work = MagicMock(return_value=[
            {"project_id": "p", "agent_type": "programmer", "kind": "ai_a", "title": "A", "description": "D", "priority": 3},
            {"project_id": "p", "agent_type": "programmer", "kind": "ai_b", "title": "B", "description": "D", "priority": 3},
        ])
        orch.awareness.format_context_for_agent = MagicMock(return_value="")

        projects = [{"id": "p", "repoPath": str(tmp_path)}]
        result = orch._get_ai_suggestions(projects, [])

        assert len(result) == 1

    def test_increments_daily_count(self, monkeypatch, tmp_path):
        settings = {"proactive": {"ai_advisor_enabled": True}}
        orch = _make_orchestrator(monkeypatch, tmp_path, settings=settings)

        orch.agent_meta.suggest_proactive_work = MagicMock(return_value=[
            {"project_id": "p", "agent_type": "programmer", "kind": "ai_t", "title": "T", "description": "D", "priority": 3},
        ])
        orch.awareness.format_context_for_agent = MagicMock(return_value="")

        projects = [{"id": "p", "repoPath": str(tmp_path)}]
        assert orch._ai_advisor_daily_count == 0

        orch._get_ai_suggestions(projects, [])
        assert orch._ai_advisor_daily_count == 1

        orch._get_ai_suggestions(projects, [])
        assert orch._ai_advisor_daily_count == 2


# ===================================================================
# status() output includes AI advisor info
# ===================================================================


class TestStatusOutput:
    def test_status_includes_ai_advisor(self, monkeypatch, tmp_path):
        orch = _make_orchestrator(monkeypatch, tmp_path)

        status = orch.status()

        assert "proactive" in status
        assert "ai_advisor" in status["proactive"]
        ai = status["proactive"]["ai_advisor"]
        assert "enabled" in ai
        assert "daily_count" in ai
        assert "next_agent" in ai
        assert ai["next_agent"] == "programmer"  # starts at index 0
        assert ai["daily_count"] == 0

    def test_status_next_agent_advances(self, monkeypatch, tmp_path):
        orch = _make_orchestrator(monkeypatch, tmp_path)

        # Advance the round-robin
        orch._pick_advisor_agent_type()  # programmer
        orch._pick_advisor_agent_type()  # researcher

        status = orch.status()
        assert status["proactive"]["ai_advisor"]["next_agent"] == "reviewer"


# ===================================================================
# _process_proactive_work integration
# ===================================================================


class TestProactiveWorkIntegration:
    def test_ai_suggestions_merged_with_observer(self, monkeypatch, tmp_path):
        """AI suggestions get merged into the opportunity list."""
        settings = {"proactive": {"enabled": True, "ai_advisor_enabled": True}}
        orch = _make_orchestrator(monkeypatch, tmp_path, settings=settings)

        # Mock observer to return one deterministic opportunity
        det_opp = Opportunity(
            project_id="p", agent_type="programmer", kind="todo_comment",
            title="Resolve TODO", description="D", priority=OpportunityPriority.NORMAL,
            score=40.0,
        )
        orch.observer.scan_for_opportunities = MagicMock(return_value=[det_opp])

        # Mock AI advisor to return one AI opportunity
        ai_opp = Opportunity(
            project_id="p", agent_type="programmer", kind="ai_fix_auth",
            title="Fix auth", description="D", priority=OpportunityPriority.HIGH,
        )
        orch._get_ai_suggestions = MagicMock(return_value=[ai_opp])

        # Track created tasks
        created_tasks = []
        original_create = orch._create_task_from_opportunity

        def tracking_create(opp):
            created_tasks.append(opp)
            return "fake-id"

        orch._create_task_from_opportunity = tracking_create

        orch._process_proactive_work(
            projects=[{"id": "p"}],
            explicit_work=[],
        )

        # Both opportunities should have been processed
        assert len(created_tasks) == 2
        kinds = {t.kind for t in created_tasks}
        assert "todo_comment" in kinds
        assert "ai_fix_auth" in kinds
