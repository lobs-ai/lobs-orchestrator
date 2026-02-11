"""Tests for escalation Level 2 reviewer failure-analysis wiring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


class FakeProvider:
    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self.alerts: dict[str, dict] = {}
        self.inbox: list[dict] = []

    def get_task(self, task_id: str):
        return self.tasks.get(task_id)

    def update_task(self, task_id: str, updates: dict):
        # In the real provider, update_task merges updates onto existing.
        cur = self.tasks.get(task_id, {"id": task_id})
        cur.update(updates)
        self.tasks[task_id] = cur

    def get_active_alerts(self):
        return [a for a in self.alerts.values() if a.get("status") != "resolved"]

    def update_alert(self, alert_id: str, updates: dict):
        cur = self.alerts.get(alert_id, {"id": alert_id})
        cur.update(updates)
        self.alerts[alert_id] = cur

    def add_inbox_item(self, item: dict):
        self.inbox.append(item)


class DummyChat:
    def send_alert(self, *args, **kwargs):
        return None


@pytest.fixture
def provider(monkeypatch):
    # Patch chat service used by EscalationManager.
    import orchestrator.core.escalation as escalation

    monkeypatch.setattr(escalation, "get_chat_service", lambda: DummyChat())
    return FakeProvider()


def test_level2_spawns_reviewer_task(provider):
    from orchestrator.core.escalation import EscalationManager

    esc = EscalationManager(provider)

    provider.tasks["t1"] = {
        "id": "t1",
        "projectId": "p1",
        "title": "Broken task",
        "notes": "do thing",
        "status": "active",
        "workState": "failed",
        "failureReason": "worker_failed",
    }

    alert = {
        "id": "a1",
        "taskId": "t1",
        "projectId": "p1",
        "errorLog": "Error: 503 Service Unavailable",
        "level": 2,
        "status": "active",
    }

    esc._handle_level_2(alert)

    # Reviewer task created
    reviewer_task_id = provider.alerts["a1"]["reviewerTaskId"]
    assert reviewer_task_id in provider.tasks

    reviewer_task = provider.tasks[reviewer_task_id]
    assert reviewer_task["agentType"] == "reviewer"
    assert reviewer_task["agent"] == "reviewer"
    assert reviewer_task["projectId"] == "p1"
    assert reviewer_task["escalationMeta"]["alertId"] == "a1"
    assert reviewer_task["escalationMeta"]["failedTaskId"] == "t1"
    assert "Failed task JSON" in reviewer_task["notes"]


def test_process_reviewer_result_retry_updates_failed_task(tmp_path: Path, provider, monkeypatch):
    from orchestrator.core.escalation import EscalationManager
    import orchestrator.config as config

    # Point WORKER_RESULTS_DIR at temp dir
    monkeypatch.setattr(config, "WORKER_RESULTS_DIR", tmp_path)

    esc = EscalationManager(provider)

    failed_id = "t2"
    provider.tasks[failed_id] = {
        "id": failed_id,
        "projectId": "p2",
        "title": "Task that failed",
        "notes": "Original notes",
        "status": "active",
        "workState": "failed",
        "retryCount": 0,
    }

    provider.alerts["a2"] = {
        "id": "a2",
        "taskId": failed_id,
        "projectId": "p2",
        "errorLog": "boom",
        "level": 2,
        "status": "waiting_reviewer",
    }

    reviewer_task_id = "review_a2"
    reviewer_task = {
        "id": reviewer_task_id,
        "projectId": "p2",
        "agentType": "reviewer",
        "status": "completed",
        "workState": "completed",
        "escalationMeta": {"alertId": "a2", "failedTaskId": failed_id, "projectId": "p2"},
    }

    # Write reviewer output log containing trailing JSON recommendation
    out = "Some analysis here\n" + json.dumps(
        {"recommendation": "retry", "guidance": "Add missing env var", "rootCause": "missing_env"}
    )
    (tmp_path / f"{reviewer_task_id}.log").write_text(out, encoding="utf-8")

    esc.process_reviewer_result(reviewer_task)

    updated = provider.tasks[failed_id]
    assert updated["retryCount"] == 1
    assert updated["workState"] == "not_started"
    assert updated["status"] == "active"
    assert "Retry #1" in updated["notes"]
    assert "Add missing env var" in updated["notes"]
    assert updated["lastRetryReason"].startswith("reviewer:")

    alert = provider.alerts["a2"]
    assert alert["status"] == "resolved"
