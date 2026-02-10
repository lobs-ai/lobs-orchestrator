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

from orchestrator.utils.work_windows import prioritize_tasks_by_work_windows

from orchestrator.config import POLL_INTERVAL, STATE_DIR
from orchestrator.core.worker import WorkerManager
from orchestrator.core.failure_rotation import FailureRotation
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
        self.failure_rotation = FailureRotation(STATE_DIR)
        self.worker_manager = WorkerManager(STATE_DIR, provider, failure_rotation=self.failure_rotation)
        self.reconciler = Reconciler(provider)
        self.monitor = Monitor(provider)
        self.heartbeat = HeartbeatManager()
        self.message_processor = MessageProcessor(provider)
        self.last_reconcile = 0
        self.reconcile_interval = 300  # 5 minutes
        self.last_heartbeat = 0
        self.start_time = time.time()

        # Round-robin offset used when multiple projects have overlapping active work windows.
        self._work_window_rr_offset = 0

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
            try:
                self.reconciler.reconcile(project_ids)
                logger.info("Periodic reconciliation complete.")
            except Exception as e:
                logger.error(f"Reconciliation failed: {e}", exc_info=True)
            finally:
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

        # 8. Work Assignment and Queueing
        eligible_work_raw = self.provider.get_tasks()

        # Failure rotation: skip tasks that are in cooldown after repeated failures
        eligible_work: list[dict[str, Any]] = []
        skipped_work: list[dict[str, Any]] = []
        for item in eligible_work_raw:
            work_id = item.get("id")
            if work_id and self.failure_rotation.should_skip(work_id):
                skipped_work.append(item)
            else:
                eligible_work.append(item)

        # If everything is skipped, pick one to retry to avoid queue deadlock.
        if not eligible_work and skipped_work:
            retry_id = self.failure_rotation.pick_retry_when_all_skipped([w["id"] for w in skipped_work if "id" in w])
            if retry_id:
                retry_item = next((w for w in skipped_work if w.get("id") == retry_id), None)
                if retry_item:
                    logger.warning(
                        f"[FAIL-ROTATION] All eligible tasks are in cooldown. Forcing retry of {retry_id[:8]} to avoid deadlock."
                    )
                    eligible_work = [retry_item]
                    skipped_work = [w for w in skipped_work if w.get("id") != retry_id]

        # Work windows (soft scheduling hint): during active windows, prefer tasks from those projects.
        now_utc = datetime.now(timezone.utc)
        eligible_work, active_window_projects = prioritize_tasks_by_work_windows(
            eligible_work,
            projects,
            now_utc=now_utc,
            rr_offset=self._work_window_rr_offset,
        )
        if active_window_projects:
            logger.info(f"[WORK-WINDOW] Active: {', '.join(active_window_projects)}")
            if len(active_window_projects) > 1:
                self._work_window_rr_offset = (self._work_window_rr_offset + 1) % len(active_window_projects)
        else:
            self._work_window_rr_offset = 0

        if eligible_work_raw:
            self.heartbeat.notify_work_available(len(eligible_work_raw))

            # Log queue depth if worker is busy
            worker_status = self.worker_manager.get_worker_status()
            if worker_status["busy"] and len(eligible_work_raw) > 0:
                current = worker_status["current_task"][:8] if worker_status["current_task"] else "unknown"
                logger.info(
                    f"[QUEUE] Worker busy (current: {current}, state: {worker_status['state']}). "
                    f"{len(eligible_work_raw)} task(s) queued."
                )

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

            # Try to assign work (spawn_worker handles queueing if busy)
            activity = True
            logger.info(f"Assigning {kind} {work_id} to project {project_id}")

            # NOTE: Single shared "worker" agent handles ALL work types
            # agentType is metadata used to customize the prompt, not to select different agents
            agent_type = item.get("agentType") or "worker"

            # Get rules from provider
            rules = self.provider.get_engineering_rules()

            # Spawn the single worker (will return False if busy/queued)
            spawned = self.worker_manager.spawn_worker(
                item, project_id, agent_type=agent_type, rules=rules
            )

            # Only update state if worker was actually spawned (not queued)
            if spawned:
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
        worker_status = self.worker_manager.get_worker_status()
        return {
            "running": True,
            "uptime_seconds": int(time.time() - self.start_time),
            "worker_busy": worker_status["busy"],
            "worker_state": worker_status["state"],
            "current_task": worker_status["current_task"],
            "active_workers": len(self.worker_manager.active_workers),  # Should be 0 or 1
            "pending_workers": len(self.worker_manager.pending_workers),  # Should be 0 or 1
            "last_reconcile": self.last_reconcile,
            "last_heartbeat": self.last_heartbeat,
        }
