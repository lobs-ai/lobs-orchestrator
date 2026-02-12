from __future__ import annotations

from orchestrator.core.governance import GovernanceManager


class FakeProvider:
    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self.inbox: list[dict] = []
        self.alerts: dict[str, dict] = {}

    def get_tasks(self):
        return list(self.tasks.values())

    def get_task(self, task_id: str):
        return self.tasks.get(task_id)

    def get_projects(self):
        return []

    def update_task(self, task_id: str, updates: dict):
        cur = self.tasks.get(task_id, {"id": task_id})
        cur.update(updates)
        self.tasks[task_id] = cur

    def update_worker_status(self, updates: dict):
        return None

    def check_pending_request(self):
        return False

    def consume_request(self):
        return None

    def sync(self):
        return []

    def get_engineering_rules(self):
        return ""

    def update_project(self, project_id: str, updates: dict):
        return None

    def add_inbox_item(self, item: dict):
        self.inbox.append(item)

    def get_inbox_items(self):
        return list(self.inbox)

    def update_inbox_item(self, item_id: str, updates: dict):
        return None

    def get_active_alerts(self):
        return [a for a in self.alerts.values() if a.get("status") != "resolved"]

    def update_alert(self, alert_id: str, updates: dict):
        cur = self.alerts.get(alert_id, {"id": alert_id})
        cur.update(updates)
        self.alerts[alert_id] = cur


def test_assess_task_tiers():
    provider = FakeProvider()
    gm = GovernanceManager(provider)

    local = gm.assess_task({"title": "Fix flaky test in module"})
    assert local.tier == 1

    cross = gm.assess_task({"title": "Update public API contract for auth schema"})
    assert cross.tier >= 2

    strategic = gm.assess_task({"title": "Change orchestrator autonomy boundaries"})
    assert strategic.tier == 3


def test_tier2_gate_creates_architect_task():
    provider = FakeProvider()
    gm = GovernanceManager(provider)
    task = {
        "id": "task-1",
        "title": "Refactor public API for service",
        "notes": "Changes API contract",
    }
    provider.tasks["task-1"] = task.copy()

    allow, assessment, reason = gm.evaluate_and_gate(provider.tasks["task-1"], "proj-a")
    assert allow is False
    assert assessment.tier == 2
    assert "awaiting" in reason

    updated = provider.get_task("task-1")
    assert updated["reviewState"] == "needs_architect_approval"
    approval_id = updated["governance"]["approvalTaskId"]
    approval_task = provider.get_task(approval_id)
    assert approval_task is not None
    assert approval_task["agent"] == "architect"


def test_tier3_gate_creates_human_request():
    provider = FakeProvider()
    gm = GovernanceManager(provider)
    task = {
        "id": "task-3",
        "title": "Revise orchestrator governance strategy and autonomy rules",
    }
    provider.tasks["task-3"] = task.copy()
    allow, assessment, _reason = gm.evaluate_and_gate(provider.tasks["task-3"], "proj-z")
    assert allow is False
    assert assessment.tier == 3
    assert provider.get_task("task-3")["reviewState"] == "needs_human_approval"
    assert len(provider.inbox) == 1
    assert provider.inbox[0]["type"] == "approval_required"


def test_approval_completion_unblocks_target():
    provider = FakeProvider()
    gm = GovernanceManager(provider)
    provider.tasks["target-1"] = {
        "id": "target-1",
        "title": "Cross-module change",
        "governance": {"approvalTaskId": "approve-1", "status": "awaiting_architect_approval"},
        "reviewState": "needs_architect_approval",
    }
    approval_task = {
        "id": "approve-1",
        "governance": {"approvalForTaskId": "target-1"},
    }
    gm.process_approval_task_completion(approval_task)
    updated = provider.get_task("target-1")
    assert updated["reviewState"] == "approved"
    assert updated["governance"]["approvedByArchitectAt"]


def test_wrongness_pause_and_dashboard_metrics():
    provider = FakeProvider()
    gm = GovernanceManager(provider)
    for i in range(6):
        gm.record_task_outcome(
            task_id=f"t{i}",
            project_id="p",
            success=True,
            attempts=1,
            duration_seconds=10,
            failure_reason="",
            error_log="",
        )
    for i in range(2):
        gm.record_task_outcome(
            task_id=f"rb{i}",
            project_id="p",
            success=False,
            attempts=2,
            duration_seconds=8,
            failure_reason="rollback_after_failure",
            error_log="merge conflict rollback",
        )
    assert gm.should_pause_proactive() is True

    metrics = gm.get_dashboard_metrics()
    assert "executionQuality" in metrics
    assert "stability" in metrics
    assert "strategicEffectiveness" in metrics


def test_inbox_response_approves_pending_task():
    provider = FakeProvider()
    gm = GovernanceManager(provider)
    provider.tasks["task-approve"] = {
        "id": "task-approve",
        "title": "Needs approval",
        "reviewState": "needs_human_approval",
        "governance": {"status": "awaiting_human_approval"},
    }
    item = {
        "id": "resp-1",
        "kind": "inbox_response",
        "messages": [{"author": "rafe", "text": "Approved. Please proceed with task-approve"}],
    }
    handled = gm.process_inbox_response(item)
    assert handled is True
    updated = provider.tasks["task-approve"]
    assert updated["reviewState"] == "approved"
    assert updated["governance"]["humanApprovedAt"]


def test_inbox_response_rejects_pending_task():
    provider = FakeProvider()
    gm = GovernanceManager(provider)
    provider.tasks["task-reject"] = {
        "id": "task-reject",
        "title": "Needs approval",
        "reviewState": "needs_human_approval",
        "governance": {"status": "awaiting_human_approval"},
    }
    item = {
        "id": "resp-2",
        "kind": "inbox_response",
        "lastMessage": "I reject this change for now. Do not approve task-reject",
    }
    handled = gm.process_inbox_response(item)
    assert handled is True
    updated = provider.tasks["task-reject"]
    assert updated["reviewState"] == "rejected"
    assert updated["workState"] == "blocked"
