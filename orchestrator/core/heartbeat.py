"""
Heartbeat manager for the orchestrator.

The orchestrator itself IS the heartbeat - it handles periodic checks,
worker spawning, and system monitoring. This module adds optional
integration with the main chat session for:
- Morning briefs
- Proactive suggestion reviews
- Important notifications
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
    Manages periodic interactions with the main chat session.
    """

    def __init__(self):
        self.chat = get_chat_service()
        self.last_morning_brief = 0
        self.last_proactive_ping = 0
        
        # Intervals (in seconds)
        self.morning_brief_hour = get_setting("morning_brief_hour", 7)  # 7 AM
        self.proactive_interval = get_setting("proactive_interval", 3600 * 4)  # 4 hours

    def tick(self) -> None:
        """Called periodically by the engine."""
        now = time.time()
        current_hour = datetime.now().hour
        
        # Morning brief check (once per day at configured hour)
        if self._should_send_morning_brief(current_hour):
            self._send_morning_brief()
            self.last_morning_brief = now

    def _should_send_morning_brief(self, current_hour: int) -> bool:
        """Check if we should send the morning brief."""
        if current_hour != self.morning_brief_hour:
            return False
        
        # Only send once per day
        last_brief_date = datetime.fromtimestamp(self.last_morning_brief).date() if self.last_morning_brief else None
        today = datetime.now().date()
        
        return last_brief_date != today

    def _send_morning_brief(self) -> None:
        """
        Trigger a morning brief.
        This is handled by a cron job in OpenClaw, so we just log here.
        The orchestrator could optionally trigger it directly.
        """
        logger.info("Morning brief time - handled by OpenClaw cron job")
        # If we want to trigger it from here:
        # self.chat.send_system_event("Generate and deliver the morning brief.")

    def notify_work_available(self, count: int) -> None:
        """
        Optionally notify when new work is available.
        Usually silent - just logs.
        """
        logger.info(f"Work available: {count} items")
        # Could notify if configured:
        # if get_setting("notify_work_available", False):
        #     self.chat.send_message(f"📋 {count} tasks available for processing")

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
