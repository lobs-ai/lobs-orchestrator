from __future__ import annotations

import json
from datetime import datetime, timezone

from orchestrator.utils.settings import set_setting


def _read_tasks(tasks_dir):
    out = []
    for p in tasks_dir.glob("*.json"):
        out.append(json.loads(p.read_text(encoding="utf-8")))
    return out


def test_recurring_due_creates_task_and_persists_state(temp_control_repo, tmp_path):
    # Arrange
    set_setting(
        "recurring",
        [
            {
                "id": "every-minute",
                "name": "Every minute",
                "schedule": "* * * * *",
                "tz": "UTC",
                "enabled": True,
                "task": {
                    "title": "Ping",
                    "agent": "researcher",
                    "projectId": "lobs-control",
                    "notes": "hello",
                },
            }
        ],
    )

    from orchestrator.config import TASKS_DIR, RECURRING_STATE_FILE
    from orchestrator.core.recurring_scheduler import RecurringScheduler

    scheduler = RecurringScheduler(tasks_dir=TASKS_DIR, state_path=RECURRING_STATE_FILE)

    now = datetime(2026, 1, 1, 0, 0, 30, tzinfo=timezone.utc)

    # Act
    created = scheduler.tick_with_persistence(now_utc=now)

    # Assert
    assert len(created) == 1
    tasks = _read_tasks(TASKS_DIR)
    assert len(tasks) == 1
    t = tasks[0]
    assert t["title"] == "Ping"
    assert t["agent"] == "researcher"
    assert t["projectId"] == "lobs-control"
    assert t["recurringMeta"]["id"] == "every-minute"
    assert t["recurringMeta"]["date"] == "2026-01-01"

    state_path = RECURRING_STATE_FILE
    assert state_path.exists()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["last_run"]["every-minute"].startswith("2026-01-01T00:00")


def test_recurring_prevents_duplicate_same_day(temp_control_repo):
    set_setting(
        "recurring",
        [
            {
                "id": "daily",
                "name": "Daily",
                "schedule": "* * * * *",
                "tz": "UTC",
                "enabled": True,
                "task": {
                    "title": "Daily",
                    "agent": "programmer",
                    "projectId": "lobs-control",
                    "notes": "",
                },
            }
        ],
    )

    from orchestrator.config import TASKS_DIR, RECURRING_STATE_FILE
    from orchestrator.core.recurring_scheduler import RecurringScheduler

    scheduler = RecurringScheduler(tasks_dir=TASKS_DIR, state_path=RECURRING_STATE_FILE)

    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    created1 = scheduler.tick_with_persistence(now_utc=now)
    created2 = scheduler.tick_with_persistence(now_utc=now)

    assert len(created1) == 1
    assert created2 == []
    assert len(_read_tasks(TASKS_DIR)) == 1


def test_recurring_skips_if_task_already_exists_today(temp_control_repo):
    set_setting(
        "recurring",
        [
            {
                "id": "existing",
                "name": "Existing",
                "schedule": "* * * * *",
                "tz": "UTC",
                "enabled": True,
                "task": {
                    "title": "Existing",
                    "agent": "programmer",
                    "projectId": "lobs-control",
                },
            }
        ],
    )

    from orchestrator.config import TASKS_DIR, RECURRING_STATE_FILE
    from orchestrator.core.recurring_scheduler import RecurringScheduler

    # Pre-create a task for the same recurring id+date
    pre = {
        "id": "PRE",
        "title": "Existing",
        "projectId": "lobs-control",
        "status": "active",
        "owner": "lobs",
        "reviewState": "approved",
        "workState": "not_started",
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-01T00:00:00Z",
        "agent": "programmer",
        "recurringMeta": {"id": "existing", "date": "2026-01-01"},
    }
    (TASKS_DIR / "PRE.json").write_text(json.dumps(pre) + "\n", encoding="utf-8")

    scheduler = RecurringScheduler(tasks_dir=TASKS_DIR, state_path=RECURRING_STATE_FILE)

    now = datetime(2026, 1, 1, 0, 0, 30, tzinfo=timezone.utc)
    created = scheduler.tick_with_persistence(now_utc=now)

    assert created == []
    assert len(_read_tasks(TASKS_DIR)) == 1

    # last_run should still be advanced so we don't keep trying.
    state = json.loads(RECURRING_STATE_FILE.read_text(encoding="utf-8"))
    assert "existing" in state.get("last_run", {})
