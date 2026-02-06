import time
import logging
import json
from datetime import datetime, timezone
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
        self.last_heartbeat = 0

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

    def _pulse_system_heartbeat(self) -> None:
        """Update the system heartbeat to indicate liveness."""
        now = time.time()
        # Pulse every 30 seconds
        if now - self.last_heartbeat > 30:
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.provider.update_worker_status({
                "lastHeartbeat": now_iso,
                # Ensure active is true when system is running
                "active": True 
            })
            self.last_heartbeat = now

    def run_once(self) -> bool:
        activity = False

        # 0. System Heartbeat
        self._pulse_system_heartbeat()

        # 1. Sync with provider and process messages
        messages = self.process_control()
        if messages:
            activity = True
            self.message_processor.process_messages(messages)

        # 2. Check active workers
        initial_active = len(self.worker_manager.active_workers)
        self.process_workers()
        if len(self.worker_manager.active_workers) != initial_active:
            activity = True

        # 3. System Monitoring & Cron watching
        self.monitor.tick()

        # 4. Scan for new work and facts from provider
        projects = self.provider.get_projects()
        project_ids = [p["id"] for p in projects if not p.get("archived", False)]
        # Always allow lobs-control for inbox/system tasks
        if "lobs-control" not in project_ids:
            project_ids.append("lobs-control")

        # 5. Periodic reconciliation
        self.process_reconciliation(project_ids)

        # 6. Handle dashboard requests
        has_request = self.provider.check_pending_request()
        if has_request:
            activity = True
            self.process_requests(True)

        # 7. Work Assignment
        eligible_work = self.provider.get_tasks()
        for item in eligible_work:
            kind = item.get("kind", "task")
            project_id = item.get("projectId")
            
            # Default project mapping for certain kinds
            if not project_id:
                if kind == "inbox_response":
                    project_id = "lobs-control"
                else:
                    project_id = "default"

            if project_id not in project_ids:
                logger.warning(
                    f"Work {item['id']} has invalid, archived, or unregistered projectId '{project_id}'. Skipping."
                )
                continue

            work_id = item["id"]

            if not self.worker_manager.is_domain_locked(project_id):
                activity = True
                logger.info(f"Assigning {kind} {work_id} to project {project_id}")

                # Determine agent type
                agent_type = item.get("agentType")
                if not agent_type:
                    if kind == "research_request":
                        agent_type = "researcher"
                    elif kind == "inbox_response":
                        agent_type = "inbox-processor"
                    else:
                        agent_type = "task-runner"

                # Get rules from provider
                rules = self.provider.get_engineering_rules()

                self.worker_manager.spawn_worker(
                    item, project_id, agent_type=agent_type, rules=rules
                )

                # Update state to in_progress via provider
                if kind == "task":
                    self.provider.update_task(work_id, {"workState": "in_progress"})
                else:
                    self.provider.update_task(work_id, {"status": "in_progress"})

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
