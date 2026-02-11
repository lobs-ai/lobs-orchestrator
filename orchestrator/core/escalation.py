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
        """Level 2: LLM Diagnosis."""
        logger.info(f"Escalation Level 2 for {alert['id']}: Spawning diagnostic agent.")
        
        # We create a new task for a diagnostic agent
        diagnostic_task = {
            "id": f"diag_{alert['id']}",
            "projectId": alert["projectId"],
            "title": f"Diagnostic: analyze failure {alert['taskId']}",
            "notes": f"Analyze this error and propose a fix:\n\n{alert['errorLog'][:1500]}",
            "status": "active",
            "workState": "not_started",
            "agentType": "diagnostic",
            "priority": 0,  # High priority
        }
        self.provider.update_task(diagnostic_task["id"], diagnostic_task)
        self.provider.update_alert(alert["id"], {
            "level": 3,
            "diagnosticTaskId": diagnostic_task["id"],
            "note": "Diagnostic agent spawned."
        })

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
