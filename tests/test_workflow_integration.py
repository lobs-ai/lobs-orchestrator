"""Test that WorkflowEngine is properly integrated into the Orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
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


def test_orchestrator_has_workflow_engine(monkeypatch, tmp_path):
    """Verify that Orchestrator instantiates a WorkflowEngine."""
    
    # Mock WorkerManager to avoid dependencies
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

    monkeypatch.setattr(engine_mod, "WorkerManager", _WM)
    
    # Mock state directory
    monkeypatch.setattr(engine_mod, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(engine_mod, "TASKS_DIR", tmp_path / "tasks")

    orch = engine_mod.Orchestrator(_StubProvider())

    # Verify workflow engine exists
    assert hasattr(orch, "workflow")
    assert orch.workflow is not None
    assert isinstance(orch.workflow, engine_mod.WorkflowEngine)


def test_orchestrator_updates_workflow_state(monkeypatch, tmp_path):
    """Verify that workflow state is updated during run_once."""
    
    # Mock WorkerManager
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

    monkeypatch.setattr(engine_mod, "WorkerManager", _WM)
    
    # Setup test environment
    state_dir = tmp_path / "state"
    tasks_dir = tmp_path / "tasks"
    state_dir.mkdir(parents=True)
    tasks_dir.mkdir(parents=True)
    
    monkeypatch.setattr(engine_mod, "STATE_DIR", state_dir)
    monkeypatch.setattr(engine_mod, "TASKS_DIR", tasks_dir)
    
    # Create a test task with initiative
    import json
    task = {
        "id": "test-task-1",
        "kind": "task",
        "title": "Test Task",
        "initiative": "test-initiative",
        "agent": "programmer",
        "workState": "in_progress",
        "projectId": "test-project",
    }
    (tasks_dir / "test-task-1.json").write_text(json.dumps(task))

    orch = engine_mod.Orchestrator(_StubProvider())
    
    # Force immediate update by setting last update time to 0
    orch._last_workflow_update = 0
    
    # Run one iteration
    orch.run_once()
    
    # Verify workflow state was saved
    workflow_state_path = state_dir / "workflow-state.json"
    assert workflow_state_path.exists()
    
    # Load and verify the snapshot
    snapshot_data = json.loads(workflow_state_path.read_text())
    assert "version" in snapshot_data
    assert "initiatives" in snapshot_data
    assert "test-initiative" in snapshot_data["initiatives"]
    
    init_data = snapshot_data["initiatives"]["test-initiative"]
    assert init_data["total"] == 1
    assert init_data["done"] == 0
    assert init_data["complete"] is False


def test_orchestrator_get_initiative_status(monkeypatch, tmp_path):
    """Verify get_initiative_status returns correct data."""
    
    # Mock WorkerManager
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

    monkeypatch.setattr(engine_mod, "WorkerManager", _WM)
    
    # Setup test environment
    state_dir = tmp_path / "state"
    tasks_dir = tmp_path / "tasks"
    state_dir.mkdir(parents=True)
    tasks_dir.mkdir(parents=True)
    
    monkeypatch.setattr(engine_mod, "STATE_DIR", state_dir)
    monkeypatch.setattr(engine_mod, "TASKS_DIR", tasks_dir)
    
    # Create test tasks
    import json
    tasks = [
        {
            "id": "task1",
            "kind": "task",
            "title": "Task 1",
            "initiative": "auth",
            "agent": "programmer",
            "workState": "completed",
        },
        {
            "id": "task2",
            "kind": "task",
            "title": "Task 2",
            "initiative": "auth",
            "agent": "programmer",
            "workState": "in_progress",
            "collaboration": {"parentTaskId": "task1"},
        },
    ]
    
    for task in tasks:
        (tasks_dir / f"{task['id']}.json").write_text(json.dumps(task))

    orch = engine_mod.Orchestrator(_StubProvider())
    
    # Force update
    orch._last_workflow_update = 0
    orch.run_once()
    
    # Get all initiatives
    all_status = orch.get_initiative_status()
    assert "initiatives" in all_status
    assert "auth" in all_status["initiatives"]
    assert all_status["total_initiatives"] == 1
    
    # Get specific initiative
    auth_status = orch.get_initiative_status("auth")
    assert auth_status["initiative"] == "auth"
    assert auth_status["progress"]["total"] == 2
    assert auth_status["progress"]["done"] == 1
    assert auth_status["progress"]["complete"] is False
