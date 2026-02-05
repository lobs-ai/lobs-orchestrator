import logging
import time
import json
from pathlib import Path
from typing import Any
from orchestrator.providers.base import TaskProvider

logger = logging.getLogger(__name__)

class Monitor:
    """
    Observer component that monitors system state, cron jobs, and project health.
    Generates suggestions or alerts based on observations.
    """

    def __init__(self, provider: TaskProvider):
        self.provider = provider
        self.last_check = 0
        self.check_interval = 600  # 10 minutes

    def tick(self) -> None:
        """Periodic check for system health and inbox processing."""
        now = time.time()
        if now - self.last_check < self.check_interval:
            return

        logger.info("Running periodic system monitoring and inbox processing...")
        self.check_worker_heartbeats()
        self.check_cron_jobs()
        self.generate_proactive_suggestions()
        self.process_inbox()
        
        self.last_check = now

    def process_inbox(self) -> None:
        """Scan and respond to items in the inbox intended for the system."""
        items = self.provider.get_inbox_items()
        for item in items:
            if item.get("status") == "resolved":
                continue
            
            # If the item is assigned to the system and needs a response
            if item.get("responder") == "system" and item.get("actionRequired"):
                self._respond_to_inbox_item(item)

    def _respond_to_inbox_item(self, item: dict[str, Any]) -> None:
        """Handle a specific inbox item."""
        logger.info(f"System responding to inbox item: {item['id']}")
        
        item_type = item.get("type")
        if item_type == "request":
            # For example, if it's a request to analyze something
            logger.info(f"Processing system request: {item.get('title')}")
            # Potentially spawn a task or update state
            self.provider.update_inbox_item(item["id"], {
                "status": "resolved", 
                "response": "Acknowledged and processed by system monitor."
            })
        elif item_type == "suggestion" and item.get("autoApprove"):
            # Turn suggestion into a real task
            new_task = {
                "id": f"task_from_inbox_{item['id']}",
                "projectId": item.get("projectId", "default"),
                "title": item.get("title"),
                "notes": item.get("body"),
                "status": "active",
                "workState": "not_started"
            }
            self.provider.update_task(new_task["id"], new_task)
            self.provider.update_inbox_item(item["id"], {"status": "resolved", "taskId": new_task["id"]})

    def check_worker_heartbeats(self) -> None:
        """Verify that worker-status.json is being updated."""
        from orchestrator.config import WORKER_STATUS_JSON
        if not WORKER_STATUS_JSON.exists():
            return

        try:
            with open(WORKER_STATUS_JSON, "r") as f:
                status = json.load(f)
            
            last_heartbeat_str = status.get("lastHeartbeat")
            if not last_heartbeat_str or not status.get("active"):
                return

            from datetime import datetime
            last_heartbeat = datetime.fromisoformat(last_heartbeat_str.replace("Z", "+00:00"))
            now = datetime.now(datetime.UTC if hasattr(datetime, 'UTC') else None) # Handle 3.11/3.12 compatibility
            
            if not now.tzinfo:
                from datetime import timezone
                now = now.replace(tzinfo=timezone.utc)

            delta = (now - last_heartbeat).total_seconds()
            if delta > 1800:  # 30 minutes
                logger.warning(f"Worker heartbeat is stale ({delta}s). Notifying inbox.")
                self.provider.add_inbox_item({
                    "id": f"stale_worker_{int(time.time())}",
                    "title": "Alert: Stalled Worker Detected",
                    "body": f"The worker for task {status.get('currentTask')} hasn't reported a heartbeat in {int(delta/60)} minutes.",
                    "type": "alert",
                    "severity": "medium",
                    "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                })
        except Exception as e:
            logger.error(f"Failed to check heartbeats: {e}")

    def check_cron_jobs(self) -> None:
        """Check status of managed cron jobs/scheduled tasks."""
        # Check if worker-watcher (from lobs-control) is running
        # We can look for the request file's age if it exists, or a dedicated cron-heartbeat
        from orchestrator.config import CONTROL_REPO_PATH
        watcher_log = Path("/tmp/worker-watcher.log") # Based on lobs-control/README.md
        if watcher_log.exists():
            mtime = watcher_log.stat().st_mtime
            if time.time() - mtime > 600: # 10 minutes
                logger.warning("worker-watcher log is stale. It might be down.")
                self.provider.add_inbox_item({
                    "id": f"cron_failure_{int(time.time())}",
                    "title": "Alert: worker-watcher may be down",
                    "body": "The log file for worker-watcher hasn't been updated in 10 minutes.",
                    "type": "alert",
                    "severity": "medium",
                    "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                })

    def generate_proactive_suggestions(self) -> None:
        """LLM-powered pass to generate suggestions for the user inbox."""
        # In a real setup, this might spawn a 'light-check' or 'overview-review' agent
        # For now, we simulate a suggestion based on project activity.
        projects = self.provider.get_projects()
        for p in projects:
            if p.get("id") == "prairielearn":
                 # Example suggestion
                 self.provider.add_inbox_item({
                     "id": f"suggest_pl_{int(time.time())}",
                     "title": "Suggestion: Auto-generate test cases",
                     "body": "I noticed you're working on the PrairieLearn builder. I can help generate some initial test cases for the YAML editor.",
                     "type": "suggestion",
                     "projectId": "prairielearn",
                     "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                 })
                 break
