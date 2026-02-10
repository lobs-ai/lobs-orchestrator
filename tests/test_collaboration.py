from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from orchestrator.core.collaboration import CollaborationManager
from orchestrator.providers.base import TaskProvider


@dataclass
class _StubProvider(TaskProvider):
    tasks: dict[str, dict[str, Any]] = field(default_factory=dict)
    inbox: list[dict[str, Any]] = field(default_factory=list)

    def get_tasks(self) -> list[dict[str, Any]]:
        return list(self.tasks.values())

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        return self.tasks.get(task_id)

    def get_projects(self) -> list[dict[str, Any]]:
        return []

    def update_task(self, task_id: str, updates: dict[str, Any]) -> None:
        # For create flows, updates is the full task dict.
        self.tasks[task_id] = dict(updates)

    def update_worker_status(self, updates: dict[str, Any]) -> None:
        pass

    def check_pending_request(self) -> bool:
        return False

    def consume_request(self) -> None:
        pass

    def sync(self) -> list[dict[str, Any]]:
        return []

    def get_engineering_rules(self) -> str:
        return "Standard engineering practices."

    def update_project(self, project_id: str, updates: dict[str, Any]) -> None:
        pass

    def add_inbox_item(self, item: dict[str, Any]) -> None:
        self.inbox.append(dict(item))

    def get_inbox_items(self) -> list[dict[str, Any]]:
        return list(self.inbox)

    def update_inbox_item(self, item_id: str, updates: dict[str, Any]) -> None:
        pass

    def get_active_alerts(self) -> list[dict[str, Any]]:
        return []

    def update_alert(self, alert_id: str, updates: dict[str, Any]) -> None:
        pass


def test_process_handoff_creates_task_and_records_state(tmp_path):
    provider = _StubProvider()
    state_path = tmp_path / "collab.json"
    mgr = CollaborationManager(provider, state_path=state_path)

    payload = {
        "from": "architect",
        "to": "programmer",
        "initiative": "user-auth-system",
        "work": {
            "title": "Implement JWT middleware",
            "context": "See design doc at docs/auth-design.md",
            "acceptance": "Validates tokens per spec",
        },
        "projectId": "lobs-orchestrator",
    }

    new_id = mgr.process_handoff(payload, parent_task_id="PARENT")

    assert new_id in provider.tasks
    task = provider.tasks[new_id]
    assert task["agent"] == "programmer"
    assert task["initiative"] == "user-auth-system"
    assert task["projectId"] == "lobs-orchestrator"
    assert "Acceptance" in (task.get("notes") or "")
    assert task["collaboration"]["parentTaskId"] == "PARENT"

    # State file written and grouped by initiative.
    assert state_path.exists()
    initiatives = mgr.initiatives()
    assert "user-auth-system" in initiatives
    assert initiatives["user-auth-system"]["tasks"][0]["id"] == new_id


def test_process_handoff_validates_payload(tmp_path):
    provider = _StubProvider()
    mgr = CollaborationManager(provider, state_path=tmp_path / "state.json")

    with pytest.raises(ValueError):
        mgr.process_handoff({"from": "architect"})

    with pytest.raises(ValueError):
        mgr.process_handoff(
            {
                "from": "architect",
                "to": "nobody",
                "initiative": "x",
                "work": {"title": "t"},
            }
        )
