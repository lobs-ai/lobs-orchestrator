"""orchestrator.core.agent_tracker

Per-agent status tracking. Maintains an in-memory cache of each agent type's
current status (idle/working/thinking/finalizing) and periodically syncs
dirty state to disk in lobs-control/state/agents/<type>.json.

Thinking updates are in-memory only (served via API, never committed to git).
Lifecycle events (start/finish) are written to disk and batched into git commits
at most once per 60 seconds.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

AGENT_TYPES = ("architect", "programmer", "researcher", "reviewer", "writer")

GIT_SYNC_INTERVAL = 60  # seconds between git commits for agent status


@dataclass
class AgentStats:
    tasks_completed: int = 0
    tasks_failed: int = 0
    avg_duration_seconds: int = 0
    last_week_completed: int = 0
    _durations: list[float] = field(default_factory=list, repr=False)

    def record_duration(self, seconds: float) -> None:
        self._durations.append(seconds)
        # Keep last 50 for rolling average
        if len(self._durations) > 50:
            self._durations = self._durations[-50:]
        self.avg_duration_seconds = int(sum(self._durations) / len(self._durations))

    def to_dict(self) -> dict[str, Any]:
        return {
            "tasksCompleted": self.tasks_completed,
            "tasksFailed": self.tasks_failed,
            "avgDurationSeconds": self.avg_duration_seconds,
            "lastWeekCompleted": self.last_week_completed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentStats:
        return cls(
            tasks_completed=data.get("tasksCompleted", 0),
            tasks_failed=data.get("tasksFailed", 0),
            avg_duration_seconds=data.get("avgDurationSeconds", 0),
            last_week_completed=data.get("lastWeekCompleted", 0),
        )


@dataclass
class AgentStatus:
    agent_type: str
    status: str = "idle"  # idle|working|thinking|finalizing
    activity: str | None = None
    thinking: str | None = None
    current_task_id: str | None = None
    current_project_id: str | None = None
    last_active_at: str | None = None
    last_completed_task_id: str | None = None
    last_completed_at: str | None = None
    stats: AgentStats = field(default_factory=AgentStats)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agentType": self.agent_type,
            "status": self.status,
            "activity": self.activity,
            "thinking": self.thinking,
            "currentTaskId": self.current_task_id,
            "currentProjectId": self.current_project_id,
            "lastActiveAt": self.last_active_at,
            "lastCompletedTaskId": self.last_completed_task_id,
            "lastCompletedAt": self.last_completed_at,
            "stats": self.stats.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentStatus:
        stats_data = data.get("stats", {})
        return cls(
            agent_type=data.get("agentType", "unknown"),
            status=data.get("status", "idle"),
            activity=data.get("activity"),
            thinking=data.get("thinking"),
            current_task_id=data.get("currentTaskId"),
            current_project_id=data.get("currentProjectId"),
            last_active_at=data.get("lastActiveAt"),
            last_completed_task_id=data.get("lastCompletedTaskId"),
            last_completed_at=data.get("lastCompletedAt"),
            stats=AgentStats.from_dict(stats_data) if stats_data else AgentStats(),
        )


class AgentTracker:
    """Tracks per-agent-type status and syncs to lobs-control."""

    def __init__(
        self,
        control_repo_path: Path,
        agent_types: list[str] | None = None,
    ) -> None:
        self._control_repo = control_repo_path
        self._agents_dir = control_repo_path / "state" / "agents"
        self._agents_dir.mkdir(parents=True, exist_ok=True)

        types = agent_types or list(AGENT_TYPES)
        self._statuses: dict[str, AgentStatus] = {}
        for t in types:
            self._statuses[t] = self._load_status(t)

        self._dirty: set[str] = set()
        self._last_git_sync: float = 0.0
        self._completion_counts: dict[str, int] = {t: 0 for t in types}

        logger.info(
            "[AGENT_TRACKER] Initialized with %d agent types: %s",
            len(types),
            ", ".join(types),
        )

    # ------------------------------------------------------------------
    # Disk I/O
    # ------------------------------------------------------------------

    def _status_path(self, agent_type: str) -> Path:
        return self._agents_dir / f"{agent_type}.json"

    def _load_status(self, agent_type: str) -> AgentStatus:
        path = self._status_path(agent_type)
        if not path.exists():
            return AgentStatus(agent_type=agent_type)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return AgentStatus.from_dict(data)
        except Exception as e:
            logger.warning("[AGENT_TRACKER] Failed to load %s: %s", agent_type, e)
            return AgentStatus(agent_type=agent_type)

    def _write_status(self, agent_type: str) -> None:
        path = self._status_path(agent_type)
        try:
            path.write_text(
                json.dumps(self._statuses[agent_type].to_dict(), indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception as e:
            logger.error("[AGENT_TRACKER] Failed to write %s: %s", agent_type, e)

    # ------------------------------------------------------------------
    # Lifecycle events
    # ------------------------------------------------------------------

    def mark_working(
        self,
        agent_type: str,
        task_id: str,
        project_id: str,
        activity: str,
    ) -> None:
        """Mark an agent as actively working on a task."""
        status = self._statuses.get(agent_type)
        if not status:
            return
        status.status = "working"
        status.activity = activity
        status.thinking = None
        status.current_task_id = task_id
        status.current_project_id = project_id
        status.last_active_at = datetime.now(timezone.utc).isoformat()
        self._dirty.add(agent_type)
        logger.info("[AGENT_TRACKER] %s -> working: %s", agent_type, activity[:80])

    def update_thinking(self, agent_type: str, snippet: str) -> None:
        """Update the thinking snippet for an active agent.

        Written to disk so the dashboard can read it from the control repo.
        """
        status = self._statuses.get(agent_type)
        if not status or status.status == "idle":
            return
        old = status.thinking
        status.thinking = snippet[:500] if snippet else None
        if status.thinking != old:
            self._dirty.add(agent_type)

    def mark_completed(
        self,
        agent_type: str,
        task_id: str,
        duration_seconds: float,
    ) -> None:
        """Mark a task as successfully completed."""
        status = self._statuses.get(agent_type)
        if not status:
            return
        now = datetime.now(timezone.utc).isoformat()
        status.last_completed_task_id = task_id
        status.last_completed_at = now
        status.last_active_at = now
        status.stats.tasks_completed += 1
        status.stats.record_duration(duration_seconds)
        self._dirty.add(agent_type)
        self._completion_counts[agent_type] = (
            self._completion_counts.get(agent_type, 0) + 1
        )
        logger.info(
            "[AGENT_TRACKER] %s completed task %s (%.0fs)",
            agent_type,
            task_id[:8],
            duration_seconds,
        )

    def mark_failed(self, agent_type: str, task_id: str) -> None:
        """Mark a task as failed."""
        status = self._statuses.get(agent_type)
        if not status:
            return
        status.last_active_at = datetime.now(timezone.utc).isoformat()
        status.stats.tasks_failed += 1
        self._dirty.add(agent_type)
        logger.info("[AGENT_TRACKER] %s failed task %s", agent_type, task_id[:8])

    def mark_idle(self, agent_type: str) -> None:
        """Mark an agent as idle (no active work)."""
        status = self._statuses.get(agent_type)
        if not status:
            return
        status.status = "idle"
        status.activity = None
        status.thinking = None
        status.current_task_id = None
        status.current_project_id = None
        self._dirty.add(agent_type)
        logger.debug("[AGENT_TRACKER] %s -> idle", agent_type)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_all_statuses(self) -> dict[str, AgentStatus]:
        """Return all agent statuses (for API)."""
        return dict(self._statuses)

    def get_status(self, agent_type: str) -> AgentStatus | None:
        return self._statuses.get(agent_type)

    def get_completion_count(self, agent_type: str) -> int:
        """Get the number of completions since orchestrator start (for trait evolution)."""
        return self._completion_counts.get(agent_type, 0)

    # ------------------------------------------------------------------
    # Disk + git sync
    # ------------------------------------------------------------------

    def sync_to_disk(self, force: bool = False) -> None:
        """Write dirty agent status files and optionally git commit.

        Git commits are batched: at most once per GIT_SYNC_INTERVAL seconds.
        """
        if not self._dirty and not force:
            return

        # Write changed files
        for agent_type in list(self._dirty):
            self._write_status(agent_type)
        self._dirty.clear()

        # Rate-limit git commits
        now = time.time()
        if not force and (now - self._last_git_sync) < GIT_SYNC_INTERVAL:
            return

        self._git_commit_status()
        self._last_git_sync = now

    def _git_commit_status(self) -> None:
        """Stage and commit agent status files in lobs-control."""
        try:
            subprocess.run(
                ["git", "add", "state/agents/"],
                cwd=self._control_repo,
                capture_output=True,
                timeout=10,
            )
            result = subprocess.run(
                ["git", "diff", "--cached", "--quiet"],
                cwd=self._control_repo,
                capture_output=True,
                timeout=10,
            )
            if result.returncode == 0:
                return  # Nothing staged

            subprocess.run(
                ["git", "commit", "-m", "agents: status update"],
                cwd=self._control_repo,
                capture_output=True,
                timeout=30,
            )
            logger.debug("[AGENT_TRACKER] Committed agent status update")
        except Exception as e:
            logger.warning("[AGENT_TRACKER] Git commit failed: %s", e)

    # ------------------------------------------------------------------
    # Log tailing for thinking extraction
    # ------------------------------------------------------------------

    @staticmethod
    def extract_thinking_from_log(log_path: Path) -> str | None:
        """Extract a thinking snippet from the tail of a worker log file."""
        if not log_path or not log_path.exists():
            return None
        try:
            content = log_path.read_text(encoding="utf-8", errors="replace")
            lines = content.strip().splitlines()
            if not lines:
                return None

            # Take last 20 lines, filter out empty/whitespace
            tail = [
                line.strip()
                for line in lines[-20:]
                if line.strip() and not line.startswith("---")
            ]
            if not tail:
                return None

            # Return last meaningful line (skip common noise)
            noise_prefixes = ("$", "```", "===", ">>>", "...", "exit")
            for line in reversed(tail):
                if not any(line.startswith(p) for p in noise_prefixes):
                    return line[:500]

            return tail[-1][:500] if tail else None
        except Exception:
            return None
