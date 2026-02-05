import time
import logging
import json
from orchestrator.config import POLL_INTERVAL, LOCKS_DIR
from orchestrator.core.worker import WorkerManager
from orchestrator.core.reconciler import Reconciler
from orchestrator.providers.base import TaskProvider
from orchestrator.core.monitor import Monitor
from orchestrator.services.messages import MessageProcessor

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, provider: TaskProvider):
        self.provider = provider
        self.worker_manager = WorkerManager(LOCKS_DIR, provider)
        self.reconciler = Reconciler(provider)
        self.monitor = Monitor(provider)
        self.message_processor = MessageProcessor(provider)
        self.last_reconcile = 0
        self.reconcile_interval = 300  # 5 minutes

    def process_control(self) -> list[dict[str, Any]]:
        """Process pending control operations via provider."""
        return self.provider.sync()

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

    def run_once(self) -> bool:
        activity = False

        # 1. Sync with provider and process messages
        messages = self.process_control()
        if messages:
            activity = True
            self.message_processor.process_messages(messages)

        # 2. Check active workers
        # We consider worker check as activity only if something finished
        initial_active = len(self.worker_manager.active_workers)
        self.process_workers()
        if len(self.worker_manager.active_workers) != initial_active:
            activity = True

        # 3. System Monitoring & Cron watching
        self.monitor.tick()

        # 4. Scan for new work and facts from provider
        projects = self.provider.get_projects()
        project_ids = [p["id"] for p in projects if not p.get("archived", False)]

        # 5. Periodic reconciliation
        self.process_reconciliation(project_ids)

        # 6. Handle dashboard requests
        has_request = self.provider.check_pending_request()
        if has_request:
            activity = True
            self.process_requests(True)

        # 7. Task Assignment
        eligible_tasks = self.provider.get_tasks()
        for task in eligible_tasks:
            project_id = task.get("projectId")
            if not project_id or project_id not in project_ids:
                logger.warning(
                    f"Task {task['id']} has invalid or unregistered projectId '{project_id}'. Skipping."
                )
                continue

            task_id = task["id"]

            if not self.worker_manager.is_domain_locked(project_id):
                activity = True
                logger.info(f"Assigning task {task_id} to project {project_id}")

                # Determine agent type
                agent_type = task.get("agentType", "task-runner")

                # Get rules from provider
                rules = self.provider.get_engineering_rules()

                self.worker_manager.spawn_worker(
                    task, project_id, agent_type=agent_type, rules=rules
                )

                # Update task state to in_progress via provider
                self.provider.update_task(task_id, {"workState": "in_progress"})

        return activity

    def loop(self):
        logger.info("Orchestrator loop started.")
        current_interval = POLL_INTERVAL
        while True:
            try:
                activity = self.run_once()
                if activity:
                    current_interval = POLL_INTERVAL
                else:
                    # Adaptive backoff: increase sleep if idle, up to 6x POLL_INTERVAL or 60s
                    current_interval = min(current_interval + 2, POLL_INTERVAL * 6, 60)
            except Exception as e:
                logger.error(f"Error in orchestrator loop: {e}", exc_info=True)
                current_interval = POLL_INTERVAL  # Reset on error

            if current_interval > POLL_INTERVAL:
                logger.debug(f"Idle, sleeping for {current_interval}s")

            time.sleep(current_interval)
