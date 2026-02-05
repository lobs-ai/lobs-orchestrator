import json
import logging
from pathlib import Path
from typing import List, Dict, Any
from .config import TASKS_JSON, CONTROL_REPO_PATH

logger = logging.getLogger(__name__)


class Scanner:
    """
    Scripted scanner (no LLM).
    Produces facts about current state.
    """

    def __init__(self):
        self.request_file = CONTROL_REPO_PATH / "state" / "worker-request.json"

    def scan(self) -> Dict[str, Any]:
        facts = {
            "pending_request": self.check_pending_request(),
            "eligible_tasks": self.get_eligible_tasks(),
        }
        return facts

    def check_pending_request(self) -> bool:
        return self.request_file.exists()

    def get_eligible_tasks(self) -> List[Dict[str, Any]]:
        from .config import TASKS_DIR, TASKS_FILE

        tasks = []
        if TASKS_DIR.exists():
            for task_file in TASKS_DIR.glob("*.json"):
                try:
                    with open(task_file, "r") as f:
                        tasks.append(json.load(f))
                except Exception as e:
                    logger.error(f"Failed to read task file {task_file}: {e}")
        elif TASKS_FILE.exists():
            try:
                with open(TASKS_FILE, "r") as f:
                    data = json.load(f)
                    tasks = data.get("tasks", [])
            except Exception as e:
                logger.error(f"Failed to read legacy tasks file: {e}")

        eligible = [
            t
            for t in tasks
            if t.get("status") == "active" and t.get("workState") == "not_started"
        ]
        return eligible
