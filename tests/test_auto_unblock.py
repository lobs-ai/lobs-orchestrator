"""Tests for Monitor.auto_unblock_blocked_tasks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


class FakeProvider:
    def __init__(self, tasks: dict[str, dict]):
        self.tasks = tasks
        self.updated: list[tuple[str, dict]] = []
        self.inbox: list[dict] = []

    def get_task(self, task_id: str):
        return self.tasks.get(task_id)

    def update_task(self, task_id: str, updates: dict):
        cur = self.tasks.get(task_id, {"id": task_id})
        cur.update(updates)
        self.tasks[task_id] = cur
        self.updated.append((task_id, updates))

    def add_inbox_item(self, item: dict):
        self.inbox.append(item)

    # unused
    def get_inbox_items(self):
        return []


@pytest.fixture
def control_repo(tmp_path: Path):
    control = tmp_path / "control"
    (control / "state" / "tasks").mkdir(parents=True)
    (control / "state" / "worker-results").mkdir(parents=True)
    return control


def _write_task(control_repo: Path, task: dict):
    p = control_repo / "state" / "tasks" / f"{task['id']}.json"
    p.write_text(json.dumps(task, indent=2) + "\n", encoding="utf-8")


def test_unblock_when_deps_completed(monkeypatch, control_repo: Path):
    import orchestrator.core.monitor as monitor_mod

    monkeypatch.setattr(monitor_mod, "CONTROL_REPO_PATH", control_repo)

    tasks = {
        "dep": {"id": "dep", "projectId": "p", "workState": "completed", "status": "completed"},
        "t": {
            "id": "t",
            "projectId": "p",
            "title": "blocked",
            "workState": "blocked",
            "status": "active",
            "dependsOn": ["dep"],
        },
    }
    _write_task(control_repo, tasks["t"])

    from orchestrator.core.monitor import Monitor

    m = Monitor(FakeProvider(tasks))
    m.auto_unblock_blocked_tasks()

    assert tasks["t"]["workState"] == "not_started"
    assert tasks["t"]["lastUnblockReason"] == "deps_completed"


def test_unblock_retry_pattern(monkeypatch, control_repo: Path, tmp_path: Path):
    import orchestrator.core.monitor as monitor_mod
    import orchestrator.config as config

    monkeypatch.setattr(monitor_mod, "CONTROL_REPO_PATH", control_repo)
    monkeypatch.setattr(config, "WORKER_RESULTS_DIR", control_repo / "state" / "worker-results")

    tasks = {
        "t": {
            "id": "t",
            "projectId": "p",
            "title": "blocked",
            "workState": "blocked",
            "status": "active",
            "retryCount": 0,
            "notes": "hi",
        }
    }
    _write_task(control_repo, tasks["t"])

    # Write a worker log that matches network_timeout pattern
    (control_repo / "state" / "worker-results" / "t.log").write_text(
        "Error: Connection timeout after 30 seconds\n", encoding="utf-8"
    )

    from orchestrator.core.monitor import Monitor

    m = Monitor(FakeProvider(tasks))
    m.auto_unblock_blocked_tasks()

    assert tasks["t"]["workState"] == "not_started"
    assert tasks["t"]["retryCount"] == 1
    assert tasks["t"]["lastUnblockReason"].startswith("retry:")


def test_unblock_escalates_after_max_attempts(monkeypatch, control_repo: Path):
    import orchestrator.core.monitor as monitor_mod

    monkeypatch.setattr(monitor_mod, "CONTROL_REPO_PATH", control_repo)

    tasks = {
        "t": {
            "id": "t",
            "projectId": "p",
            "title": "blocked",
            "workState": "blocked",
            "status": "active",
            "unblockAttempts": 3,
            "notes": "n",
        }
    }
    _write_task(control_repo, tasks["t"])

    provider = FakeProvider(tasks)

    from orchestrator.core.monitor import Monitor

    m = Monitor(provider)
    m.auto_unblock_blocked_tasks()

    assert provider.inbox, "expected inbox item"
    assert provider.inbox[0]["taskId"] == "t"
