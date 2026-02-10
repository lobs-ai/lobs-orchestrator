from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from orchestrator.core.worker import WorkerManager
from orchestrator.core.collaboration import CollaborationManager
from orchestrator.providers.base import TaskProvider


@dataclass
class _StubProvider(TaskProvider):
    tasks: dict[str, dict[str, Any]] = field(default_factory=dict)

    def get_tasks(self) -> list[dict[str, Any]]:
        return list(self.tasks.values())

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        return self.tasks.get(task_id)

    def get_projects(self) -> list[dict[str, Any]]:
        return []

    def update_task(self, task_id: str, updates: dict[str, Any]) -> None:
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
        raise NotImplementedError

    def get_inbox_items(self) -> list[dict[str, Any]]:
        return []

    def update_inbox_item(self, item_id: str, updates: dict[str, Any]) -> None:
        raise NotImplementedError

    def get_active_alerts(self) -> list[dict[str, Any]]:
        return []

    def update_alert(self, alert_id: str, updates: dict[str, Any]) -> None:
        raise NotImplementedError


def test_worker_process_handoffs_creates_tasks_and_cleans_directory(tmp_path):
    provider = _StubProvider()
    collab_state = tmp_path / "collab.json"
    collab = CollaborationManager(provider, state_path=collab_state)

    # Create a fake project workspace with a .handoffs file.
    project_path = tmp_path / "proj"
    handoffs_dir = project_path / ".handoffs"
    handoffs_dir.mkdir(parents=True)

    (handoffs_dir / "h1.json").write_text(
        json.dumps(
            {
                "to": "programmer",
                "initiative": "collab-test",
                "title": "Do the thing",
                "context": "Some context",
                "acceptance": "It works",
                "files": ["src/a.py", "README.md"],
            }
        )
    )

    # Construct a minimal WorkerManager instance without running full __init__.
    wm = WorkerManager.__new__(WorkerManager)
    wm.provider = provider
    wm.collaboration = collab
    wm._get_repo_path = lambda project_id: project_path  # type: ignore[assignment]

    WorkerManager._process_handoffs(wm, "PARENT", "proj", "architect")

    # A derived task should be created.
    assert provider.tasks, "expected derived task to be created"
    derived = next(iter(provider.tasks.values()))
    assert derived["initiative"] == "collab-test"
    assert derived["agent"] == "programmer"
    assert derived["collaboration"]["parentTaskId"] == "PARENT"
    assert "Files:" in (derived.get("notes") or "")

    # Handoffs directory should be cleaned up.
    assert not handoffs_dir.exists()
