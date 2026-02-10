"""
Main orchestrator engine.

Runs the polling loop that:
1. Syncs with the control repo
2. Checks active workers
3. Scans for new work
4. Spawns workers for eligible tasks
5. Handles reconciliation and monitoring
6. Discovers and creates proactive work when idle
"""

import json
import time
import uuid
import logging
from datetime import datetime, timezone, time as dt_time
from pathlib import Path
from typing import Any

from orchestrator.utils.work_windows import prioritize_tasks_by_work_windows

from orchestrator.config import POLL_INTERVAL, STATE_DIR, CONTROL_REPO_PATH, TASKS_DIR
from orchestrator.core.worker import WorkerManager
from orchestrator.core.router import Router
from orchestrator.core.collaboration import CollaborationManager
from orchestrator.core.failure_rotation import FailureRotation
from orchestrator.core.reconciler import Reconciler
from orchestrator.core.heartbeat import HeartbeatManager
from orchestrator.providers.base import TaskProvider
from orchestrator.core.monitor import Monitor
from orchestrator.core.observer import Observer, Opportunity, OpportunityPriority
from orchestrator.services.messages import MessageProcessor
from orchestrator.utils.settings import get_setting

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
        self.collaboration = CollaborationManager(provider)
        self.worker_manager = WorkerManager(
            STATE_DIR,
            provider,
            failure_rotation=self.failure_rotation,
            collaboration_manager=self.collaboration,
        )
        self.router = Router()
        self.reconciler = Reconciler(provider)
        self.monitor = Monitor(provider)
        self.heartbeat = HeartbeatManager()
        self.message_processor = MessageProcessor(provider)
        self.observer = Observer()
        self.last_reconcile = 0
        self.reconcile_interval = 300  # 5 minutes
        self.last_heartbeat = 0
        self.start_time = time.time()

        # Round-robin offset used when multiple projects have overlapping active work windows.
        self._work_window_rr_offset = 0
        
        # Proactive work tracking
        self._proactive_stats = self._load_proactive_stats()
        self._last_proactive_scan = 0

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

    def _load_proactive_stats(self) -> dict[str, Any]:
        """Load proactive work statistics from state file."""
        stats_file = STATE_DIR / "proactive-stats.json"
        if not stats_file.exists():
            return {"daily_counts": {}, "total_created": 0, "total_completed": 0}
        try:
            with open(stats_file, "r") as f:
                return json.load(f)
        except Exception:
            return {"daily_counts": {}, "total_created": 0, "total_completed": 0}

    def _save_proactive_stats(self) -> None:
        """Save proactive work statistics to state file."""
        stats_file = STATE_DIR / "proactive-stats.json"
        stats_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(stats_file, "w") as f:
                json.dump(self._proactive_stats, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save proactive stats: {e}")

    def _get_proactive_count_today(self) -> int:
        """Get count of proactive tasks created today."""
        today = datetime.now(timezone.utc).date().isoformat()
        return self._proactive_stats.get("daily_counts", {}).get(today, 0)

    def _increment_proactive_count(self) -> None:
        """Increment today's proactive task count."""
        today = datetime.now(timezone.utc).date().isoformat()
        daily = self._proactive_stats.setdefault("daily_counts", {})
        daily[today] = daily.get(today, 0) + 1
        self._proactive_stats["total_created"] = self._proactive_stats.get("total_created", 0) + 1
        self._save_proactive_stats()

    def _prune_old_daily_counts(self) -> None:
        """Remove daily counts older than 30 days."""
        from datetime import date, timedelta
        try:
            cutoff = (datetime.now(timezone.utc).date() - timedelta(days=30)).isoformat()
            daily = self._proactive_stats.get("daily_counts", {})
            for day in list(daily.keys()):
                if day < cutoff:
                    del daily[day]
            self._save_proactive_stats()
        except Exception:
            pass

    def _should_scan_proactive(self, explicit_work: list[dict[str, Any]]) -> bool:
        """Check if we should scan for proactive work."""
        # Check if proactive work is enabled
        proactive_config = get_setting("proactive", {})
        if isinstance(proactive_config, dict):
            enabled = proactive_config.get("enabled", True)
        else:
            enabled = get_setting("proactive_enabled", True)
        
        if not enabled:
            return False

        # Only scan when idle (no explicit work pending)
        if explicit_work:
            return False

        # Check daily limit
        max_daily = self._get_proactive_max_daily()
        today_count = self._get_proactive_count_today()
        if today_count >= max_daily:
            return False

        # Check quiet hours
        if self._in_quiet_hours():
            return False

        return True

    def _get_proactive_max_daily(self) -> int:
        """Get max proactive tasks per day from config."""
        proactive_config = get_setting("proactive", {})
        if isinstance(proactive_config, dict):
            return int(proactive_config.get("max_daily_tasks", proactive_config.get("maxDailyTasks", 5)))
        return int(get_setting("proactive_max_daily_tasks", 5))

    def _get_proactive_min_priority(self) -> OpportunityPriority:
        """Get minimum priority threshold from config."""
        proactive_config = get_setting("proactive", {})
        if isinstance(proactive_config, dict):
            min_prio_str = proactive_config.get("min_priority", proactive_config.get("minPriority", "NORMAL"))
        else:
            min_prio_str = get_setting("proactive_min_priority", "NORMAL")
        
        # Map string to enum
        priority_map = {
            "URGENT": OpportunityPriority.URGENT,
            "HIGH": OpportunityPriority.HIGH,
            "NORMAL": OpportunityPriority.NORMAL,
            "LOW": OpportunityPriority.LOW,
            "BACKGROUND": OpportunityPriority.BACKGROUND,
        }
        return priority_map.get(min_prio_str.upper(), OpportunityPriority.NORMAL)

    def _in_quiet_hours(self) -> bool:
        """Check if current time is within configured quiet hours."""
        proactive_config = get_setting("proactive", {})
        if isinstance(proactive_config, dict):
            start_str = proactive_config.get("quiet_hours_start", proactive_config.get("quietHoursStart"))
            end_str = proactive_config.get("quiet_hours_end", proactive_config.get("quietHoursEnd"))
        else:
            start_str = get_setting("proactive_quiet_hours_start")
            end_str = get_setting("proactive_quiet_hours_end")

        if not start_str or not end_str:
            return False

        try:
            # Parse HH:MM format
            start_parts = start_str.split(":")
            end_parts = end_str.split(":")
            start_time = dt_time(int(start_parts[0]), int(start_parts[1]))
            end_time = dt_time(int(end_parts[0]), int(end_parts[1]))
            
            now = datetime.now().time()
            
            if start_time <= end_time:
                # Same day window
                return start_time <= now < end_time
            else:
                # Window spans midnight
                return now >= start_time or now < end_time
        except Exception:
            return False

    def _create_task_from_opportunity(self, opp: Opportunity) -> str | None:
        """Create a task file from an opportunity. Returns task_id if successful."""
        try:
            task_id = str(uuid.uuid4()).upper()
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            # Build task notes from opportunity details
            notes_parts = [opp.description]
            if opp.evidence:
                notes_parts.append(f"\n**Evidence:**\n```\n{opp.evidence}\n```")
            if opp.file_path:
                notes_parts.append(f"\n**File:** `{opp.file_path}`")
                if opp.line_number:
                    notes_parts.append(f" (line {opp.line_number})")
            notes = "\n".join(notes_parts)

            task = {
                "id": task_id,
                "title": opp.title,
                "notes": notes,
                "projectId": opp.project_id,
                "status": "active",
                "owner": "lobs",
                "reviewState": "approved",
                "workState": "not_started",
                "createdAt": now,
                "updatedAt": now,
                "tags": ["proactive", f"kind:{opp.kind}", f"priority:{opp.priority.name.lower()}"],
                "agent": opp.agent_type,  # Explicit agent routing
                "proactiveMeta": {
                    "opportunityId": opp.opportunity_id,
                    "kind": opp.kind,
                    "priority": opp.priority.name,
                    "score": opp.score,
                }
            }

            TASKS_DIR.mkdir(parents=True, exist_ok=True)
            task_path = TASKS_DIR / f"{task_id}.json"
            with open(task_path, "w") as f:
                json.dump(task, f, indent=2)
                f.write("\n")

            logger.info(
                f"[PROACTIVE] Created task {task_id[:8]} from {opp.kind} opportunity: {opp.title}"
            )
            return task_id
        except Exception as e:
            logger.error(f"Failed to create task from opportunity: {e}", exc_info=True)
            return None

    def _process_proactive_work(self, projects: list[dict[str, Any]], explicit_work: list[dict[str, Any]]) -> None:
        """Scan for and create proactive work tasks when idle."""
        if not self._should_scan_proactive(explicit_work):
            return

        try:
            # Scan for opportunities
            opportunities = self.observer.scan_for_opportunities(projects)
            
            if not opportunities:
                logger.debug("[PROACTIVE] No opportunities found")
                return

            # Filter by minimum priority
            min_priority = self._get_proactive_min_priority()
            filtered = [opp for opp in opportunities if opp.priority <= min_priority]
            
            if not filtered:
                logger.debug(f"[PROACTIVE] No opportunities above min priority {min_priority.name}")
                return

            # Respect daily limit
            max_daily = self._get_proactive_max_daily()
            today_count = self._get_proactive_count_today()
            remaining = max_daily - today_count
            
            if remaining <= 0:
                logger.debug(f"[PROACTIVE] Daily limit reached ({today_count}/{max_daily})")
                return

            # Create tasks from top opportunities
            created_count = 0
            for opp in filtered[:remaining]:
                task_id = self._create_task_from_opportunity(opp)
                if task_id:
                    self._increment_proactive_count()
                    created_count += 1

            if created_count > 0:
                logger.info(
                    f"[PROACTIVE] Created {created_count} proactive task(s). "
                    f"Today: {self._get_proactive_count_today()}/{max_daily}"
                )
                
                # Prune old stats periodically
                self._prune_old_daily_counts()

        except Exception as e:
            logger.error(f"Failed to process proactive work: {e}", exc_info=True)

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

            # Select an agent template for this task.
            # If an explicit `agent` field is provided on the task, honor it.
            explicit_agent = (item.get("agent") or "").strip()
            try:
                if explicit_agent:
                    agent_type = explicit_agent
                    logger.info(f"[ROUTER] Using explicit agent '{agent_type}' for {work_id[:8]}")
                else:
                    agent_type = self.router.route(item)
                    logger.info(f"[ROUTER] Selected agent '{agent_type}' for {work_id[:8]}")
            except Exception as e:
                # Router/registry validation shouldn't take down the engine.
                logger.warning(
                    f"[ROUTER] Failed to select agent for {work_id[:8]} ({work_title}): {e}. Falling back to 'programmer'."
                )
                agent_type = "programmer"

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

        # 9. Proactive Work Discovery (when idle)
        # After all explicit work is processed, check for proactive opportunities
        if not eligible_work and not self.worker_manager.get_worker_status()["busy"]:
            self._process_proactive_work(projects, eligible_work)

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
        
        # Calculate proactive stats
        proactive_enabled = get_setting("proactive", {}).get("enabled", True) if isinstance(get_setting("proactive", {}), dict) else get_setting("proactive_enabled", True)
        today_count = self._get_proactive_count_today()
        max_daily = self._get_proactive_max_daily()
        
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
            "proactive": {
                "enabled": proactive_enabled,
                "today_count": today_count,
                "max_daily": max_daily,
                "total_created": self._proactive_stats.get("total_created", 0),
                "in_quiet_hours": self._in_quiet_hours(),
            }
        }
