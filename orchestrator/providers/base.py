from abc import ABC, abstractmethod
from typing import Any


class TaskProvider(ABC):
    """
    Abstract interface for task management platforms.
    Decouples the Orchestrator from specific backends (Git, API, DB).
    """

    @abstractmethod
    def get_tasks(self) -> list[dict[str, Any]]:
        """Fetch all tasks ready for processing."""
        pass

    @abstractmethod
    def get_projects(self) -> list[dict[str, Any]]:
        """Fetch all registered projects."""
        pass

    @abstractmethod
    def update_task(self, task_id: str, updates: dict[str, Any]) -> None:
        """Update a specific task's state."""
        pass

    @abstractmethod
    def update_worker_status(self, updates: dict[str, Any]) -> None:
        """Update the global worker status."""
        pass

    @abstractmethod
    def check_pending_request(self) -> bool:
        """Check if there is an explicit worker request."""
        pass

    @abstractmethod
    def consume_request(self) -> None:
        """Consume/acknowledge the pending worker request."""
        pass

    @abstractmethod
    def sync(self) -> list[dict[str, Any]]:
        """Sync with the remote backend and return any processed messages."""
        pass

    @abstractmethod
    def get_engineering_rules(self) -> str:
        """Fetch global engineering rules."""
        pass

    @abstractmethod
    def update_project(self, project_id: str, updates: dict[str, Any]) -> None:
        pass

    @abstractmethod
    def add_inbox_item(self, item: dict[str, Any]) -> None:
        """Add an item to the user's inbox (suggestions, alerts)."""
        pass

    @abstractmethod
    def get_inbox_items(self) -> list[dict[str, Any]]:
        """Fetch items from the inbox."""
        pass

    @abstractmethod
    def update_inbox_item(self, item_id: str, updates: dict[str, Any]) -> None:
        """Update an inbox item's state."""
        pass

    @abstractmethod
    def get_active_alerts(self) -> list[dict[str, Any]]:
        """Fetch active error/failure alerts."""
        pass

    @abstractmethod
    def update_alert(self, alert_id: str, updates: dict[str, Any]) -> None:
        """Update an alert's state or escalation level."""
        pass
