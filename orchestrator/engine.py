import time
import logging
from .config import POLL_INTERVAL, LOCKS_DIR
from .control import ControlManager
from .worker import WorkerManager
from .scanner import Scanner
from .reconciler import Reconciler

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self):
        self.control = ControlManager()
        self.worker_manager = WorkerManager(LOCKS_DIR)
        self.scanner = Scanner()
        self.reconciler = Reconciler()
        self.last_reconcile = 0
        self.reconcile_interval = 300  # 5 minutes

    def run_once(self):
        # 1. Process pending control operations (exactly one writer pattern)
        self.control.process_ops()

        # 2. Check active workers and release locks
        self.worker_manager.check_workers()

        # 3. Periodic reconciliation (Self-healing)
        now = time.time()
        if now - self.last_reconcile > self.reconcile_interval:
            logger.info("Starting periodic reconciliation...")
            from .config import BASE_DIR
            projects = [d.name for d in BASE_DIR.iterdir() if d.is_dir() and (d / ".git").exists()]
            self.reconciler.reconcile(projects)
            self.last_reconcile = now

        # 4. Scan for new work
        facts = self.scanner.scan()

        # 5. Decision logic
        # Handle explicit worker requests from dashboard
        if facts["pending_request"]:
            logger.info("Worker request detected from dashboard. Prioritizing.")
            # We could use this to trigger a scan or bypass some throttle
            try:
                from .config import CONTROL_REPO_PATH
                (CONTROL_REPO_PATH / "state" / "worker-request.json").unlink()
                logger.info("Consumed worker-request.json")
            except FileNotFoundError:
                pass

        eligible_tasks = facts["eligible_tasks"]
        for task in eligible_tasks:
            project_id = task.get("projectId")
            if not project_id or project_id == "default":
                # Try to infer project or skip
                logger.warning(f"Task {task['id']} has no projectId. Skipping.")
                continue

            task_id = task["id"]

            if not self.worker_manager.is_domain_locked(project_id):
                logger.info(f"Assigning task {task_id} to project {project_id}")
                
                # Determine agent type
                agent_type = task.get("agentType", "task-runner")
                
                self.worker_manager.spawn_worker(task, project_id, agent_type=agent_type)

                # Update task state to in_progress
                ControlManager.request_op(
                    {
                        "type": "update_task",
                        "task_id": task_id,
                        "updates": {"workState": "in_progress"},
                    }
                )

                # Only spawn one worker per loop to keep it simple and avoid race conditions
                break

    def loop(self):
        logger.info("Orchestrator loop started.")
        while True:
            try:
                self.run_once()
            except Exception as e:
                logger.error(f"Error in orchestrator loop: {e}", exc_info=True)

            time.sleep(POLL_INTERVAL)
