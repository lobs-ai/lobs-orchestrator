import json
import logging
import subprocess
from pathlib import Path
from typing import List, Dict, Any
from .config import BASE_DIR, TASKS_DIR, CONTROL_REPO_PATH
from .control import ControlManager

logger = logging.getLogger(__name__)


class Reconciler:
    """
    Self-healing component that cross-checks project repos with control state.
    """

    def reconcile(self, project_ids: List[str]):
        """
        Scans project repos for recent commits and cross-checks task state.
        """
        for project_id in project_ids:
            self._reconcile_project(project_id)

    def _reconcile_project(self, project_id: str):
        project_path = BASE_DIR / project_id
        if not project_path.exists():
            return

        try:
            # Get recent commits
            result = subprocess.run(
                ["git", "log", "-n", "10", "--pretty=format:%s"],
                cwd=project_path,
                check=True,
                capture_output=True,
                text=True,
            )
            commits = result.stdout.split("\n")

            # Look for task completion markers in commit messages
            # e.g. "lobs: complete task UUID"
            for commit_msg in commits:
                if "lobs: complete task" in commit_msg:
                    task_id = commit_msg.split("lobs: complete task")[-1].strip()
                    self._ensure_task_completed(task_id)
        except Exception as e:
            logger.error(f"Reconciliation failed for project {project_id}: {e}")

    def _ensure_task_completed(self, task_id: str):
        # Cross-check with control state
        task_path = TASKS_DIR / f"{task_id}.json"
        if task_path.exists():
            with open(task_path, "r") as f:
                task = json.load(f)

            if task.get("status") != "completed":
                logger.warning(
                    f"Reconciler found completed task {task_id} in git but not in control state. Fixing."
                )
                ControlManager.request_op(
                    {
                        "type": "update_task",
                        "task_id": task_id,
                        "updates": {"workState": "completed", "status": "completed"},
                    }
                )
