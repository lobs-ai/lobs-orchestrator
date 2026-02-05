import logging
import time
from typing import Any
from orchestrator.providers.base import TaskProvider

logger = logging.getLogger(__name__)

class EscalationManager:
    """
    Handles tiered escalation for errors and failures.
    Levels:
    1. Auto-fix (Code execution)
    2. Diagnostic (LLM Analysis)
    3. Human Intervention (Inbox Message)
    """

    def __init__(self, provider: TaskProvider):
        self.provider = provider

    def process_failure(self, task_id: str, project_id: str, error_log: str):
        """Initial entry point for a worker failure."""
        alert_id = f"alert_{task_id}"
        
        # Level 0: Create the alert record
        alert = {
            "id": alert_id,
            "taskId": task_id,
            "projectId": project_id,
            "errorLog": error_log,
            "level": 1,
            "status": "active"
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
        
        # In a real system, we'd match error patterns.
        # For now, we simulate "no auto-fix found" and move to Level 2.
        self.provider.update_alert(alert["id"], {"level": 2, "note": "No auto-fix found."})
        self._handle_level_2(alert)

    def _handle_level_2(self, alert: dict[str, Any]):
        """Level 2: LLM Diagnosis."""
        logger.info(f"Escalation Level 2 for {alert['id']}: Spawning diagnostic agent.")
        
        # We create a new task for a diagnostic agent
        diagnostic_task = {
            "id": f"diag_{alert['id']}",
            "projectId": alert["projectId"],
            "title": f"Diagnostic for failure {alert['taskId']}",
            "notes": f"Analyze this error: {alert['errorLog']}",
            "status": "active",
            "workState": "not_started",
            "agentType": "diagnostic"
        }
        # We use update_task to create it
        self.provider.update_task(diagnostic_task["id"], diagnostic_task)
        self.provider.update_alert(alert["id"], {"level": 3, "diagnosticTaskId": diagnostic_task["id"], "note": "Diagnostic agent spawned."})

    def _handle_level_3(self, alert: dict[str, Any]):
        """Level 3: Human help."""
        logger.info(f"Escalation Level 3 for {alert['id']}: Notifying human.")
        
        inbox_item = {
            "id": f"help_{alert['id']}",
            "title": "Action Required: Persistent Failure",
            "body": f"Task {alert['taskId']} in {alert['projectId']} has failed multiple escalation tiers.\nError: {alert['errorLog']}",
            "type": "alert",
            "severity": "high",
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        }
        self.provider.add_inbox_item(inbox_item)
        self.provider.update_alert(alert["id"], {"status": "escalated_to_human"})
