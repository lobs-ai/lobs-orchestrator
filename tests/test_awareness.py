"""Tests for the awareness monitor."""

import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from orchestrator.core.awareness import AwarenessMonitor, WorkItem


class MockProvider:
    """Mock provider for testing."""
    
    def get_tasks(self):
        return []


def test_awareness_monitor_tracks_work(tmp_path):
    """Test that awareness monitor tracks work lifecycle."""
    # Create temporary state file
    state_file = tmp_path / "awareness-state.json"
    
    provider = MockProvider()
    monitor = AwarenessMonitor(provider)
    monitor.STATE_FILE = state_file
    
    # Track work started
    monitor.track_work_started(
        task_id="task-123",
        project_id="test-project",
        title="Test Task",
        agent_type="programmer",
        kind="task",
    )
    
    # Check active work
    snapshot = monitor.get_snapshot()
    assert len(snapshot.active_work) == 1
    assert snapshot.active_work[0].task_id == "task-123"
    assert snapshot.active_work[0].project_id == "test-project"
    assert snapshot.active_work[0].agent_type == "programmer"
    
    # Track completion
    monitor.track_work_completed("task-123", duration_seconds=120.0)
    
    # Check completed work
    snapshot = monitor.get_snapshot()
    assert len(snapshot.active_work) == 0
    assert len(snapshot.recent_completions) == 1
    assert snapshot.recent_completions[0].status == "completed"
    assert snapshot.metrics.total_tasks_completed == 1


def test_awareness_monitor_tracks_failures(tmp_path):
    """Test that awareness monitor tracks failures."""
    state_file = tmp_path / "awareness-state.json"
    
    provider = MockProvider()
    monitor = AwarenessMonitor(provider)
    monitor.STATE_FILE = state_file
    
    # Track work started
    monitor.track_work_started(
        task_id="task-456",
        project_id="test-project",
        title="Failing Task",
        agent_type="programmer",
        kind="task",
    )
    
    # Track failure
    monitor.track_work_failed("task-456", duration_seconds=60.0)
    
    # Check failed work
    snapshot = monitor.get_snapshot()
    assert len(snapshot.active_work) == 0
    assert len(snapshot.recent_failures) == 1
    assert snapshot.recent_failures[0].status == "failed"
    assert snapshot.metrics.total_tasks_failed == 1


def test_awareness_monitor_detects_patterns(tmp_path):
    """Test that awareness monitor detects patterns."""
    state_file = tmp_path / "awareness-state.json"
    
    provider = MockProvider()
    monitor = AwarenessMonitor(provider)
    monitor.STATE_FILE = state_file
    
    # Create multiple failures for same project
    for i in range(3):
        task_id = f"task-{i}"
        monitor.track_work_started(
            task_id=task_id,
            project_id="problematic-project",
            title=f"Task {i}",
            agent_type="programmer",
            kind="task",
        )
        monitor.track_work_failed(task_id)
    
    # Check patterns
    snapshot = monitor.get_snapshot()
    assert any("problematic-project" in pattern for pattern in snapshot.patterns)


def test_awareness_monitor_formats_context(tmp_path):
    """Test that awareness monitor formats context for agents."""
    state_file = tmp_path / "awareness-state.json"
    
    provider = MockProvider()
    monitor = AwarenessMonitor(provider)
    monitor.STATE_FILE = state_file
    
    # Track some work
    monitor.track_work_started(
        task_id="task-789",
        project_id="active-project",
        title="Active Work",
        agent_type="programmer",
        kind="task",
    )
    
    # Get formatted context
    context = monitor.format_context_for_agent("programmer")
    
    # Check that context contains expected sections
    assert "# System Awareness Context" in context
    assert "## Currently Active" in context
    assert "Active Work" in context
    assert "## System Metrics" in context


def test_awareness_monitor_persists_state(tmp_path):
    """Test that awareness monitor persists state to disk."""
    state_file = tmp_path / "awareness-state.json"
    
    provider = MockProvider()
    monitor = AwarenessMonitor(provider)
    monitor.STATE_FILE = state_file
    
    # Track some work and complete it
    monitor.track_work_started(
        task_id="task-persist",
        project_id="test-project",
        title="Persisted Task",
        agent_type="programmer",
    )
    monitor.track_work_completed("task-persist", duration_seconds=90.0)
    
    # Check that state file was created
    assert state_file.exists()
    
    # Load state and verify
    with open(state_file) as f:
        state = json.load(f)
    
    assert state["total_completed"] == 1
    assert len(state["completed"]) == 1
    
    # Create new monitor and verify it loads state
    monitor2 = AwarenessMonitor(provider)
    monitor2.STATE_FILE = state_file
    monitor2._load_state()
    
    assert monitor2._total_completed == 1


def test_awareness_monitor_recommendations(tmp_path):
    """Test that awareness monitor generates recommendations."""
    state_file = tmp_path / "awareness-state.json"
    
    provider = MockProvider()
    monitor = AwarenessMonitor(provider)
    monitor.STATE_FILE = state_file
    
    # System idle - should recommend proactive work
    snapshot = monitor.get_snapshot()
    
    # Check for recommendations
    assert len(snapshot.recommendations) > 0
    assert any("proactive" in rec.lower() or "idle" in rec.lower() 
               for rec in snapshot.recommendations)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
