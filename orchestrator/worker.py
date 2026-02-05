import subprocess
import logging
import json
import time
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Tuple, Any
from .config import ORCHESTRATOR_REPO_PATH, CONTROL_REPO_PATH, BASE_DIR
from .control import ControlManager

logger = logging.getLogger(__name__)


class WorkerManager:
    """
    Manages spawning and tracking worker subprocesses.
    Enforced via domain locks.
    """

    def __init__(self, lock_dir: Path):
        self.lock_dir = lock_dir
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        # task_id -> (process, project_id)
        self.active_workers: Dict[str, Tuple[subprocess.Popen, str]] = {}

    def is_domain_locked(self, project_id: str) -> bool:
        lock_file = self.lock_dir / f"{project_id}.lock"
        if not lock_file.exists():
            return False

        try:
            with open(lock_file, "r") as f:
                data = json.load(f)
                pid = data.get("pid")
                if pid:
                    os.kill(pid, 0)
                    return True
        except (ProcessLookupError, json.JSONDecodeError, KeyError, FileNotFoundError):
            lock_file.unlink()
            return False

        return False

    def acquire_lock(self, project_id: str, pid: int):
        lock_file = self.lock_dir / f"{project_id}.lock"
        with open(lock_file, "w") as f:
            json.dump({"pid": pid, "acquired_at": time.time()}, f)

    def release_lock(self, project_id: str):
        lock_file = self.lock_dir / f"{project_id}.lock"
        if lock_file.exists():
            lock_file.unlink()

    def spawn_worker(
        self, task: Dict[str, Any], project_id: str, agent_type: str = "task-runner"
    ):
        task_id = task["id"]
        if self.is_domain_locked(project_id):
            logger.info(f"Domain {project_id} is locked. Skipping.")
            return None

        # Workspace path (the project repo)
        workspace = BASE_DIR / project_id

        # Step 1: Project repo sync (Optimistic start)
        try:
            logger.info(f"Syncing project repo {project_id}...")
            subprocess.run(
                ["git", "pull", "--rebase"],
                cwd=workspace,
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as e:
            logger.error(
                f"Failed to sync project repo {project_id}: {e.stderr.decode()}"
            )
            return None

        # Build prompt
        from .prompter import Prompter

        prompt = Prompter.build_task_prompt(task, project_id)

        # Save prompt to temp file
        import tempfile

        prompt_file = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        prompt_file.write(prompt)
        prompt_file.close()

        # OpenClaw command
        cmd = [
            "openclaw",
            "run",
            "--agent",
            agent_type,
            "--workspace",
            str(workspace),
            "--prompt-file",
            prompt_file.name,
        ]

        try:
            # We use a wrapper or log the prompt file location for debugging
            logger.info(f"Invoking OpenClaw: {' '.join(cmd)}")

            from .config import WORKER_RESULTS_DIR
            WORKER_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            
            log_file_path = WORKER_RESULTS_DIR / f"{task_id}.log"
            log_file = open(log_file_path, "w")

            process = subprocess.Popen(
                cmd, 
                stdout=log_file, 
                stderr=subprocess.STDOUT, 
                text=True,
                start_new_session=True # Run in its own session
            )

            self.acquire_lock(project_id, process.pid)
            # Store the log file handle so we can close it later
            self.active_workers[task_id] = (process, project_id, log_file, prompt_file.name)

            ControlManager.request_op(
                {
                    "type": "update_worker_status",
                    "updates": {
                        "active": True,
                        "currentTask": task_id,
                        "lastHeartbeat": datetime.now(timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        ),
                    },
                }
            )

            logger.info(
                f"Spawned worker {process.pid} for task {task_id} on {project_id} using agent {agent_type}. Logs: {log_file_path}"
            )
            return process
        except Exception as e:
            logger.error(f"Failed to spawn worker: {e}")
            # Cleanup prompt file on failure
            try:
                os.unlink(prompt_file.name)
            except:
                pass
            return None

    def check_workers(self):
        """Check status of active workers and release locks on completion."""
        finished_tasks = []
        for task_id, (process, project_id, log_file, prompt_path) in self.active_workers.items():
            retcode = process.poll()
            if retcode is not None:
                # Process finished
                logger.info(
                    f"Worker process for task {task_id} on {project_id} finished with code {retcode}"
                )
                
                log_file.close()

                if retcode == 0:
                    # Step 3: Project repo commit & push (Reality happens first)
                    success = self.finalize_project_changes(task_id, project_id)
                    if success:
                        # Step 4: Control repo reconciliation (Control state catches up)
                        self.handle_worker_success(task_id, project_id)
                    else:
                        self.handle_worker_failure(
                            task_id, project_id, "Project repo push failed"
                        )
                else:
                    # Read end of log for error
                    from .config import WORKER_RESULTS_DIR
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
                
                # Cleanup
                try:
                    os.unlink(prompt_path)
                except:
                    pass
                    
                finished_tasks.append(task_id)

        for task_id in finished_tasks:
            del self.active_workers[task_id]

        if finished_tasks:
            self.update_active_worker_status()

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
        logger.info(f"Worker success for task {task_id}. Requesting completion op.")
        ControlManager.request_op(
            {
                "type": "update_task",
                "task_id": task_id,
                "updates": {"workState": "completed", "status": "completed"},
            }
        )

    def handle_worker_failure(self, task_id: str, project_id: str, error_log: str):
        logger.error(f"Worker failure for task {task_id} on {project_id}")
        
        # Write diagnostic artifact
        from .config import WORKER_RESULTS_DIR
        diagnostic_path = WORKER_RESULTS_DIR / f"{task_id}_diagnostic.md"
        with open(diagnostic_path, "w") as f:
            f.write(f"# Diagnostic for Task {task_id}\n\n")
            f.write(f"**Project:** {project_id}\n")
            f.write(f"**Timestamp:** {datetime.now(timezone.utc).isoformat()}\n\n")
            f.write("## Error Log\n\n```\n")
            f.write(error_log)
            f.write("\n```\n")
            
        ControlManager.request_op(
            {
                "type": "update_task",
                "task_id": task_id,
                "updates": {"workState": "failed"},
            }
        )

    def update_active_worker_status(self):
        ControlManager.request_op(
            {
                "type": "update_worker_status",
                "updates": {
                    "active": len(self.active_workers) > 0,
                    "currentTask": None
                    if not self.active_workers
                    else list(self.active_workers.keys())[0],
                    "lastHeartbeat": datetime.now(timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                },
            }
        )
