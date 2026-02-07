"""
Main orchestrator engine.

Runs the polling loop that:
1. Syncs with the control repo
2. Checks active workers
3. Scans for new work
4. Spawns workers for eligible tasks
5. Handles reconciliation and monitoring
"""

import time
import logging
from datetime import datetime, timezone
from typing import Any

from orchestrator.config import POLL_INTERVAL, LOCKS_DIR
from orchestrator.core.worker import WorkerManager
from orchestrator.core.reconciler import Reconciler
from orchestrator.core.heartbeat import HeartbeatManager
from orchestrator.providers.base import TaskProvider
from orchestrator.core.monitor import Monitor
from orchestrator.services.messages import MessageProcessor

logger = logging.getLogger(__name__)


class Orchestrator:
    """
    Main orchestration engine.
    
    Responsibilities:
    - Poll for work and spawn workers
    - Track worker lifecycle
    - Handle control operations
    - Periodic reconciliation and monitoring
    """

    def __init__(self, provider: TaskProvider):
        self.provider = provider
        self.worker_manager = WorkerManager(LOCKS_DIR, provider)
        self.reconciler = Reconciler(provider)
        self.monitor = Monitor(provider)
        self.heartbeat = HeartbeatManager()
        self.message_processor = MessageProcessor(provider)
        self.last_reconcile = 0
        self.reconcile_interval = 300  # 5 minutes
        self.last_heartbeat = 0
        self.start_time = time.time()

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
            uptime_s = int(now - self.start_time)
            self.provider.update_worker_status({
                "lastHeartbeat": now_iso,
                "active": True,
                "uptimeSeconds": uptime_s,
            })
            self.last_heartbeat = now

    def run_once(self) -> bool:
        """
        Execute one iteration of the orchestration loop.
        Returns True if there was activity.
        """
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

        # 3. Heartbeat manager tick (morning briefs, etc.)
        self.heartbeat.tick()

        # 4. System Monitoring
        self.monitor.tick()

        # 5. Scan for new work and facts from provider
        projects = self.provider.get_projects()
        project_ids = [p["id"] for p in projects if not p.get("archived", False)]
        # Always allow lobs-control for inbox/system tasks
        if "lobs-control" not in project_ids:
            project_ids.append("lobs-control")

        # 6. Periodic reconciliation
        self.process_reconciliation(project_ids)

        # 7. Handle dashboard requests
        has_request = self.provider.check_pending_request()
        if has_request:
            activity = True
            self.process_requests(True)

        # 8. Work Assignment
        eligible_work = self.provider.get_tasks()
        
        if eligible_work:
            self.heartbeat.notify_work_available(len(eligible_work))

        for item in eligible_work:
            kind = item.get("kind", "task")
            project_id = item.get("projectId")
            
            # Default project mapping for certain kinds
            if not project_id:
                if kind == "inbox_response":
                    project_id = "lobs-control"
                else:
                    project_id = "default"

            # Validate project exists
            if project_id not in project_ids and project_id != "lobs-control":
                logger.warning(
                    f"Work {item['id']} has invalid, archived, or unregistered projectId '{project_id}'. Skipping."
                )
                continue

            work_id = item["id"]
            work_title = item.get("title", item.get("prompt", work_id[:8]))

            if not self.worker_manager.is_domain_locked(project_id):
                activity = True
                logger.info(f"Assigning {kind} {work_id} to project {project_id}")

                # Single shared worker handles all kinds of work
                agent_type = item.get("agentType") or "worker"

                # Get rules from provider
                rules = self.provider.get_engineering_rules()

                # Spawn the worker
                self.worker_manager.spawn_worker(
                    item, project_id, agent_type=agent_type, rules=rules
                )

                # Notify heartbeat manager
                self.heartbeat.notify_worker_started(work_id, work_title, project_id)

                # Update state to in_progress via provider
                if kind == "task":
                    self.provider.update_task(work_id, {"workState": "in_progress"})
                else:
                    self.provider.update_task(work_id, {"status": "in_progress"})

        return activity

    def loop(self):
        """Main orchestration loop."""
        logger.info("=" * 60)
        logger.info("Lobs Orchestrator started")
        logger.info("=" * 60)
        
        current_interval = POLL_INTERVAL
        iteration = 0
        
        while True:
            try:
                iteration += 1
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
                logger.debug(f"Idle, sleeping for {current_interval}s (iteration {iteration})")

            time.sleep(current_interval)

    def status(self) -> dict[str, Any]:
        """Return current orchestrator status."""
        return {
            "running": True,
            "uptime_seconds": int(time.time() - self.start_time),
            "active_workers": len(self.worker_manager.active_workers),
            "pending_workers": len(self.worker_manager.pending_workers),
            "last_reconcile": self.last_reconcile,
            "last_heartbeat": self.last_heartbeat,
        }
