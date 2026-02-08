import json
import logging
import subprocess
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Any
from orchestrator.providers.base import TaskProvider
from orchestrator.config import BASE_DIR, TASKS_DIR, CONTROL_REPO_PATH, PROJECTS_FILE

logger = logging.getLogger(__name__)


class Reconciler:
    """
    Self-healing component that:
    1. Cross-checks project repos with control state
    2. Resets stuck/failed tasks to allow retries
    3. Ensures the orchestrator never gets permanently stuck
    """

    def __init__(self, provider: TaskProvider):
        self.provider = provider
        self.stuck_task_timeout = 3600  # 1 hour - tasks stuck in_progress for this long get reset
        self.failed_task_cooldown = 300  # 5 minutes - wait this long before retrying failed tasks
        self._repo_path_cache = {}
    
    def _get_repo_path(self, project_id: str) -> Path:
        """
        Get the repository path for a project.
        
        Looks up repoPath from projects.json, falls back to BASE_DIR / project_id.
        Caches results to avoid repeated file reads.
        """
        if project_id in self._repo_path_cache:
            return self._repo_path_cache[project_id]
        
        # Try to load from projects.json
        if PROJECTS_FILE.exists():
            try:
                with open(PROJECTS_FILE, "r") as f:
                    data = json.load(f)
                    for project in data.get("projects", []):
                        if project.get("id") == project_id:
                            repo_path = project.get("repoPath")
                            if repo_path:
                                path = Path(repo_path)
                                self._repo_path_cache[project_id] = path
                                return path
            except Exception as e:
                logger.warning(f"Failed to read repoPath for {project_id} from projects.json: {e}")
        
        # Fallback to BASE_DIR / project_id
        path = BASE_DIR / project_id
        self._repo_path_cache[project_id] = path
        return path

    def reconcile(self, project_ids: list[str]) -> None:
        """
        Performs reconciliation:
        1. Cross-check git commits with task state
        2. Reset stuck tasks
        """
        # First, check git commits for completed tasks
        for project_id in project_ids:
            self._reconcile_project(project_id)

        # Then, reset stuck/failed tasks to unblock the orchestrator
        self._reset_stuck_tasks()

    def _reconcile_project(self, project_id: str) -> None:
        project_path = self._get_repo_path(project_id)
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
                timeout=10,  # 10 second timeout
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
                self.provider.update_task(task_id, {"workState": "completed", "status": "completed"})

    def _reset_stuck_tasks(self):
        """
        Scans all tasks and resets stuck/failed ones to allow retries.

        Rules:
        - Tasks in_progress for > stuck_task_timeout: reset to not_started
        - Tasks failed for > failed_task_cooldown: reset to not_started
        - This ensures the orchestrator always has work to do
        """
        logger.debug("[RECONCILER] Starting stuck task scan...")
        tasks_dir = CONTROL_REPO_PATH / "state" / "tasks"
        if not tasks_dir.exists():
            return

        now = time.time()
        reset_count = 0

        for task_file in tasks_dir.glob("*.json"):
            try:
                with open(task_file, "r") as f:
                    task = json.load(f)

                task_id = task.get("id", task_file.stem)
                work_state = task.get("workState", "not_started")
                status = task.get("status", "active")
                updated_at = task.get("updatedAt", "")

                # Skip if not active or if already not_started/completed
                if status != "active":
                    continue
                if work_state in ("not_started", "completed"):
                    continue

                # Calculate time since last update
                try:
                    if updated_at:
                        updated_ts = datetime.fromisoformat(updated_at.replace('Z', '+00:00')).timestamp()
                        elapsed = now - updated_ts
                    else:
                        elapsed = 0
                except:
                    elapsed = 0

                should_reset = False
                reason = ""

                # Check if in_progress for too long
                if work_state == "in_progress" and elapsed > self.stuck_task_timeout:
                    should_reset = True
                    reason = f"stuck in_progress for {elapsed/60:.1f} minutes"

                # Check if failed and cooldown period has passed
                elif work_state == "failed" and elapsed > self.failed_task_cooldown:
                    should_reset = True
                    reason = f"failed, cooldown ({elapsed/60:.1f} minutes) elapsed"

                if should_reset:
                    logger.info(f"[RECONCILER] Resetting task {task_id[:8]} to not_started ({reason})")
                    self.provider.update_task(task_id, {"workState": "not_started"})
                    reset_count += 1

            except Exception as e:
                logger.error(f"Error processing task file {task_file}: {e}")

        if reset_count > 0:
            logger.info(f"[RECONCILER] Reset {reset_count} stuck/failed task(s) to not_started")
