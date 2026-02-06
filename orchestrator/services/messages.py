import logging
import json
from typing import Any
from orchestrator.providers.base import TaskProvider

logger = logging.getLogger(__name__)

class MessageProcessor:
    """
    Handles structured messages from LLMs to the system.
    Messages are usually control-ops with 'type': 'message'.
    """

    def __init__(self, provider: TaskProvider):
        self.provider = provider

    def process_messages(self, messages: list[dict[str, Any]]):
        for msg in messages:
            try:
                self._handle_message(msg)
            except Exception as e:
                logger.error(f"Failed to process message: {e}")

    def _handle_message(self, msg: dict[str, Any]):
        msg_type = msg.get("action")
        payload = msg.get("payload", {})
        
        logger.info(f"Processing LLM message action: {msg_type}")

        if msg_type == "register_project":
            self._register_project(payload)
        elif msg_type == "update_instructions":
            self._update_instructions(payload)
        elif msg_type == "broadcast":
            self._broadcast_message(payload)
        elif msg_type == "system_request":
            self._system_request(payload)
        else:
            logger.warning(f"Unknown message action: {msg_type}")

    def _system_request(self, payload: Any):
        """Create a request for the system monitor to handle."""
        self.provider.add_inbox_item({
            "title": payload.get("title", "System Request"),
            "body": payload.get("content"),
            "type": "request",
            "responder": "system",
            "actionRequired": True,
            "sender": payload.get("sender", "worker-llm")
        })

    def _register_project(self, payload: Any):
        project_id = payload.get("id")
        if not project_id:
            return
        
        # Read current projects
        projects = self.provider.get_projects()
        if any(p["id"] == project_id for p in projects):
            logger.info(f"Project {project_id} already registered.")
            return
            
        new_project = {
            "id": project_id,
            "name": payload.get("name", project_id),
            "path": payload.get("path"),
            "description": payload.get("description", "")
        }
        
        # We use a control-op to actually save it via the provider
        self.provider.update_project(project_id, new_project)
        logger.info(f"LLM requested registration of project: {project_id}")

    def _update_instructions(self, payload: Any):
        project_id = payload.get("projectId")
        instructions = payload.get("instructions")
        if not project_id or not instructions:
            return
            
        # This could update a project-specific instructions file or a task
        logger.info(f"LLM updating instructions for {project_id}")
        # Implementation depends on how instructions are stored (e.g. state/instructions/...)
        self.provider.add_inbox_item({
            "title": f"Instruction Update: {project_id}",
            "body": instructions,
            "type": "info",
            "projectId": project_id
        })

    def _broadcast_message(self, payload: Any):
        # Add to global inbox or specific project inboxes
        self.provider.add_inbox_item({
            "title": payload.get("title", "LLM Broadcast"),
            "body": payload.get("content"),
            "type": "broadcast",
            "sender": payload.get("sender", "worker-llm")
        })
