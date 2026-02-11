"""Filesystem-based TaskProvider implementation used by integration tests.

This provider is similar to LocalTaskProvider but allows injecting an arbitrary
control repo root (tmp_path) for tests.

Only implements the subset of TaskProvider used by the test suite.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from orchestrator.providers.base import TaskProvider

logger = logging.getLogger(__name__)


class FileSystemProvider(TaskProvider):
    def __init__(self, control_repo_path: str | Path):
        self.control_repo_path = Path(control_repo_path).resolve()
        self.state_dir = self.control_repo_path / "state"
        self.tasks_dir = self.state_dir / "tasks"
        self.alerts_dir = self.state_dir / "alerts"
        self.inbox_dir = self.state_dir / "inbox"

        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.alerts_dir.mkdir(parents=True, exist_ok=True)
        self.inbox_dir.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------------
    # Tasks
    # ---------------------------------------------------------------------

    def get_tasks(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for p in self.tasks_dir.glob("*.json"):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                continue
        return out

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        p = self.tasks_dir / f"{task_id}.json"
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Failed to read task {task_id}: {e}")
            return None

    def update_task(self, task_id: str, updates: dict[str, Any]) -> None:
        # Mirror ControlManager merge semantics: read existing, update, write.
        kind = updates.pop("kind", "task")
        _ = kind  # reserved for future schema differences

        cur = self.get_task(task_id) or {"id": task_id}
        cur.update(updates)
        cur.setdefault("updatedAt", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

        (self.tasks_dir / f"{task_id}.json").write_text(
            json.dumps(cur, indent=2) + "\n",
            encoding="utf-8",
        )

    # ---------------------------------------------------------------------
    # Alerts / Inbox (minimal)
    # ---------------------------------------------------------------------

    def get_active_alerts(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for p in self.alerts_dir.glob("*.json"):
            try:
                alert = json.loads(p.read_text(encoding="utf-8"))
                if alert.get("status") != "resolved":
                    out.append(alert)
            except Exception:
                continue
        return out

    def update_alert(self, alert_id: str, updates: dict[str, Any]) -> None:
        p = self.alerts_dir / f"{alert_id}.json"
        cur: dict[str, Any] = {}
        if p.exists():
            try:
                cur = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                cur = {}
        cur.setdefault("id", alert_id)
        cur.update(updates)
        p.write_text(json.dumps(cur, indent=2) + "\n", encoding="utf-8")

    def add_inbox_item(self, item: dict[str, Any]) -> None:
        item_id = item.get("id") or f"inbox_{int(time.time()*1000)}"
        p = self.inbox_dir / f"{item_id}.json"
        p.write_text(json.dumps({**item, "id": item_id}, indent=2) + "\n", encoding="utf-8")

    # ---------------------------------------------------------------------
    # Not needed for current tests
    # ---------------------------------------------------------------------

    def get_projects(self) -> list[dict[str, Any]]:  # pragma: no cover
        return []

    def update_project(self, project_id: str, updates: dict[str, Any]) -> None:  # pragma: no cover
        return None

    def update_worker_status(self, updates: dict[str, Any]) -> None:  # pragma: no cover
        return None

    def check_pending_request(self) -> bool:  # pragma: no cover
        return False

    def consume_request(self) -> None:  # pragma: no cover
        return None

    def sync(self) -> list[dict[str, Any]]:  # pragma: no cover
        return []

    def get_engineering_rules(self) -> str:  # pragma: no cover
        return "Standard engineering practices."

    def get_inbox_items(self) -> list[dict[str, Any]]:  # pragma: no cover
        return []

    def update_inbox_item(self, item_id: str, updates: dict[str, Any]) -> None:  # pragma: no cover
        return None
