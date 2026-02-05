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

    def process_control(self) -> None:
        """Process pending control operations."""
        self.control.process_ops()

    def process_workers(self) -> None:
        """Check active workers and handle their lifecycle."""
        self.worker_manager.check_workers()

    def process_reconciliation(self, project_ids: list[str]) -> None:
        """Periodic self-healing pass."""
        now = time.time()
        if now - self.last_reconcile > self.reconcile_interval:
            logger.info("Starting periodic reconciliation...")
            self.reconciler.reconcile(project_ids)
            self.last_reconcile = now

    def process_requests(self, pending_request: bool) -> None:
        """Handle explicit worker requests from the dashboard."""
        if pending_request:
            logger.info("Worker request detected from dashboard. Prioritizing.")
            try:
                from .config import CONTROL_REPO_PATH
                request_file = CONTROL_REPO_PATH / "state" / "worker-request.json"
                if request_file.exists():
                    request_file.unlink()
                    logger.info("Consumed worker-request.json")
            except Exception as e:
                logger.error(f"Failed to consume worker-request: {e}")

    def run_once(self) -> None:
        # 1. Process control state (exactly one writer pattern)
        self.process_control()

        # 2. Check active workers
        self.process_workers()

        # 3. Scan for new work and facts
        facts = self.scanner.scan()
        projects = facts["projects"]
        project_ids = [p["id"] for p in projects if not p.get("archived", False)]

        # 4. Periodic reconciliation
        self.process_reconciliation(project_ids)

        # 5. Handle dashboard requests
        self.process_requests(facts["pending_request"])

        # 6. Task Assignment
        eligible_tasks = facts["eligible_tasks"]
        for task in eligible_tasks:
            project_id = task.get("projectId")
            if not project_id or project_id not in project_ids:
                logger.warning(f"Task {task['id']} has invalid or unregistered projectId '{project_id}'. Skipping.")
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

                # Continue to next task to potentially spawn more workers for other projects
                continue

    def loop(self):
        logger.info("Orchestrator loop started.")
        while True:
            try:
                self.run_once()
            except Exception as e:
                logger.error(f"Error in orchestrator loop: {e}", exc_info=True)

            time.sleep(POLL_INTERVAL)
