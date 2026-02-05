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
    def sync(self) -> None:
        """Sync with the remote backend (e.g. git pull/push)."""
        pass

    @abstractmethod
    def get_engineering_rules(self) -> str:
        """Fetch global engineering rules."""
        pass
