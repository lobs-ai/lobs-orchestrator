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
    WORKER_STATUS_JSON,
    ORCHESTRATOR_REPO_PATH
)
from orchestrator.providers.base import TaskProvider
from orchestrator.core.escalation import EscalationManager
from orchestrator.core.agents import AgentManager
from orchestrator.core.ollama_client import OllamaClient

logger = logging.getLogger(__name__)


class WorkerManager:
    """
    Manages spawning and tracking a SINGLE worker subprocess.

    CRITICAL CONSTRAINTS:
    - Only ONE worker runs at a time globally (enforced by max_workers=1)
    - All work is queued automatically - tasks are processed sequentially
    - Worker is spawned via `openclaw agent --agent worker` command
    - Same worker agent is reused for all tasks (session reset between tasks)

    Queueing Mechanism:
    - spawn_worker() checks for active/pending workers and returns early if busy
    - Unprocessed tasks remain in provider's queue
    - Next engine loop iteration picks up queued work when worker becomes free
    """

    def __init__(self, lock_dir: Path, provider: TaskProvider):
        self.lock_dir = lock_dir
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self.provider = provider
        self.escalation = EscalationManager(provider)

        # SINGLE WORKER TRACKING:
        # task_id -> (process, project_id, log_file, start_time, agent_id)
        self.active_workers: dict[str, tuple[subprocess.Popen, str, Any, float, str]] = {}
        # task_id -> project_id (workers currently syncing or finalizing)
        self.pending_workers: set[str] = set()

        # ENFORCE SINGLE WORKER: ThreadPoolExecutor with max_workers=1
        # This ensures only one async spawn can run at a time
        self.executor = ThreadPoolExecutor(max_workers=1)
        
        # Agent manager for provisioning workers
        template_dir = ORCHESTRATOR_REPO_PATH / "worker-template"
        self.agent_manager = AgentManager(template_dir)
        
        # Track if worker agent has been provisioned (avoid repeated gateway restarts)
        self._worker_provisioned = False
        
        # Cache for project repo paths
        self._repo_path_cache = {}

        # Ollama client for diagnostic/analysis tasks
        from orchestrator.utils.settings import get_setting
        ollama_url = get_setting("ollama_url", "http://localhost:11434")
        ollama_model = get_setting("ollama_model", "llama3.1")
        ollama_keep_alive = get_setting("ollama_keep_alive", "5m")
        self.ollama = OllamaClient(
            base_url=ollama_url,
            model=ollama_model,
            keep_alive=ollama_keep_alive
        )

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

    def _get_repo_path(self, project_id: str) -> Path:
        """
        Get the repository path for a project.
        
        Looks up repoPath from projects.json, auto-discovers if missing, 
        falls back to BASE_DIR / project_id.
        Caches results to avoid repeated file reads.
        """
        if project_id in self._repo_path_cache:
            return self._repo_path_cache[project_id]
        
        # Try to load from projects.json
        from orchestrator.config import PROJECTS_FILE, CONTROL_REPO_PATH
        if PROJECTS_FILE.exists():
            try:
                import json
                with open(PROJECTS_FILE, "r") as f:
                    data = json.load(f)
                    for project in data.get("projects", []):
                        if project.get("id") == project_id:
                            repo_path = project.get("repoPath")
                            if repo_path:
                                path = Path(repo_path)
                                self._repo_path_cache[project_id] = path
                                return path
                            else:
                                # Project exists but has no repoPath - try auto-discovery
                                logger.info(f"Project {project_id} missing repoPath, running auto-discovery...")
                                try:
                                    # Run discover-repos to populate missing paths
                                    result = subprocess.run(
                                        ["python3", "bin/discover-repos"],
                                        cwd=CONTROL_REPO_PATH,
                                        check=True,
                                        capture_output=True,
                                        timeout=10
                                    )
                                    logger.info(f"Auto-discovery output: {result.stdout.decode().strip()}")
                                    
                                    # Reload projects.json and try again
                                    with open(PROJECTS_FILE, "r") as f2:
                                        data2 = json.load(f2)
                                        for p2 in data2.get("projects", []):
                                            if p2.get("id") == project_id:
                                                repo_path2 = p2.get("repoPath")
                                                if repo_path2:
                                                    path = Path(repo_path2)
                                                    self._repo_path_cache[project_id] = path
                                                    logger.info(f"Auto-discovered repo for {project_id}: {path}")
                                                    return path
                                except Exception as e:
                                    logger.warning(f"Auto-discovery failed for {project_id}: {e}")
            except Exception as e:
                logger.warning(f"Failed to read repoPath for {project_id} from projects.json: {e}")
        
        # Fallback to BASE_DIR / project_id
        logger.warning(f"No repoPath found for {project_id}, using fallback: {BASE_DIR / project_id}")
        path = BASE_DIR / project_id
        self._repo_path_cache[project_id] = path
        return path

    def _determine_runtime(self, task: dict[str, Any]) -> str:
        """
        Determine which runtime to use for this task.

        Rules:
        - All regular tasks (kind="task") → OpenClaw
        - Error fixing/diagnostics (agentType="diagnostic") → Ollama
        - Explicit runtime field overrides default behavior

        Returns:
            "ollama" for diagnostic/error-fixing tasks, "openclaw" for all other work
        """
        # Check explicit runtime field first
        if "runtime" in task:
            return task["runtime"]

        # Check agentType for diagnostics/error fixing
        agent_type = task.get("agentType", "")
        if agent_type == "diagnostic":
            # Use Ollama for diagnostics if available
            if self.ollama.is_available():
                return "ollama"
            else:
                logger.warning("Ollama not available, falling back to OpenClaw for diagnostic task")
                return "openclaw"

        # Default to OpenClaw for all regular tasks
        return "openclaw"

    def spawn_worker(
        self, task: dict[str, Any], project_id: str, agent_type: str = "task-runner", rules: str = ""
    ) -> bool:
        """
        Spawn a worker for the given task.

        QUEUEING: If a worker is already active or pending, this method returns False.
        The task remains in the provider's queue and will be picked up in the next
        orchestrator loop iteration when the worker becomes free.

        This implements automatic queueing without needing a separate queue data structure.

        Returns:
            True if worker was spawned, False if task was queued (worker busy)
        """
        task_id = task["id"]
        task_title = task.get("title", task.get("prompt", task_id[:8]))

        # QUEUE CHECK: Enforce single worker - reject if any worker is active/pending
        if self.active_workers or self.pending_workers:
            active_task = list(self.active_workers.keys())[0] if self.active_workers else list(self.pending_workers)[0]
            logger.info(f"[QUEUE] Worker busy with {active_task}. Queueing task {task_id} ({task_title})")
            return False

        # We don't use cache here to be absolutely sure
        if self._check_lock_file(self.lock_dir / f"{project_id}.lock", project_id):
            logger.info(f"Domain {project_id} is locked. Skipping.")
            return False

        # Acquire lock early in "syncing" state
        self.acquire_lock(project_id, status="syncing")
        self.pending_workers.add(task_id)

        # Determine runtime (Ollama vs OpenClaw)
        runtime = self._determine_runtime(task)
        logger.info(f"[WORKER] Task {task_id} will use runtime: {runtime}")

        # Start async spawn based on runtime
        if runtime == "ollama":
            self.executor.submit(self._async_spawn_ollama_flow, task, project_id, agent_type, rules)
        else:
            self.executor.submit(self._async_spawn_openclaw_flow, task, project_id, agent_type, rules)
        
        # Return True to indicate worker was actually spawned (not queued)
        return True

    def _async_spawn_ollama_flow(self, task: dict[str, Any], project_id: str, agent_type: str, rules: str):
        """
        Spawn an Ollama worker for diagnostic/analysis tasks.

        Simpler flow than OpenClaw:
        - No agent provisioning needed
        - No workspace management
        - Direct API calls to Ollama
        - Still tracks as active worker for queueing
        """
        task_id = task["id"]
        workspace = self._get_repo_path(project_id)

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

            # Step 3: Run Ollama
            logger.info(f"Running Ollama for {task_id} with model {self.ollama.model}")

            WORKER_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            log_file_path = WORKER_RESULTS_DIR / f"{task_id}.log"

            # Use a pseudo-process for tracking (Ollama runs synchronously)
            # We'll create a marker file to track completion
            with open(log_file_path, "w") as log_file:
                log_file.write(f"=== Ollama Task Execution ===\n")
                log_file.write(f"Task ID: {task_id}\n")
                log_file.write(f"Model: {self.ollama.model}\n")
                log_file.write(f"Project: {project_id}\n")
                log_file.write(f"Agent Type: {agent_type}\n")
                log_file.write(f"\n=== Prompt ===\n{prompt}\n\n")
                log_file.write(f"=== Response ===\n")
                log_file.flush()

                # Run Ollama with streaming
                try:
                    full_response = ""
                    for chunk in self.ollama.generate(prompt, stream=True, temperature=0.3):
                        if "response" in chunk:
                            text = chunk["response"]
                            full_response += text
                            log_file.write(text)
                            log_file.flush()

                        if chunk.get("done", False):
                            break

                    log_file.write(f"\n\n=== Completed ===\n")

                    # Update lock to "finalizing"
                    self.acquire_lock(project_id, status="finalizing")

                    # Finalize changes
                    success = self.finalize_project_changes(task_id, project_id)

                    if success:
                        self.handle_worker_success(task_id, project_id, "ollama")
                    else:
                        self.handle_worker_failure(task_id, project_id, "Failed to finalize changes", "ollama")

                except Exception as e:
                    error_msg = f"Ollama execution failed: {e}"
                    logger.error(error_msg)
                    log_file.write(f"\n\n=== ERROR ===\n{error_msg}\n")
                    self.handle_worker_failure(task_id, project_id, error_msg, "ollama")

        except Exception as e:
            logger.error(f"Failed to async spawn Ollama worker for {task_id}: {e}")
            self.handle_worker_failure(task_id, project_id, str(e), "ollama")
        finally:
            self.release_lock(project_id)
            if task_id in self.pending_workers:
                self.pending_workers.remove(task_id)

    def _async_spawn_openclaw_flow(self, task: dict[str, Any], project_id: str, agent_type: str, rules: str):
        task_id = task["id"]
        workspace = self._get_repo_path(project_id)

        try:
            # Step 1: ALWAYS use the single shared "worker" agent
            # Note: agent_type parameter is just metadata for prompts/logging
            # The actual OpenClaw agent is ALWAYS "worker" - no other agents exist
            agent_id = "worker"
            logger.info(f"[WORKER] Using shared worker agent: {agent_id} (type: {agent_type})")

            # Check if worker agent exists and is registered (one-time setup)
            # CRITICAL: Only do this check ONCE to avoid repeated gateway restarts
            if not self._worker_provisioned:
                needs_provision = not self.agent_manager.worker_exists("worker")
                needs_registration = not self.agent_manager.is_agent_registered("worker")

                if needs_provision or needs_registration:
                    logger.info(f"Provisioning shared worker agent...")
                    if not self.agent_manager.provision_worker("worker", register=True):
                        raise Exception(f"Failed to provision worker agent")

                    # If we just registered a new agent, restart gateway
                    if needs_registration:
                        logger.warning(f"New worker registered: {agent_id} - restarting gateway...")
                        if not self.agent_manager.restart_gateway():
                            logger.error("Failed to restart gateway - worker may not be available")
                            raise Exception("Gateway restart failed after agent registration")

                        # Wait for gateway to come back online
                        time.sleep(3)
                        logger.info("Gateway restarted, worker should now be available")
                
                # Mark as provisioned so we never check again
                self._worker_provisioned = True
            
            # Sync worker template files before each task (ensures WORKER_RULES.md etc. are up to date)
            logger.debug(f"Syncing worker template files...")
            if not self.agent_manager.sync_worker_templates("worker"):
                logger.warning("Failed to sync worker templates - continuing anyway")
            
            # Step 2: Sync Repo
            logger.info(f"Syncing project repo {project_id}...")
            subprocess.run(
                ["git", "pull", "--rebase"],
                cwd=workspace,
                check=True,
                capture_output=True,
                timeout=60
            )
            
            # Step 3: Build Prompt
            from orchestrator.services.prompter import Prompter
            prompt = Prompter.build_task_prompt(task, project_id, rules=rules)

            # Step 4: Launch OpenClaw agent with shared worker
            from orchestrator.utils.settings import get_setting
            executable = get_setting("openclaw_executable", "openclaw")

            # Spawn with shared worker agent (always "worker")
            # Single worker at a time across all projects
            cmd = [
                executable, "agent",
                "--agent", agent_id,
                "-m", prompt,
            ]
            
            WORKER_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            log_file_path = WORKER_RESULTS_DIR / f"{task_id}.log"
            log_file = open(log_file_path, "w")

            logger.info(f"Invoking OpenClaw for {task_id} with agent {agent_id}")
            process = subprocess.Popen(
                cmd, 
                stdout=log_file, 
                stderr=subprocess.STDOUT, 
                text=True,
                start_new_session=True,
                cwd=workspace  # Run in the project directory
            )

            # Update lock to "running" with PID
            self.acquire_lock(project_id, pid=process.pid, status="running")

            # VALIDATION: Ensure single-worker constraint is never violated
            if len(self.active_workers) > 0:
                logger.error(
                    f"CRITICAL: Single-worker constraint violated! "
                    f"Active workers: {list(self.active_workers.keys())}"
                )
                raise RuntimeError("Single-worker constraint violated")

            # Store process info with agent_id
            self.active_workers[task_id] = (process, project_id, log_file, time.time(), agent_id)
            
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.provider.update_worker_status({
                "active": True,
                "currentTask": task_id,
                "agentType": agent_type,
                "projectId": project_id,
                "startedAt": now_iso,
                "lastHeartbeat": now_iso,
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
        for task_id, (process, project_id, log_file, start_time, agent_id) in list(self.active_workers.items()):
            retcode = process.poll()
            if retcode is not None:
                elapsed = time.time() - start_time
                logger.info(f"Worker for {task_id} on {project_id} finished with code {retcode} ({elapsed:.1f}s)")
                log_file.close()
                finished_tasks.append((task_id, agent_id))
                
                if retcode == 0:
                    # Transition to "finalizing" state
                    self.acquire_lock(project_id, status="finalizing")
                    self.executor.submit(self._async_finalize_flow, task_id, project_id, agent_id)
                else:
                    self._handle_immediate_failure(task_id, project_id, agent_id)

        for task_id, agent_id in finished_tasks:
            if task_id in self.active_workers:
                del self.active_workers[task_id]

        if finished_tasks:
            self.update_active_worker_status()

    def _handle_immediate_failure(self, task_id: str, project_id: str, agent_id: str):
        log_file_path = WORKER_RESULTS_DIR / f"{task_id}.log"
        error_tail = ""
        try:
            with open(log_file_path, "r") as f:
                lines = f.readlines()
                error_tail = "".join(lines[-50:])
        except:
            pass
        
        self.handle_worker_failure(task_id, project_id, error_tail, agent_id)
        self.release_lock(project_id)

    def _async_finalize_flow(self, task_id: str, project_id: str, agent_id: str):
        try:
            success = self.finalize_project_changes(task_id, project_id)
            if success:
                self.handle_worker_success(task_id, project_id, agent_id)
            else:
                self.handle_worker_failure(task_id, project_id, "Project repo push failed", agent_id)
        except Exception as e:
            logger.error(f"Error in finalization for {task_id}: {e}")
            self.handle_worker_failure(task_id, project_id, str(e), agent_id)
        finally:
            self.release_lock(project_id)

    def finalize_project_changes(self, task_id: str, project_id: str) -> bool:
        """Commits and pushes changes in the project repository."""
        project_path = self._get_repo_path(project_id)
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

            # Push
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

    def _cleanup_worker_session(self, agent_id: str):
        """
        Clean up the worker agent's session files after task completion.

        IMPORTANT: We do NOT use sessions.reset with agentId because that would
        reset ALL sessions for the agent. Instead, we delete the specific session
        files for the worker agent to clear its context between tasks.

        This approach:
        - Clears worker context between tasks (fresh start)
        - Does not affect any other sessions in the system
        - Safe to run after each task completion
        """
        logger.info(f"[WORKER] Cleaning up session for agent {agent_id}...")
        try:
            # Delete worker session files directly
            # Worker sessions are stored in ~/.openclaw/agents/worker/sessions/
            agent_sessions_dir = Path.home() / ".openclaw" / "agents" / agent_id / "sessions"

            if not agent_sessions_dir.exists():
                logger.debug(f"No session directory found for agent {agent_id}")
                return

            # Delete all session files (JSONL transcripts, lock files, and sessions.json store)
            deleted_count = 0
            for session_file in agent_sessions_dir.glob("*.jsonl*"):
                try:
                    session_file.unlink()
                    deleted_count += 1
                except Exception as e:
                    logger.warning(f"Failed to delete session file {session_file}: {e}")

            # Also clear the sessions store (sessions.json)
            store_file = agent_sessions_dir / "sessions.json"
            if store_file.exists():
                try:
                    store_file.unlink()
                    logger.debug(f"Deleted sessions store for agent {agent_id}")
                except Exception as e:
                    logger.warning(f"Failed to delete sessions store: {e}")

            if deleted_count > 0:
                logger.info(f"[WORKER] Cleared {deleted_count} session file(s) for agent {agent_id} - fresh session ready for next task")
            else:
                logger.debug(f"No session files to clean for agent {agent_id}")

        except Exception as e:
            logger.warning(f"Error cleaning worker sessions for {agent_id}: {e}")

    def handle_worker_success(self, task_id: str, project_id: str, agent_id: str):
        logger.info(f"Worker success for task {task_id}. Updating state.")
        
        # Check if worker already marked task as completed via complete-task script
        # If so, don't duplicate the update
        from orchestrator.config import TASKS_DIR
        task_file = TASKS_DIR / f"{task_id}.json"
        if task_file.exists():
            try:
                with open(task_file, "r") as f:
                    task_data = json.load(f)
                    if task_data.get("workState") == "completed":
                        logger.info(f"Task {task_id} already marked completed by worker")
                        self._cleanup_worker_session(agent_id)
                        return
            except Exception:
                pass
        
        # Auto-complete if worker didn't do it
        self.provider.update_task(task_id, {
            "workState": "completed",
            "status": "completed",
            "action": "complete",
            "summary": f"Task completed successfully"
        })
        
        # Reset the worker session for next task
        self._cleanup_worker_session(agent_id)

    def handle_worker_failure(self, task_id: str, project_id: str, error_log: str, agent_id: str):
        logger.error(f"Worker failure for task {task_id} on {project_id}")
        self.escalation.process_failure(task_id, project_id, error_log)
        
        # Still update task to failed so it doesn't get re-run immediately by scanner
        self.provider.update_task(task_id, {"workState": "failed"})
        
        # Reset the worker session for next task
        self._cleanup_worker_session(agent_id)

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

    def is_worker_busy(self) -> bool:
        """
        Check if the single worker is currently busy.

        Returns:
            True if worker is active or pending (syncing/finalizing)
        """
        return bool(self.active_workers or self.pending_workers)

    def get_worker_status(self) -> dict[str, Any]:
        """
        Get detailed worker status for logging/monitoring.

        Returns:
            Dict with worker status including:
            - busy: whether worker is active
            - current_task: ID of current task (if any)
            - state: 'idle', 'syncing', 'running', or 'finalizing'
        """
        if self.pending_workers:
            task_id = list(self.pending_workers)[0]
            return {
                "busy": True,
                "current_task": task_id,
                "state": "syncing/finalizing",
            }
        elif self.active_workers:
            task_id = list(self.active_workers.keys())[0]
            return {
                "busy": True,
                "current_task": task_id,
                "state": "running",
            }
        else:
            return {
                "busy": False,
                "current_task": None,
                "state": "idle",
            }
