import logging
import time
import subprocess
from typing import Any
from orchestrator.providers.base import TaskProvider

logger = logging.getLogger(__name__)

class EscalationManager:
    """
    Handles tiered escalation for errors and failures.
    Levels:
    1. Auto-fix (Common pattern matching)
    2. Diagnostic (LLM Analysis)
    3. Human Intervention (Inbox Message)
    """

    def __init__(self, provider: TaskProvider):
        self.provider = provider

    def process_failure(self, task_id: str, project_id: str, error_log: str):
        """Initial entry point for a worker failure."""
        alert_id = f"alert_{task_id}_{int(time.time())}"
        
        # Level 0: Create the alert record
        alert = {
            "id": alert_id,
            "taskId": task_id,
            "projectId": project_id,
            "errorLog": error_log,
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
        
        # Example Pattern: Missing node_modules
        if "cannot find module" in error_log or "module not found" in error_log:
            logger.info(f"Detected potential missing dependency in {project_id}. Attempting install...")
            try:
                if (project_path / "package.json").exists():
                    subprocess.run(["npm", "install"], cwd=project_path, check=True)
                    auto_fixed = True
                elif (project_path / "requirements.txt").exists():
                    subprocess.run(["pip", "install", "-r", "requirements.txt"], cwd=project_path, check=True)
                    auto_fixed = True
            except Exception as e:
                logger.error(f"Auto-fix failed: {e}")

        if auto_fixed:
            logger.info(f"Auto-fix successful for {alert['id']}. Re-activating task.")
            self.provider.update_task(task_id, {"workState": "not_started", "status": "active"})
            self.provider.update_alert(alert["id"], {"status": "resolved", "note": "Auto-fixed by Level 1."})
        else:
            # Move to Level 2
            self.provider.update_alert(alert["id"], {"level": 2, "note": "No auto-fix patterns matched."})
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
            "title": f"Diagnostic for failure {alert['taskId']}",
            "notes": f"Analyze this error: {alert['errorLog']}",
            "status": "active",
            "workState": "not_started",
            "agentType": "diagnostic"
        }
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
