from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class TaskStatus(Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class WorkState(Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Task:
    id: str
    projectId: str
    title: str
    notes: str
    status: str
    workState: Optional[str] = None
    reviewState: Optional[str] = None
    createdAt: Optional[str] = None
    updatedAt: Optional[str] = None
    failureReason: Optional[str] = None
    failedAt: Optional[str] = None
    failureCount: Optional[int] = None
    retryCount: Optional[int] = None
    lastRetryReason: Optional[str] = None
    retryHistory: Optional[list] = None


@dataclass
class WorkerStatus:
    active: bool
    workerId: str
    startedAt: str
    lastHeartbeat: str
    currentTask: Optional[str] = None
    tasksCompleted: int = 0


@dataclass
class DomainLock:
    projectId: str
    workerId: str
    acquiredAt: datetime
