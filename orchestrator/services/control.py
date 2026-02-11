import json
import subprocess
import time
import logging
import random
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.config import (
    CONTROL_REPO_PATH,
    CONTROL_OPS_DIR,
    PROJECTS_FILE,
    TASKS_DIR,
    TASKS_FILE,
    WORKER_STATUS_JSON
)

logger = logging.getLogger(__name__)


class ControlManager:
    """
    Exactly one writer for the control repo.
    Reads requests from state/control-ops/, applies them, and pushes.
    """

    @property
    def ops_dir(self) -> Path:
        return CONTROL_OPS_DIR

    def _check_git_rebase_state(self, repo_path: Path, *, timeout_sec: float = 8.0) -> bool:
        """Check if repo is in rebase state (returns True if in rebase)."""
        try:
            result = subprocess.run(
                ["git", "status"],
                cwd=repo_path,
                capture_output=True,
                timeout=timeout_sec,
                text=True,
            )
            output = result.stdout + result.stderr
            # Check for common rebase indicators
            return any([
                "rebase in progress" in output.lower(),
                "you are currently rebasing" in output.lower(),
                (repo_path / ".git" / "rebase-merge").exists(),
                (repo_path / ".git" / "rebase-apply").exists(),
            ])
        except Exception as e:
            logger.warning(f"Failed to check git rebase state for {repo_path}: {e}")
            return False
    
    def _abort_git_rebase(self, repo_path: Path, *, timeout_sec: float = 30.0) -> bool:
        """Abort stuck git rebase. Returns True if successful."""
        try:
            logger.warning(f"Aborting stuck git rebase in {repo_path}")
            subprocess.run(
                ["git", "rebase", "--abort"],
                cwd=repo_path,
                check=True,
                capture_output=True,
                timeout=timeout_sec,
            )
            logger.info(f"Successfully aborted git rebase in {repo_path}")
            return True
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or b"").decode()
            logger.error(f"Failed to abort git rebase in {repo_path}: {stderr}")
            return False
        except subprocess.TimeoutExpired:
            logger.error(f"Timeout aborting git rebase in {repo_path}")
            return False
        except Exception as e:
            logger.error(f"Error aborting git rebase in {repo_path}: {e}")
            return False
    
    def _git_hard_reset_to_origin(self, repo_path: Path, *, timeout_sec: float = 60.0) -> bool:
        """Hard reset to origin (fallback recovery). Returns True if successful."""
        try:
            logger.warning(f"Performing hard reset to origin in {repo_path}")
            
            # Get current branch
            branch_result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=repo_path,
                check=True,
                capture_output=True,
                timeout=timeout_sec,
                text=True,
            )
            branch = branch_result.stdout.strip()
            
            # Fetch latest
            subprocess.run(
                ["git", "fetch", "origin"],
                cwd=repo_path,
                check=True,
                capture_output=True,
                timeout=timeout_sec,
            )
            
            # Hard reset to origin
            subprocess.run(
                ["git", "reset", "--hard", f"origin/{branch}"],
                cwd=repo_path,
                check=True,
                capture_output=True,
                timeout=timeout_sec,
            )
            
            logger.info(f"Successfully reset {repo_path} to origin/{branch}")
            return True
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or b"").decode()
            logger.error(f"Failed to hard reset {repo_path}: {stderr}")
            return False
        except subprocess.TimeoutExpired:
            logger.error(f"Timeout during hard reset in {repo_path}")
            return False
        except Exception as e:
            logger.error(f"Error during hard reset in {repo_path}: {e}")
            return False

    def pull(self) -> None:
        """Pull latest changes from control repo with robust error handling.
        
        Checks for stuck rebase state and recovers if needed.
        Falls back to hard reset if rebase fails.
        """
        # Skip if control repo doesn't exist or isn't a git repo
        if not CONTROL_REPO_PATH.exists() or not (CONTROL_REPO_PATH / ".git").exists():
            logger.debug(f"Control repo not found or not a git repo, skipping pull")
            return
        
        # Check if repo is in stuck rebase state
        if self._check_git_rebase_state(CONTROL_REPO_PATH):
            logger.warning(f"Control repo is in rebase state, attempting to abort")
            if not self._abort_git_rebase(CONTROL_REPO_PATH):
                logger.error(f"Failed to abort rebase in control repo, attempting hard reset")
                if not self._git_hard_reset_to_origin(CONTROL_REPO_PATH):
                    logger.error(f"Unable to recover from stuck git state in control repo")
                    return
        
        try:
            subprocess.run(
                ["git", "pull", "--rebase"],
                cwd=CONTROL_REPO_PATH,
                check=True,
                capture_output=True,
                timeout=60.0,
            )
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or b"").decode()
            logger.error(f"Git pull failed in control repo: {stderr}")
            
            # Check if this is a "cannot rebase onto multiple branches" error
            if "cannot rebase" in stderr.lower() or "multiple branches" in stderr.lower():
                logger.warning(f"Rebase conflict detected in control repo, attempting recovery")
                # Abort the failed rebase
                self._abort_git_rebase(CONTROL_REPO_PATH)
                # Try hard reset as fallback
                if self._git_hard_reset_to_origin(CONTROL_REPO_PATH):
                    logger.info(f"Successfully recovered control repo from rebase failure via hard reset")
                else:
                    logger.error(f"Failed to recover control repo from git pull failure")
        except subprocess.TimeoutExpired:
            logger.error(f"Git pull timed out in control repo after 60s")

    def push(self, message: str) -> None:
        """Push changes to control repo with timeouts and error handling."""
        # Skip if control repo doesn't exist or isn't a git repo
        if not CONTROL_REPO_PATH.exists() or not (CONTROL_REPO_PATH / ".git").exists():
            logger.debug(f"Control repo not found or not a git repo, skipping push")
            return
        
        try:
            # Check if there are changes to commit
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=CONTROL_REPO_PATH,
                check=True,
                capture_output=True,
                timeout=10.0,
            ).stdout.decode().strip()
            
            if not status:
                return

            subprocess.run(
                ["git", "add", "."],
                cwd=CONTROL_REPO_PATH,
                check=True,
                capture_output=True,
                timeout=30.0,
            )

            subprocess.run(
                ["git", "commit", "-m", message],
                cwd=CONTROL_REPO_PATH,
                check=True,
                capture_output=True,
                timeout=30.0,
            )
            
            subprocess.run(
                ["git", "push"],
                cwd=CONTROL_REPO_PATH,
                check=True,
                capture_output=True,
                timeout=60.0,
            )
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or b"").decode()
            logger.error(f"Git push failed in control repo: {stderr}")
        except subprocess.TimeoutExpired as e:
            logger.error(f"Git operation timed out in control repo: {e}")

    def process_ops(self) -> list[dict[str, Any]]:
        self.ops_dir.mkdir(parents=True, exist_ok=True)
        ops_files = sorted(self.ops_dir.glob("*.json"))
        if not ops_files:
            return []

        # Ensure we have the latest control state before processing
        self.pull()

        applied_ops = []
        messages = []
        op_summaries = []
        
        for op_file in ops_files:
            try:
                with open(op_file, "r") as f:
                    op = json.load(f)

                if op.get("type") == "message":
                    messages.append(op)
                else:
                    self.apply_op(op)
                    applied_ops.append(op_file)
                    
                    # Only include meaningful ops in commit message
                    # Worker status updates are applied but don't trigger standalone commits
                    if op.get("type") != "update_worker_status":
                        op_summaries.append(self._summarize_op(op))
            except Exception as e:
                logger.error(f"Failed to apply op {op_file}: {e}")

        if applied_ops:
            for op_file in applied_ops:
                op_file.unlink()

            # Only push if there are meaningful changes (not just worker status)
            if op_summaries:
                commit_msg = self._build_commit_message(op_summaries)
                self.push(commit_msg)
            else:
                # Worker status changed but no other updates - don't create a commit
                logger.debug("Only worker status updated, skipping commit")
        
        return messages

    def apply_op(self, op: dict[str, Any]) -> None:
        op_type = op.get("type")
        if op_type == "update_task":
            self._update_item(op["task_id"], op["updates"], kind=op.get("kind", "task"))
        elif op_type == "update_project":
            self._update_project(op["project_id"], op["updates"])
        elif op_type == "update_worker_status":
            self._update_worker_status(op["updates"])
        elif op_type == "add_project":
            self._add_project(op["project"])
        elif op_type == "update_alert":
            self._update_alert(op["alert_id"], op["updates"])
        elif op_type == "update_inbox_item":
            self._update_inbox_item(op["item_id"], op["updates"])
        elif op_type == "add_inbox_item":
            self._add_inbox_item(op["item"])
        elif op_type == "log_worker_usage":
            self._log_worker_usage(op["usage_data"])
        else:
            logger.warning(f"Unknown op type: {op_type}")

    def _update_item(self, item_id: str, updates: dict[str, Any], kind: str = "task") -> None:
        from orchestrator.config import CONTROL_REPO_PATH, TASKS_DIR, TASKS_FILE
        
        item_path = None
        if kind == "task":
            item_path = TASKS_DIR / f"{item_id}.json"
        elif kind == "research_request":
            # Research requests are in state/research/*/requests/*.json
            research_dir = CONTROL_REPO_PATH / "state" / "research"
            for req_dir in research_dir.glob("*/requests"):
                p = req_dir / f"{item_id}.json"
                if p.exists():
                    item_path = p
                    break
        elif kind == "inbox_response":
            item_path = CONTROL_REPO_PATH / "state" / "inbox-responses" / f"{item_id}.json"
            if not item_path.exists():
                # Check rglob for nested inbox-responses
                for p in (CONTROL_REPO_PATH / "state" / "inbox-responses").rglob(f"{item_id}.json"):
                    item_path = p
                    break
        
        if item_path and item_path.exists():
            try:
                with open(item_path, "r") as f:
                    data = json.load(f)

                data.update(updates)
                data["updatedAt"] = datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )

                with open(item_path, "w") as f:
                    json.dump(data, f, indent=2)
                    f.write("\n")
                logger.info(f"Updated {kind} file {item_path}")
            except Exception as e:
                logger.error(f"Failed to update {kind} item {item_id}: {e}")

        # Fallback for legacy tasks.json
        if kind == "task" and TASKS_FILE.exists():
            try:
                with open(TASKS_FILE, "r") as f:
                    data = json.load(f)

                found = False
                for task in data.get("tasks", []):
                    if task["id"] == item_id:
                        task.update(updates)
                        task["updatedAt"] = datetime.now(timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        )
                        found = True
                        break

                if found:
                    with open(TASKS_FILE, "w") as f:
                        json.dump(data, f, indent=2)
                        f.write("\n")
                    logger.info(f"Updated legacy tasks file {TASKS_FILE}")
            except Exception as e:
                logger.error(f"Failed to update legacy tasks file: {e}")

    def _add_project(self, project: dict[str, Any]) -> None:
        """Add or replace a project in projects.json."""
        from orchestrator.config import PROJECTS_FILE

        PROJECTS_FILE.parent.mkdir(parents=True, exist_ok=True)

        data: dict[str, Any]
        if PROJECTS_FILE.exists():
            try:
                with open(PROJECTS_FILE, "r") as f:
                    data = json.load(f)
            except Exception:
                data = {"projects": []}
        else:
            data = {"projects": []}

        projects: list[dict[str, Any]] = data.get("projects", [])
        project_id = project.get("id")
        if not project_id:
            raise ValueError("Project is missing required field 'id'")

        replaced = False
        for i, existing in enumerate(projects):
            if existing.get("id") == project_id:
                projects[i] = project
                replaced = True
                break

        if not replaced:
            projects.append(project)

        data["projects"] = projects

        with open(PROJECTS_FILE, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")

        logger.info(f"{'Replaced' if replaced else 'Added'} project {project_id}")

    def _update_project(self, project_id: str, updates: dict[str, Any]) -> None:
        from orchestrator.config import PROJECTS_FILE
        if not PROJECTS_FILE.exists():
            return

        try:
            with open(PROJECTS_FILE, "r") as f:
                data = json.load(f)

            found = False
            for project in data.get("projects", []):
                if project["id"] == project_id:
                    project.update(updates)
                    found = True
                    break

            if found:
                with open(PROJECTS_FILE, "w") as f:
                    json.dump(data, f, indent=2)
                    f.write("\n")
                logger.info(f"Updated project {project_id}")
        except Exception as e:
            logger.error(f"Failed to update project {project_id}: {e}")

    def _update_alert(self, alert_id: str, updates: dict[str, Any]) -> None:
        from orchestrator.config import CONTROL_REPO_PATH
        alert_path = CONTROL_REPO_PATH / "state" / "alerts" / f"{alert_id}.json"
        if not alert_path.exists():
            return

        try:
            with open(alert_path, "r") as f:
                data = json.load(f)

            data.update(updates)
            with open(alert_path, "w") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
            logger.info(f"Updated alert {alert_id}")
        except Exception as e:
            logger.error(f"Failed to update alert {alert_id}: {e}")

    def _update_inbox_item(self, item_id: str, updates: dict[str, Any]) -> None:
        from orchestrator.config import CONTROL_REPO_PATH
        inbox_dir = CONTROL_REPO_PATH / "state" / "inbox"
        item_path = inbox_dir / f"{item_id}.json"
        if not item_path.exists():
            return

        try:
            with open(item_path, "r") as f:
                data = json.load(f)

            data.update(updates)
            with open(item_path, "w") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
            logger.info(f"Updated inbox item {item_id}")
        except Exception as e:
            logger.error(f"Failed to update inbox item {item_id}: {e}")

    def _add_inbox_item(self, item: dict[str, Any]) -> None:
        from orchestrator.config import CONTROL_REPO_PATH
        import time
        inbox_dir = CONTROL_REPO_PATH / "state" / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        
        timestamp = int(time.time() * 1000)
        item_id = item.get("id", f"inbox_{timestamp}")
        item_path = inbox_dir / f"{item_id}.json"
        
        try:
            with open(item_path, "w") as f:
                json.dump(item, f, indent=2)
                f.write("\n")
            logger.info(f"Added inbox item: {item_id}")
        except Exception as e:
            logger.error(f"Failed to add inbox item: {e}")

    def _update_worker_status(self, updates: dict[str, Any]) -> None:
        """
        Update worker status file (local only, never pushed to git).
        This is runtime state, not canonical control state.
        """
        from orchestrator.config import WORKER_STATUS_JSON

        if not WORKER_STATUS_JSON.exists():
            status = {}
        else:
            with open(WORKER_STATUS_JSON, "r") as f:
                status = json.load(f)

        status.update(updates)
        with open(WORKER_STATUS_JSON, "w") as f:
            json.dump(status, f, indent=2)
        
        # Note: worker status is NOT pushed to git (it's in .gitignore)

    def _log_worker_usage(self, usage_data: dict[str, Any]) -> None:
        """
        Append worker usage data to worker-history.json.
        This is pushed to git as part of control state.
        """
        history_path = CONTROL_REPO_PATH / "state" / "worker-history.json"
        
        # Load existing history
        if history_path.exists():
            try:
                with open(history_path, "r") as f:
                    history = json.load(f)
            except Exception as e:
                logger.error(f"Failed to read worker history: {e}")
                history = {"runs": []}
        else:
            history = {"runs": []}
        
        # Append new run
        history["runs"].append(usage_data)
        
        # Write back
        try:
            with open(history_path, "w") as f:
                json.dump(history, f, indent=2)
                f.write("\n")
            logger.info(f"Logged worker usage: {usage_data.get('totalTokens', 0)} tokens, ${usage_data.get('totalCostUSD', 0):.4f}")
        except Exception as e:
            logger.error(f"Failed to write worker history: {e}")

    def _get_task_title(self, task_id: str) -> str:
        """Look up task title from the task file."""
        task_file = TASKS_DIR / f"{task_id}.json"
        if task_file.exists():
            try:
                with open(task_file, "r") as f:
                    task = json.load(f)
                    return task.get("title", task_id[:8])
            except Exception:
                pass
        return task_id[:8]

    def _summarize_op(self, op: dict[str, Any]) -> str:
        """Generate a human-readable summary of an operation."""
        op_type = op.get("type")
        
        # Check for explicit summary first
        if "summary" in op and op["summary"]:
            return op["summary"]
        
        # Check for action hint
        action = op.get("action", "")
        
        if op_type == "update_task":
            task_id = op.get("task_id", "unknown")
            task_title = self._get_task_title(task_id)
            updates = op.get("updates", {})
            
            if action == "complete":
                return f"complete task: {task_title}"
            elif action.startswith("update_state_"):
                state = action.split("_")[-1]
                return f"mark task as {state}: {task_title}"
            elif "workState" in updates:
                return f"update task state to {updates['workState']}: {task_title}"
            else:
                fields = ", ".join(updates.keys())
                return f"update task ({fields}): {task_title}"
        
        elif op_type == "add_inbox_item":
            item = op.get("item", {})
            title = item.get("title", "unknown")
            if action == "add_suggestion":
                return f"add suggestion: {title}"
            return f"add inbox item: {title}"
        
        elif op_type == "update_worker_status":
            return "update worker status"
        
        elif op_type == "log_worker_usage":
            usage = op.get("usage_data", {})
            tokens = usage.get("totalTokens", 0)
            cost = usage.get("totalCostUSD", 0)
            return f"log worker usage: {tokens:,} tokens, ${cost:.4f}"
        
        elif op_type == "update_project":
            project_id = op.get("project_id", "unknown")
            return f"update project {project_id}"
        
        else:
            return f"{op_type} operation"
    
    def _build_commit_message(self, summaries: list[str]) -> str:
        """Build a meaningful commit message from operation summaries."""
        if not summaries:
            return "lobs: update control state"
        
        if len(summaries) == 1:
            return f"lobs: {summaries[0]}"
        
        # Multiple operations - use first as headline, rest as body
        headline = summaries[0]
        body = "\n".join(f"- {s}" for s in summaries[1:])
        
        return f"lobs: {headline}\n\n{body}"

    @staticmethod
    def request_op(op: dict[str, Any]) -> None:
        """
        Static method for other components to request an operation.
        """
        from orchestrator.config import CONTROL_OPS_DIR
        import random
        import string

        CONTROL_OPS_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = int(time.time() * 1000)
        rand = ''.join(random.choices(string.ascii_lowercase + string.digits, k=4))
        op_file = CONTROL_OPS_DIR / f"op_{timestamp}_{rand}.json"
        with open(op_file, "w") as f:
            json.dump(op, f, indent=2)
            f.write("\n")
