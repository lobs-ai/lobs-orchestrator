"""
Heartbeat manager for the orchestrator.

The orchestrator itself IS the heartbeat - it handles periodic checks,
worker spawning, and system monitoring. This module provides optional
hooks for notifications on important events.

Note: Daily briefs removed - not appropriate for multi-user deployments.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional, Any
from orchestrator.services.chat import get_chat_service
from orchestrator.utils.settings import get_setting

logger = logging.getLogger(__name__)


class HeartbeatManager:
    """
    Manages notifications and periodic interactions.
    """

    def __init__(self):
        self.chat = get_chat_service()

    def tick(self) -> None:
        """Called periodically by the engine. Currently a no-op."""
        pass

    def notify_work_available(self, count: int) -> None:
        """
        Optionally notify when new work is available.
        Usually silent - just logs.
        """
        logger.info(f"Work available: {count} items")

    def notify_worker_started(self, task_id: str, task_title: str, project_id: str) -> None:
        """Log worker start - usually silent."""
        logger.info(f"Worker started: {task_title} ({project_id})")

    def notify_worker_complete(
        self,
        task_id: str,
        task_title: str,
        project_id: str,
        success: bool,
        duration_s: float
    ) -> None:
        """
        Notify on worker completion.
        Only sends to chat if configured to do so.
        """
        status = "completed" if success else "failed"
        logger.info(f"Worker {status}: {task_title} ({project_id}) in {duration_s:.1f}s")
        
        # Send notification if configured
        notify_on_complete = get_setting("notify_on_complete", False)
        notify_on_failure = get_setting("notify_on_failure", True)
        
        if (success and notify_on_complete) or (not success and notify_on_failure):
            self.chat.notify_task_complete(task_id, task_title, project_id, success)

    def notify_escalation(self, task_id: str, level: int, message: str) -> None:
        """
        Notify on escalation events.
        Level 3 (human needed) always notifies.
        """
        if level >= 3:
            self.chat.send_alert(
                title="Escalation: Human Needed",
                body=message,
                severity="high"
            )
        else:
            logger.info(f"Escalation level {level} for {task_id}: {message}")
