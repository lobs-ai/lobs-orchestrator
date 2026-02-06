import json
import logging
import time
from typing import Any
from orchestrator.providers.base import TaskProvider
from orchestrator.services.scanner import Scanner
from orchestrator.services.control import ControlManager

logger = logging.getLogger(__name__)

class LocalTaskProvider(TaskProvider):
    """
    Implementation of TaskProvider using the local filesystem (lobs-control repo).
    """

    def __init__(self):
        self.scanner = Scanner()
        self.control = ControlManager()

    def get_tasks(self) -> list[dict[str, Any]]:
        return self.scanner.get_eligible_tasks()

    def get_projects(self) -> list[dict[str, Any]]:
        return self.scanner.get_projects()

    def update_project(self, project_id: str, updates: dict[str, Any]) -> None:
        self.control.request_op({
            "type": "update_project",
            "project_id": project_id,
            "updates": updates
        })

    def update_task(self, task_id: str, updates: dict[str, Any]) -> None:
        kind = updates.pop("kind", "task")
        self.control.request_op({
            "type": "update_task",
            "task_id": task_id,
            "updates": updates,
            "kind": kind
        })

    def update_worker_status(self, updates: dict[str, Any]) -> None:
        self.control.request_op({
            "type": "update_worker_status",
            "updates": updates
        })

    def check_pending_request(self) -> bool:
        return self.scanner.check_pending_request()

    def consume_request(self) -> None:
        try:
            from orchestrator.config import CONTROL_REPO_PATH
            request_file = CONTROL_REPO_PATH / "state" / "worker-request.json"
            if request_file.exists():
                request_file.unlink()
                logger.info("Consumed worker-request.json")
        except Exception as e:
            logger.error(f"Failed to consume request: {e}")

    def sync(self) -> list[dict[str, Any]]:
        return self.control.process_ops()

    def get_engineering_rules(self) -> str:
        from orchestrator.config import CONTROL_REPO_PATH
        rules_path = CONTROL_REPO_PATH / "ENGINEERING_RULES.md"
        if rules_path.exists():
            return rules_path.read_text()
        return "Standard engineering practices."

    def add_inbox_item(self, item: dict[str, Any]) -> None:
        from orchestrator.config import CONTROL_REPO_PATH
        inbox_dir = CONTROL_REPO_PATH / "state" / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        
        timestamp = int(time.time() * 1000)
        item_id = item.get("id", f"inbox_{timestamp}")
        item_path = inbox_dir / f"{item_id}.json"
        
        with open(item_path, "w") as f:
            json.dump(item, f, indent=2)
        logger.info(f"Added inbox item: {item_id}")

    def get_inbox_items(self) -> list[dict[str, Any]]:
        from orchestrator.config import CONTROL_REPO_PATH
        inbox_dir = CONTROL_REPO_PATH / "state" / "inbox"
        if not inbox_dir.exists():
            return []
        
        items = []
        for item_file in inbox_dir.glob("*.json"):
            try:
                with open(item_file, "r") as f:
                    items.append(json.load(f))
            except Exception as e:
                logger.error(f"Failed to read inbox item {item_file}: {e}")
        return items

    def update_inbox_item(self, item_id: str, updates: dict[str, Any]) -> None:
        self.control.request_op({
            "type": "update_inbox_item",
            "item_id": item_id,
            "updates": updates
        })

    def get_active_alerts(self) -> list[dict[str, Any]]:
        from orchestrator.config import CONTROL_REPO_PATH
        alerts_dir = CONTROL_REPO_PATH / "state" / "alerts"
        if not alerts_dir.exists():
            return []
        
        alerts = []
        for alert_file in alerts_dir.glob("*.json"):
            try:
                with open(alert_file, "r") as f:
                    alert = json.load(f)
                    if alert.get("status") != "resolved":
                        alerts.append(alert)
            except Exception as e:
                logger.error(f"Failed to read alert {alert_file}: {e}")
        return alerts

    def update_alert(self, alert_id: str, updates: dict[str, Any]) -> None:
        self.control.request_op({
            "type": "update_alert",
            "alert_id": alert_id,
            "updates": updates
        })
