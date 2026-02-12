"""
Escalation manager for handling worker failures.

Levels:
0. Auto-retry (transient failures)
1. Auto-fix (common pattern matching)
2. Diagnostic (LLM analysis)
3. Human intervention (inbox message + notification)
"""

import logging
import time
import subprocess
from typing import Any
from orchestrator.providers.base import TaskProvider
from orchestrator.services.chat import get_chat_service
from orchestrator.core.retry import get_retry_manager

logger = logging.getLogger(__name__)


class EscalationManager:
    """
    Handles tiered escalation for errors and failures.
    
    Levels:
    1. Auto-fix: Common pattern matching (missing deps, etc.)
    2. Diagnostic: Spawn a diagnostic agent to analyze
    3. Human: Notify the owner and wait for input
    """

    def __init__(self, provider: TaskProvider):
        self.provider = provider
        self.chat = get_chat_service()

    def process_failure(self, task_id: str, project_id: str, error_log: str):
        """Initial entry point for a worker failure."""
        
        # Level 0: Check if this failure is eligible for automatic retry
        retry_manager = get_retry_manager()
        
        # Get current task state
        task = self.provider.get_task(task_id)
        if not task:
            logger.warning(f"Cannot retry task {task_id}: task not found")
            # Fall through to alert creation
        else:
            should_retry, pattern_name, guidance = retry_manager.should_retry(task, error_log)
            
            if should_retry:
                logger.info(
                    f"Auto-retry triggered for task {task_id}: "
                    f"pattern={pattern_name}, guidance={guidance}"
                )
                
                # Prepare retry updates
                updates = retry_manager.prepare_retry(task, pattern_name, guidance, error_log)
                
                # Apply retry updates to task
                self.provider.update_task(task_id, updates)
                
                logger.info(
                    f"Task {task_id} reset for retry "
                    f"(attempt {updates.get('retryCount', 1)})"
                )
                
                # Don't create an alert - retry instead
                return
        
        # If retry not eligible, proceed with alert escalation
        alert_id = f"alert_{task_id}_{int(time.time())}"
        
        # Create the alert record
        alert = {
            "id": alert_id,
            "taskId": task_id,
            "projectId": project_id,
            "errorLog": error_log[:2000],  # Truncate for storage
            "level": 1,
            "status": "active",
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        }
        self.provider.update_alert(alert_id, alert)
        
        # Attempt Level 1
        self.escalate(alert_id)

    def escalate(self, alert_id: str):
        """Move an alert to the next escalation level."""
        alerts = self.provider.get_active_alerts()
        if not isinstance(alerts, list):
            logger.warning("Provider returned non-list active alerts; skipping escalation step")
            return
        alert = next((a for a in alerts if a["id"] == alert_id), None)
        
        if not alert:
            return

        level = alert.get("level", 1)
        
        if level == 1:
            self._handle_level_1(alert)
        elif level == 2:
            self._handle_level_2(alert)
        elif level == 3:
            self._handle_level_3(alert)
        else:
            logger.info(f"Alert {alert_id} reached max escalation.")

    def _handle_level_1(self, alert: dict[str, Any]):
        """Level 1: Auto-fix or quick code attempt."""
        logger.info(f"Escalation Level 1 for {alert['id']}: Checking for auto-fixes.")
        
        error_log = alert.get("errorLog", "").lower()
        project_id = alert.get("projectId")
        task_id = alert.get("taskId")
        
        from orchestrator.config import BASE_DIR
        project_path = BASE_DIR / project_id
        
        auto_fixed = False
        fix_note = ""
        
        # Pattern: Missing node_modules
        if "cannot find module" in error_log or "module not found" in error_log:
            logger.info(f"Detected potential missing dependency in {project_id}. Attempting install...")
            try:
                if (project_path / "package.json").exists():
                    subprocess.run(["npm", "install"], cwd=project_path, check=True, timeout=120)
                    auto_fixed = True
                    fix_note = "Ran npm install"
                elif (project_path / "requirements.txt").exists():
                    subprocess.run(["pip", "install", "-r", "requirements.txt"], cwd=project_path, check=True, timeout=120)
                    auto_fixed = True
                    fix_note = "Ran pip install -r requirements.txt"
            except Exception as e:
                logger.error(f"Auto-fix failed: {e}")

        # Pattern: Git conflicts
        if "conflict" in error_log and "merge" in error_log:
            logger.info(f"Detected git conflict in {project_id}. Cannot auto-fix, escalating.")
            fix_note = "Git conflict detected - requires human resolution"

        # Pattern: Permission denied
        if "permission denied" in error_log:
            logger.info(f"Permission denied in {project_id}. Cannot auto-fix, escalating.")
            fix_note = "Permission issue detected"

        if auto_fixed:
            logger.info(f"Auto-fix successful for {alert['id']}. Re-activating task.")
            self.provider.update_task(task_id, {"workState": "not_started", "status": "active"})
            self.provider.update_alert(alert["id"], {
                "status": "resolved",
                "note": f"Auto-fixed by Level 1: {fix_note}"
            })
        else:
            # Move to Level 2
            self.provider.update_alert(alert["id"], {
                "level": 2,
                "note": fix_note or "No auto-fix patterns matched."
            })
            # Re-fetch alert with updated level and continue
            alert["level"] = 2
            self._handle_level_2(alert)

    def _handle_level_2(self, alert: dict[str, Any]):
        """Level 2: Failure diagnosis via reviewer agent (failure-analysis mode)."""
        logger.info(f"Escalation Level 2 for {alert['id']}: Spawning reviewer failure-analysis task.")

        task_id = alert.get("taskId")
        project_id = alert.get("projectId")
        error_log = (alert.get("errorLog") or "")

        failed_task = self.provider.get_task(task_id) if task_id else None

        # Reviewer task should emit a machine-readable JSON recommendation.
        reviewer_task_id = f"review_{alert['id']}"
        reviewer_notes = self._build_reviewer_failure_prompt(
            alert_id=alert["id"],
            failed_task=failed_task,
            project_id=project_id,
            error_log=error_log,
        )

        reviewer_task = {
            "id": reviewer_task_id,
            "projectId": project_id,
            "title": f"Failure analysis: {task_id}",
            "notes": reviewer_notes,
            "status": "active",
            "workState": "not_started",
            "agent": "reviewer",
            "agentType": "reviewer",
            "priority": 0,
            # Metadata so WorkerManager can route the result back here.
            "escalationMeta": {
                "alertId": alert["id"],
                "failedTaskId": task_id,
                "projectId": project_id,
            },
        }

        self.provider.update_task(reviewer_task_id, reviewer_task)
        self.provider.update_alert(
            alert["id"],
            {
                "level": 2,
                "reviewerTaskId": reviewer_task_id,
                "status": "waiting_reviewer",
                "note": "Reviewer failure-analysis task spawned.",
            },
        )

    def process_reviewer_result(self, reviewer_task: dict[str, Any]) -> None:
        """Process a completed reviewer failure-analysis task and act on recommendation."""
        meta = reviewer_task.get("escalationMeta") or {}
        alert_id = meta.get("alertId")
        failed_task_id = meta.get("failedTaskId")
        project_id = meta.get("projectId") or reviewer_task.get("projectId")

        if not alert_id or not failed_task_id or not project_id:
            logger.warning("Reviewer result missing escalationMeta; cannot process")
            return

        from orchestrator.config import WORKER_RESULTS_DIR

        log_path = WORKER_RESULTS_DIR / f"{reviewer_task['id']}.log"
        try:
            text = log_path.read_text(encoding="utf-8", errors="ignore") if log_path.exists() else ""
        except Exception as e:
            logger.warning(f"Failed to read reviewer log for {reviewer_task['id']}: {e}")
            text = ""

        rec = self._extract_reviewer_recommendation(text)
        if rec is None:
            logger.info(f"Reviewer task {reviewer_task['id']} produced no parseable recommendation; escalating")
            self.provider.update_alert(alert_id, {"level": 3, "status": "active", "note": "Reviewer output not parseable; escalating to human."})
            self._handle_level_3({"id": alert_id, "taskId": failed_task_id, "projectId": project_id, "errorLog": text})
            return

        recommendation = (rec.get("recommendation") or "").strip().lower()
        guidance = (rec.get("guidance") or "").strip()
        root_cause = (rec.get("rootCause") or rec.get("root_cause") or "").strip()

        failed_task = self.provider.get_task(failed_task_id)
        if not failed_task:
            logger.warning(f"Failed task {failed_task_id} not found; cannot apply reviewer recommendation")
            return

        if recommendation == "retry":
            self._apply_reviewer_retry(
                alert_id=alert_id,
                failed_task=failed_task,
                guidance=guidance,
                root_cause=root_cause,
                reviewer_task_id=reviewer_task.get("id"),
            )
            return

        if recommendation == "subtask":
            self._apply_reviewer_subtask(
                alert_id=alert_id,
                failed_task=failed_task,
                rec=rec,
                reviewer_task_id=reviewer_task.get("id"),
            )
            return

        # Default / explicit escalate
        self.provider.update_alert(
            alert_id,
            {
                "level": 3,
                "status": "active",
                "note": f"Reviewer recommended escalation (recommendation={recommendation or 'unknown'}).",
            },
        )
        self._handle_level_3({"id": alert_id, "taskId": failed_task_id, "projectId": project_id, "errorLog": failed_task.get("failureReason") or ""})

    def _build_reviewer_failure_prompt(
        self,
        *,
        alert_id: str,
        failed_task: dict[str, Any] | None,
        project_id: str,
        error_log: str,
    ) -> str:
        import json

        task_blob = json.dumps(failed_task or {}, indent=2)[:6000]
        err_blob = (error_log or "")[:3000]

        return (
            "You are in FAILURE ANALYSIS mode.\n\n"
            "Analyze the failed task and error logs. Produce a diagnosis and an actionable recommendation.\n\n"
            "IMPORTANT: End your response with a single JSON object on its own lines, like:\n"
            "{\"recommendation\": \"retry\", \"guidance\": \"...\", \"rootCause\": \"...\"}\n"
            "Valid recommendation values: retry | subtask | escalate\n\n"
            f"Alert ID: {alert_id}\n"
            f"Project: {project_id}\n\n"
            "Failed task JSON:\n`````\n"
            f"{task_blob}\n"
            "`````\n\n"
            "Worker error log (truncated):\n`````\n"
            f"{err_blob}\n"
            "`````\n"
        )

    def _extract_reviewer_recommendation(self, text: str) -> dict[str, Any] | None:
        """Best-effort extraction of a trailing JSON object from reviewer output."""
        import json

        if not text:
            return None

        # Fast path: whole text is JSON.
        s = text.strip()
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass

        # Heuristic: find last JSON object in text.
        start = s.rfind("{")
        end = s.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None

        blob = s[start : end + 1]
        try:
            obj = json.loads(blob)
            if isinstance(obj, dict):
                return obj
        except Exception:
            return None
        return None

    def _apply_reviewer_retry(
        self,
        *,
        alert_id: str,
        failed_task: dict[str, Any],
        guidance: str,
        root_cause: str,
        reviewer_task_id: str | None,
    ) -> None:
        retry_manager = get_retry_manager()

        # Enforce max retry and prevent repeating same root cause.
        retry_count = int(failed_task.get("retryCount") or 0)
        if retry_count >= 3:
            self.provider.update_alert(alert_id, {"level": 3, "status": "active", "note": "Reviewer suggested retry but max retryCount reached; escalating."})
            return

        reason = (root_cause or "reviewer_guidance").strip()[:80] or "reviewer_guidance"
        pattern_name = f"reviewer:{reason}"

        last_reason = (failed_task.get("lastRetryReason") or "")
        if last_reason == pattern_name:
            self.provider.update_alert(alert_id, {"level": 3, "status": "active", "note": "Reviewer suggested retry but same root cause as last retry; escalating."})
            return

        guidance_msg = guidance or "Reviewer recommended retry with updated approach."
        updates = retry_manager.prepare_retry(failed_task, pattern_name, guidance_msg, "")
        self.provider.update_task(failed_task["id"], updates)

        self.provider.update_alert(
            alert_id,
            {
                "status": "resolved",
                "note": f"Reviewer recommended retry (reason={pattern_name}).",
                "reviewerTaskId": reviewer_task_id,
            },
        )

    def _apply_reviewer_subtask(
        self,
        *,
        alert_id: str,
        failed_task: dict[str, Any],
        rec: dict[str, Any],
        reviewer_task_id: str | None,
    ) -> None:
        import time

        title = (rec.get("subtaskTitle") or rec.get("title") or "Fix blocker from failure analysis").strip()
        context = (rec.get("guidance") or rec.get("context") or "").strip()
        to_agent = (rec.get("to") or "programmer").strip().lower()
        if to_agent not in {"programmer", "architect", "researcher", "writer", "reviewer"}:
            to_agent = "programmer"

        sub_id = f"sub_{failed_task['id']}_{int(time.time())}"
        subtask = {
            "id": sub_id,
            "projectId": failed_task.get("projectId"),
            "title": title,
            "notes": (
                f"Created from failure analysis of task {failed_task['id']}.\n\n"
                f"Context / guidance:\n{context}\n"
            ),
            "status": "active",
            "workState": "not_started",
            "agent": to_agent,
            "agentType": to_agent,
        }

        self.provider.update_task(sub_id, subtask)
        self.provider.update_alert(
            alert_id,
            {
                "status": "resolved",
                "note": f"Reviewer recommended subtask; created {sub_id} assigned to {to_agent}.",
                "reviewerTaskId": reviewer_task_id,
            },
        )

    def _handle_level_3(self, alert: dict[str, Any]):
        """Level 3: Human help needed."""
        logger.info(f"Escalation Level 3 for {alert['id']}: Notifying human.")
        
        task_id = alert.get("taskId", "unknown")
        project_id = alert.get("projectId", "unknown")
        error_preview = alert.get("errorLog", "")[:500]
        
        # Create inbox item
        inbox_item = {
            "id": f"help_{alert['id']}",
            "title": "🚨 Action Required: Persistent Failure",
            "body": f"Task `{task_id}` in **{project_id}** has failed multiple escalation tiers.\n\nError:\n```\n{error_preview}\n```",
            "type": "alert",
            "severity": "high",
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        }
        self.provider.add_inbox_item(inbox_item)
        
        # Send notification to chat
        self.chat.send_alert(
            title="Task Failure - Human Needed",
            body=f"Task `{task_id}` in **{project_id}** failed after auto-fix and diagnostic attempts.\n\nCheck the inbox for details.",
            severity="high"
        )
        
        self.provider.update_alert(alert["id"], {"status": "escalated_to_human"})
