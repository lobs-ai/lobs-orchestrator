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
        """Get eligible tasks from both local state and GitHub projects."""
        from orchestrator.config import CONTROL_REPO_PATH
        
        # Get local tasks
        local_tasks = self._get_local_eligible_tasks()
        
        # Get GitHub tasks from all GitHub-tracked projects
        github_tasks = self._get_github_eligible_tasks()
        
        return local_tasks + github_tasks
    
    def _get_local_eligible_tasks(self) -> list[dict[str, Any]]:
        """Get eligible tasks from local state/tasks."""
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
            now_timestamp = time.time()
            
            for item in all_work:
                if item.get("kind") == "task":
                    work_state = item.get("workState")
                    
                    # Skip tasks with retryAfter in the future
                    retry_after = item.get("retryAfter")
                    if retry_after and retry_after > now_timestamp:
                        logger.debug(
                            f"Skipping task {item.get('id', 'unknown')[:8]} - "
                            f"retry scheduled for {int((retry_after - now_timestamp) / 60)}min from now"
                        )
                        continue
                    
                    # Include not_started tasks and failed tasks ready for retry
                    if work_state == "not_started" and item.get("status") == "active":
                        eligible.append(item)
                    elif work_state == "failed" and item.get("status") == "active":
                        # Failed tasks are eligible if retryAfter has passed (or is not set)
                        if not retry_after or retry_after <= now_timestamp:
                            # Clear retryAfter when picking up for retry
                            item["_clearing_retry"] = True
                            eligible.append(item)
                elif item.get("kind") in ("research_request", "tracker_request", "inbox_response", "text_dump"):
                    # These kinds usually don't have workState yet, so if they are in open-work they are eligible
                    eligible.append(item)
            
            return eligible
        except Exception as e:
            logger.error(f"Failed to run open-work: {e}")
            return []
    
    def _get_github_eligible_tasks(self) -> list[dict[str, Any]]:
        """Get eligible tasks from GitHub-tracked projects."""
        from orchestrator.config import CONTROL_REPO_PATH
        
        eligible = []
        projects = self.get_projects()
        
        for project in projects:
            # Skip non-GitHub projects
            if project.get("tracking") != "github":
                continue
            
            # Skip archived projects
            if project.get("archived"):
                continue
            
            project_id = project.get("id")
            if not project_id:
                continue
            
            try:
                # Sync GitHub issues to cache
                self._sync_github_project(project_id)
                
                # Read cached issues
                issues = self._read_github_cache(project)
                
                # Convert eligible issues to task format
                for issue in issues:
                    # Skip closed issues
                    if issue.get("state") != "open":
                        continue
                    
                    # Skip issues with "in-progress" label
                    labels = issue.get("labels", [])
                    if "in-progress" in labels:
                        continue
                    
                    # Parse GitHub repo from repoPath if available
                    repo_path = project.get("repoPath", "")
                    github_repo = self._extract_github_repo(repo_path)
                    
                    # Create synthetic task
                    task = {
                        "id": f"github-{github_repo.replace('/', '-')}-{issue['number']}",
                        "kind": "task",
                        "title": issue.get("title", ""),
                        "notes": issue.get("body", ""),
                        "projectId": project_id,
                        "status": "active",
                        "workState": "not_started",
                        "githubMeta": {
                            "repo": github_repo,
                            "number": issue["number"]
                        }
                    }
                    eligible.append(task)
                    
            except Exception as e:
                logger.error(f"Failed to get GitHub tasks for project {project_id}: {e}")
                continue
        
        return eligible
    
    def _sync_github_project(self, project_id: str):
        """Run gh-sync to refresh GitHub issue cache for a project."""
        from orchestrator.config import CONTROL_REPO_PATH
        
        script_path = CONTROL_REPO_PATH / "bin" / "gh-sync"
        if not script_path.exists():
            logger.warning(f"gh-sync script not found at {script_path}")
            return
        
        try:
            subprocess.run(
                [str(script_path), project_id],
                cwd=CONTROL_REPO_PATH,
                capture_output=True,
                text=True,
                check=True,
                timeout=30
            )
        except subprocess.TimeoutExpired:
            logger.error(f"gh-sync timed out for project {project_id}")
        except Exception as e:
            logger.error(f"Failed to sync GitHub project {project_id}: {e}")
    
    def _read_github_cache(self, project: dict[str, Any]) -> list[dict[str, Any]]:
        """Read cached GitHub issues for a project."""
        from orchestrator.config import CONTROL_REPO_PATH
        
        repo_path = project.get("repoPath")
        if not repo_path:
            return []
        
        # Extract GitHub repo from repoPath
        github_repo = self._extract_github_repo(repo_path)
        if not github_repo:
            return []
        
        # Construct cache path
        cache_key = github_repo.replace("/", "-")
        cache_file = CONTROL_REPO_PATH / "state" / "cache" / "github" / cache_key / "issues.json"
        
        if not cache_file.exists():
            return []
        
        try:
            with open(cache_file, "r") as f:
                data = json.load(f)
                return data.get("issues", [])
        except Exception as e:
            logger.error(f"Failed to read GitHub cache for {github_repo}: {e}")
            return []
    
    def _extract_github_repo(self, repo_path: str) -> str:
        """Extract GitHub owner/repo from a repo path by reading git remote."""
        if not repo_path:
            return ""
        
        try:
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                check=True,
                timeout=5
            )
            remote = result.stdout.strip()
            
            # Parse GitHub repo from various formats:
            # - git@github.com:owner/repo.git
            # - https://github.com/owner/repo.git
            # - https://github.com/owner/repo
            import re
            match = re.search(r'github\.com[:/]([^/]+)/([^/\s]+?)(\.git)?$', remote)
            if match:
                owner = match.group(1)
                repo = match.group(2).replace('.git', '')
                return f"{owner}/{repo}"
        except Exception as e:
            logger.debug(f"Failed to extract GitHub repo from {repo_path}: {e}")
        
        return ""
