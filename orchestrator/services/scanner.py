import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class Scanner:
    """
    Scripted scanner (no LLM).
    Produces facts about current state.
    """

    def __init__(self):
        self._projects_cache = None
        self._projects_mtime = 0
        self._tasks_cache = None
        self._tasks_dir_mtime = 0
        self._tasks_file_mtime = 0

    def scan(self) -> dict[str, Any]:
        facts = {
            "pending_request": self.check_pending_request(),
            "eligible_tasks": self.get_eligible_tasks(),
            "projects": self.get_projects(),
        }
        return facts

    def check_pending_request(self) -> bool:
        from orchestrator.config import CONTROL_REPO_PATH
        request_file = CONTROL_REPO_PATH / "state" / "worker-request.json"
        return request_file.exists()

    def get_projects(self) -> list[dict[str, Any]]:
        from orchestrator.config import PROJECTS_FILE
        if not PROJECTS_FILE.exists():
            return []
        
        try:
            mtime = PROJECTS_FILE.stat().st_mtime
            if self._projects_cache is not None and mtime <= self._projects_mtime:
                return self._projects_cache

            with open(PROJECTS_FILE, "r") as f:
                data = json.load(f)
                self._projects_cache = data.get("projects", [])
                self._projects_mtime = mtime
                return self._projects_cache
        except Exception as e:
            logger.error(f"Failed to read projects file: {e}")
            return self._projects_cache or []

    def get_eligible_tasks(self) -> list[dict[str, Any]]:
        from orchestrator.config import TASKS_DIR, TASKS_FILE

        tasks = []
        should_reload = False

        if TASKS_DIR.exists():
            mtime = TASKS_DIR.stat().st_mtime
            if self._tasks_cache is None or mtime > self._tasks_dir_mtime:
                should_reload = True
                self._tasks_dir_mtime = mtime
        elif TASKS_FILE.exists():
            mtime = TASKS_FILE.stat().st_mtime
            if self._tasks_cache is None or mtime > self._tasks_file_mtime:
                should_reload = True
                self._tasks_file_mtime = mtime

        if not should_reload and self._tasks_cache is not None:
            tasks = self._tasks_cache
        else:
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
            
            self._tasks_cache = tasks

        eligible = [
            t
            for t in tasks
            if t.get("status") == "active" and t.get("workState") == "not_started"
        ]
        return eligible
