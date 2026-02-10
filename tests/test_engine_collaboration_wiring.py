from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import orchestrator.core.engine as engine_mod
from orchestrator.providers.base import TaskProvider


@dataclass
class _StubProvider(TaskProvider):
    def get_tasks(self) -> list[dict[str, Any]]:
        return []

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        return None

    def get_projects(self) -> list[dict[str, Any]]:
        return []

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
        return "rules"

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


def test_orchestrator_wires_collaboration_manager_into_worker(monkeypatch):
    captured: dict[str, Any] = {}

    class _WM:
        def __init__(self, state_dir, provider, failure_rotation=None, collaboration_manager=None):
            captured["collaboration_manager"] = collaboration_manager
            self.active_workers = {}
            self.pending_workers = set()

        def check_workers(self):
            pass

        def get_worker_status(self):
            return {"busy": False, "current_task": None, "state": "idle"}

        def spawn_worker(self, *args, **kwargs):
            return False

    monkeypatch.setattr(engine_mod, "WorkerManager", _WM)

    orch = engine_mod.Orchestrator(_StubProvider())

    assert captured["collaboration_manager"] is orch.collaboration
