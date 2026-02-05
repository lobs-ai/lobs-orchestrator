import json
import logging
from typing import Any
from ..base import TaskProvider
from ..scanner import Scanner
from ..control import ControlManager

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

    def update_task(self, task_id: str, updates: dict[str, Any]) -> None:
        self.control.request_op({
            "type": "update_task",
            "task_id": task_id,
            "updates": updates
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
            from ..config import CONTROL_REPO_PATH
            request_file = CONTROL_REPO_PATH / "state" / "worker-request.json"
            if request_file.exists():
                request_file.unlink()
                logger.info("Consumed worker-request.json")
        except Exception as e:
            logger.error(f"Failed to consume request: {e}")

    def sync(self) -> None:
        self.control.process_ops()

    def get_engineering_rules(self) -> str:
        rules_path = CONTROL_REPO_PATH / "ENGINEERING_RULES.md"
        if rules_path.exists():
            return rules_path.read_text()
        return "Standard engineering practices."
