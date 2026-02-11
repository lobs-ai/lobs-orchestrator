"""
Chat integration service for communicating with the main OpenClaw session.

This module provides methods to:
- Send heartbeats to the main session
- Deliver messages to Discord/channels
- Trigger system events
"""

import logging
import subprocess
import json
from typing import Optional, Any
from orchestrator.utils.settings import get_setting

logger = logging.getLogger(__name__)


class ChatService:
    """
    Handles communication with the OpenClaw main session and channels.
    """

    def __init__(self):
        self.executable = get_setting("openclaw_executable", "openclaw")

    def send_system_event(self, text: str, timeout: int = 30) -> bool:
        """
        Send a system event to the main session.
        This injects a message that the main agent will process.
        """
        try:
            cmd = [
                self.executable, "system", "event",
                "--text", text,
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout
            )
            if result.returncode == 0:
                logger.info(f"System event sent: {text[:50]}...")
                return True
            else:
                logger.error(f"Failed to send system event: {result.stderr}")
                return False
        except subprocess.TimeoutExpired:
            logger.error("System event timed out")
            return False
        except Exception as e:
            logger.error(f"Error sending system event: {e}")
            return False

    def send_message(
        self,
        message: str,
        channel: str = "discord",
        target: Optional[str] = None,
        timeout: int = 30
    ) -> bool:
        """
        Send a message to a channel (Discord, etc.).
        """
        try:
            cmd = [
                self.executable, "message",
                "--channel", channel,
                "--action", "send",
                "--message", message,
            ]
            if target:
                cmd.extend(["--target", target])
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout
            )
            if result.returncode == 0:
                logger.info(f"Message sent to {channel}: {message[:50]}...")
                return True
            else:
                logger.error(f"Failed to send message: {result.stderr}")
                return False
        except subprocess.TimeoutExpired:
            logger.error("Message send timed out")
            return False
        except Exception as e:
            logger.error(f"Error sending message: {e}")
            return False

    def trigger_heartbeat(self, timeout: int = 120) -> bool:
        """
        Trigger a heartbeat in the main session.
        This causes the main agent to read HEARTBEAT.md and act.
        """
        heartbeat_text = (
            "Read HEARTBEAT.md if it exists (workspace context). "
            "Also review your MEMORY.md for patterns and lessons from previous work. "
            "Follow instructions strictly. Do not infer or repeat old tasks from prior chats. "
            "If nothing needs attention, reply HEARTBEAT_OK."
        )
        return self.send_system_event(heartbeat_text, timeout=timeout)

    def notify_task_complete(
        self,
        task_id: str,
        task_title: str,
        project_id: str,
        success: bool = True,
        notes: Optional[str] = None
    ) -> bool:
        """
        Notify about task completion (success or failure).
        Only sends if notification settings allow.
        """
        # Check notification settings
        notify_on_complete = get_setting("notify_on_complete", False)
        notify_on_failure = get_setting("notify_on_failure", True)
        
        if success and not notify_on_complete:
            return True
        if not success and not notify_on_failure:
            return True
        
        status = "✅ completed" if success else "❌ failed"
        message = f"Task {status}: **{task_title}** ({project_id})"
        if notes:
            message += f"\n{notes}"
        
        # Get notification target
        notify_target = get_setting("notify_target", "user:644578016298795010")
        notify_channel = get_setting("notify_channel", "discord")
        
        return self.send_message(message, channel=notify_channel, target=notify_target)

    def send_alert(
        self,
        title: str,
        body: str,
        severity: str = "medium"
    ) -> bool:
        """
        Send an alert to the owner.
        """
        emoji = {"low": "ℹ️", "medium": "⚠️", "high": "🚨"}.get(severity, "⚠️")
        message = f"{emoji} **{title}**\n{body}"
        
        notify_target = get_setting("notify_target", "user:644578016298795010")
        notify_channel = get_setting("notify_channel", "discord")
        
        return self.send_message(message, channel=notify_channel, target=notify_target)


# Singleton instance
_chat_service: Optional[ChatService] = None


def get_chat_service() -> ChatService:
    global _chat_service
    if _chat_service is None:
        _chat_service = ChatService()
    return _chat_service
