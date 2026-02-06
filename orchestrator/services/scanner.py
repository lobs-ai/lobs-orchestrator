import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class Scanner:
    """
    Scripted scanner that uses lobs-control scripts.
    Produces facts about current state.
    """

    def __init__(self):
        self._projects_cache = None
        self._projects_mtime = 0

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
        from orchestrator.config import CONTROL_REPO_PATH
        
        script_path = CONTROL_REPO_PATH / "bin" / "open-work"
        if not script_path.exists():
            logger.warning(f"open-work script not found at {script_path}")
            return []

        try:
            result = subprocess.run(
                [str(script_path)],
                cwd=CONTROL_REPO_PATH,
                capture_output=True,
                text=True,
                check=True
            )
            all_work = json.loads(result.stdout)
            
            # Filter to items that are not started or active but not assigned
            # open-work already filters out completed/done items.
            # We specifically want things that the orchestrator can pick up.
            eligible = []
            for item in all_work:
                if item.get("kind") == "task":
                    if item.get("workState") == "not_started" and item.get("status") == "active":
                        eligible.append(item)
                elif item.get("kind") in ("research_request", "tracker_request", "inbox_response", "text_dump"):
                    # These kinds usually don't have workState yet, so if they are in open-work they are eligible
                    eligible.append(item)
            
            return eligible
        except Exception as e:
            logger.error(f"Failed to run open-work: {e}")
            return []
