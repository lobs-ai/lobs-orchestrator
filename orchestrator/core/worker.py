import subprocess
import logging
import json
import time
import os
import sys
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Any
from concurrent.futures import ThreadPoolExecutor

from orchestrator.config import (
    BASE_DIR,
    WORKER_RESULTS_DIR,
    WORKER_STATUS_JSON,
    ORCHESTRATOR_REPO_PATH
)
from orchestrator.providers.base import TaskProvider
from orchestrator.core.escalation import EscalationManager
from orchestrator.core.agents import AgentManager
from orchestrator.core.ollama_client import OllamaClient
from orchestrator.core.registry import AgentRegistry, AgentConfig

logger = logging.getLogger(__name__)


class _PidMonitor:
    """
    Lightweight wrapper to monitor an existing PID.
    Used when re-adopting workers after orchestrator restart.
    Mimics the subset of subprocess.Popen interface we need.
    """
    def __init__(self, pid: int):
        self.pid = pid
    
    def poll(self) -> Optional[int]:
        """Check if process is still running. Returns None if running, 0 if finished."""
        try:
            os.kill(self.pid, 0)
            return None  # Still running
        except ProcessLookupError:
            return 0  # Process finished (assume success)
        except PermissionError:
            return None  # Can't check, assume running


from orchestrator.core.failure_rotation import FailureRotation


def _normalize_agent_template_type(agent_type: str) -> str:
    """Normalize legacy agent_type values to agent template types.

    Historically, `agentType` was metadata (e.g. 'worker', 'task-runner').
    With agent templates living under `agents/<type>/`, we treat those legacy
    values as the default 'programmer' template.
    """

    t = (agent_type or "").strip().lower()
    if not t:
        return "programmer"

    legacy_to_programmer = {
        "worker",
        "task-runner",
        "worker-template",
    }
    if t in legacy_to_programmer:
        return "programmer"

    return t


def _write_agent_template_files(workspace_dir: Path, cfg: AgentConfig) -> None:
    """Write agent template context files into the worker workspace."""

    workspace_dir.mkdir(parents=True, exist_ok=True)
    (workspace_dir / "AGENTS.md").write_text(cfg.agents_md, encoding="utf-8")
    (workspace_dir / "SOUL.md").write_text(cfg.soul_md, encoding="utf-8")
    (workspace_dir / "TOOLS.md").write_text(cfg.tools_md, encoding="utf-8")
    (workspace_dir / "USER.md").write_text(cfg.user_md, encoding="utf-8")
    (workspace_dir / "IDENTITY.md").write_text(cfg.identity_md, encoding="utf-8")


