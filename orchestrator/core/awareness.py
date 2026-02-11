"""orchestrator.core.awareness

Awareness Monitor - provides agents with situational awareness of system state.

This component tracks:
- Active work (what other agents are doing)
- Recent completions and failures
- System health metrics
- Workload patterns and bottlenecks

Agents receive this context in their prompts to make informed decisions about
what work to do next and how to prioritize their tasks.

Reference: docs/ARCHITECTURE.md (awareness + proactive context).
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Deque

from orchestrator.config import STATE_DIR
from orchestrator.providers.base import TaskProvider

logger = logging.getLogger(__name__)


@dataclass
class WorkItem:
    """A work item in the system (task, research request, etc.)."""
    
    task_id: str
    project_id: str
    title: str
    agent_type: str
    kind: str
    started_at: str
    status: str  # running|completed|failed


@dataclass
class SystemMetrics:
    """System health and performance metrics."""
    
    uptime_seconds: int
    total_tasks_completed: int
    total_tasks_failed: int
    tasks_last_hour: int
    failures_last_hour: int
    avg_task_duration_seconds: float
    proactive_tasks_today: int
    proactive_tasks_total: int
    queue_depth: int


@dataclass
class AwarenessSnapshot:
    """A point-in-time snapshot of system awareness."""
    
    timestamp: str
    active_work: list[WorkItem]
    recent_completions: list[WorkItem]
    recent_failures: list[WorkItem]
    metrics: SystemMetrics
    patterns: list[str]  # Detected patterns (e.g., "High failure rate in project X")
    recommendations: list[str]  # System-level recommendations


class AwarenessMonitor:
    """
    Maintains situational awareness of system state.
    
    Tracks active work, recent history, and system health to provide
    agents with context about what's happening across the system.
    """
    
    STATE_FILE = STATE_DIR / "awareness-state.json"
    HISTORY_LIMIT = 100  # Keep last N completed/failed tasks in memory
    RECENT_WINDOW_HOURS = 1  # Window for "recent" activity
    
    def __init__(self, provider: TaskProvider):
        self.provider = provider
        
        # In-memory tracking
        self._active_work: dict[str, WorkItem] = {}
        self._completed: Deque[WorkItem] = deque(maxlen=self.HISTORY_LIMIT)
        self._failed: Deque[WorkItem] = deque(maxlen=self.HISTORY_LIMIT)
        
        # Metrics tracking
        self._total_completed = 0
        self._total_failed = 0
        self._task_durations: Deque[float] = deque(maxlen=50)  # Last 50 task durations
        self._start_time = time.time()
        
        # Load persisted state
        self._load_state()
    
    # =========================================================================
    # State Management
    # =========================================================================
    
    def _load_state(self) -> None:
        """Load persisted awareness state from disk."""
        if not self.STATE_FILE.exists():
            return
        
        try:
            with open(self.STATE_FILE, "r") as f:
                data = json.load(f)
            
            # Restore counters
            self._total_completed = data.get("total_completed", 0)
            self._total_failed = data.get("total_failed", 0)
            self._start_time = data.get("start_time", time.time())
            
            # Restore recent history (most recent items only)
            completed_data = data.get("completed", [])[-self.HISTORY_LIMIT:]
            for item_data in completed_data:
                self._completed.append(WorkItem(**item_data))
            
            failed_data = data.get("failed", [])[-self.HISTORY_LIMIT:]
            for item_data in failed_data:
                self._failed.append(WorkItem(**item_data))
            
            logger.info(
                f"[AWARENESS] Loaded state: {self._total_completed} completed, "
                f"{self._total_failed} failed"
            )
        except Exception as e:
            logger.warning(f"[AWARENESS] Failed to load state: {e}")
    
    def _save_state(self) -> None:
        """Persist awareness state to disk."""
        try:
            data = {
                "total_completed": self._total_completed,
                "total_failed": self._total_failed,
                "start_time": self._start_time,
                "completed": [asdict(item) for item in list(self._completed)],
                "failed": [asdict(item) for item in list(self._failed)],
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }
            
            self.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(self.STATE_FILE, "w") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
        except Exception as e:
            logger.error(f"[AWARENESS] Failed to save state: {e}")
    
    # =========================================================================
    # Event Tracking
    # =========================================================================
    
    def track_work_started(
        self,
        task_id: str,
        project_id: str,
        title: str,
        agent_type: str,
        kind: str = "task",
    ) -> None:
        """Record that work has started on a task."""
        work_item = WorkItem(
            task_id=task_id,
            project_id=project_id,
            title=title,
            agent_type=agent_type,
            kind=kind,
            started_at=datetime.now(timezone.utc).isoformat(),
            status="running",
        )
        self._active_work[task_id] = work_item
        logger.debug(f"[AWARENESS] Work started: {task_id[:8]} ({agent_type})")
    
    def track_work_completed(
        self,
        task_id: str,
        duration_seconds: float | None = None,
    ) -> None:
        """Record that work has completed successfully."""
        work_item = self._active_work.pop(task_id, None)
        if not work_item:
            logger.warning(f"[AWARENESS] Completed unknown task: {task_id[:8]}")
            return
        
        work_item.status = "completed"
        self._completed.append(work_item)
        self._total_completed += 1
        
        if duration_seconds:
            self._task_durations.append(duration_seconds)
        
        self._save_state()
        logger.debug(f"[AWARENESS] Work completed: {task_id[:8]}")
    
    def track_work_failed(
        self,
        task_id: str,
        duration_seconds: float | None = None,
    ) -> None:
        """Record that work has failed."""
        work_item = self._active_work.pop(task_id, None)
        if not work_item:
            logger.warning(f"[AWARENESS] Failed unknown task: {task_id[:8]}")
            return
        
        work_item.status = "failed"
        self._failed.append(work_item)
        self._total_failed += 1
        
        if duration_seconds:
            self._task_durations.append(duration_seconds)
        
        self._save_state()
        logger.debug(f"[AWARENESS] Work failed: {task_id[:8]}")
    
    # =========================================================================
    # Metrics & Analysis
    # =========================================================================
    
    def _get_metrics(self, proactive_stats: dict[str, Any] | None = None) -> SystemMetrics:
        """Calculate current system metrics."""
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=self.RECENT_WINDOW_HOURS)
        
        # Count recent activity
        recent_completed = sum(
            1 for item in self._completed
            if datetime.fromisoformat(item.started_at) >= cutoff
        )
        recent_failed = sum(
            1 for item in self._failed
            if datetime.fromisoformat(item.started_at) >= cutoff
        )
        
        # Calculate average task duration
        avg_duration = sum(self._task_durations) / len(self._task_durations) if self._task_durations else 0.0
        
        # Get queue depth from provider
        try:
            eligible_work = self.provider.get_tasks()
            queue_depth = len(eligible_work)
        except Exception:
            queue_depth = 0
        
        # Get proactive stats
        proactive_today = 0
        proactive_total = 0
        if proactive_stats:
            today = datetime.now(timezone.utc).date().isoformat()
            proactive_today = proactive_stats.get("daily_counts", {}).get(today, 0)
            proactive_total = proactive_stats.get("total_created", 0)
        
        return SystemMetrics(
            uptime_seconds=int(time.time() - self._start_time),
            total_tasks_completed=self._total_completed,
            total_tasks_failed=self._total_failed,
            tasks_last_hour=recent_completed,
            failures_last_hour=recent_failed,
            avg_task_duration_seconds=avg_duration,
            proactive_tasks_today=proactive_today,
            proactive_tasks_total=proactive_total,
            queue_depth=queue_depth,
        )
    
    def _detect_patterns(self, metrics: SystemMetrics) -> list[str]:
        """Detect notable patterns in system behavior."""
        patterns: list[str] = []
        
        # High failure rate
        if metrics.failures_last_hour > 0 and metrics.tasks_last_hour > 0:
            failure_rate = metrics.failures_last_hour / max(1, metrics.tasks_last_hour + metrics.failures_last_hour)
            if failure_rate > 0.3:  # >30% failure rate
                patterns.append(
                    f"⚠️ High failure rate in last hour ({int(failure_rate * 100)}%)"
                )
        
        # Queue backlog building
        if metrics.queue_depth > 10:
            patterns.append(
                f"📊 Work queue growing ({metrics.queue_depth} tasks pending)"
            )
        
        # Project concentration (are failures concentrated in one project?)
        if len(self._failed) >= 3:
            recent_failed_projects = [item.project_id for item in list(self._failed)[-10:]]
            from collections import Counter
            project_counts = Counter(recent_failed_projects)
            for project, count in project_counts.most_common(1):
                if count >= 3:
                    patterns.append(
                        f"🔍 Multiple recent failures in project: {project}"
                    )
        
        # Agent type patterns
        if len(self._failed) >= 3:
            recent_failed_agents = [item.agent_type for item in list(self._failed)[-10:]]
            from collections import Counter
            agent_counts = Counter(recent_failed_agents)
            for agent, count in agent_counts.most_common(1):
                if count >= 3:
                    patterns.append(
                        f"🤖 Multiple failures with agent type: {agent}"
                    )
        
        # System idle (low activity)
        if metrics.tasks_last_hour == 0 and metrics.queue_depth == 0:
            patterns.append(
                "💤 System idle - consider proactive work"
            )
        
        # High proactive productivity
        if metrics.proactive_tasks_today >= 3:
            patterns.append(
                f"✨ Proactive work active ({metrics.proactive_tasks_today} tasks today)"
            )
        
        return patterns
    
    def _generate_recommendations(
        self,
        metrics: SystemMetrics,
        patterns: list[str],
    ) -> list[str]:
        """Generate recommendations based on current state."""
        recommendations: list[str] = []
        
        # Based on failure rate
        if metrics.failures_last_hour >= 2:
            recommendations.append(
                "Consider reviewing recent failures for common root causes"
            )
        
        # Based on queue depth
        if metrics.queue_depth > 15:
            recommendations.append(
                "Large work queue - prioritize high-impact tasks"
            )
        elif metrics.queue_depth == 0 and not self._active_work:
            recommendations.append(
                "No pending work - good time for proactive improvements or technical debt"
            )
        
        # Based on agent activity
        active_agents = {item.agent_type for item in self._active_work.values()}
        if len(active_agents) == 0 and metrics.queue_depth > 0:
            recommendations.append(
                "Work available but no agents active - system may need attention"
            )
        
        return recommendations
    
    # =========================================================================
    # Public API
    # =========================================================================
    
    def get_snapshot(self, proactive_stats: dict[str, Any] | None = None) -> AwarenessSnapshot:
        """Get a current snapshot of system awareness."""
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=self.RECENT_WINDOW_HOURS)
        
        # Get recent completions and failures
        recent_completed = [
            item for item in self._completed
            if datetime.fromisoformat(item.started_at) >= cutoff
        ]
        recent_failed = [
            item for item in self._failed
            if datetime.fromisoformat(item.started_at) >= cutoff
        ]
        
        # Calculate metrics
        metrics = self._get_metrics(proactive_stats)
        
        # Detect patterns
        patterns = self._detect_patterns(metrics)
        
        # Generate recommendations
        recommendations = self._generate_recommendations(metrics, patterns)
        
        return AwarenessSnapshot(
            timestamp=now.isoformat(),
            active_work=list(self._active_work.values()),
            recent_completions=recent_completed,
            recent_failures=recent_failed,
            metrics=metrics,
            patterns=patterns,
            recommendations=recommendations,
        )
    
    def format_context_for_agent(
        self,
        agent_type: str,
        proactive_stats: dict[str, Any] | None = None,
        include_detailed: bool = False,
    ) -> str:
        """
        Format situational awareness context for inclusion in agent prompts.
        
        Args:
            agent_type: The type of agent requesting context (for filtering relevance)
            proactive_stats: Optional proactive work statistics
            include_detailed: If True, include full task lists (default: summarized)
        
        Returns:
            Markdown-formatted context string
        """
        snapshot = self.get_snapshot(proactive_stats)
        
        lines: list[str] = [
            "# System Awareness Context",
            "",
            f"*As of {datetime.fromisoformat(snapshot.timestamp).strftime('%Y-%m-%d %H:%M UTC')}*",
            "",
        ]
        
        # Active Work Section
        if snapshot.active_work:
            lines.append("## Currently Active")
            lines.append("")
            for item in snapshot.active_work:
                age_mins = (
                    datetime.now(timezone.utc) - datetime.fromisoformat(item.started_at)
                ).total_seconds() / 60
                lines.append(
                    f"- **[{item.project_id}]** {item.title} "
                    f"({item.agent_type}, {int(age_mins)}m ago)"
                )
            lines.append("")
        else:
            lines.append("## Currently Active")
            lines.append("")
            lines.append("*No active work - system is idle*")
            lines.append("")
        
        # Recent Completions
        if snapshot.recent_completions:
            lines.append(f"## Recent Completions (last {self.RECENT_WINDOW_HOURS}h)")
            lines.append("")
            if include_detailed:
                for item in snapshot.recent_completions[-5:]:  # Last 5
                    lines.append(
                        f"- [{item.project_id}] {item.title} ({item.agent_type})"
                    )
            else:
                count_by_project = {}
                for item in snapshot.recent_completions:
                    count_by_project[item.project_id] = count_by_project.get(item.project_id, 0) + 1
                for project, count in sorted(count_by_project.items()):
                    lines.append(f"- {project}: {count} task(s)")
            lines.append("")
        
        # Recent Failures (always show if present)
        if snapshot.recent_failures:
            lines.append(f"## Recent Failures (last {self.RECENT_WINDOW_HOURS}h)")
            lines.append("")
            for item in snapshot.recent_failures[-3:]:  # Last 3 failures
                age_mins = (
                    datetime.now(timezone.utc) - datetime.fromisoformat(item.started_at)
                ).total_seconds() / 60
                lines.append(
                    f"- ⚠️ [{item.project_id}] {item.title} "
                    f"({item.agent_type}, {int(age_mins)}m ago)"
                )
            lines.append("")
        
        # System Metrics
        m = snapshot.metrics
        lines.append("## System Metrics")
        lines.append("")
        lines.append(f"- **Queue Depth:** {m.queue_depth} task(s)")
        lines.append(f"- **Uptime:** {m.uptime_seconds // 3600}h {(m.uptime_seconds % 3600) // 60}m")
        lines.append(f"- **Completed:** {m.total_tasks_completed} total, {m.tasks_last_hour} last hour")
        lines.append(f"- **Failed:** {m.total_tasks_failed} total, {m.failures_last_hour} last hour")
        if m.avg_task_duration_seconds > 0:
            lines.append(f"- **Avg Duration:** {int(m.avg_task_duration_seconds / 60)}m")
        if m.proactive_tasks_today > 0:
            lines.append(
                f"- **Proactive Work:** {m.proactive_tasks_today} today, "
                f"{m.proactive_tasks_total} total"
            )
        lines.append("")
        
        # Patterns (if any detected)
        if snapshot.patterns:
            lines.append("## Observed Patterns")
            lines.append("")
            for pattern in snapshot.patterns:
                lines.append(f"- {pattern}")
            lines.append("")
        
        # Recommendations (filtered by agent type)
        if snapshot.recommendations:
            relevant_recommendations = self._filter_recommendations_for_agent(
                snapshot.recommendations,
                agent_type,
            )
            if relevant_recommendations:
                lines.append("## Recommendations")
                lines.append("")
                for rec in relevant_recommendations:
                    lines.append(f"- {rec}")
                lines.append("")
        
        return "\n".join(lines)
    
    def _filter_recommendations_for_agent(
        self,
        recommendations: list[str],
        agent_type: str,
    ) -> list[str]:
        """Filter recommendations to those relevant for the agent type."""
        # For now, show all recommendations to all agents
        # Could be refined to filter by agent capabilities
        return recommendations
    
    def get_status(self) -> dict[str, Any]:
        """Get raw status dict for API/debugging."""
        metrics = self._get_metrics()
        return {
            "active_work_count": len(self._active_work),
            "completed_count": len(self._completed),
            "failed_count": len(self._failed),
            "total_completed": metrics.total_tasks_completed,
            "total_failed": metrics.total_tasks_failed,
            "uptime_seconds": metrics.uptime_seconds,
        }
