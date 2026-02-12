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
import subprocess
import time
import uuid
import logging
from datetime import datetime, timezone, time as dt_time
from pathlib import Path
from typing import Any

from orchestrator.utils.work_windows import prioritize_tasks_by_work_windows

from orchestrator.config import POLL_INTERVAL, STATE_DIR, CONTROL_REPO_PATH, TASKS_DIR, MAX_WORKERS
from orchestrator.core.worker import WorkerManager
from orchestrator.core.router import Router
from orchestrator.core.collaboration import CollaborationManager
from orchestrator.core.failure_rotation import FailureRotation
from orchestrator.core.reconciler import Reconciler
from orchestrator.core.heartbeat import HeartbeatManager
from orchestrator.core.workflow import WorkflowEngine
from orchestrator.core.recurring_scheduler import RecurringScheduler
from orchestrator.core.pipelines import PipelineManager
from orchestrator.providers.base import TaskProvider
from orchestrator.core.monitor import Monitor
from orchestrator.core.observer import Observer, Opportunity, OpportunityPriority
from orchestrator.core.awareness import AwarenessMonitor
from orchestrator.core.agent_tracker import AgentTracker
from orchestrator.core.agent_memory import AgentMemoryManager
from orchestrator.core.llm_factory import create_backends
from orchestrator.core.llm_backend import HaikuBackend
from orchestrator.services.agent_meta import AgentMetaBrain
from orchestrator.services.messages import MessageProcessor
from orchestrator.utils.settings import get_setting
from orchestrator.core.registry import AgentRegistry

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
        
        # Awareness monitor for agent situational awareness
        self.awareness = AwarenessMonitor(provider)

        # Per-agent status tracking and memory
        self.agent_tracker = AgentTracker(CONTROL_REPO_PATH)
        self.agent_memory = AgentMemoryManager(CONTROL_REPO_PATH)

        # LLM backends for agent meta-brain
        backends = create_backends()
        # Use a HaikuBackend for the guardrail audit tier (if available)
        audit_backend = next((b for b in backends if isinstance(b, HaikuBackend)), None)
        self.agent_meta = AgentMetaBrain(
            backends=backends,
            agent_tracker=self.agent_tracker,
            agent_memory=self.agent_memory,
            thinking_interval=int(get_setting("ollama_meta_thinking_interval", 30)),
            activity_interval=int(get_setting("ollama_meta_activity_interval", 60)),
            audit_backend=audit_backend,
        )

        # Workflow engine for initiative tracking
        self.workflow = WorkflowEngine(state_path=STATE_DIR / "workflow-state.json")

        # Pick the first available backend for worker diagnostic tasks
        worker_backend = backends[0] if backends else None
        self.worker_manager = WorkerManager(
            STATE_DIR,
            provider,
            failure_rotation=self.failure_rotation,
            collaboration_manager=self.collaboration,
            awareness_monitor=self.awareness,
            agent_tracker=self.agent_tracker,
            agent_memory=self.agent_memory,
            agent_meta=self.agent_meta,
            max_workers=MAX_WORKERS,
            llm_backend=worker_backend,
        )
        self.router = Router()
        self.reconciler = Reconciler(provider)
        self.monitor = Monitor(provider)
        self.heartbeat = HeartbeatManager()

        # Autonomous agent launcher
        from orchestrator.core.autonomous import AutonomousLauncher
        self.autonomous = AutonomousLauncher()
        self.message_processor = MessageProcessor(provider)
        self.observer = Observer()

        # Cron-like recurring tasks
        from orchestrator.config import RECURRING_STATE_FILE

        self.recurring = RecurringScheduler(
            tasks_dir=TASKS_DIR,
            state_path=RECURRING_STATE_FILE,
        )

        # Multi-stage pipeline manager
        self.pipelines = PipelineManager()

        self.last_reconcile = 0
        self.reconcile_interval = 300  # 5 minutes
        self.last_heartbeat = 0
        self.start_time = time.time()

        # Round-robin offset used when multiple projects have overlapping active work windows.
        self._work_window_rr_offset = 0
        
        # Agent registry for AI advisor
        self.registry = AgentRegistry()

        # Proactive work tracking
        self._proactive_stats = self._load_proactive_stats()
        self._last_proactive_scan = 0

        # AI advisor round-robin state
        self._advisor_rr_index = 0
        self._ai_advisor_daily_count = 0
        self._ai_advisor_daily_date: str = ""
        
        # Workflow tracking
        self._last_workflow_update = 0
        self._workflow_update_interval = 60  # 1 minute

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

        # Rate limit: scan at most once per hour (configurable via scan_interval_seconds)
        scan_interval = int(
            proactive_config.get("scan_interval_seconds", 3600)
            if isinstance(proactive_config, dict)
            else get_setting("proactive_scan_interval_seconds", 3600)
        )
        now = time.time()
        if now - self._last_proactive_scan < scan_interval:
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

    def _create_inbox_proposal(self, opp: Opportunity) -> str | None:
        """Create an inbox proposal from an AI suggestion. Returns proposal_id if successful."""
        try:
            from orchestrator.config import CONTROL_REPO_PATH
            proposal_id = f"proposal_{opp.opportunity_id or str(uuid.uuid4()).upper()[:8]}"
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            body_parts = [opp.description]
            if opp.evidence:
                body_parts.append(f"\n**Evidence:**\n```\n{opp.evidence}\n```")
            if opp.file_path:
                body_parts.append(f"\n**File:** `{opp.file_path}`")
                if opp.line_number:
                    body_parts.append(f" (line {opp.line_number})")

            proposal = {
                "id": proposal_id,
                "title": f"💡 Proposal: {opp.title}",
                "body": "\n".join(body_parts),
                "type": "proposal",
                "severity": "low",
                "projectId": opp.project_id,
                "agent": opp.agent_type,
                "createdAt": now,
                "proactiveMeta": {
                    "opportunityId": opp.opportunity_id,
                    "kind": opp.kind,
                    "priority": opp.priority.name,
                    "score": opp.score,
                }
            }

            inbox_dir = CONTROL_REPO_PATH / "state" / "inbox"
            inbox_dir.mkdir(parents=True, exist_ok=True)
            proposal_path = inbox_dir / f"{proposal_id}.json"
            with open(proposal_path, "w") as f:
                json.dump(proposal, f, indent=2)
                f.write("\n")

            logger.info(
                f"[PROACTIVE] Created inbox proposal {proposal_id}: {opp.title}"
            )
            return proposal_id
        except Exception as e:
            logger.error(f"Failed to create inbox proposal: {e}", exc_info=True)
            return None

    def _process_proactive_work(self, projects: list[dict[str, Any]], explicit_work: list[dict[str, Any]]) -> None:
        """Scan for and create proactive work tasks when idle.
        
        Code-quality opportunities (TODOs, missing tests, etc.) become tasks directly.
        AI-suggested features/ideas become inbox proposals for human approval.
        """
        if not self._should_scan_proactive(explicit_work):
            return

        self._last_proactive_scan = time.time()

        try:
            # Phase 1: Deterministic Observer (code quality — auto-create tasks)
            opportunities = self.observer.scan_for_opportunities(projects)

            # Filter by minimum priority
            min_priority = self._get_proactive_min_priority()
            filtered = [opp for opp in opportunities if opp.priority <= min_priority]

            # Respect daily limit for auto-created tasks
            max_daily = self._get_proactive_max_daily()
            today_count = self._get_proactive_count_today()
            remaining = max_daily - today_count

            if remaining > 0 and filtered:
                created_count = 0
                for opp in filtered[:remaining]:
                    task_id = self._create_task_from_opportunity(opp)
                    if task_id:
                        self._increment_proactive_count()
                        created_count += 1

                if created_count > 0:
                    logger.info(
                        f"[PROACTIVE] Created {created_count} code-quality task(s). "
                        f"Today: {self._get_proactive_count_today()}/{max_daily}"
                    )
                    self._prune_old_daily_counts()

            # Phase 2: AI-powered suggestions → inbox proposals (need human approval)
            ai_opportunities = self._get_ai_suggestions(projects, opportunities)
            if ai_opportunities:
                proposal_count = 0
                for opp in ai_opportunities:
                    proposal_id = self._create_inbox_proposal(opp)
                    if proposal_id:
                        proposal_count += 1
                if proposal_count > 0:
                    logger.info(
                        f"[PROACTIVE] Created {proposal_count} inbox proposal(s) for review"
                    )

        except Exception as e:
            logger.error(f"Failed to process proactive work: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # AI Advisor (Phase 2 of proactive work)
    # ------------------------------------------------------------------

    _ADVISOR_AGENT_TYPES = ["programmer", "researcher", "reviewer", "writer", "architect"]

    def _get_ai_suggestions(
        self,
        projects: list[dict[str, Any]],
        existing_opportunities: list[Opportunity],
    ) -> list[Opportunity]:
        """Query the AI advisor for proactive work suggestions.

        Returns a list of Opportunity objects with ai_-prefixed kinds.
        Gracefully returns [] if AI advisor is disabled or no model is available.
        """
        # Check if AI advisor is enabled
        proactive_config = get_setting("proactive", {})
        if isinstance(proactive_config, dict):
            if not proactive_config.get("ai_advisor_enabled", True):
                return []
        else:
            return []  # No config dict → skip AI

        # Check daily limit for AI suggestions
        max_daily_ai = int(
            proactive_config.get("ai_advisor_max_daily", 10)
            if isinstance(proactive_config, dict) else 10
        )
        today = datetime.now(timezone.utc).date().isoformat()
        if self._ai_advisor_daily_date != today:
            self._ai_advisor_daily_count = 0
            self._ai_advisor_daily_date = today
        if self._ai_advisor_daily_count >= max_daily_ai:
            logger.debug("[PROACTIVE-AI] Daily AI suggestion limit reached (%d/%d)", self._ai_advisor_daily_count, max_daily_ai)
            return []

        # Pick one agent type via round-robin
        agent_type = self._pick_advisor_agent_type()

        # Get awareness context
        awareness_context = self.awareness.format_context_for_agent(agent_type)

        # Get agent config for capabilities/proactive lists
        try:
            agent_config = self.registry.get_agent(agent_type)
            capabilities = agent_config.capabilities
            proactive_list = agent_config.proactive
        except Exception:
            capabilities = []
            proactive_list = []

        # Build project summaries
        project_summaries = self._build_project_summaries(projects)

        # Collect existing opportunity kinds for dedup
        existing_kinds = {opp.kind for opp in existing_opportunities}

        # Call the AI advisor
        max_per_scan = int(
            proactive_config.get("ai_advisor_max_per_scan", 3)
            if isinstance(proactive_config, dict) else 3
        )

        suggestions = self.agent_meta.suggest_proactive_work(
            agent_type=agent_type,
            awareness_context=awareness_context,
            agent_capabilities=capabilities,
            agent_proactive=proactive_list,
            project_summaries=project_summaries,
            existing_kinds=existing_kinds,
        )

        # Convert dicts to Opportunity objects
        priority_map = {1: OpportunityPriority.URGENT, 2: OpportunityPriority.HIGH,
                        3: OpportunityPriority.NORMAL, 4: OpportunityPriority.LOW}
        result: list[Opportunity] = []
        for s in suggestions[:max_per_scan]:
            prio = priority_map.get(s.get("priority", 3), OpportunityPriority.NORMAL)
            opp = Opportunity(
                project_id=s["project_id"],
                agent_type=s["agent_type"],
                kind=s["kind"],
                title=s["title"],
                description=s["description"],
                priority=prio,
            )
            result.append(opp)

        if result:
            self._ai_advisor_daily_count += len(result)

        return result

    def _pick_advisor_agent_type(self) -> str:
        """Round-robin through agent types for AI advisor queries."""
        agent_type = self._ADVISOR_AGENT_TYPES[self._advisor_rr_index % len(self._ADVISOR_AGENT_TYPES)]
        self._advisor_rr_index = (self._advisor_rr_index + 1) % len(self._ADVISOR_AGENT_TYPES)
        return agent_type

    def _build_project_summaries(self, projects: list[dict[str, Any]]) -> list[dict]:
        """Build lightweight project summaries for the AI advisor prompt."""
        summaries: list[dict] = []
        for p in projects:
            if p.get("archived"):
                continue
            project_id = str(p.get("id") or "").strip()
            if not project_id:
                continue

            from orchestrator.config import BASE_DIR
            repo_path = Path(p.get("repoPath") or (BASE_DIR / project_id)).resolve()
            repo_exists = repo_path.exists() and repo_path.is_dir()

            summaries.append({
                "id": project_id,
                "has_python": repo_exists and (repo_path / "requirements.txt").exists(),
                "has_node": repo_exists and (repo_path / "package.json").exists(),
                "has_swift": repo_exists and (repo_path / "Package.swift").exists(),
                "repo_exists": repo_exists,
            })
        return summaries

    def _update_workflow_state(self) -> None:
        """Update workflow state and save snapshot periodically."""
        now = time.time()
        
        # Only update periodically to avoid excessive I/O
        if now - self._last_workflow_update < self._workflow_update_interval:
            return
        
        try:
            # Get all tasks (not just eligible work) to track full initiative state
            # We need to load tasks directly from filesystem to include all states
            all_tasks = []
            if TASKS_DIR.exists():
                for task_file in TASKS_DIR.glob("*.json"):
                    try:
                        with open(task_file, "r") as f:
                            task = json.load(f)
                            all_tasks.append(task)
                    except Exception as e:
                        logger.debug(f"Failed to load task from {task_file.name}: {e}")
            
            # Compute workflow snapshot
            snapshot = self.workflow.compute(all_tasks)
            
            # Save snapshot for reporting
            self.workflow.save_snapshot(snapshot)
            
            # Log active initiatives
            if snapshot.initiatives:
                active_initiatives = [
                    f"{name} ({prog.done}/{prog.total})"
                    for name, prog in sorted(snapshot.initiatives.items())
                    if not prog.complete
                ]
                if active_initiatives:
                    logger.info(f"[WORKFLOW] Active initiatives: {', '.join(active_initiatives)}")
                
                # Log completed initiatives
                completed_initiatives = [
                    name for name, prog in snapshot.initiatives.items() if prog.complete
                ]
                if completed_initiatives:
                    logger.debug(f"[WORKFLOW] Completed initiatives: {', '.join(completed_initiatives)}")
            
            self._last_workflow_update = now
            
        except Exception as e:
            logger.error(f"Failed to update workflow state: {e}", exc_info=True)

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

        # 2b. Sync agent status files to disk (rate-limited)
        self.agent_tracker.sync_to_disk()

        # 3. Heartbeat manager tick (morning briefs, etc.)
        self.heartbeat.tick()

        # 4. System Monitoring
        self.monitor.tick()

        # 4.5 Recurring work scheduler (cron-like)
        try:
            created = self.recurring.tick_with_persistence()
            if created:
                activity = True
        except Exception as e:
            logger.error(f"[RECURRING] Tick failed: {e}", exc_info=True)

        # 4.6 Pipeline approvals / auto-advance tick
        try:
            created = self.pipelines.tick()
            if created:
                activity = True
        except Exception as e:
            logger.error(f"[PIPELINE] Tick failed: {e}", exc_info=True)

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

        # 8. Workflow State Update
        # Compute initiative progress from all tasks (not just eligible work)
        self._update_workflow_state()

        # 9. Work Assignment and Queueing
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
                    # Try to infer project from docId (e.g. "inbox/flock-..." or "artifacts/flock-...")
                    doc_id = item.get("docId", "")
                    doc_lower = doc_id.lower()
                    inferred_project = None
                    for pid in project_ids:
                        if pid in doc_lower:
                            inferred_project = pid
                            break
                    project_id = inferred_project or "lobs-control"
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

            # Notify via system event so the main agent can relay to Discord
            if kind == "inbox_response":
                doc_id = item.get("docId", "unknown")
                last_msg = item.get("lastMessage", "")[:80]
                try:
                    self.heartbeat.chat.send_system_event(
                        f"[INBOX_PROCESSING] Inbox response picked up for processing. "
                        f"Doc: {doc_id}. Response: \"{last_msg}\". "
                        f"Routed to {agent_type} agent. "
                        f"Send a brief Discord message to Rafe confirming his inbox response is being handled."
                    )
                    logger.info(f"Sent inbox processing system event for {doc_id}")
                except Exception as e:
                    logger.warning(f"Failed to send inbox response notification: {e}")

            # Get rules from provider
            rules = self.provider.get_engineering_rules()

            # Spawn a worker slot (returns False when queued by capacity/lock checks)
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

        # 10. Proactive Work Discovery
        self._process_proactive_work(projects, eligible_work)

        # 11. Autonomous Agent Launches
        # When agents have no queued tasks, launch them to do their standing work
        self._launch_autonomous_agents(eligible_work)

        return activity

    def _launch_autonomous_agents(self, eligible_work: list[dict[str, Any]]) -> None:
        """Launch idle agents to do autonomous work (their standing mission)."""
        # Determine which agent types are active
        active_types = set(self.worker_manager.agent_locks.keys())
        
        # Only count explicitly-assigned tasks as queued for that agent type.
        # Router-inferred types shouldn't block autonomous launches — most tasks
        # default to programmer anyway.
        queued_types: set[str] = set()
        for item in eligible_work:
            explicit = (item.get("agent") or "").strip()
            if explicit:
                queued_types.add(explicit)
        
        logger.debug(
            f"[AUTONOMOUS] active={active_types}, queued={queued_types}, "
            f"eligible_work={len(eligible_work)}"
        )

        idle_agents = self.autonomous.get_idle_agents(active_types, queued_types)
        
        if not idle_agents:
            return

        for agent_type, config in idle_agents:
            # Check circuit breaker
            allowed, reason = self.worker_manager.circuit_breaker.should_allow_spawn()
            if not allowed:
                logger.debug(f"[AUTONOMOUS] Skipping {agent_type}: {reason}")
                break

            # Create a real task file so it shows on the dashboard
            import uuid
            task_id = str(uuid.uuid4()).upper()
            project_id = config["project"]
            
            from orchestrator.core.autonomous import AUTONOMOUS_PROMPT
            prompt = AUTONOMOUS_PROMPT.format(agent_type=agent_type)
            
            task = {
                "id": task_id,
                "title": f"Autonomous: {agent_type}",
                "prompt": prompt,
                "notes": prompt,
                "projectId": project_id,
                "kind": "task",
                "agent": agent_type,
                "autonomous": True,
                "status": "active",
                "workState": "in_progress",
                "owner": "lobs",
            }
            
            # Write task file to lobs-control so dashboard can see it
            try:
                from orchestrator.config import TASKS_DIR
                import json as _json
                task_file = TASKS_DIR / f"{task_id}.json"
                task_file.write_text(_json.dumps(task, indent=2), encoding="utf-8")
                logger.info(f"[AUTONOMOUS] Created task file for {agent_type}: {task_id[:8]}")
            except Exception as e:
                logger.warning(f"[AUTONOMOUS] Failed to write task file: {e}")
            
            rules = self.provider.get_engineering_rules()
            spawned = self.worker_manager.spawn_worker(
                task, project_id, agent_type=agent_type, rules=rules
            )
            
            if spawned:
                self.autonomous.mark_launched(agent_type)
                logger.info(
                    f"[AUTONOMOUS] Launched {agent_type} for autonomous work "
                    f"(project: {project_id})"
                )

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

    def shutdown(self, timeout: float = 300.0):
        """Gracefully shutdown the orchestrator.
        
        Args:
            timeout: Maximum time (in seconds) to wait for workers to complete.
                    Default: 300s (5 minutes)
        """
        logger.info("=" * 60)
        logger.info("Lobs Orchestrator shutting down...")
        logger.info("=" * 60)
        
        # Shutdown worker manager (which handles all active workers)
        self.worker_manager.shutdown(timeout=timeout)
        
        logger.info("Orchestrator shutdown complete")
        logger.info("=" * 60)

    def get_initiative_status(self, initiative: str | None = None) -> dict[str, Any]:
        """Get status for a specific initiative or all initiatives.
        
        Args:
            initiative: Initiative name, or None for all initiatives
            
        Returns:
            Initiative status including progress, tasks, and blocked tasks
        """
        try:
            # Load the latest workflow snapshot
            snapshot_data = self.workflow.load_snapshot()
            if not snapshot_data:
                return {"error": "No workflow state available"}
            
            if initiative:
                # Return specific initiative
                initiatives = snapshot_data.get("initiatives", {})
                if initiative not in initiatives:
                    return {"error": f"Initiative '{initiative}' not found"}
                
                init_data = initiatives[initiative]
                tasks = snapshot_data.get("tasks", {}).get(initiative, [])
                blocked_ids = set(snapshot_data.get("blockedTaskIds", []))
                
                return {
                    "initiative": initiative,
                    "progress": init_data,
                    "tasks": tasks,
                    "blocked_count": len([t for t in tasks if t["id"] in blocked_ids]),
                }
            else:
                # Return all initiatives
                return {
                    "initiatives": snapshot_data.get("initiatives", {}),
                    "total_initiatives": len(snapshot_data.get("initiatives", {})),
                    "blocked_task_count": len(snapshot_data.get("blockedTaskIds", [])),
                }
        except Exception as e:
            logger.error(f"Failed to get initiative status: {e}", exc_info=True)
            return {"error": str(e)}

    def status(self) -> dict[str, Any]:
        """Return current orchestrator status."""
        worker_status = self.worker_manager.get_worker_status()
        
        # Calculate proactive stats
        proactive_enabled = get_setting("proactive", {}).get("enabled", True) if isinstance(get_setting("proactive", {}), dict) else get_setting("proactive_enabled", True)
        today_count = self._get_proactive_count_today()
        max_daily = self._get_proactive_max_daily()
        
        # Get workflow summary
        workflow_summary = self.get_initiative_status()
        
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
                "ai_advisor": {
                    "enabled": get_setting("proactive", {}).get("ai_advisor_enabled", True) if isinstance(get_setting("proactive", {}), dict) else False,
                    "daily_count": self._ai_advisor_daily_count,
                    "next_agent": self._ADVISOR_AGENT_TYPES[self._advisor_rr_index % len(self._ADVISOR_AGENT_TYPES)],
                },
            },
            "awareness": self.awareness.get_status() if self.awareness else {},
            "workflow": workflow_summary,
        }
