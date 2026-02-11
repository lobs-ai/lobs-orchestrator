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
    ORCHESTRATOR_REPO_PATH,
    CONTROL_REPO_PATH,
    PROJECTS_FILE,
    WORKER_WARNING_TIMEOUT,
    WORKER_KILL_TIMEOUT,
    WORKER_HEARTBEAT_TIMEOUT,
)
from orchestrator.providers.base import TaskProvider
from orchestrator.core.escalation import EscalationManager
from orchestrator.core.agents import AgentManager
from orchestrator.core.registry import AgentRegistry, AgentConfig
from orchestrator.core.collaboration import CollaborationManager

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
    Manages spawning and tracking multiple concurrent worker subprocesses.

    State is persisted to a single JSON file for crash recovery.
    Workers are tracked in the active_workers dictionary.

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

    def __init__(
        self,
        state_dir: Path,
        provider: TaskProvider,
        failure_rotation: FailureRotation | None = None,
        collaboration_manager: CollaborationManager | None = None,
        awareness_monitor: Any | None = None,
        agent_tracker: Any | None = None,
        agent_memory: Any | None = None,
        agent_meta: Any | None = None,
        max_workers: int = 5,
        llm_backend: Any | None = None,
    ):
        """
        Initialize WorkerManager.

        Args:
            state_dir: Directory for state files (kept for compatibility, but we use STATE_FILE)
            provider: Task provider for state updates
            failure_rotation: Failure rotation manager
            collaboration_manager: Collaboration manager for agent handoffs
            awareness_monitor: Awareness monitor for tracking system state
            agent_tracker: Per-agent status tracker
            agent_memory: Per-agent memory manager
            agent_meta: Meta-brain for agent self-awareness
            max_workers: Maximum number of concurrent workers (default: 5)
            llm_backend: LLM backend for diagnostic tasks (optional)
        """
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.provider = provider
        self.escalation = EscalationManager(provider)
        self.failure_rotation = failure_rotation or FailureRotation(state_dir)
        self.max_workers = max_workers

        # Collaboration manager for agent-to-agent handoffs.
        # Engine wires this in so collaboration state is centralized.
        self.collaboration = collaboration_manager

        # Awareness monitor for tracking work lifecycle.
        self.awareness = awareness_monitor

        # Per-agent status tracking and memory.
        self.agent_tracker = agent_tracker
        self.agent_memory = agent_memory

        # Ollama-powered meta-brain for agent self-awareness.
        self.agent_meta = agent_meta

        # In-memory tracking (primary source of truth while running)
        # task_id -> (process, project_id, log_file, start_time, agent_id, task_title, agent_template, worker_id, session_label, last_heartbeat)
        self.active_workers: dict[str, tuple[subprocess.Popen, str, Any, float, str, str, str, str, str, float]] = {}
        self.pending_workers: set[str] = set()  # Tasks in syncing/finalizing state
        
        # Per-project worker limiting: track which projects have active workers
        # project_id -> task_id
        self.project_locks: dict[str, str] = {}
        
        # Per-agent-type limiting: only one instance of each agent type at a time
        # agent_type -> task_id
        self.agent_locks: dict[str, str] = {}

        # Circuit breaker: pause spawning on infrastructure failures
        from orchestrator.core.circuit_breaker import CircuitBreaker
        self.circuit_breaker = CircuitBreaker(threshold=3, cooldown_seconds=120)

        # Thread pool executor for spawning workers concurrently
        self.executor = ThreadPoolExecutor(max_workers=5)
        
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

        # LLM backend for diagnostic tasks
        self._llm_backend = llm_backend

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
        agent_id: str = "worker",
        agent_template: str = "programmer",
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
            "agent_template": agent_template,
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
        agent_template = state.get("agent_template", "programmer")
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
                    # Re-acquire project and agent locks
                    self.project_locks[project_id] = task_id
                    agent_template = _normalize_agent_template_type(agent_type) if agent_type else agent_id
                    self.agent_locks[agent_template] = task_id
                    # Generate worker_id and session_label for re-adopted worker
                    worker_id = f"{agent_id}-{int(time.time())}-{task_id[:8]}-readopted"
                    session_label = f"worker-{worker_id}"
                    current_time = time.time()
                    self.active_workers[task_id] = (
                        _PidMonitor(pid),  # Wrapper to monitor PID
                        project_id,
                        log_file,
                        current_time,  # Approximate - we don't know exact start time
                        agent_id,
                        task_title,
                        agent_template,
                        worker_id,
                        session_label,
                        current_time,  # last_heartbeat
                    )
                    logger.info(f"Successfully re-adopted worker for {task_id[:8]}")
                    logger.info(f"[PROJECT-LOCK] Re-acquired lock for project {project_id} (re-adopted task {task_id[:8]})")
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
                    [sys.executable, "bin/discover-repos"],
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
            if self._llm_backend and self._llm_backend.is_available():
                return "ollama"
            logger.warning("LLM backend not available, falling back to OpenClaw")

        return "openclaw"

    def _write_memory_to_workspace(self, workspace_dir: Path, agent_type: str) -> None:
        """No-op. The workspace IS the agent's home — files persist there between runs.
        
        We don't overwrite workspace files. The agent owns them.
        After runs, we copy workspace files to lobs-control for dashboard display.
        """
        pass

    def _sync_workspace_for_agent_template(self, agent_template_type: str) -> str:
        """Prepare the per-agent workspace for a task.

        Template files (AGENTS.md, SOUL.md, etc.) are managed by bin/setup-agents
        and are read-only. This method only writes task-specific memory context.

        Returns the effective template type used (may fall back to 'programmer').
        """

        requested = (agent_template_type or "").strip().lower() or "programmer"
        workspace_dir = self.agent_manager.openclaw_dir / f"workspace-{requested}"

        # Verify workspace exists (setup-agents should have created it)
        if not workspace_dir.exists():
            logger.warning(f"[WORKER] Workspace not found for {requested}, falling back to programmer")
            if requested != "programmer":
                requested = "programmer"
                workspace_dir = self.agent_manager.openclaw_dir / "workspace-programmer"
            if not workspace_dir.exists():
                logger.error("[WORKER] No agent workspace found. Run bin/setup-agents first.")
                workspace_dir.mkdir(parents=True, exist_ok=True)

        # Only write memory context — template files are read-only
        self._write_memory_to_workspace(workspace_dir, requested)
        logger.info(f"[WORKER] Prepared workspace for agent template: {requested}")
        return requested

    # =========================================================================
    # Worker Spawning
    # =========================================================================

    def spawn_worker(
        self, task: dict[str, Any], project_id: str, agent_type: str = "programmer", rules: str = ""
    ) -> bool:
        """
        Spawn a worker for the given task.
        Returns True if spawned, False if at capacity or project already has an active worker.
        
        Enforces:
        1. Global max_workers limit (default: 5 concurrent workers)
        2. One worker per project at a time
        
        When at capacity or project is locked, the engine will queue the task 
        and retry on the next poll cycle.
        """
        task_id = task["id"]
        task_title = task.get("title", task.get("prompt", task_id[:8]))

        # Check global worker capacity
        active_count = len(self.active_workers)
        if active_count >= self.max_workers:
            logger.info(
                f"[CAPACITY] At max capacity ({active_count}/{self.max_workers} workers). "
                f"Queueing task {task_id[:8]}."
            )
            return False

        # Check if project already has an active worker
        if project_id in self.project_locks:
            existing_task = self.project_locks[project_id]
            logger.info(
                f"[PROJECT-LOCK] Project {project_id} already has active worker (task {existing_task[:8]}). "
                f"Queueing task {task_id[:8]}."
            )
            return False

        # Check if this agent type already has an active worker (1 instance per agent type)
        template_type = _normalize_agent_template_type(agent_type)
        if template_type in self.agent_locks:
            existing_task = self.agent_locks[template_type]
            logger.info(
                f"[AGENT-LOCK] Agent type '{template_type}' already has active worker (task {existing_task[:8]}). "
                f"Queueing task {task_id[:8]}."
            )
            return False

        # Mark as pending and save state
        self.pending_workers.add(task_id)
        self._save_state(
            task_id,
            project_id,
            state="syncing",
            task_title=task_title,
            agent_template=agent_type,
        )

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
            # Acquire project and agent locks
            self.project_locks[project_id] = task_id
            self.agent_locks[agent_type] = task_id
            logger.info(f"[PROJECT-LOCK] Acquired lock for project {project_id} (Ollama task {task_id[:8]})")
            # Sync repo (skip for research projects without repos)
            from orchestrator.config import CONTROL_REPO_PATH
            if self._is_git_repo(workspace) and workspace != CONTROL_REPO_PATH:
                logger.info(f"Syncing project repo {project_id}...")
                self._git_pull_rebase_with_autostash(workspace, task_id=task_id)
            else:
                logger.info(f"Skipping git sync for project {project_id} (workspace={workspace})")

            # Mark GitHub issue as in-progress if this is a GitHub-tracked task
            self._mark_github_in_progress(task, project_id)

            # Get awareness context for agent
            awareness_context = None
            if self.awareness:
                try:
                    awareness_context = self.awareness.format_context_for_agent(agent_type)
                except Exception as e:
                    logger.warning(f"Failed to get awareness context: {e}")

            # Build prompt
            from orchestrator.services.prompter import Prompter
            prompt = Prompter.build_task_prompt(
                task,
                project_id,
                rules=rules,
                workspace_path=workspace,
                agent_type=agent_type,
                awareness_context=awareness_context,
            )

            # Run LLM backend
            backend_name = self._llm_backend.name if self._llm_backend else "unknown"
            logger.info(f"Running LLM ({backend_name}) for {task_id[:8]}")
            WORKER_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            log_file_path = WORKER_RESULTS_DIR / f"{task_id}.log"

            with open(log_file_path, "w") as log_file:
                log_file.write(f"=== LLM Task Execution ===\n")
                log_file.write(f"Task ID: {task_id}\nBackend: {backend_name}\n")
                log_file.write(f"Project: {project_id}\nAgent Type: {agent_type}\n\n")
                log_file.write(f"=== Prompt ===\n{prompt}\n\n=== Response ===\n")
                log_file.flush()

                try:
                    response = self._llm_backend.generate(prompt, temperature=0.3)
                    if response:
                        log_file.write(response)
                        log_file.flush()

                    log_file.write(f"\n\n=== Completed ===\n")
                    self._save_state(
                        task_id,
                        project_id,
                        state="finalizing",
                        task_title=task_title,
                        agent_id=agent_id,
                        agent_template=agent_type,
                    )

                    success = self.finalize_project_changes(task_id, project_id, task_title)
                    if success:
                        self.handle_worker_success(
                            task_id,
                            project_id,
                            agent_id,
                            agent_type,
                            start_time,
                            task_title,
                            "",  # Ollama doesn't use OpenClaw sessions
                        )
                    else:
                        self.handle_worker_failure(
                            task_id,
                            project_id,
                            "Failed to finalize changes",
                            agent_id,
                            start_time=start_time,
                            task_title=task_title,
                            failure_reason="finalize_failed",
                            session_label="",  # Ollama doesn't use OpenClaw sessions
                        )

                except Exception as e:
                    error_msg = f"LLM execution failed: {e}"
                    logger.error(error_msg)
                    log_file.write(f"\n\n=== ERROR ===\n{error_msg}\n")
                    self.handle_worker_failure(
                        task_id,
                        project_id,
                        error_msg,
                        agent_id,
                        start_time=start_time,
                        task_title=task_title,
                        failure_reason="llm_failed",
                        session_label="",
                    )

        except Exception as e:
            logger.error(f"Failed to spawn LLM worker for {task_id}: {e}")
            self.handle_worker_failure(
                task_id,
                project_id,
                str(e),
                agent_id,
                start_time=start_time,
                task_title=task_title,
                failure_reason="llm_spawn_failed",
                session_label="",
            )
        finally:
            # Release project and agent locks
            if project_id in self.project_locks and self.project_locks[project_id] == task_id:
                del self.project_locks[project_id]
                logger.info(f"[PROJECT-LOCK] Released lock for project {project_id} (LLM task {task_id[:8]})")
            for atype, tid in list(self.agent_locks.items()):
                if tid == task_id:
                    del self.agent_locks[atype]
                    break
            
            self._clear_state()
            self.pending_workers.discard(task_id)
            self._update_provider_status()

    def _async_spawn_openclaw_flow(self, task: dict[str, Any], project_id: str, agent_type: str, rules: str):
        """Spawn an OpenClaw worker."""
        task_id = task["id"]
        task_title = task.get("title", task_id[:8])
        workspace = self._get_repo_path(project_id)
        
        # Clear retryAfter if this is a retry attempt
        if task.get("workState") == "failed" and task.get("retryAfter"):
            retry_attempt = task.get("failureCount", 0)
            logger.info(
                f"[AUTO-RETRY] Starting retry attempt #{retry_attempt} for task {task_id[:8]}"
            )
            self.provider.update_task(task_id, {
                "retryAfter": None,
                "workState": "in_progress",
            })
        
        # Use agent type directly as OpenClaw agent id
        # Models are configured per-agent in OpenClaw config (agents.list[].model)
        template_type = _normalize_agent_template_type(agent_type)
        agent_id = template_type  # e.g., "programmer", "architect", "researcher"
        
        # Generate unique worker ID and session label for session isolation
        worker_id = f"{agent_id}-{int(time.time())}-{task_id[:8]}"
        session_label = f"worker-{worker_id}"  # Unique label for this worker's session

        try:
            logger.info(f"[WORKER] Using agent: {agent_id}")

            # Sync repo (skip for research projects without repos)
            from orchestrator.config import CONTROL_REPO_PATH
            if self._is_git_repo(workspace) and workspace != CONTROL_REPO_PATH:
                logger.info(f"Syncing project repo {project_id}...")
                self._git_pull_rebase_with_autostash(workspace, task_id=task_id)
            else:
                logger.info(f"Skipping git sync for project {project_id} (workspace={workspace})")

            # Mark GitHub issue as in-progress if this is a GitHub-tracked task
            self._mark_github_in_progress(task, project_id)

            # Get awareness context for agent
            awareness_context = None
            if self.awareness:
                try:
                    awareness_context = self.awareness.format_context_for_agent(template_type)
                except Exception as e:
                    logger.warning(f"Failed to get awareness context: {e}")

            # Agent memory is written directly to workspace as MEMORY.md
            # (handled by _write_memory_to_workspace), not injected into prompt

            # Build prompt
            from orchestrator.services.prompter import Prompter
            prompt = Prompter.build_task_prompt(
                task,
                project_id,
                rules=rules,
                workspace_path=workspace,
                agent_type=agent_type,
                awareness_context=awareness_context,
                memory_context=None,
            )

            # Launch OpenClaw
            # Models are configured per-agent in OpenClaw config (agents.list[].model)
            # No --model flag needed; OpenClaw uses the agent's configured model
            from orchestrator.utils.settings import get_setting
            executable = get_setting("openclaw_executable", "openclaw")
            
            # Note: Model configuration must be done in openclaw.json agent config
            # The CLI doesn't support --model flag for agent command
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
            self._save_state(
                task_id,
                project_id,
                pid=process.pid,
                state="running",
                task_title=task_title,
                agent_id=agent_id,
                agent_template=template_type,
            )

            # Acquire project and agent locks, track active worker
            self.project_locks[project_id] = task_id
            self.agent_locks[template_type] = task_id
            current_time = time.time()
            self.active_workers[task_id] = (
                process,
                project_id,
                log_file,
                current_time,
                agent_id,
                task_title,
                template_type,
                worker_id,
                session_label,
                current_time,  # last_heartbeat
            )
            self.pending_workers.discard(task_id)
            
            logger.info(f"[PROJECT-LOCK] Acquired lock for project {project_id} (task {task_id[:8]})")
            
            # Track work started in awareness monitor
            if self.awareness:
                kind = task.get("kind", "task")
                self.awareness.track_work_started(
                    task_id=task_id,
                    project_id=project_id,
                    title=task_title,
                    agent_type=template_type,
                    kind=kind,
                )

            # Track per-agent status
            if self.agent_tracker:
                self.agent_tracker.mark_working(template_type, task_id, project_id, task_title)

            # Generate initial activity description via Ollama meta-brain
            if self.agent_meta:
                self.agent_meta.describe_activity(
                    template_type, task_title, project_id, task.get("notes", ""),
                )

            # Update provider status with new schema
            self._update_provider_status()

        except Exception as e:
            logger.error(f"Failed to spawn worker for {task_id}: {e}")
            self._clear_state()
            # Release agent lock on spawn failure
            for atype, tid in list(self.agent_locks.items()):
                if tid == task_id:
                    del self.agent_locks[atype]
                    break
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
        """Check status of active workers and handle completion.
        
        Also monitors for stuck workers based on:
        1. Total runtime exceeding kill timeout (default: 1 hour)
        2. No heartbeat/status update for heartbeat timeout (default: 5 minutes)
        
        Stuck workers are killed and their tasks are failed.
        """
        finished = []
        stuck = []
        current_time = time.time()
        
        for task_id, (
            process,
            project_id,
            log_file,
            start_time,
            agent_id,
            task_title,
            agent_template,
            worker_id,
            session_label,
            last_heartbeat,
        ) in list(self.active_workers.items()):
            retcode = process.poll()
            
            # Check if worker finished normally
            if retcode is not None:
                elapsed = current_time - start_time
                logger.info(f"Worker for {task_id[:8]} finished with code {retcode} ({elapsed:.1f}s)")
                log_file.close()
                finished.append((task_id, project_id, agent_id, agent_template, retcode, start_time, task_title, session_label))
                continue
            
            # Update agent thinking and activity via Ollama meta-brain
            if self.agent_meta:
                log_path = WORKER_RESULTS_DIR / f"{task_id}.log"
                self.agent_meta.summarize_thinking(agent_template, log_path)
                self.agent_meta.describe_activity(agent_template, task_title, project_id)
            elif self.agent_tracker:
                # Fallback: dumb log-tail extraction when meta-brain not available
                from orchestrator.core.agent_tracker import AgentTracker as _AT
                log_path = WORKER_RESULTS_DIR / f"{task_id}.log"
                snippet = _AT.extract_thinking_from_log(log_path)
                if snippet:
                    self.agent_tracker.update_thinking(agent_template, snippet)

            # Check for stuck workers
            elapsed = current_time - start_time
            heartbeat_age = current_time - last_heartbeat
            
            # Log warning if worker has been running for a long time
            if elapsed > WORKER_WARNING_TIMEOUT and elapsed < WORKER_KILL_TIMEOUT:
                # Only log warning once per minute to avoid spam
                if int(elapsed) % 60 < 10:  # Log in first 10 seconds of each minute
                    logger.warning(
                        f"Worker for task {task_id[:8]} ({project_id}) has been running for "
                        f"{int(elapsed/60)} minutes (warning threshold: {int(WORKER_WARNING_TIMEOUT/60)}min, "
                        f"kill threshold: {int(WORKER_KILL_TIMEOUT/60)}min)"
                    )
            
            # Check if worker should be killed (exceeded timeout or no heartbeat)
            should_kill = False
            kill_reason = None
            
            if elapsed > WORKER_KILL_TIMEOUT:
                should_kill = True
                kill_reason = f"exceeded maximum runtime ({int(WORKER_KILL_TIMEOUT/60)}min)"
                logger.error(
                    f"Worker for task {task_id[:8]} ({project_id}) exceeded maximum runtime "
                    f"({int(elapsed/60)}min > {int(WORKER_KILL_TIMEOUT/60)}min). Killing worker."
                )
            elif heartbeat_age > WORKER_HEARTBEAT_TIMEOUT:
                should_kill = True
                kill_reason = f"no heartbeat for {int(heartbeat_age/60)}min"
                logger.error(
                    f"Worker for task {task_id[:8]} ({project_id}) has not sent heartbeat for "
                    f"{int(heartbeat_age/60)} minutes (threshold: {int(WORKER_HEARTBEAT_TIMEOUT/60)}min). "
                    f"Worker appears stuck. Killing worker."
                )
            
            if should_kill:
                stuck.append((task_id, project_id, agent_id, agent_template, start_time, task_title, 
                             session_label, process, log_file, kill_reason))
        
        # Handle stuck workers
        for task_id, project_id, agent_id, agent_template, start_time, task_title, session_label, process, log_file, kill_reason in stuck:
            try:
                pid = process.pid if hasattr(process, 'pid') else None
                if pid:
                    logger.warning(f"Terminating stuck worker (PID {pid}) for task {task_id[:8]}")
                    try:
                        # Try graceful termination first (SIGTERM)
                        process.terminate()
                        time.sleep(2)  # Give it 2 seconds to terminate gracefully
                        
                        # Check if still running
                        if process.poll() is None:
                            # Force kill if still running
                            logger.warning(f"Worker {task_id[:8]} did not respond to SIGTERM, sending SIGKILL")
                            process.kill()
                            process.wait(timeout=5)
                        
                        logger.info(f"Successfully killed stuck worker for task {task_id[:8]}")
                    except Exception as e:
                        logger.error(f"Failed to kill stuck worker for {task_id[:8]}: {e}")
                
                # Close log file
                try:
                    log_file.close()
                except Exception:
                    pass
                
                # Mark as failed with stuck reason
                finished.append((task_id, project_id, agent_id, agent_template, 1, start_time, task_title, session_label))
                
                # Add to finished list so normal cleanup happens
                # But we need to record the specific failure reason
                self._stuck_workers_reasons = getattr(self, '_stuck_workers_reasons', {})
                self._stuck_workers_reasons[task_id] = kill_reason
                
            except Exception as e:
                logger.error(f"Error handling stuck worker {task_id[:8]}: {e}", exc_info=True)

        for task_id, project_id, agent_id, agent_template, retcode, start_time, task_title, session_label in finished:
            # Release project and agent locks
            if project_id in self.project_locks and self.project_locks[project_id] == task_id:
                del self.project_locks[project_id]
                logger.info(f"[PROJECT-LOCK] Released lock for project {project_id} (task {task_id[:8]})")
            for atype, tid in list(self.agent_locks.items()):
                if tid == task_id:
                    del self.agent_locks[atype]
                    break
            
            del self.active_workers[task_id]
            
            if retcode == 0:
                self._save_state(
                    task_id,
                    project_id,
                    state="finalizing",
                    agent_id=agent_id,
                    agent_template=agent_template,
                )
                self.executor.submit(
                    self._async_finalize_flow,
                    task_id,
                    project_id,
                    agent_id,
                    agent_template,
                    start_time,
                    task_title,
                    session_label,
                )
            else:
                self._handle_immediate_failure(task_id, project_id, agent_id, start_time, task_title, session_label)

        if finished:
            self._update_provider_status()

    def _handle_immediate_failure(
        self,
        task_id: str,
        project_id: str,
        agent_id: str,
        start_time: float | None = None,
        task_title: str = "",
        session_label: str = "",
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

        # Check if this was a stuck worker (killed by health monitoring)
        stuck_reasons = getattr(self, '_stuck_workers_reasons', {})
        failure_reason = "worker_nonzero_exit"
        if task_id in stuck_reasons:
            failure_reason = f"worker_stuck_{stuck_reasons[task_id].replace(' ', '_')}"
            error_tail = f"Worker stuck: {stuck_reasons.pop(task_id)}\n\n{error_tail}"

        self.handle_worker_failure(
            task_id,
            project_id,
            error_tail,
            agent_id,
            start_time=start_time,
            task_title=task_title,
            failure_reason=failure_reason,
            session_label=session_label,
        )
        self._clear_state()

    def _async_finalize_flow(
        self,
        task_id: str,
        project_id: str,
        agent_id: str,
        agent_template: str,
        start_time: float,
        task_title: str = "",
        session_label: str = "",
    ):
        """Finalize worker completion (commit, push, update state)."""
        try:
            success = self.finalize_project_changes(task_id, project_id, task_title)
            if success:
                self.handle_worker_success(
                    task_id,
                    project_id,
                    agent_id,
                    agent_template,
                    start_time,
                    task_title,
                    session_label,
                )
            else:
                self.handle_worker_failure(
                    task_id,
                    project_id,
                    "Project repo push failed",
                    agent_id,
                    start_time=start_time,
                    task_title=task_title,
                    failure_reason="finalize_failed",
                    session_label=session_label,
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
                session_label=session_label,
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

    def _git_pull_rebase_with_autostash(self, repo_path: Path, *, task_id: str, timeout_sec: float = 60.0):
        """Run `git pull --rebase` while tolerating pre-existing uncommitted changes.

        Workers can crash/timeout leaving a dirty repo, which would cause the next
        `git pull --rebase` to fail. We auto-stash before pulling, then pop afterward.

        Notes:
        - Checks for stuck rebase state and aborts if needed
        - Uses `git stash push -u` to include untracked files.
        - If `stash pop` fails (e.g. conflicts), we log a warning and leave the stash
          in place for manual inspection, rather than discarding work.
        - Falls back to hard reset if rebase fails
        """
        # Check if repo is in stuck rebase state
        if self._check_git_rebase_state(repo_path):
            logger.warning(f"Repo {repo_path} is in rebase state, attempting to abort")
            if not self._abort_git_rebase(repo_path):
                logger.error(f"Failed to abort rebase in {repo_path}, attempting hard reset")
                if self._git_hard_reset_to_origin(repo_path):
                    return  # Successfully recovered
                else:
                    raise RuntimeError(f"Unable to recover from stuck git state in {repo_path}")
        
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
            try:
                subprocess.run(
                    ["git", "stash", "push", "-u", "-m", stash_msg],
                    cwd=repo_path,
                    check=True,
                    capture_output=True,
                    timeout=timeout_sec,
                )
                did_stash = True
            except subprocess.CalledProcessError as e:
                stderr = (e.stderr or b"").decode()
                logger.error(f"Failed to stash changes in {repo_path}: {stderr}")
                raise

        # Pull (rebase) with error handling
        try:
            subprocess.run(
                ["git", "pull", "--rebase"],
                cwd=repo_path,
                check=True,
                capture_output=True,
                timeout=timeout_sec,
            )
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or b"").decode()
            logger.error(f"Git pull --rebase failed in {repo_path}: {stderr}")
            
            # Check if this is a "cannot rebase onto multiple branches" error
            if "cannot rebase" in stderr.lower() or "multiple branches" in stderr.lower():
                logger.warning(f"Rebase conflict detected, attempting recovery")
                # Abort the failed rebase
                self._abort_git_rebase(repo_path)
                # Try hard reset as fallback
                if self._git_hard_reset_to_origin(repo_path):
                    logger.info(f"Successfully recovered from rebase failure via hard reset")
                else:
                    raise RuntimeError(f"Failed to recover from git pull failure in {repo_path}") from e
            else:
                raise
        except subprocess.TimeoutExpired:
            logger.error(f"Git pull --rebase timed out in {repo_path}")
            raise

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

    def _trigger_automatic_review(self, task: dict[str, Any], agent_template: str, project_id: str) -> None:
        """Trigger automatic code review after programmer task completion (quality gate).
        
        Creates a review task if:
        - Agent was programmer (code changes likely)
        - Task is part of an initiative (significant work)
        - Auto-review is enabled in config
        - Task is not already a review task
        """
        try:
            from orchestrator.utils.settings import get_setting
            
            # Check if auto-review is enabled
            auto_review_config = get_setting("auto_review", {})
            if isinstance(auto_review_config, dict):
                enabled = auto_review_config.get("enabled", False)
            else:
                enabled = get_setting("auto_review_enabled", False)
            
            if not enabled:
                return
            
            # Only review programmer tasks
            if agent_template != "programmer":
                return
            
            # Don't review review tasks (avoid loops)
            tags = task.get("tags", [])
            if "review" in tags or "diagnostic" in tags:
                return
            
            # Check if task is part of an initiative (significant work worth reviewing)
            initiative = task.get("initiative") or task.get("collaboration", {}).get("initiative")
            if not initiative:
                # Optionally review all programmer tasks, or skip standalone tasks
                review_all = False
                if isinstance(auto_review_config, dict):
                    review_all = auto_review_config.get("review_all_tasks", False)
                if not review_all:
                    return
            
            task_id = task.get("id")
            task_title = task.get("title", "")
            
            # Create review task via collaboration manager
            if not self.collaboration:
                logger.debug("[AUTO-REVIEW] Collaboration manager not available, skipping auto-review")
                return
            
            review_payload = {
                "from": "programmer",
                "to": "reviewer",
                "initiative": initiative or f"review-{task_id[:8]}",
                "work": {
                    "title": f"Review: {task_title}",
                    "context": (
                        f"Please review the changes from task {task_id[:8]}.\n\n"
                        f"Original task: {task_title}\n\n"
                        f"Focus on:\n"
                        f"- Code correctness and logic\n"
                        f"- Test coverage\n"
                        f"- Code quality and maintainability\n"
                        f"- Potential bugs or edge cases\n\n"
                        f"If you find issues, create follow-up tasks via handoffs."
                    ),
                    "acceptance": (
                        "Review complete with feedback documented. "
                        "Critical issues should result in follow-up tasks."
                    ),
                },
                "projectId": project_id,
            }
            
            review_task_id = self.collaboration.process_handoff(
                review_payload,
                parent_task_id=task_id,
                default_project_id=project_id,
            )
            
            logger.info(
                f"[AUTO-REVIEW] Created review task {review_task_id[:8]} for completed task {task_id[:8]}"
            )
            
        except Exception as e:
            # Don't fail task completion if auto-review fails
            logger.error(f"[AUTO-REVIEW] Failed to create review task: {e}", exc_info=True)

    def _process_handoffs(self, task_id: str, project_id: str, from_agent: str) -> None:
        """Read and process agent handoffs from .handoffs/ directory.
        
        Agents can create handoff files in .handoffs/{id}.json to delegate work
        to other specialized agents. Each handoff creates a new task.
        """
        project_path = self._get_repo_path(project_id)
        handoffs_dir = project_path / ".handoffs"
        
        if not handoffs_dir.exists() or not handoffs_dir.is_dir():
            return
        
        handoff_files = list(handoffs_dir.glob("*.json"))
        if not handoff_files:
            return
        
        logger.info(f"[HANDOFF] Found {len(handoff_files)} handoff(s) from task {task_id[:8]}")

        # Initialize CollaborationManager (injected by Engine when available).
        collab = self.collaboration or CollaborationManager(self.provider)

        # CollaborationManager only accepts known agent types.
        from_norm = (from_agent or "").strip().lower()
        if from_norm not in {"programmer", "researcher", "reviewer", "writer", "architect"}:
            logger.warning(
                f"[HANDOFF] Invalid from-agent '{from_agent}' for task {task_id[:8]}; defaulting to 'programmer'"
            )
            from_norm = "programmer"

        for handoff_file in handoff_files:
            try:
                handoff_data = json.loads(handoff_file.read_text())

                # Transform agent handoff format to collaboration manager format
                context = handoff_data.get("context")
                files = handoff_data.get("files")
                if isinstance(files, list) and files:
                    files_list = "\n".join(f"- {p}" for p in files if isinstance(p, str) and p.strip())
                    if files_list:
                        context = (context or "").rstrip() + "\n\nFiles:\n" + files_list

                payload = {
                    "from": from_norm,
                    "to": handoff_data.get("to", "programmer"),
                    "initiative": handoff_data.get("initiative", handoff_data.get("title", "handoff")),
                    "work": {
                        "title": handoff_data.get("title", "Untitled handoff"),
                        "context": context,
                        "acceptance": handoff_data.get("acceptance"),
                    },
                    "projectId": project_id,
                }

                # Create the handoff task
                new_task_id = collab.process_handoff(
                    payload,
                    parent_task_id=task_id,
                    default_project_id=project_id,
                )

                logger.info(
                    f"[HANDOFF] Created task {new_task_id[:8]} "
                    f"({from_norm} → {payload['to']}) "
                    f"for '{payload['work']['title']}'"
                )

            except Exception as e:
                logger.error(f"[HANDOFF] Failed to process {handoff_file.name}: {e}", exc_info=True)
        
        # Clean up .handoffs/ directory after processing
        try:
            for handoff_file in handoff_files:
                handoff_file.unlink()
            handoffs_dir.rmdir()
            logger.debug(f"[HANDOFF] Cleaned up .handoffs/ directory")
        except Exception as e:
            logger.warning(f"[HANDOFF] Failed to clean up .handoffs/ directory: {e}")

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
            
            # Use configured project file path
            if not PROJECTS_FILE.exists():
                logger.warning("projects.json not found, skipping cross-repo finalization")
                return finalized_repos
            
            with open(PROJECTS_FILE) as f:
                projects_data = json.load(f)
            
            # Include orchestrator and control repos explicitly (only if they exist)
            repos_to_check = []
            if ORCHESTRATOR_REPO_PATH.exists() and ORCHESTRATOR_REPO_PATH.is_dir():
                repos_to_check.append(str(ORCHESTRATOR_REPO_PATH))
            if CONTROL_REPO_PATH.exists() and CONTROL_REPO_PATH.is_dir():
                repos_to_check.append(str(CONTROL_REPO_PATH))
            
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
                        subprocess.run(["git", "add", "."], cwd=repo_path, check=True, timeout=30.0)
                        commit_msg = f"Auto-finalize: changes from task {task_id[:8]}"
                        subprocess.run(["git", "commit", "-m", commit_msg], cwd=repo_path, check=True, timeout=30.0)
                        logger.info(f"Auto-committed changes in {Path(repo_path).name}")
                        finalized_repos.append(repo_path)
                        has_unpushed = True  # Now we have commits to push
                    
                    if has_unpushed:
                        # Push commits
                        subprocess.run(["git", "push"], cwd=repo_path, check=True, timeout=60.0)
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
            subprocess.run(["git", "add", "."], cwd=project_path, check=True, timeout=30.0)
            subprocess.run(["git", "commit", "-m", subject, "-m", body], cwd=project_path, check=True, timeout=30.0)
            logger.info(f"WIP-committed changes for failed task {task_id[:8]} in {project_id}")
        except subprocess.CalledProcessError as e:
            # If commit fails (e.g. conflicts), don't crash failure handling.
            stderr = (e.stderr or b"").decode() if hasattr(e.stderr, 'decode') else str(e.stderr or "")
            logger.warning(f"WIP commit failed for {project_id} (task {task_id[:8]}): {stderr}")
            return False
        except subprocess.TimeoutExpired:
            logger.warning(f"WIP commit timed out for {project_id} (task {task_id[:8]})")
            return False

        try:
            subprocess.run(["git", "push"], cwd=project_path, check=True, timeout=60.0)
            logger.info(f"WIP-pushed changes for failed task {task_id[:8]} to {project_id}")
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or b"").decode() if hasattr(e.stderr, 'decode') else str(e.stderr or "")
            logger.warning(f"WIP push failed for {project_id} (task {task_id[:8]}): {stderr}")
            # Still consider it a partial success: repo is clean now.
        except subprocess.TimeoutExpired:
            logger.warning(f"WIP push timed out for {project_id} (task {task_id[:8]})")
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
                timeout=10.0,
            ).stdout.decode()

            if status:
                # Stage and commit
                subprocess.run(["git", "add", "."], cwd=project_path, check=True, timeout=30.0)
                commit_msg = self._generate_commit_message(task_id, project_id, project_path, work_summary, task_title)
                subprocess.run(["git", "commit", "-m", commit_msg], cwd=project_path, check=True, timeout=30.0)
                logger.info(f"Committed changes for task {task_id}")

                # Push with retry and conflict recovery
                if not self._safe_push_with_retry(project_path, task_id, project_id):
                    return False
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
    
    def _safe_push_with_retry(self, repo_path: Path, task_id: str, project_id: str, max_retries: int = 3) -> bool:
        """Push with automatic retry and conflict recovery.
        
        Implements exponential backoff and attempts to resolve simple conflicts.
        """
        import time
        
        for attempt in range(max_retries):
            try:
                subprocess.run(
                    ["git", "push"],
                    cwd=repo_path,
                    check=True,
                    capture_output=True,
                    timeout=60
                )
                logger.info(f"Pushed changes for task {task_id} to {project_id}")
                return True
                
            except subprocess.CalledProcessError as e:
                stderr = e.stderr.decode() if e.stderr else ""
                
                # Check if this is a reject/conflict that we can recover from
                if "rejected" in stderr.lower() or "conflict" in stderr.lower():
                    logger.warning(
                        f"Push rejected for {project_id} (attempt {attempt + 1}/{max_retries}). "
                        f"Attempting rebase recovery..."
                    )
                    
                    if self._attempt_conflict_recovery(repo_path, task_id, project_id):
                        # Recovery successful, try push again
                        continue
                    else:
                        logger.error(f"Conflict recovery failed for {project_id}")
                        return False
                else:
                    # Other error, retry with backoff
                    if attempt < max_retries - 1:
                        backoff = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
                        logger.warning(
                            f"Push failed for {project_id} (attempt {attempt + 1}/{max_retries}): {stderr}. "
                            f"Retrying in {backoff}s..."
                        )
                        time.sleep(backoff)
                        continue
                    else:
                        logger.error(f"Push failed after {max_retries} attempts for {project_id}: {stderr}")
                        return False
            
            except subprocess.TimeoutExpired:
                if attempt < max_retries - 1:
                    logger.warning(f"Push timed out for {project_id}, retrying...")
                    continue
                else:
                    logger.error(f"Push timed out after {max_retries} attempts for {project_id}")
                    return False
        
        return False
    
    def _record_failure_for_monitoring(
        self,
        task_id: str,
        project_id: str,
        agent_id: str,
        failure_reason: str,
        error_log: str
    ) -> None:
        """Record failure in history for Monitor pattern detection."""
        try:
            from orchestrator.core.monitor import Monitor
            
            failure_entry = {
                "taskId": task_id,
                "projectId": project_id,
                "agentType": agent_id,
                "failureReason": failure_reason,
                "error": error_log[:500] if error_log else "",  # Truncate for storage
                "failedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            
            # Append to failure history
            history_path = STATE_DIR / "failure-history.json"
            history_data = {"failures": []}
            
            if history_path.exists():
                try:
                    with open(history_path, "r") as f:
                        history_data = json.load(f)
                except Exception:
                    pass
            
            # Keep only last 100 failures to avoid unbounded growth
            failures = history_data.get("failures", [])
            failures.append(failure_entry)
            if len(failures) > 100:
                failures = failures[-100:]
            
            history_data["failures"] = failures
            
            history_path.parent.mkdir(parents=True, exist_ok=True)
            with open(history_path, "w") as f:
                json.dump(history_data, f, indent=2)
                f.write("\n")
            
            logger.debug(f"[MONITORING] Recorded failure for task {task_id[:8]} in failure history")
            
        except Exception as e:
            # Don't fail the failure handler if monitoring fails
            logger.debug(f"Failed to record failure for monitoring: {e}")
    
    def _attempt_conflict_recovery(self, repo_path: Path, task_id: str, project_id: str) -> bool:
        """Attempt to recover from git conflicts by rebasing.
        
        Returns True if recovery successful, False otherwise.
        Uses hard reset as fallback if rebase fails.
        """
        try:
            # Check if we're already in a rebase state
            if self._check_git_rebase_state(repo_path):
                logger.warning(f"Repo {repo_path} already in rebase state, aborting first")
                self._abort_git_rebase(repo_path)
            
            # Fetch latest changes
            try:
                subprocess.run(
                    ["git", "fetch", "origin"],
                    cwd=repo_path,
                    check=True,
                    capture_output=True,
                    timeout=30
                )
            except subprocess.CalledProcessError as e:
                stderr = (e.stderr or b"").decode()
                logger.error(f"Git fetch failed for {project_id}: {stderr}")
                return False
            except subprocess.TimeoutExpired:
                logger.error(f"Git fetch timed out for {project_id}")
                return False
            
            # Get current branch
            try:
                branch_result = subprocess.run(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    cwd=repo_path,
                    check=True,
                    capture_output=True,
                    timeout=10,
                    text=True,
                )
                branch = branch_result.stdout.strip()
            except subprocess.CalledProcessError as e:
                stderr = (e.stderr or "").strip()
                logger.error(f"Failed to get current branch for {project_id}: {stderr}")
                return False
            
            # Attempt rebase
            try:
                rebase_result = subprocess.run(
                    ["git", "rebase", f"origin/{branch}"],
                    cwd=repo_path,
                    capture_output=True,
                    timeout=60,
                    text=True,
                )
                
                if rebase_result.returncode == 0:
                    logger.info(f"Successfully rebased {project_id} for task {task_id}")
                    return True
                else:
                    # Rebase had conflicts
                    stderr = rebase_result.stderr or ""
                    stdout = rebase_result.stdout or ""
                    output = stderr + stdout
                    
                    if "conflict" in output.lower() or "cannot rebase" in output.lower():
                        logger.warning(f"Rebase conflicts in {project_id}: {output[:200]}")
                        # Abort the rebase to restore clean state
                        self._abort_git_rebase(repo_path)
                        
                        # Try hard reset as fallback
                        logger.info(f"Attempting hard reset fallback for {project_id}")
                        if self._git_hard_reset_to_origin(repo_path):
                            logger.info(f"Successfully recovered {project_id} via hard reset")
                            return True
                    return False
            except subprocess.TimeoutExpired:
                logger.error(f"Git rebase timed out for {project_id}")
                self._abort_git_rebase(repo_path)
                return False
                
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or b"").decode() if hasattr(e.stderr, 'decode') else str(e.stderr or "")
            logger.warning(f"Git conflict recovery failed for {project_id}: {stderr}")
            # Try to abort rebase if it's in progress
            self._abort_git_rebase(repo_path)
            return False
        except Exception as e:
            logger.error(f"Unexpected error during conflict recovery for {project_id}: {e}")
            # Try to abort rebase if it's in progress
            self._abort_git_rebase(repo_path)
            return False

    # =========================================================================
    # Session Cleanup
    # =========================================================================

    def _cleanup_worker_session(self, agent_id: str, session_label: str = ""):
        """Clean up worker agent's session via OpenClaw gateway API.
        
        Uses session_label to target the specific worker session when provided,
        allowing multiple workers of the same agent type to coexist safely.
        Falls back to the main session if no label provided (legacy behavior).
        """
        if session_label:
            session_key = f"agent:{agent_id}:{session_label}"
            logger.info(f"[WORKER] Cleaning up session {session_label} for agent {agent_id}...")
        else:
            session_key = f"agent:{agent_id}:main"
            logger.info(f"[WORKER] Cleaning up main session for agent {agent_id}...")
        
        try:
            from orchestrator.utils.settings import get_setting
            executable = get_setting("openclaw_executable", "openclaw")
            
            # Use the gateway API to properly delete the session
            # This archives the transcript and removes the session entry
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
                self._cleanup_worker_session_files(agent_id, session_label)
                
        except subprocess.TimeoutExpired:
            logger.warning(f"[WORKER] Session cleanup timed out, falling back to file cleanup")
            self._cleanup_worker_session_files(agent_id, session_label)
        except Exception as e:
            logger.warning(f"Error cleaning worker sessions via API: {e}")
            self._cleanup_worker_session_files(agent_id, session_label)

    def _cleanup_worker_session_files(self, agent_id: str, session_label: str = ""):
        """Fallback: Clean up worker agent's session files directly.
        
        If session_label is provided, only removes files for that specific session.
        Otherwise, removes all sessions (legacy behavior for backward compatibility).
        """
        try:
            sessions_dir = Path.home() / ".openclaw" / "agents" / agent_id / "sessions"
            if not sessions_dir.exists():
                return

            deleted = 0
            
            if session_label:
                # Only clean up the specific labeled session
                # Session files are stored with session IDs, so we need to look up
                # the session ID from sessions.json first
                sessions_json = sessions_dir / "sessions.json"
                if sessions_json.exists():
                    try:
                        sessions_data = json.loads(sessions_json.read_text())
                        session_key = f"agent:{agent_id}:{session_label}"
                        session_meta = sessions_data.get(session_key, {})
                        session_id = session_meta.get("sessionId")
                        
                        if session_id:
                            # Remove specific session files
                            for pattern in [f"{session_id}.jsonl", f"{session_id}.jsonl.lock"]:
                                target = sessions_dir / pattern
                                if target.exists():
                                    try:
                                        target.unlink()
                                        deleted += 1
                                    except Exception:
                                        pass
                            
                            # Update sessions.json to remove the entry
                            if session_key in sessions_data:
                                del sessions_data[session_key]
                                sessions_json.write_text(json.dumps(sessions_data, indent=2))
                    except Exception as e:
                        logger.warning(f"Failed to clean up specific session {session_label}: {e}")
            else:
                # Legacy: clean up all sessions for this agent
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
                label_str = f" (label: {session_label})" if session_label else ""
                logger.info(f"[WORKER] Cleared {deleted} session file(s) for agent {agent_id}{label_str}")

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
                # Only try to compute cost if control repo exists
                if CONTROL_REPO_PATH.exists():
                    sys.path.insert(0, str(CONTROL_REPO_PATH / "bin"))
                    from lib.pricing import compute_cost

                    cost_usd = float(compute_cost(input_tokens, output_tokens, str(model)))
                else:
                    raise FileNotFoundError("Control repo not found")
            except Exception as e:
                logger.warning(f"[USAGE] Could not compute cost precisely (model={model}): {e}. Using rough estimate")
                # Generic rough estimate (kept intentionally conservative)
                cost_usd = (input_tokens / 1_000_000 * 2.0) + (output_tokens / 1_000_000 * 8.0)

        # Read worker status to get workerId and startedAt
        worker_status_path = CONTROL_REPO_PATH / "state" / "worker-status.json"
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
                # New schema: look for task_id in activeWorkers array
                active_workers = status_data.get("activeWorkers", [])
                for worker in active_workers:
                    if worker.get("taskId") == task_id:
                        worker_id = worker.get("workerId")
                        if worker.get("startedAt"):
                            started_at = worker["startedAt"]
                        break
                # If not found in activeWorkers (legacy or edge case), fall back
                if worker_id is None:
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
        agent_template: str,
        start_time: float,
        task_title: str = "",
        session_label: str = "",
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

        # If this was a reviewer failure-analysis task spawned by escalation level 2,
        # process its recommendation before marking it complete.
        try:
            if task and isinstance(task.get("escalationMeta"), dict):
                meta = task.get("escalationMeta") or {}
                if meta.get("alertId") and meta.get("failedTaskId"):
                    logger.info(
                        f"[ESCALATION] Processing reviewer result for alert {str(meta.get('alertId'))[:8]} "
                        f"(failedTask={str(meta.get('failedTaskId'))[:8]})"
                    )
                    self.escalation.process_reviewer_result(task)
        except Exception as e:
            logger.warning(f"[ESCALATION] Failed to process reviewer result for task {task_id[:8]}: {e}", exc_info=True)

        # Reset failure streak on success
        try:
            self.failure_rotation.record_success(task_id)
        except Exception:
            logger.debug("Failed to record success for failure rotation", exc_info=True)

        # Process any handoffs created by the agent
        self._process_handoffs(task_id, project_id, agent_template)

        # Pipeline auto-advance (if this task is part of a pipeline)
        try:
            from orchestrator.core.pipelines import PipelineManager

            if task:
                PipelineManager().on_task_completed(task)
        except Exception:
            logger.warning("[PIPELINE] Failed to auto-advance pipeline after task completion", exc_info=True)
        
        # Quality gate: trigger automatic review for programmer tasks (if enabled)
        self._trigger_automatic_review(task, agent_template, project_id)

        self.provider.update_task(
            task_id,
            {
                "workState": "completed",
                "status": "completed",
                "action": "complete",
                "summary": "Task completed successfully",
                "failureCount": 0,  # Reset failure count on success
                "retryAfter": None,  # Clear retry timestamp
                "failureReason": None,  # Clear failure reason
            },
        )

        # Close GitHub issue if this is a GitHub-tracked task
        if task:
            self._close_github_issue_if_needed(task, project_id, task_id)

        # Track completion in awareness monitor
        if self.awareness:
            duration = time.time() - start_time if start_time else None
            self.awareness.track_work_completed(task_id, duration)

        # Track per-agent status and memory
        duration = time.time() - start_time if start_time else 0
        if self.agent_tracker:
            self.agent_tracker.mark_completed(agent_template, task_id, duration)
            self.agent_tracker.mark_idle(agent_template)
        if self.agent_memory:
            self.agent_memory.append_task_outcome(
                agent_template, project_id, task_title, True, duration,
            )
            # Recover any personal files the agent may have evolved during the run
            workspace_dir = self.agent_manager.openclaw_dir / f"workspace-{agent_template}"
            self.agent_memory.recover_personal_files_from_workspace(
                agent_template, workspace_dir,
            )

        # Ollama meta-brain: reflection, memory synthesis, trait evolution
        if self.agent_meta:
            log_path = WORKER_RESULTS_DIR / f"{task_id}.log"
            self.agent_meta.reflect_on_task(
                agent_template, task_title, project_id, True, duration, log_path,
            )
            self.agent_meta.synthesize_memory(agent_template)
            if self.agent_tracker:
                count = self.agent_tracker.get_completion_count(agent_template)
                self.agent_meta.evolve_traits(agent_template, count)

        self._cleanup_worker_session(agent_id, session_label)

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
        session_label: str = "",
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
        retry_count = 0
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
        
        # Record failure in history for Monitor pattern detection
        self._record_failure_for_monitoring(task_id, project_id, agent_id, failure_reason, error_log)
        
        # Get current task data to increment failure count
        current_task = self.provider.get_task(task_id)
        current_failure_count = 0
        if current_task:
            current_failure_count = current_task.get("failureCount", 0)
        
        new_failure_count = current_failure_count + 1
        
        # Retry configuration: max retries and exponential backoff
        MAX_RETRIES = 3
        RETRY_DELAYS = [5 * 60, 15 * 60, 60 * 60]  # 5min, 15min, 1hr in seconds
        
        # Determine if we should retry or block
        now = datetime.now(timezone.utc)
        updates = {
            "failureReason": failure_reason,
            "failedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "failureCount": new_failure_count,
        }
        
        if new_failure_count <= MAX_RETRIES:
            # Schedule retry with exponential backoff
            delay_index = min(new_failure_count - 1, len(RETRY_DELAYS) - 1)
            retry_delay_seconds = RETRY_DELAYS[delay_index]
            retry_after = now.timestamp() + retry_delay_seconds
            
            updates["workState"] = "failed"
            updates["retryAfter"] = int(retry_after)
            
            # Convert to human-readable time for logging
            from datetime import timedelta
            retry_time = datetime.fromtimestamp(retry_after, tz=timezone.utc)
            logger.info(
                f"[AUTO-RETRY] Task {task_id[:8]} will retry after {retry_delay_seconds // 60}min "
                f"(attempt {new_failure_count}/{MAX_RETRIES}) at {retry_time.strftime('%H:%M:%S UTC')}"
            )
        else:
            # Max retries exceeded - mark as blocked and create alert
            updates["workState"] = "blocked"
            updates["retryAfter"] = None  # Clear retry timestamp
            
            logger.warning(
                f"[AUTO-RETRY] Task {task_id[:8]} exceeded max retries ({MAX_RETRIES}), "
                f"marking as blocked"
            )
            
            # Create alert for blocked task
            try:
                task_title = task_title or current_task.get("title", "Unknown task") if current_task else "Unknown task"
                alert_id = f"blocked-task-{task_id}"
                alert = {
                    "id": alert_id,
                    "type": "blocked_task",
                    "severity": "high",
                    "status": "active",
                    "taskId": task_id,
                    "projectId": project_id,
                    "title": f"Task blocked after {MAX_RETRIES} failures: {task_title[:50]}",
                    "message": (
                        f"Task '{task_title}' failed {MAX_RETRIES} times and has been blocked. "
                        f"Last failure reason: {failure_reason}. "
                        f"Manual intervention required."
                    ),
                    "createdAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "failureCount": new_failure_count,
                    "lastFailureReason": failure_reason,
                }
                
                # Add alert via provider
                from orchestrator.config import CONTROL_REPO_PATH
                import uuid
                alerts_dir = CONTROL_REPO_PATH / "state" / "alerts"
                alerts_dir.mkdir(parents=True, exist_ok=True)
                alert_file = alerts_dir / f"{alert_id}.json"
                
                import json
                with open(alert_file, "w") as f:
                    json.dump(alert, f, indent=2)
                    f.write("\n")
                
                logger.info(f"[AUTO-RETRY] Created alert for blocked task: {alert_id}")
            except Exception as e:
                logger.error(f"[AUTO-RETRY] Failed to create alert for blocked task: {e}")
        
        self.provider.update_task(task_id, updates)
        
        # Track failure in awareness monitor
        if self.awareness:
            duration = time.time() - start_time if start_time else None
            self.awareness.track_work_failed(task_id, duration)

        # Track per-agent status and memory
        duration = time.time() - start_time if start_time else 0
        agent_type_normalized = _normalize_agent_template_type(agent_id)
        if self.agent_tracker:
            self.agent_tracker.mark_failed(agent_type_normalized, task_id)
            self.agent_tracker.mark_idle(agent_type_normalized)
        if self.agent_memory:
            self.agent_memory.append_task_outcome(
                agent_type_normalized, project_id, task_title, False, duration,
            )

        # Ollama meta-brain: reflect on failure
        if self.agent_meta:
            log_path = WORKER_RESULTS_DIR / f"{task_id}.log"
            self.agent_meta.reflect_on_task(
                agent_type_normalized, task_title, project_id, False, duration, log_path,
            )

        self._cleanup_worker_session(agent_id, session_label)

    def _update_provider_status(self):
        """Update provider with current worker status using multi-worker schema.
        
        Also updates heartbeat timestamps for all active workers.
        """
        now = time.time()
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        
        # Build activeWorkers array from current active workers
        active_workers_list = []
        for task_id, (
            process,
            project_id,
            log_file,
            start_time,
            agent_id,
            task_title,
            agent_template,
            worker_id,
            session_label,
            last_heartbeat,
        ) in self.active_workers.items():
            # Update heartbeat timestamp for this worker
            self.active_workers[task_id] = (
                process,
                project_id,
                log_file,
                start_time,
                agent_id,
                task_title,
                agent_template,
                worker_id,
                session_label,
                now,  # Updated heartbeat
            )
            
            active_workers_list.append({
                "workerId": worker_id,
                "taskId": task_id,
                "projectId": project_id,
                "agentType": agent_template,
                "startedAt": datetime.fromtimestamp(start_time, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "lastHeartbeat": now_iso,
                "status": "running",
                "taskTitle": task_title,
            })
        
        self.provider.update_worker_status({
            "activeWorkers": active_workers_list,
            "totalActiveWorkers": len(active_workers_list),
            "lastUpdated": now_iso,
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

    # =========================================================================
    # Graceful Shutdown
    # =========================================================================

    def shutdown(self, timeout: float = 300.0):
        """Gracefully shutdown all active workers.
        
        Args:
            timeout: Maximum time (in seconds) to wait for workers to complete
                    before forcing termination. Default: 300s (5 minutes)
        
        Process:
            1. Stop accepting new work (caller's responsibility)
            2. Wait for all active workers to complete naturally
            3. For workers exceeding timeout:
               - Send SIGTERM (graceful termination)
               - Wait 10s
               - Send SIGKILL if still running
               - Run WIP finalization to clean up repo
            4. Clean up all pending workers
        """
        if not self.active_workers and not self.pending_workers:
            logger.info("[SHUTDOWN] No active or pending workers to shut down")
            return

        total_workers = len(self.active_workers) + len(self.pending_workers)
        logger.info(f"[SHUTDOWN] Beginning graceful shutdown of {total_workers} worker(s) (timeout: {timeout:.0f}s)")
        
        start_time = time.time()
        check_interval = 2.0  # Check every 2 seconds
        
        # Wait for workers to complete naturally (with timeout)
        while self.active_workers or self.pending_workers:
            elapsed = time.time() - start_time
            remaining = timeout - elapsed
            
            if remaining <= 0:
                logger.warning(f"[SHUTDOWN] Timeout reached after {timeout:.0f}s with {len(self.active_workers)} active and {len(self.pending_workers)} pending worker(s)")
                break
            
            # Check and handle completed workers
            self.check_workers()
            
            if not self.active_workers and not self.pending_workers:
                logger.info(f"[SHUTDOWN] All workers completed gracefully in {elapsed:.1f}s")
                return
            
            # Log progress periodically
            if int(elapsed) % 30 == 0 and elapsed > 0:
                logger.info(
                    f"[SHUTDOWN] Waiting for {len(self.active_workers)} active and {len(self.pending_workers)} pending worker(s) "
                    f"(elapsed: {elapsed:.0f}s, remaining: {remaining:.0f}s)"
                )
            
            time.sleep(min(check_interval, remaining))
        
        # Force termination of remaining workers
        if self.active_workers:
            logger.warning(f"[SHUTDOWN] Force terminating {len(self.active_workers)} worker(s) that did not complete in time")
            
            for task_id, (
                process,
                project_id,
                log_file,
                start_time,
                agent_id,
                task_title,
                agent_template,
                worker_id,
                session_label,
                last_heartbeat,
            ) in list(self.active_workers.items()):
                try:
                    pid = process.pid
                    logger.warning(f"[SHUTDOWN] Terminating worker for task {task_id[:8]} (PID {pid})")
                    
                    # Try graceful termination first (SIGTERM)
                    try:
                        process.terminate()
                        # Wait up to 10 seconds for graceful shutdown
                        try:
                            process.wait(timeout=10)
                            logger.info(f"[SHUTDOWN] Worker {task_id[:8]} terminated gracefully")
                        except subprocess.TimeoutExpired:
                            # Force kill if still running
                            logger.warning(f"[SHUTDOWN] Worker {task_id[:8]} did not respond to SIGTERM, sending SIGKILL")
                            process.kill()
                            process.wait(timeout=5)
                            logger.info(f"[SHUTDOWN] Worker {task_id[:8]} force killed")
                    except Exception as e:
                        logger.error(f"[SHUTDOWN] Failed to terminate worker {task_id[:8]}: {e}")
                    
                    # Close log file
                    try:
                        log_file.close()
                    except Exception:
                        pass
                    
                    # Release project and agent locks
                    if project_id in self.project_locks and self.project_locks[project_id] == task_id:
                        del self.project_locks[project_id]
                        logger.info(f"[PROJECT-LOCK] Released lock for project {project_id} (shutdown)")
                    for atype, tid in list(self.agent_locks.items()):
                        if tid == task_id:
                            del self.agent_locks[atype]
                            break
                    
                    # WIP finalization to avoid leaving dirty repos
                    try:
                        logger.info(f"[SHUTDOWN] Running WIP finalization for task {task_id[:8]}")
                        self.finalize_project_changes_wip(
                            task_id,
                            project_id,
                            task_title=task_title,
                            failure_reason="shutdown_timeout",
                        )
                    except Exception as e:
                        logger.error(f"[SHUTDOWN] WIP finalization failed for {task_id[:8]}: {e}")
                    
                    # Capture usage stats for the interrupted worker
                    try:
                        self._capture_worker_usage(
                            agent_id,
                            task_id,
                            start_time,
                            succeeded=False,
                            failure_reason="shutdown_timeout",
                        )
                    except Exception as e:
                        logger.warning(f"[SHUTDOWN] Failed to capture usage for {task_id[:8]}: {e}")
                    
                    # Update task status
                    try:
                        self.provider.update_task(task_id, {
                            "workState": "failed",
                            "status": "failed",
                        })
                    except Exception as e:
                        logger.error(f"[SHUTDOWN] Failed to update task status for {task_id[:8]}: {e}")
                    
                    # Clean up session
                    try:
                        self._cleanup_worker_session(agent_id, session_label)
                    except Exception as e:
                        logger.warning(f"[SHUTDOWN] Failed to cleanup session for {task_id[:8]}: {e}")
                    
                except Exception as e:
                    logger.error(f"[SHUTDOWN] Error force-terminating worker {task_id[:8]}: {e}", exc_info=True)
            
            # Clear all active workers
            self.active_workers.clear()
        
        # Clean up pending workers
        if self.pending_workers:
            logger.info(f"[SHUTDOWN] Cleaning up {len(self.pending_workers)} pending worker(s)")
            self.pending_workers.clear()
        
        # Clear state file
        self._clear_state()
        
        # Update provider status
        self._update_provider_status()
        
        logger.info("[SHUTDOWN] Worker manager shutdown complete")
