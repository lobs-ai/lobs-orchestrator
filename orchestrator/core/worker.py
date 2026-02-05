import subprocess
import logging
import json
import time
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Any
from concurrent.futures import ThreadPoolExecutor

from orchestrator.config import (
    BASE_DIR,
    LOCKS_DIR,
    WORKER_RESULTS_DIR,
    WORKER_STATUS_JSON
)
from orchestrator.providers.base import TaskProvider
from orchestrator.core.escalation import EscalationManager

logger = logging.getLogger(__name__)


class WorkerManager:
    """
    Manages spawning and tracking worker subprocesses.
    Enforced via domain locks. Git operations on projects are asynchronous.
    """

    def __init__(self, lock_dir: Path, provider: TaskProvider):
        self.lock_dir = lock_dir
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self.provider = provider
        self.escalation = EscalationManager(provider)
        # task_id -> (process, project_id, log_file, prompt_path)
        self.active_workers: dict[str, tuple[subprocess.Popen, str, Any, str]] = {}
        # task_id -> project_id (workers currently syncing or finalizing)
        self.pending_workers: set[str] = set()
        self.executor = ThreadPoolExecutor(max_workers=10)
        
        # Cache for lock status to avoid redundant disk I/O within a single loop
        self._lock_cache: dict[str, tuple[float, bool]] = {}

    def is_domain_locked(self, project_id: str) -> bool:
        now = time.time()
        if project_id in self._lock_cache:
            cache_ts, is_locked = self._lock_cache[project_id]
            if now - cache_ts < 2:  # 2 second cache for rapid successive calls
                return is_locked

        lock_file = self.lock_dir / f"{project_id}.lock"
        locked = self._check_lock_file(lock_file, project_id)
        self._lock_cache[project_id] = (now, locked)
        return locked

    def _check_lock_file(self, lock_file: Path, project_id: str) -> bool:
        if not lock_file.exists():
            return False

        try:
            with open(lock_file, "r") as f:
                data = json.load(f)
                pid = data.get("pid")
                status = data.get("status", "running")
                
                if status in ("syncing", "finalizing"):
                    # Lock is active during these stages even without a PID
                    # Check for timeout (e.g. 10 mins)
                    acquired_at = data.get("acquired_at", 0)
                    if time.time() - acquired_at > 600:
                        logger.warning(f"Lock for {project_id} timed out ({status}).")
                        lock_file.unlink()
                        return False
                    return True
                
                if pid:
                    os.kill(pid, 0)
                    return True
        except (ProcessLookupError, json.JSONDecodeError, KeyError, FileNotFoundError):
            try:
                lock_file.unlink()
            except:
                pass
            return False

        return False

    def acquire_lock(self, project_id: str, pid: Optional[int] = None, status: str = "running"):
        lock_file = self.lock_dir / f"{project_id}.lock"
        data = {
            "pid": pid,
            "status": status,
            "acquired_at": time.time()
        }
        with open(lock_file, "w") as f:
            json.dump(data, f)
        # Invalidate cache
        self._lock_cache[project_id] = (time.time(), True)

    def release_lock(self, project_id: str):
        lock_file = self.lock_dir / f"{project_id}.lock"
        if lock_file.exists():
            try:
                lock_file.unlink()
            except:
                pass
        # Invalidate cache
        self._lock_cache[project_id] = (time.time(), False)

    def spawn_worker(
        self, task: dict[str, Any], project_id: str, agent_type: str = "task-runner", rules: str = ""
    ) -> None:
        task_id = task["id"]
        # We don't use cache here to be absolutely sure
        if self._check_lock_file(self.lock_dir / f"{project_id}.lock", project_id):
            logger.info(f"Domain {project_id} is locked. Skipping.")
            return

        # Acquire lock early in "syncing" state
        self.acquire_lock(project_id, status="syncing")
        self.pending_workers.add(task_id)
        
        # Start async spawn
        self.executor.submit(self._async_spawn_flow, task, project_id, agent_type, rules)

    def _async_spawn_flow(self, task: dict[str, Any], project_id: str, agent_type: str, rules: str):
        task_id = task["id"]
        workspace = BASE_DIR / project_id

        try:
            # Step 1: Sync Repo
            logger.info(f"Syncing project repo {project_id}...")
            subprocess.run(
                ["git", "pull", "--rebase"],
                cwd=workspace,
                check=True,
                capture_output=True,
                timeout=60
            )
            
            # Step 2: Build Prompt
            from orchestrator.services.prompter import Prompter
            prompt = Prompter.build_task_prompt(task, project_id, rules=rules)

            import tempfile
            prompt_file = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
            prompt_file.write(prompt)
            prompt_file.close()

            # Step 3: Launch OpenClaw
            from orchestrator.utils.settings import get_setting
            executable = get_setting("openclaw_executable", "openclaw")
            
            cmd = [
                executable, "run",
                "--agent", agent_type,
                "--workspace", str(workspace),
                "--prompt-file", prompt_file.name,
            ]
            
            WORKER_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            log_file_path = WORKER_RESULTS_DIR / f"{task_id}.log"
            log_file = open(log_file_path, "w")

            logger.info(f"Invoking OpenClaw for {task_id}: {' '.join(cmd)}")
            process = subprocess.Popen(
                cmd, 
                stdout=log_file, 
                stderr=subprocess.STDOUT, 
                text=True,
                start_new_session=True
            )

            # Update lock to "running" with PID
            self.acquire_lock(project_id, pid=process.pid, status="running")
            self.active_workers[task_id] = (process, project_id, log_file, prompt_file.name)
            
            self.provider.update_worker_status({
                "active": True,
                "currentTask": task_id,
                "lastHeartbeat": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            })

        except Exception as e:
            logger.error(f"Failed to async spawn worker for {task_id}: {e}")
            self.release_lock(project_id)
            self.handle_worker_failure(task_id, project_id, str(e))
        finally:
            if task_id in self.pending_workers:
                self.pending_workers.remove(task_id)

    def check_workers(self):
        """Check status of active workers and release locks on completion."""
        finished_tasks = []
        for task_id, (process, project_id, log_file, prompt_path) in list(self.active_workers.items()):
            retcode = process.poll()
            if retcode is not None:
                logger.info(f"Worker for {task_id} on {project_id} finished with code {retcode}")
                log_file.close()
                finished_tasks.append(task_id)
                
                if retcode == 0:
                    # Transition to "finalizing" state
                    self.acquire_lock(project_id, status="finalizing")
                    self.executor.submit(self._async_finalize_flow, task_id, project_id, prompt_path)
                else:
                    self._handle_immediate_failure(task_id, project_id, prompt_path)

        for task_id in finished_tasks:
            if task_id in self.active_workers:
                del self.active_workers[task_id]

        if finished_tasks:
            self.update_active_worker_status()

    def _handle_immediate_failure(self, task_id: str, project_id: str, prompt_path: str):
        log_file_path = WORKER_RESULTS_DIR / f"{task_id}.log"
        error_tail = ""
        try:
            with open(log_file_path, "r") as f:
                lines = f.readlines()
                error_tail = "".join(lines[-20:])
        except:
            pass
        
        self.handle_worker_failure(task_id, project_id, error_tail)
        self.release_lock(project_id)
        try:
            os.unlink(prompt_path)
        except:
            pass

    def _async_finalize_flow(self, task_id: str, project_id: str, prompt_path: str):
        try:
            success = self.finalize_project_changes(task_id, project_id)
            if success:
                self.handle_worker_success(task_id, project_id)
            else:
                self.handle_worker_failure(task_id, project_id, "Project repo push failed")
        except Exception as e:
            logger.error(f"Error in finalization for {task_id}: {e}")
            self.handle_worker_failure(task_id, project_id, str(e))
        finally:
            self.release_lock(project_id)
            try:
                os.unlink(prompt_path)
            except:
                pass

    def finalize_project_changes(self, task_id: str, project_id: str) -> bool:
        """Commits and pushes changes in the project repository."""
        project_path = BASE_DIR / project_id
        try:
            # Check if there are changes
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=project_path,
                check=True,
                capture_output=True,
            ).stdout.decode()
            if not status:
                logger.info(f"No changes to commit for task {task_id} in {project_id}")
                return True

            # Commit
            subprocess.run(["git", "add", "."], cwd=project_path, check=True)
            commit_msg = f"lobs: complete task {task_id}\n\nProject: {project_id}"
            subprocess.run(
                ["git", "commit", "-m", commit_msg], cwd=project_path, check=True
            )

            # Push (Reality first)
            subprocess.run(["git", "push"], cwd=project_path, check=True)
            logger.info(
                f"Successfully pushed changes for task {task_id} to {project_id}"
            )
            return True
        except subprocess.CalledProcessError as e:
            logger.error(
                f"Git operation failed for {project_id}: {e.stderr.decode() if e.stderr else str(e)}"
            )
            return False

    def handle_worker_success(self, task_id: str, project_id: str):
        logger.info(f"Worker success for task {task_id}. Updating state.")
        self.provider.update_task(task_id, {"workState": "completed", "status": "completed"})

    def handle_worker_failure(self, task_id: str, project_id: str, error_log: str):
        logger.error(f"Worker failure for task {task_id} on {project_id}")
        self.escalation.process_failure(task_id, project_id, error_log)
        
        # Still update task to failed so it doesn't get re-run immediately by scanner
        self.provider.update_task(task_id, {"workState": "failed"})

    def update_active_worker_status(self):
        self.provider.update_worker_status({
            "active": len(self.active_workers) > 0,
            "currentTask": None
            if not self.active_workers
            else list(self.active_workers.keys())[0],
            "lastHeartbeat": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        })