class WorkerManager:
    """
    Manages spawning and tracking a SINGLE worker subprocess.

    State is persisted to a single JSON file for crash recovery.
    No per-project locks - just one worker at a time, period.

    State file contains:
    - active: bool
    - task_id: str
    - project_id: str
    - pid: int (of openclaw process)
    - state: syncing | running | finalizing
    - started_at: ISO timestamp
    - agent_id: str
    - task_title: str (for display)
    """

    STATE_FILE = Path.home() / ".openclaw" / "worker-state.json"
    STALE_TIMEOUT = 600  # 10 minutes

    def __init__(self, state_dir: Path, provider: TaskProvider, failure_rotation: FailureRotation | None = None):
        """
        Initialize WorkerManager.
        
        Args:
            state_dir: Directory for state files (kept for compatibility, but we use STATE_FILE)
            provider: Task provider for state updates
        """
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.provider = provider
        self.escalation = EscalationManager(provider)
        self.failure_rotation = failure_rotation or FailureRotation(state_dir)

        # In-memory tracking (primary source of truth while running)
        # task_id -> (process, project_id, log_file, start_time, agent_id, task_title)
        self.active_workers: dict[str, tuple[subprocess.Popen, str, Any, float, str, str]] = {}
        self.pending_workers: set[str] = set()  # Tasks in syncing/finalizing state

        # Single-threaded executor ensures one spawn at a time
        self.executor = ThreadPoolExecutor(max_workers=1)
        
        # Agent manager for provisioning the shared OpenClaw agent + legacy worker rules.
        # NOTE: `worker-template/` remains the source of WORKER_RULES.md (and fallback context)
        # for backwards compatibility.
        template_dir = ORCHESTRATOR_REPO_PATH / "worker-template"
        self.agent_manager = AgentManager(template_dir)
        self._worker_provisioned = False

        # Agent registry for per-task agent templates (agents/<type>/...)
        self.registry = AgentRegistry()
        
        # Cache for project repo paths
        self._repo_path_cache = {}

        # Ollama client for diagnostic tasks
        from orchestrator.utils.settings import get_setting
        ollama_url = get_setting("ollama_url", "http://localhost:11434")
        ollama_model = get_setting("ollama_model", "llama3.1")
        ollama_keep_alive = get_setting("ollama_keep_alive", "5m")
        self.ollama = OllamaClient(
            base_url=ollama_url,
            model=ollama_model,
            keep_alive=ollama_keep_alive
        )

        # Clean up any orphaned workers from previous run
        self._cleanup_orphaned_workers()

    # =========================================================================
    # State Persistence
    # =========================================================================

    def _load_state(self) -> dict[str, Any]:
        """Load persisted worker state from disk."""
        if not self.STATE_FILE.exists():
            return {"active": False}
        try:
            with open(self.STATE_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Failed to load worker state: {e}")
            return {"active": False}

    def _save_state(
        self,
        task_id: str,
        project_id: str,
        pid: Optional[int] = None,
        state: str = "running",
        task_title: str = "",
        agent_id: str = "worker"
    ):
        """Save worker state to disk for crash recovery."""
        self.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "active": True,
            "task_id": task_id,
            "project_id": project_id,
            "pid": pid,
            "state": state,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "agent_id": agent_id,
            "task_title": task_title,
        }
        with open(self.STATE_FILE, "w") as f:
            json.dump(data, f, indent=2)
        logger.debug(f"Saved worker state: {state} for {task_id}")

    def _clear_state(self):
        """Clear persisted worker state."""
        if self.STATE_FILE.exists():
            try:
                self.STATE_FILE.unlink()
                logger.debug("Cleared worker state file")
            except IOError as e:
                logger.warning(f"Failed to clear worker state: {e}")

    def _is_process_alive(self, pid: int) -> bool:
        """Check if a process is still running."""
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def _cleanup_orphaned_workers(self):
        """
        On startup, check for workers from a previous run.
        If still running, re-adopt them instead of killing.
        Only clean up if the process is dead.
        """
        state = self._load_state()
        if not state.get("active"):
            return

        pid = state.get("pid")
        task_id = state.get("task_id", "unknown")
        project_id = state.get("project_id", "unknown")
        agent_id = state.get("agent_id", "worker")
        task_title = state.get("task_title", task_id[:8])
        worker_state = state.get("state", "running")
        
        if pid and self._is_process_alive(pid):
            # Worker is still running - re-adopt it instead of killing
            logger.info(f"Re-adopting running worker (PID {pid}) for task {task_id[:8]} ({worker_state})")
            
            if worker_state == "running":
                # Re-open the log file and track the worker
                log_file_path = WORKER_RESULTS_DIR / f"{task_id}.log"
                try:
                    log_file = open(log_file_path, "a")  # Append mode
                    # Create a pseudo-process object to track the PID
                    # We can't get the original Popen object, but we can monitor the PID
                    self.active_workers[task_id] = (
                        _PidMonitor(pid),  # Wrapper to monitor PID
                        project_id,
                        log_file,
                        time.time(),  # Approximate - we don't know exact start time
                        agent_id,
                        task_title,
                    )
                    logger.info(f"Successfully re-adopted worker for {task_id[:8]}")
                    return  # Don't clear state - worker is still running
                except Exception as e:
                    logger.warning(f"Failed to re-adopt worker: {e}")
            elif worker_state in ("syncing", "finalizing"):
                # Worker was in a transient state - mark as pending
                self.pending_workers.add(task_id)
                logger.info(f"Worker {task_id[:8]} in {worker_state} state, marking as pending")
                return
        
        # Process is dead or we couldn't re-adopt - clean up
        if pid:
            logger.info(f"Previous worker (PID {pid}) for {task_id[:8]} is no longer running, cleaning up")
        
        self._clear_state()
        logger.info("Cleaned up stale worker state")

    # =========================================================================
    # Project Repo Path Resolution
    # =========================================================================

    def _is_git_repo(self, path: Path) -> bool:
        """Return True if the given path looks like a git repo."""
        try:
            return (path / ".git").exists()
        except Exception:
            return False

    def _get_project_record(self, project_id: str) -> dict[str, Any] | None:
        """Fetch project metadata from projects.json (if available)."""
        from orchestrator.config import PROJECTS_FILE

        if not PROJECTS_FILE.exists():
            return None

        try:
            with open(PROJECTS_FILE, "r") as f:
                data = json.load(f)
            for project in data.get("projects", []):
                if project.get("id") == project_id:
                    return project
        except Exception as e:
            logger.warning(f"Failed to read projects.json for {project_id}: {e}")

        return None

    def _get_repo_path(self, project_id: str) -> Path:
        """Get the workspace path for a project.

        For research projects that have no repoPath configured (null/empty), we default
        to using the control repo as the workspace.
        """
        if project_id in self._repo_path_cache:
            return self._repo_path_cache[project_id]

        from orchestrator.config import CONTROL_REPO_PATH

        project = self._get_project_record(project_id)
        if project is not None:
            repo_path = project.get("repoPath")
            if repo_path:
                path = Path(repo_path).resolve()
                self._repo_path_cache[project_id] = path
                return path

            # No repoPath configured. Research projects often have none.
            if project.get("type") == "research":
                logger.info(
                    f"Project {project_id} is type=research and has no repoPath; using control repo as workspace"
                )
                path = CONTROL_REPO_PATH.resolve()
                self._repo_path_cache[project_id] = path
                return path

            # Fall through to auto-discovery/fallback for non-research projects.

        # Try auto-discovery (legacy behavior)
        from orchestrator.config import PROJECTS_FILE
        if PROJECTS_FILE.exists():
            logger.info(f"Project {project_id} missing repoPath, running auto-discovery...")
            try:
                subprocess.run(
                    ["python3", "bin/discover-repos"],
                    cwd=CONTROL_REPO_PATH,
                    check=True,
                    capture_output=True,
                    timeout=10,
                )
                project2 = self._get_project_record(project_id)
                if project2 and project2.get("repoPath"):
                    path = Path(project2["repoPath"]).resolve()
                    self._repo_path_cache[project_id] = path
                    return path
            except Exception as e:
                logger.warning(f"Auto-discovery failed for {project_id}: {e}")

        # Fallback
        logger.warning(f"No repoPath found for {project_id}, using fallback: {BASE_DIR / project_id}")
        path = (BASE_DIR / project_id).resolve()
        self._repo_path_cache[project_id] = path
        return path

    # =========================================================================
    # Runtime Selection
    # =========================================================================

    def _determine_runtime(self, task: dict[str, Any]) -> str:
        """Determine which runtime to use (openclaw or ollama)."""
        if "runtime" in task:
            return task["runtime"]

        agent_type = task.get("agentType", "")
        if agent_type == "diagnostic":
            if self.ollama.is_available():
                return "ollama"
            logger.warning("Ollama not available, falling back to OpenClaw")
        
        return "openclaw"

    def _sync_workspace_for_agent_template(self, agent_template_type: str) -> str:
        """Sync the shared worker workspace to match the selected agent template.

        Returns the effective template type used (may fall back to 'programmer').
        """

        workspace_dir = self.agent_manager.openclaw_dir / "workspace-worker"

        # Always ensure WORKER_RULES.md exists (legacy behavior).
        worker_rules_src = self.agent_manager.template_dir / "WORKER_RULES.md"
        if worker_rules_src.exists():
            (workspace_dir / "WORKER_RULES.md").write_text(
                worker_rules_src.read_text(encoding="utf-8"),
                encoding="utf-8",
            )

        requested = (agent_template_type or "").strip().lower() or "programmer"

        def _try_sync(t: str) -> bool:
            try:
                cfg = self.registry.get_agent(t)
                _write_agent_template_files(workspace_dir, cfg)
                logger.info(f"[WORKER] Synced worker workspace to agent template: {t}")
                return True
            except Exception as e:
                logger.warning(f"[WORKER] Failed to sync agent template '{t}': {e}")
                return False

        # Primary: agents/<type>/
        if _try_sync(requested):
            return requested

        # Fallback: programmer
        if requested != "programmer" and _try_sync("programmer"):
            return "programmer"

        # Last resort: legacy worker-template/ sync (includes AGENTS/SOUL/etc).
        logger.warning("[WORKER] Falling back to legacy worker-template/ workspace sync")
        if not self.agent_manager.sync_worker_templates("worker"):
            logger.warning("[WORKER] Legacy template sync failed")

        return "programmer"

    # =========================================================================
    # Worker Spawning
    # =========================================================================

    def spawn_worker(
        self, task: dict[str, Any], project_id: str, agent_type: str = "programmer", rules: str = ""
    ) -> bool:
        """
        Spawn a worker for the given task.
        Returns True if spawned, False if queued (worker busy).
        """
        task_id = task["id"]
        task_title = task.get("title", task.get("prompt", task_id[:8]))

        # Check if worker is busy
        if self.active_workers or self.pending_workers:
            active = list(self.active_workers.keys())[0] if self.active_workers else list(self.pending_workers)[0]
            logger.info(f"[QUEUE] Worker busy with {active[:8]}. Queueing task {task_id[:8]} ({task_title})")
            return False

        # Mark as pending and save state
        self.pending_workers.add(task_id)
        self._save_state(task_id, project_id, state="syncing", task_title=task_title)

        # Determine runtime and spawn
        runtime = self._determine_runtime(task)
        logger.info(f"[WORKER] Task {task_id[:8]} will use runtime: {runtime}")

        if runtime == "ollama":
            self.executor.submit(self._async_spawn_ollama_flow, task, project_id, agent_type, rules)
        else:
            self.executor.submit(self._async_spawn_openclaw_flow, task, project_id, agent_type, rules)
        
        return True

    def _async_spawn_ollama_flow(self, task: dict[str, Any], project_id: str, agent_type: str, rules: str):
        """Spawn an Ollama worker for diagnostic/analysis tasks."""
        task_id = task["id"]
        task_title = task.get("title", task_id[:8])
        workspace = self._get_repo_path(project_id)
        agent_id = "ollama"
        start_time = time.time()

        try:
            # Sync repo (skip for research projects without repos)
            from orchestrator.config import CONTROL_REPO_PATH
            if self._is_git_repo(workspace) and workspace != CONTROL_REPO_PATH:
                logger.info(f"Syncing project repo {project_id}...")
                self._git_pull_rebase_with_autostash(workspace, task_id=task_id)
            else:
                logger.info(f"Skipping git sync for project {project_id} (workspace={workspace})")

            # Mark GitHub issue as in-progress if this is a GitHub-tracked task
            self._mark_github_in_progress(task, project_id)

            # Build prompt
            from orchestrator.services.prompter import Prompter
            prompt = Prompter.build_task_prompt(
                task,
                project_id,
                rules=rules,
                workspace_path=workspace,
                agent_type=agent_type,
            )

            # Run Ollama
            logger.info(f"Running Ollama for {task_id[:8]} with model {self.ollama.model}")
            WORKER_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            log_file_path = WORKER_RESULTS_DIR / f"{task_id}.log"

            with open(log_file_path, "w") as log_file:
                log_file.write(f"=== Ollama Task Execution ===\n")
                log_file.write(f"Task ID: {task_id}\nModel: {self.ollama.model}\n")
                log_file.write(f"Project: {project_id}\nAgent Type: {agent_type}\n\n")
                log_file.write(f"=== Prompt ===\n{prompt}\n\n=== Response ===\n")
                log_file.flush()

                try:
                    for chunk in self.ollama.generate(prompt, stream=True, temperature=0.3):
                        if "response" in chunk:
                            log_file.write(chunk["response"])
                            log_file.flush()
                        if chunk.get("done", False):
                            break

                    log_file.write(f"\n\n=== Completed ===\n")
                    self._save_state(task_id, project_id, state="finalizing", task_title=task_title, agent_id=agent_id)

                    success = self.finalize_project_changes(task_id, project_id, task_title)
                    if success:
                        self.handle_worker_success(task_id, project_id, agent_id, start_time, task_title)
                    else:
                        self.handle_worker_failure(
                            task_id,
                            project_id,
                            "Failed to finalize changes",
                            agent_id,
                            start_time=start_time,
                            task_title=task_title,
                            failure_reason="finalize_failed",
                        )

                except Exception as e:
                    error_msg = f"Ollama execution failed: {e}"
                    logger.error(error_msg)
                    log_file.write(f"\n\n=== ERROR ===\n{error_msg}\n")
                    self.handle_worker_failure(
                        task_id,
                        project_id,
                        error_msg,
                        agent_id,
                        start_time=start_time,
                        task_title=task_title,
                        failure_reason="ollama_failed",
                    )

        except Exception as e:
            logger.error(f"Failed to spawn Ollama worker for {task_id}: {e}")
            self.handle_worker_failure(
                task_id,
                project_id,
                str(e),
                agent_id,
                start_time=start_time,
                task_title=task_title,
                failure_reason="ollama_spawn_failed",
            )
        finally:
            self._clear_state()
            self.pending_workers.discard(task_id)
            self._update_provider_status()

    def _async_spawn_openclaw_flow(self, task: dict[str, Any], project_id: str, agent_type: str, rules: str):
        """Spawn an OpenClaw worker."""
        task_id = task["id"]
        task_title = task.get("title", task_id[:8])
        workspace = self._get_repo_path(project_id)
        agent_id = "worker"

        try:
            logger.info(f"[WORKER] Using shared worker agent: {agent_id} (template: {_normalize_agent_template_type(agent_type)})")

            # One-time provisioning
            if not self._worker_provisioned:
                needs_provision = not self.agent_manager.worker_exists("worker")
                needs_registration = not self.agent_manager.is_agent_registered("worker")

                if needs_provision or needs_registration:
                    logger.info("Provisioning shared worker agent...")
                    if not self.agent_manager.provision_worker("worker", register=True):
                        raise Exception("Failed to provision worker agent")

                    if needs_registration:
                        logger.warning("New worker registered - restarting gateway...")
                        if not self.agent_manager.restart_gateway():
                            raise Exception("Gateway restart failed after agent registration")
                        time.sleep(3)
                
                self._worker_provisioned = True
            
            # Sync workspace context for this task's agent template.
            # We always keep WORKER_RULES.md in the workspace (legacy path), but we
            # swap AGENTS/SOUL/TOOLS/USER/IDENTITY per agent_type.
            template_type = _normalize_agent_template_type(agent_type)
            effective_template_type = self._sync_workspace_for_agent_template(template_type)

            # Sync repo (skip for research projects without repos)
            from orchestrator.config import CONTROL_REPO_PATH
            if self._is_git_repo(workspace) and workspace != CONTROL_REPO_PATH:
                logger.info(f"Syncing project repo {project_id}...")
                self._git_pull_rebase_with_autostash(workspace, task_id=task_id)
            else:
                logger.info(f"Skipping git sync for project {project_id} (workspace={workspace})")

            # Mark GitHub issue as in-progress if this is a GitHub-tracked task
            self._mark_github_in_progress(task, project_id)

            # Build prompt
            from orchestrator.services.prompter import Prompter
            prompt = Prompter.build_task_prompt(
                task,
                project_id,
                rules=rules,
                workspace_path=workspace,
                agent_type=agent_type,
            )

            # Launch OpenClaw
            from orchestrator.utils.settings import get_setting
            executable = get_setting("openclaw_executable", "openclaw")
            cmd = [executable, "agent", "--agent", agent_id, "-m", prompt]
            
            WORKER_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            log_file_path = WORKER_RESULTS_DIR / f"{task_id}.log"
            log_file = open(log_file_path, "w")

            logger.info(f"Invoking OpenClaw for {task_id[:8]} with agent {agent_id}")
            process = subprocess.Popen(
                cmd, 
                stdout=log_file, 
                stderr=subprocess.STDOUT, 
                text=True,
                start_new_session=True,
                cwd=workspace
            )

            # Update state with PID
            self._save_state(task_id, project_id, pid=process.pid, state="running", task_title=task_title, agent_id=agent_id)

            # Validation
            if self.active_workers:
                raise RuntimeError("Single-worker constraint violated")

            # Track active worker
            self.active_workers[task_id] = (process, project_id, log_file, time.time(), agent_id, task_title)
            self.pending_workers.discard(task_id)
            
            # Update provider status
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.provider.update_worker_status({
                "active": True,
                "currentTask": task_id,
                "agentType": effective_template_type,
                "projectId": project_id,
                "startedAt": now_iso,
                "lastHeartbeat": now_iso,
            })

        except Exception as e:
            logger.error(f"Failed to spawn worker for {task_id}: {e}")
            self._clear_state()
            self.handle_worker_failure(
                task_id,
                project_id,
                str(e),
                agent_id,
                task_title=task_title,
                failure_reason="spawn_failed",
            )
            self.pending_workers.discard(task_id)
            self._update_provider_status()

    # =========================================================================
    # Worker Lifecycle
    # =========================================================================

    def check_workers(self):
        """Check status of active workers and handle completion."""
        finished = []
        for task_id, (process, project_id, log_file, start_time, agent_id, task_title) in list(self.active_workers.items()):
            retcode = process.poll()
            if retcode is not None:
                elapsed = time.time() - start_time
                logger.info(f"Worker for {task_id[:8]} finished with code {retcode} ({elapsed:.1f}s)")
                log_file.close()
                finished.append((task_id, project_id, agent_id, retcode, start_time, task_title))

        for task_id, project_id, agent_id, retcode, start_time, task_title in finished:
            del self.active_workers[task_id]
            
            if retcode == 0:
                self._save_state(task_id, project_id, state="finalizing", agent_id=agent_id)
                self.executor.submit(self._async_finalize_flow, task_id, project_id, agent_id, start_time, task_title)
            else:
                self._handle_immediate_failure(task_id, project_id, agent_id, start_time, task_title)

        if finished:
            self._update_provider_status()

    def _handle_immediate_failure(
        self,
        task_id: str,
        project_id: str,
        agent_id: str,
        start_time: float | None = None,
        task_title: str = "",
    ):
        """Handle worker failure (non-zero exit).

        We attempt to finalize any repo changes as WIP before marking the task failed,
        to avoid leaving orphaned uncommitted changes that would break the next spawn.
        """
        log_file_path = WORKER_RESULTS_DIR / f"{task_id}.log"
        error_tail = ""
        try:
            with open(log_file_path, "r") as f:
                lines = f.readlines()
                error_tail = "".join(lines[-50:])
        except Exception:
            pass

        try:
            self.finalize_project_changes_wip(
                task_id,
                project_id,
                task_title=task_title,
                failure_reason="worker_failed",
            )
        except Exception as e:
            logger.warning(f"WIP finalization failed for {task_id[:8]}: {e}")

        self.handle_worker_failure(
            task_id,
            project_id,
            error_tail,
            agent_id,
            start_time=start_time,
            task_title=task_title,
            failure_reason="worker_nonzero_exit",
        )
        self._clear_state()

    def _async_finalize_flow(self, task_id: str, project_id: str, agent_id: str, start_time: float, task_title: str = ""):
        """Finalize worker completion (commit, push, update state)."""
        try:
            success = self.finalize_project_changes(task_id, project_id, task_title)
            if success:
                self.handle_worker_success(task_id, project_id, agent_id, start_time, task_title)
            else:
                self.handle_worker_failure(
                    task_id,
                    project_id,
                    "Project repo push failed",
                    agent_id,
                    start_time=start_time,
                    task_title=task_title,
                    failure_reason="finalize_failed",
                )
        except Exception as e:
            logger.error(f"Error in finalization for {task_id}: {e}")
            self.handle_worker_failure(
                task_id,
                project_id,
                str(e),
                agent_id,
                start_time=start_time,
                task_title=task_title,
                failure_reason="finalize_exception",
            )
        finally:
            self._clear_state()
            self.pending_workers.discard(task_id)
            self._update_provider_status()

    # =========================================================================
    # GitHub Integration
    # =========================================================================

    def _mark_github_in_progress(self, task: dict[str, Any], project_id: str):
        """Mark a GitHub issue as in-progress if this is a GitHub-tracked task."""
        github_meta = task.get("githubMeta")
        if not github_meta:
            return  # Not a GitHub task
        
        issue_number = github_meta.get("number")
        if not issue_number:
            logger.warning(f"GitHub task {task['id']} missing issue number")
            return
        
        from orchestrator.config import CONTROL_REPO_PATH
        script_path = CONTROL_REPO_PATH / "bin" / "gh-mark-in-progress"
        
        if not script_path.exists():
            logger.warning(f"gh-mark-in-progress script not found at {script_path}")
            return
        
        try:
            logger.info(f"Marking GitHub issue #{issue_number} as in-progress for project {project_id}")
            result = subprocess.run(
                [str(script_path), project_id, str(issue_number)],
                cwd=CONTROL_REPO_PATH,
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode == 0:
                logger.info(f"Successfully marked issue #{issue_number} as in-progress")
            else:
                # Log warning but don't fail the task
                error_msg = result.stderr.strip() if result.stderr else result.stdout.strip()
                logger.warning(f"Failed to mark GitHub issue in-progress: {error_msg}")
                
        except subprocess.TimeoutExpired:
            logger.warning(f"Timeout marking GitHub issue #{issue_number} as in-progress")
        except Exception as e:
            logger.warning(f"Error marking GitHub issue #{issue_number} as in-progress: {e}")

    def _close_github_issue_if_needed(self, task: dict[str, Any], project_id: str, task_id: str):
        """Close GitHub issue if this is a GitHub-tracked task."""
        github_meta = task.get("githubMeta")
        if not github_meta:
            return  # Not a GitHub task
        
        issue_number = github_meta.get("number")
        if not issue_number:
            logger.warning(f"GitHub task {task_id} missing issue number")
            return
        
        from orchestrator.config import CONTROL_REPO_PATH
        script_path = CONTROL_REPO_PATH / "bin" / "gh-close-issue"
        
        if not script_path.exists():
            logger.warning(f"gh-close-issue script not found at {script_path}")
            return
        
        try:
            # Extract worker summary from log file
            summary = self._extract_worker_summary(task_id)
            
            # Get recent commit hashes from project repo
            commits = self._get_recent_commits(project_id, limit=3)
            
            # Format comment
            comment = "✅ Completed by Lobs\n\n"
            
            if summary:
                comment += f"{summary}\n\n"
            
            if commits:
                commit_list = "\n".join([f"- {c}" for c in commits])
                comment += f"Commits:\n{commit_list}"
            
            logger.info(f"Closing GitHub issue #{issue_number} for project {project_id}")
            result = subprocess.run(
                [str(script_path), project_id, str(issue_number), "--comment", comment],
                cwd=CONTROL_REPO_PATH,
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode == 0:
                logger.info(f"Successfully closed GitHub issue #{issue_number}")
                # Refresh GitHub cache
                self._refresh_github_cache(project_id)
            else:
                error_msg = result.stderr.strip() if result.stderr else result.stdout.strip()
                logger.warning(f"Failed to close GitHub issue: {error_msg}")
                
        except subprocess.TimeoutExpired:
            logger.warning(f"Timeout closing GitHub issue #{issue_number}")
        except Exception as e:
            logger.warning(f"Error closing GitHub issue #{issue_number}: {e}")

    def _extract_worker_summary(self, task_id: str) -> str:
        """Extract worker summary from task log file."""
        log_file_path = WORKER_RESULTS_DIR / f"{task_id}.log"
        
        if not log_file_path.exists():
            return ""
        
        try:
            with open(log_file_path, "r") as f:
                content = f.read()
            
            # Look for common summary patterns:
            # 1. Content after "Summary:" marker
            # 2. Last substantive paragraph before completion
            # 3. Extract last 500 chars as fallback
            
            lines = content.split('\n')
            summary_lines = []
            found_summary = False
            
            for line in lines:
                if 'summary:' in line.lower() or 'completed' in line.lower():
                    found_summary = True
                    continue
                if found_summary and line.strip():
                    summary_lines.append(line.strip())
                    if len(summary_lines) >= 10:  # Cap at 10 lines
                        break
            
            if summary_lines:
                return '\n'.join(summary_lines)
            
            # Fallback: last 500 chars
            return content[-500:].strip()
            
        except Exception as e:
            logger.warning(f"Failed to extract worker summary: {e}")
            return ""

    def _get_recent_commits(self, project_id: str, limit: int = 3) -> list[str]:
        """Get recent commit hashes from project repo."""
        project_path = self._get_repo_path(project_id)
        
        try:
            result = subprocess.run(
                ["git", "log", f"-{limit}", "--oneline", "--no-decorate"],
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode == 0:
                commits = result.stdout.strip().split('\n')
                return [c.strip() for c in commits if c.strip()]
            
            return []
            
        except Exception as e:
            logger.warning(f"Failed to get recent commits: {e}")
            return []

    def _refresh_github_cache(self, project_id: str):
        """Refresh GitHub issue cache for a project."""
        from orchestrator.config import CONTROL_REPO_PATH
        script_path = CONTROL_REPO_PATH / "bin" / "gh-sync"
        
        if not script_path.exists():
            logger.warning(f"gh-sync script not found at {script_path}")
            return
        
        try:
            logger.info(f"Refreshing GitHub cache for project {project_id}")
            result = subprocess.run(
                [str(script_path), project_id],
                cwd=CONTROL_REPO_PATH,
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode == 0:
                logger.info(f"Successfully refreshed GitHub cache")
            else:
                error_msg = result.stderr.strip() if result.stderr else result.stdout.strip()
                logger.warning(f"Failed to refresh GitHub cache: {error_msg}")
                
        except subprocess.TimeoutExpired:
            logger.warning(f"Timeout refreshing GitHub cache for {project_id}")
        except Exception as e:
            logger.warning(f"Error refreshing GitHub cache: {e}")

    # =========================================================================
    # Git Operations
    # =========================================================================

    def _git_status_porcelain(self, repo_path: Path, *, timeout_sec: float = 8.0) -> str:
        """Return `git status --porcelain` output (decoded)."""
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_path,
            check=True,
            capture_output=True,
            timeout=timeout_sec,
        )
        return (result.stdout or b"").decode()

    def _git_pull_rebase_with_autostash(self, repo_path: Path, *, task_id: str, timeout_sec: float = 60.0):
        """Run `git pull --rebase` while tolerating pre-existing uncommitted changes.

        Workers can crash/timeout leaving a dirty repo, which would cause the next
        `git pull --rebase` to fail. We auto-stash before pulling, then pop afterward.

        Notes:
        - Uses `git stash push -u` to include untracked files.
        - If `stash pop` fails (e.g. conflicts), we log a warning and leave the stash
          in place for manual inspection, rather than discarding work.
        """
        status = ""
        try:
            status = self._git_status_porcelain(repo_path)
        except Exception as e:
            logger.warning(f"Failed to read git status for autostash in {repo_path}: {e}")

        did_stash = False
        if status.strip():
            stash_msg = f"lobs-orchestrator autostash before pull (task {task_id[:8]})"
            logger.warning(
                f"Repo {repo_path} has uncommitted changes before pull; stashing them (task {task_id[:8]})"
            )
            subprocess.run(
                ["git", "stash", "push", "-u", "-m", stash_msg],
                cwd=repo_path,
                check=True,
                capture_output=True,
                timeout=timeout_sec,
            )
            did_stash = True

        # Pull (rebase)
        subprocess.run(
            ["git", "pull", "--rebase"],
            cwd=repo_path,
            check=True,
            capture_output=True,
            timeout=timeout_sec,
        )

        if did_stash:
            pop = subprocess.run(
                ["git", "stash", "pop"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
            )
            if pop.returncode != 0:
                # Keep stash entry for safety.
                stderr = (pop.stderr or "").strip()
                stdout = (pop.stdout or "").strip()
                msg = stderr or stdout
                logger.warning(
                    f"Autostash pop failed in {repo_path} (task {task_id[:8]}). "
                    f"Left stash for manual resolution. Details: {msg}"
                )

    def _read_work_summary(self, project_path: Path) -> tuple[str, bool]:
        """Read and clean up .work-summary file if it exists."""
        summary_file = project_path / ".work-summary"
        summary = ""
        was_blocked = False
        
        if summary_file.exists():
            try:
                summary = summary_file.read_text().strip()
                if summary.upper().startswith("BLOCKED:"):
                    was_blocked = True
                summary_file.unlink()
                logger.debug("Read and cleaned up .work-summary")
            except Exception as e:
                logger.warning(f"Failed to read .work-summary: {e}")
        
        return summary, was_blocked

    def _generate_commit_message(self, task_id: str, project_id: str, project_path: Path, work_summary: str, task_title: str = "") -> str:
        """Generate commit message from work summary, task title, or git diff."""
        if work_summary:
            lines = work_summary.split('\n')
            subject = lines[0][:72]
            body = '\n'.join(lines[1:]).strip() if len(lines) > 1 else ""
            
            msg = f"{subject}\n\n"
            if body:
                msg += f"{body}\n\n"
            msg += f"Task: {task_id}\nProject: {project_id}"
            return msg
        
        # Use task title if available
        if task_title:
            subject = task_title[:72]
            return f"{subject}\n\nTask: {task_id}\nProject: {project_id}"
        
        # Generate from diff
        try:
            files_result = subprocess.run(
                ["git", "diff", "--cached", "--name-only"],
                cwd=project_path,
                capture_output=True,
                timeout=10
            )
            files = [f for f in files_result.stdout.decode().strip().split('\n') if f]
            
            if len(files) == 1:
                subject = f"Update {files[0]}"
            elif len(files) <= 3:
                subject = f"Update {', '.join(files)}"
            else:
                dirs = set(str(Path(f).parent) for f in files)
                if len(dirs) == 1 and list(dirs)[0] != '.':
                    subject = f"Update {list(dirs)[0]}/ ({len(files)} files)"
                else:
                    subject = f"Update {len(files)} files"
            
            diff_stat = subprocess.run(
                ["git", "diff", "--cached", "--stat"],
                cwd=project_path,
                capture_output=True,
                timeout=10
            ).stdout.decode().strip()
            
            return f"{subject}\n\nTask: {task_id}\nProject: {project_id}\n\n{diff_stat}"
            
        except Exception as e:
            logger.warning(f"Failed to generate commit message from diff: {e}")
            return f"Complete task {task_id}\n\nProject: {project_id}"

    def _finalize_all_repo_changes(self, task_id: str) -> list[str]:
        """Scan all known repos for uncommitted/unpushed changes and finalize them.
        
        Returns list of repos that had changes committed/pushed.
        """
        finalized_repos = []
        
        # Get all project repos from projects.json
        try:
            import json
            from pathlib import Path
            state_dir = Path.home() / "lobs-control" / "state"
            projects_file = state_dir / "projects.json"
            
            if not projects_file.exists():
                logger.warning("projects.json not found, skipping cross-repo finalization")
                return finalized_repos
            
            with open(projects_file) as f:
                projects_data = json.load(f)
            
            # Include orchestrator and control repos explicitly
            repos_to_check = [
                str(Path.home() / "lobs-orchestrator"),
                str(Path.home() / "lobs-control"),
            ]
            
            # Add all project repos
            for project in projects_data.get("projects", []):
                repo_path = project.get("repoPath")
                if repo_path and Path(repo_path).exists():
                    repos_to_check.append(repo_path)
            
            # Check each repo for changes
            for repo_path in repos_to_check:
                try:
                    # Check for uncommitted changes
                    status_result = subprocess.run(
                        ["git", "status", "--porcelain"],
                        cwd=repo_path,
                        capture_output=True,
                        timeout=5
                    )
                    has_uncommitted = bool(status_result.stdout.decode().strip())
                    
                    # Check for unpushed commits
                    unpushed_result = subprocess.run(
                        ["git", "log", "origin/HEAD..HEAD", "--oneline"],
                        cwd=repo_path,
                        capture_output=True,
                        timeout=5
                    )
                    has_unpushed = bool(unpushed_result.stdout.decode().strip())
                    
                    if has_uncommitted:
                        # Commit changes
                        subprocess.run(["git", "add", "."], cwd=repo_path, check=True)
                        commit_msg = f"Auto-finalize: changes from task {task_id[:8]}"
                        subprocess.run(["git", "commit", "-m", commit_msg], cwd=repo_path, check=True)
                        logger.info(f"Auto-committed changes in {Path(repo_path).name}")
                        finalized_repos.append(repo_path)
                        has_unpushed = True  # Now we have commits to push
                    
                    if has_unpushed:
                        # Push commits
                        subprocess.run(["git", "push"], cwd=repo_path, check=True)
                        logger.info(f"Auto-pushed changes in {Path(repo_path).name}")
                        if repo_path not in finalized_repos:
                            finalized_repos.append(repo_path)
                    
                except subprocess.CalledProcessError as e:
                    logger.warning(f"Git operation failed for {Path(repo_path).name}: {e}")
                except subprocess.TimeoutExpired:
                    logger.warning(f"Git operation timed out for {Path(repo_path).name}")
                except Exception as e:
                    logger.warning(f"Failed to check/finalize {Path(repo_path).name}: {e}")
            
            return finalized_repos
            
        except Exception as e:
            logger.error(f"Failed to scan repos for finalization: {e}")
            return finalized_repos

    def finalize_project_changes_wip(
        self,
        task_id: str,
        project_id: str,
        *,
        task_title: str = "",
        failure_reason: str = "failed",
    ) -> bool:
        """Best-effort finalization on failure.

        If the worker crashes/times out but left changes in the repo, we commit them
        with a WIP message and attempt to push, so the next task doesn't get stuck
        behind uncommitted changes.
        """
        project_path = self._get_repo_path(project_id)

        from orchestrator.config import CONTROL_REPO_PATH
        if (not self._is_git_repo(project_path)) or project_path == CONTROL_REPO_PATH:
            logger.info(f"Skipping WIP git finalization for project {project_id} (workspace={project_path})")
            return True

        try:
            status = self._git_status_porcelain(project_path)
        except Exception as e:
            logger.warning(f"Failed to read git status for WIP finalization in {project_path}: {e}")
            return False

        if not status.strip():
            logger.info(f"No repo changes to WIP-commit for failed task {task_id[:8]} ({project_id})")
            return True

        subject_base = task_title or f"task {task_id[:8]}"
        subject = f"WIP: {subject_base}"
        subject = subject[:72]
        body = f"Task: {task_id}\nProject: {project_id}\nReason: {failure_reason}"

        try:
            subprocess.run(["git", "add", "."], cwd=project_path, check=True)
            subprocess.run(["git", "commit", "-m", subject, "-m", body], cwd=project_path, check=True)
            logger.info(f"WIP-committed changes for failed task {task_id[:8]} in {project_id}")
        except subprocess.CalledProcessError as e:
            # If commit fails (e.g. conflicts), don't crash failure handling.
            logger.warning(f"WIP commit failed for {project_id} (task {task_id[:8]}): {e}")
            return False

        try:
            subprocess.run(["git", "push"], cwd=project_path, check=True)
            logger.info(f"WIP-pushed changes for failed task {task_id[:8]} to {project_id}")
        except subprocess.CalledProcessError as e:
            logger.warning(f"WIP push failed for {project_id} (task {task_id[:8]}): {e}")
            # Still consider it a partial success: repo is clean now.

        # Also attempt to finalize any straggler changes elsewhere.
        try:
            finalized_repos = self._finalize_all_repo_changes(task_id)
            if finalized_repos:
                logger.info(f"Auto-finalized {len(finalized_repos)} additional repos after failure")
        except Exception as e:
            logger.warning(f"Cross-repo finalization after failure failed: {e}")

        return True

    def finalize_project_changes(self, task_id: str, project_id: str, task_title: str = "") -> bool:
        """Commit and push changes in the project repository, then scan all other repos.

        Research projects may not have a project repo. In that case we skip git finalization
        entirely (worker output should be written to worker-results or a research docs path).
        """
        project_path = self._get_repo_path(project_id)

        try:
            work_summary, was_blocked = self._read_work_summary(project_path)

            if was_blocked:
                logger.warning(f"Worker indicated task {task_id} is blocked: {work_summary}")
                return False

            from orchestrator.config import CONTROL_REPO_PATH
            if (not self._is_git_repo(project_path)) or project_path == CONTROL_REPO_PATH:
                logger.info(
                    f"Skipping git finalization for project {project_id} (workspace={project_path})"
                )
                return True

            # Check for changes in project repo
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=project_path,
                check=True,
                capture_output=True,
            ).stdout.decode()

            if status:
                # Stage and commit
                subprocess.run(["git", "add", "."], cwd=project_path, check=True)
                commit_msg = self._generate_commit_message(task_id, project_id, project_path, work_summary, task_title)
                subprocess.run(["git", "commit", "-m", commit_msg], cwd=project_path, check=True)
                logger.info(f"Committed changes for task {task_id}")

                # Push
                subprocess.run(["git", "push"], cwd=project_path, check=True)
                logger.info(f"Pushed changes for task {task_id} to {project_id}")
            else:
                logger.info(f"No changes to commit for task {task_id} in project repo")

            # Now check and finalize ALL other repos with changes
            finalized_repos = self._finalize_all_repo_changes(task_id)
            if finalized_repos:
                logger.info(f"Auto-finalized {len(finalized_repos)} additional repos")

            return True
            
        except subprocess.CalledProcessError as e:
            logger.error(f"Git operation failed for {project_id}: {e.stderr.decode() if e.stderr else str(e)}")
            return False

    # =========================================================================
    # Session Cleanup
    # =========================================================================

    def _cleanup_worker_session(self, agent_id: str):
        """Clean up worker agent's session via OpenClaw gateway API."""
        logger.info(f"[WORKER] Cleaning up session for agent {agent_id}...")
        try:
            from orchestrator.utils.settings import get_setting
            executable = get_setting("openclaw_executable", "openclaw")
            
            # Use the gateway API to properly delete the session
            # This archives the transcript and removes the session entry
            session_key = f"agent:{agent_id}:main"
            result = subprocess.run(
                [executable, "gateway", "call", "sessions.delete", 
                 "--params", f'{{"key":"{session_key}"}}'],
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode == 0:
                logger.info(f"[WORKER] Deleted session {session_key} via gateway API")
                # Parse result to log archived files
                try:
                    import json
                    data = json.loads(result.stdout)
                    if data.get("archived"):
                        logger.debug(f"[WORKER] Archived: {data['archived']}")
                except Exception:
                    pass
            else:
                logger.warning(f"[WORKER] Gateway session delete failed: {result.stderr}")
                # Fall back to file-based cleanup
                self._cleanup_worker_session_files(agent_id)
                
        except subprocess.TimeoutExpired:
            logger.warning(f"[WORKER] Session cleanup timed out, falling back to file cleanup")
            self._cleanup_worker_session_files(agent_id)
        except Exception as e:
            logger.warning(f"Error cleaning worker sessions via API: {e}")
            self._cleanup_worker_session_files(agent_id)

    def _cleanup_worker_session_files(self, agent_id: str):
        """Fallback: Clean up worker agent's session files directly."""
        try:
            sessions_dir = Path.home() / ".openclaw" / "agents" / agent_id / "sessions"
            if not sessions_dir.exists():
                return

            deleted = 0
            for f in sessions_dir.glob("*.jsonl"):
                try:
                    f.unlink()
                    deleted += 1
                except Exception:
                    pass
            
            # Also clean up lock files
            for f in sessions_dir.glob("*.jsonl.lock"):
                try:
                    f.unlink()
                except Exception:
                    pass

            # Clean up archived deleted files older than 1 hour
            import time
            cutoff = time.time() - 3600
            for f in sessions_dir.glob("*.deleted.*"):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                except Exception:
                    pass

            if deleted > 0:
                logger.info(f"[WORKER] Cleared {deleted} session file(s) for agent {agent_id}")

        except Exception as e:
            logger.warning(f"Error cleaning worker session files: {e}")

    # =========================================================================
    # Usage Tracking
    # =========================================================================

    def _read_openclaw_session_usage_from_files(self, agent_id: str) -> dict[str, Any] | None:
        """Best-effort usage extraction from OpenClaw session jsonl files.

        This is more reliable than parsing `openclaw session-status` output because it reads
        structured JSON lines that already contain per-turn usage.
        """
        sessions_dir = Path.home() / ".openclaw" / "agents" / agent_id / "sessions"
        sessions_json_path = sessions_dir / "sessions.json"

        if not sessions_json_path.exists():
            return None

        try:
            sessions = json.loads(sessions_json_path.read_text())
        except Exception as e:
            logger.warning(f"[USAGE] Failed reading sessions.json for {agent_id}: {e}")
            return None

        session_key = f"agent:{agent_id}:main"
        session_meta = sessions.get(session_key) or {}
        session_id = session_meta.get("sessionId")

        transcript_path: Path | None = None
        if session_id:
            candidate = sessions_dir / f"{session_id}.jsonl"
            if candidate.exists():
                transcript_path = candidate

        if transcript_path is None:
            # Fallback: choose most recently modified transcript.
            candidates = sorted(sessions_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
            transcript_path = candidates[0] if candidates else None

        if transcript_path is None or not transcript_path.exists():
            return None

        input_tokens = 0
        output_tokens = 0
        total_cost_usd = 0.0
        model_id: str | None = None

        try:
            with open(transcript_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        evt = json.loads(line)
                    except Exception:
                        continue

                    evt_type = evt.get("type")

                    if evt_type == "model_change":
                        model_id = evt.get("modelId") or model_id

                    if evt_type == "custom" and evt.get("customType") == "model-snapshot":
                        data = evt.get("data") or {}
                        model_id = data.get("modelId") or model_id

                    # OpenClaw includes per-message usage on assistant messages.
                    if evt_type == "message":
                        usage = evt.get("usage") or {}
                        try:
                            input_tokens += int(usage.get("input") or 0)
                            output_tokens += int(usage.get("output") or 0)
                        except Exception:
                            pass

                        try:
                            cost = usage.get("cost") or {}
                            total_cost_usd += float(cost.get("total") or 0.0)
                        except Exception:
                            pass

        except Exception as e:
            logger.warning(f"[USAGE] Failed parsing session transcript for {agent_id}: {e}")
            return None

        if input_tokens == 0 and output_tokens == 0:
            # It's possible we captured before any assistant turn wrote usage.
            return None

        return {
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "model": model_id,
            "totalCostUSD": total_cost_usd if total_cost_usd > 0 else None,
            "source": "session-files",
            "transcriptPath": str(transcript_path),
        }

    def _read_openclaw_session_usage_from_cli(self, agent_id: str, timeout_sec: float = 8.0) -> dict[str, Any] | None:
        """Fallback usage extraction by calling `openclaw session-status`.

        This method is inherently fragile (human-oriented output); keep it as a backup.
        """
        from orchestrator.utils.settings import get_setting

        executable = get_setting("openclaw_executable", "openclaw")
        try:
            result = subprocess.run(
                [executable, "session-status", "--agent", agent_id],
                capture_output=True,
                text=True,
                timeout=timeout_sec,
            )
        except subprocess.TimeoutExpired:
            logger.warning(f"[USAGE] Timeout running session-status for {agent_id} after {timeout_sec:.1f}s")
            return None
        except Exception as e:
            logger.warning(f"[USAGE] Failed running session-status for {agent_id}: {e}")
            return None

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            logger.warning(f"[USAGE] session-status failed for {agent_id} (code={result.returncode}): {stderr}")
            return None

        output = result.stdout or ""
        import re

        # Extract common patterns like:
        # - "Tokens: 1.4k in / 239 out"
        # - "Tokens: 1400 in / 239 out"
        tok_match = re.search(
            r"Tokens:\s*([0-9\.,kKmM]+)\s*in\s*/\s*([0-9\.,kKmM]+)\s*out",
            output,
        )
        if not tok_match:
            logger.warning(f"[USAGE] Could not parse token counts from session-status output for {agent_id}")
            return None

        input_tokens = self._parse_token_num(tok_match.group(1))
        output_tokens = self._parse_token_num(tok_match.group(2))

        model_id: str | None = None
        model_match = re.search(r"Model:\s+([^\s]+)", output)
        if model_match:
            model_id = model_match.group(1).strip().split("/")[-1]

        return {
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "model": model_id,
            "totalCostUSD": None,
            "source": "session-status",
        }

    def _estimate_usage_fallback(self, task_id: str) -> dict[str, Any] | None:
        """Last-resort usage estimate when precise capture fails.

        We estimate tokens from worker log bytes. This is intentionally coarse and is only used
        to avoid losing all telemetry when OpenClaw introspection fails.
        """
        log_path = WORKER_RESULTS_DIR / f"{task_id}.log"
        if not log_path.exists():
            return None

        try:
            byte_count = log_path.stat().st_size
        except Exception:
            return None

        # Rough heuristic: ~4 chars/token for English-ish text.
        total_tokens = max(1, int(byte_count / 4))
        input_tokens = int(total_tokens * 0.7)
        output_tokens = total_tokens - input_tokens

        return {
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "model": None,
            "totalCostUSD": None,
            "source": "estimate",
        }

    def _capture_worker_usage(
        self,
        agent_id: str,
        task_id: str,
        start_time: float | None,
        *,
        succeeded: bool,
        failure_reason: str | None = None,
    ):
        """Capture and log worker session usage stats.

        Captures on BOTH success and failure.
        """
        usage: dict[str, Any] | None = None

        # Prefer structured session files.
        usage = self._read_openclaw_session_usage_from_files(agent_id)
        if usage is None:
            logger.warning(f"[USAGE] Could not read usage from session files for {agent_id}; falling back to CLI")
            usage = self._read_openclaw_session_usage_from_cli(agent_id)

        if usage is None:
            logger.warning(f"[USAGE] Could not read usage from OpenClaw for {agent_id}; falling back to estimate")
            usage = self._estimate_usage_fallback(task_id)

        if usage is None:
            logger.warning(f"[USAGE] Failed to capture usage for task {task_id[:8]} (no fallbacks succeeded)")
            return

        input_tokens = int(usage.get("inputTokens") or 0)
        output_tokens = int(usage.get("outputTokens") or 0)
        model = usage.get("model") or "unknown"

        # Cost: prefer OpenClaw-provided cost if present, otherwise compute.
        cost_usd: float | None = None
        if usage.get("totalCostUSD") is not None:
            try:
                cost_usd = float(usage["totalCostUSD"])
            except Exception:
                cost_usd = None

        if cost_usd is None:
            # Calculate cost using lobs-control pricing module if available.
            try:
                sys.path.insert(0, str(ORCHESTRATOR_REPO_PATH / "lobs-control" / "bin"))
                from lib.pricing import compute_cost

                cost_usd = float(compute_cost(input_tokens, output_tokens, str(model)))
            except Exception as e:
                logger.warning(f"[USAGE] Could not compute cost precisely (model={model}): {e}. Using rough estimate")
                # Generic rough estimate (kept intentionally conservative)
                cost_usd = (input_tokens / 1_000_000 * 2.0) + (output_tokens / 1_000_000 * 8.0)

        # Read worker status to get workerId and startedAt
        worker_status_path = ORCHESTRATOR_REPO_PATH / "lobs-control" / "state" / "worker-status.json"
        worker_id = None
        started_at = (
            datetime.fromtimestamp(start_time, tz=timezone.utc).isoformat()
            if start_time is not None
            else datetime.now(timezone.utc).isoformat()
        )

        if worker_status_path.exists():
            try:
                with open(worker_status_path) as f:
                    status_data = json.load(f)
                worker_id = status_data.get("workerId")
                if status_data.get("startedAt"):
                    started_at = status_data["startedAt"]
            except Exception as e:
                logger.warning(f"[USAGE] Failed reading worker-status.json for usage capture: {e}")

        usage_data = {
            "workerId": worker_id or str(int(time.time())),
            "startedAt": started_at,
            "endedAt": datetime.now(timezone.utc).isoformat(),
            "tasksCompleted": 1 if succeeded else 0,
            "timeoutReason": None if succeeded else (failure_reason or "failed"),
            "model": str(model),
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "totalTokens": input_tokens + output_tokens,
            "totalCostUSD": round(float(cost_usd), 4),
            "taskId": task_id,
            "succeeded": bool(succeeded),
            "source": usage.get("source"),
        }

        # Request control-op to log usage (single-writer pattern)
        from orchestrator.services.control import ControlManager

        ControlManager.request_op(
            {
                "type": "log_worker_usage",
                "usage_data": usage_data,
                "summary": f"log worker usage for task {task_id[:8]}",
            }
        )

        total_tokens = input_tokens + output_tokens
        logger.info(
            f"Requested worker usage log ({usage.get('source')}): {total_tokens} tokens, ${float(cost_usd):.4f} "
            f"(task={task_id[:8]}, succeeded={succeeded})"
        )

    def _parse_token_num(self, s: str) -> int:
        """Parse token number with k/M suffix."""
        s = s.strip().replace(",", "")
        m = __import__('re').fullmatch(r"(\d+(?:\.\d+)?)([kKmM]?)", s)
        if not m:
            return 0
        val = float(m.group(1))
        suf = m.group(2).lower()
        if suf == "k":
            val *= 1_000
        elif suf == "m":
            val *= 1_000_000
        return int(round(val))

    # =========================================================================
    # Success/Failure Handlers
    # =========================================================================

    def handle_worker_success(
        self,
        task_id: str,
        project_id: str,
        agent_id: str,
        start_time: float,
        task_title: str = "",
    ):
        """Handle successful worker completion.

        Note: we still run a best-effort finalization here to ensure the repo is clean
        even if a caller bypassed the normal finalize flow.
        """
        logger.info(f"Worker success for task {task_id[:8]}. Marking complete.")

        # Ensure the repo is finalized/clean even on success.
        try:
            ok = self.finalize_project_changes(task_id, project_id, task_title)
            if not ok:
                logger.warning(
                    f"Finalization reported failure on success path for task {task_id[:8]} ({project_id}); "
                    "attempting WIP finalization to keep repo clean"
                )
                self.finalize_project_changes_wip(
                    task_id,
                    project_id,
                    task_title=task_title,
                    failure_reason="finalize_failed_on_success",
                )
        except Exception:
            logger.warning(
                f"Unexpected error finalizing repo on success path for task {task_id[:8]} ({project_id})",
                exc_info=True,
            )

        # Capture usage stats before cleaning up session
        self._capture_worker_usage(agent_id, task_id, start_time, succeeded=True)

        # Get task data to check for GitHub integration
        task = self.provider.get_task(task_id)

        # Reset failure streak on success
        try:
            self.failure_rotation.record_success(task_id)
        except Exception:
            logger.debug("Failed to record success for failure rotation", exc_info=True)

        self.provider.update_task(
            task_id,
            {
                "workState": "completed",
                "status": "completed",
                "action": "complete",
                "summary": "Task completed successfully",
            },
        )

        # Close GitHub issue if this is a GitHub-tracked task
        if task:
            self._close_github_issue_if_needed(task, project_id, task_id)

        self._cleanup_worker_session(agent_id)

    def handle_worker_failure(
        self,
        task_id: str,
        project_id: str,
        error_log: str,
        agent_id: str,
        start_time: float | None = None,
        *,
        task_title: str = "",
        failure_reason: str = "worker_failed",
    ):
        """Handle worker failure.

        Always attempts best-effort WIP finalization to avoid leaving the repo dirty.
        """
        logger.error(f"Worker failure for task {task_id[:8]} on {project_id}")

        # Ensure the repo is finalized/clean even on failure.
        try:
            self.finalize_project_changes_wip(
                task_id,
                project_id,
                task_title=task_title,
                failure_reason=failure_reason,
            )
        except Exception:
            logger.warning(
                f"Unexpected error during WIP finalization for failed task {task_id[:8]} ({project_id})",
                exc_info=True,
            )

        # Capture usage stats even for failures (before session cleanup).
        try:
            self._capture_worker_usage(
                agent_id,
                task_id,
                start_time,
                succeeded=False,
                failure_reason=failure_reason,
            )
        except Exception:
            logger.warning("[USAGE] Unexpected error capturing failure usage", exc_info=True)

        # Record failure and apply exponential backoff before retry.
        try:
            entry = self.failure_rotation.record_failure(task_id)
            retry_count = int(entry.get("retry_count") or entry.get("streak") or 0)
            skip_until = entry.get("skip_until_ts")
            logger.warning(
                f"[FAIL-BACKOFF] Task {task_id[:8]} failure retry_count={retry_count}. "
                f"Cooling down until {skip_until}."
            )
        except Exception:
            logger.debug("Failed to record failure for failure backoff", exc_info=True)

        self.escalation.process_failure(task_id, project_id, error_log)
        self.provider.update_task(task_id, {"workState": "failed"})
        self._cleanup_worker_session(agent_id)

    def _update_provider_status(self):
        """Update provider with current worker status."""
        self.provider.update_worker_status({
            "active": len(self.active_workers) > 0,
            "currentTask": list(self.active_workers.keys())[0] if self.active_workers else None,
            "lastHeartbeat": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })

    # =========================================================================
    # Status Queries
    # =========================================================================

    def is_worker_busy(self) -> bool:
        """Check if the worker is currently busy."""
        return bool(self.active_workers or self.pending_workers)

    def get_worker_status(self) -> dict[str, Any]:
        """Get detailed worker status for logging/monitoring."""
        if self.pending_workers:
            task_id = list(self.pending_workers)[0]
            return {"busy": True, "current_task": task_id, "state": "syncing/finalizing"}
        elif self.active_workers:
            task_id = list(self.active_workers.keys())[0]
            return {"busy": True, "current_task": task_id, "state": "running"}
        else:
            return {"busy": False, "current_task": None, "state": "idle"}
