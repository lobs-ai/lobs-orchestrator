import time
import logging
from .config import POLL_INTERVAL, LOCKS_DIR
from .worker import WorkerManager
from .reconciler import Reconciler
from .base import TaskProvider

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, provider: TaskProvider):
        self.provider = provider
        self.worker_manager = WorkerManager(LOCKS_DIR, provider)
        self.reconciler = Reconciler(provider)
        self.last_reconcile = 0
        self.reconcile_interval = 300  # 5 minutes

    def process_control(self) -> None:
        """Process pending control operations via provider."""
        self.provider.sync()

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
        """Handle explicit worker requests via provider."""
        if pending_request:
            logger.info("Worker request detected. Prioritizing.")
            self.provider.consume_request()

    def run_once(self) -> None:
        # 1. Sync with provider (e.g. process local ops or API sync)
        self.process_control()

        # 2. Check active workers
        self.process_workers()

        # 3. Scan for new work and facts from provider
        projects = self.provider.get_projects()
        project_ids = [p["id"] for p in projects if not p.get("archived", False)]

        # 4. Periodic reconciliation
        self.process_reconciliation(project_ids)

        # 5. Handle dashboard requests
        self.process_requests(self.provider.check_pending_request())

        # 6. Task Assignment
        eligible_tasks = self.provider.get_tasks()
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
                
                # Get rules from provider
                rules = self.provider.get_engineering_rules()
                
                self.worker_manager.spawn_worker(task, project_id, agent_type=agent_type, rules=rules)

                # Update task state to in_progress via provider
                self.provider.update_task(task_id, {"workState": "in_progress"})

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
