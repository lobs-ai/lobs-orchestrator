import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class Scanner:
    """
    Scripted scanner (no LLM).
    Produces facts about current state.
    """

    def scan(self) -> dict[str, Any]:
        facts = {
            "pending_request": self.check_pending_request(),
            "eligible_tasks": self.get_eligible_tasks(),
            "projects": self.get_projects(),
        }
        return facts

    def check_pending_request(self) -> bool:
        from .config import CONTROL_REPO_PATH
        request_file = CONTROL_REPO_PATH / "state" / "worker-request.json"
        return request_file.exists()

    def get_projects(self) -> list[dict[str, Any]]:
        from .config import PROJECTS_FILE
        if not PROJECTS_FILE.exists():
            return []
        try:
            with open(PROJECTS_FILE, "r") as f:
                data = json.load(f)
                return data.get("projects", [])
        except Exception as e:
            logger.error(f"Failed to read projects file: {e}")
            return []

    def get_eligible_tasks(self) -> list[dict[str, Any]]:
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
