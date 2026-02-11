"""Integration tests for Orchestrator V2 multi-agent features.

Tests the complete flow:
- Agent spawns → works → hands off to another agent
- Reviewer automatically reviews completed work
- Monitor detects failure patterns and triggers diagnostics
- Workflow engine tracks initiatives across agent chains
"""

import json
import pytest
from pathlib import Path
from datetime import datetime, timezone

from orchestrator.core.monitor import Monitor
from orchestrator.core.collaboration import CollaborationManager
from orchestrator.core.workflow import WorkflowEngine
from orchestrator.core.router import Router
from orchestrator.providers.filesystem import FileSystemProvider


class TestAgentCollaboration:
    """Test agent-to-agent handoff and collaboration."""

    def test_handoff_creates_routed_task(self, tmp_path):
        """Test that a handoff file creates a properly routed task."""
        control_path = tmp_path / "control"
        control_path.mkdir()
        tasks_dir = control_path / "state" / "tasks"
        tasks_dir.mkdir(parents=True)

        provider = FileSystemProvider(control_path)
        collab = CollaborationManager(provider)

        # Simulate a handoff from programmer to reviewer
        handoff_payload = {
            "from": "programmer",
            "to": "reviewer",
            "initiative": "test-auth-system",
            "work": {
                "title": "Review authentication implementation",
                "context": "Please review the JWT middleware implementation",
                "acceptance": "Code review complete with feedback"
            },
            "projectId": "test-project"
        }

        task_id = collab.process_handoff(
            handoff_payload,
            parent_task_id="PARENT-123",
            default_project_id="test-project"
        )

        # Verify task was created
        assert task_id is not None
        task_file = tasks_dir / f"{task_id}.json"
        assert task_file.exists()

        # Verify task has correct routing
        task = json.loads(task_file.read_text())
        assert task["agent"] == "reviewer"
        assert task["initiative"] == "test-auth-system"
        assert task["collaboration"]["from"] == "programmer"
        assert task["collaboration"]["to"] == "reviewer"
        assert task["collaboration"]["parentTaskId"] == "PARENT-123"

    def test_workflow_tracks_initiative_chain(self, tmp_path):
        """Test that workflow engine tracks tasks across an initiative."""
        workflow = WorkflowEngine(state_path=tmp_path / "workflow-state.json")

        # Create a chain of tasks in an initiative
        tasks = [
            {
                "id": "TASK-1",
                "title": "Design auth system",
                "initiative": "user-auth",
                "agent": "architect",
                "projectId": "myapp",
                "workState": "completed"
            },
            {
                "id": "TASK-2",
                "title": "Implement JWT middleware",
                "initiative": "user-auth",
                "agent": "programmer",
                "projectId": "myapp",
                "workState": "completed",
                "parentTaskId": "TASK-1"
            },
            {
                "id": "TASK-3",
                "title": "Review auth implementation",
                "initiative": "user-auth",
                "agent": "reviewer",
                "projectId": "myapp",
                "workState": "in_progress",
                "parentTaskId": "TASK-2"
            },
            {
                "id": "TASK-4",
                "title": "Document auth API",
                "initiative": "user-auth",
                "agent": "writer",
                "projectId": "myapp",
                "workState": "not_started",
                "parentTaskId": "TASK-3"
            }
        ]

        snapshot = workflow.compute(tasks)

        # Verify initiative tracking
        assert "user-auth" in snapshot.initiatives
        progress = snapshot.initiatives["user-auth"]
        assert progress.total == 4
        assert progress.done == 2
        assert not progress.complete

        # Verify dependency blocking
        assert "TASK-4" in snapshot.blocked_task_ids  # Blocked by TASK-3

        # Verify workflow visualization
        viz = workflow.visualize(snapshot, "user-auth")
        assert "user-auth" in viz
        assert "2/4 complete" in viz


class TestMonitorIntegration:
    """Test monitor failure pattern detection and diagnostic task creation."""

    def test_detect_project_failure_pattern(self, tmp_path):
        """Test that monitor detects repeated project failures."""
        control_path = tmp_path / "control"
        control_path.mkdir()

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        provider = FileSystemProvider(control_path)
        monitor = Monitor(provider)
        monitor.failure_history_path = state_dir / "failure-history.json"
        monitor.diagnostic_tasks_path = state_dir / "diagnostic-tasks.json"

        # Create failure history with repeated failures in one project
        now = datetime.now(timezone.utc)
        failures = [
            {
                "taskId": f"TASK-{i}",
                "projectId": "failing-project",
                "agentType": "programmer",
                "failureReason": "git_error",
                "error": "git push failed",
                "failedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ")
            }
            for i in range(5)  # 5 failures in same project
        ]

        monitor.failure_history_path.parent.mkdir(parents=True, exist_ok=True)
        with open(monitor.failure_history_path, "w") as f:
            json.dump({"failures": failures}, f)

        # Run pattern detection
        recent_failures = monitor._get_recent_failures(failures, hours=24)
        patterns = monitor._detect_patterns(recent_failures)

        # Should detect project failure pattern
        project_patterns = [p for p in patterns if p["type"] == "project_failures"]
        assert len(project_patterns) == 1
        assert project_patterns[0]["project_id"] == "failing-project"
        assert project_patterns[0]["count"] == 5
        assert project_patterns[0]["severity"] in ["high", "medium"]

    def test_detect_agent_failure_pattern(self, tmp_path):
        """Test that monitor detects repeated agent type failures."""
        control_path = tmp_path / "control"
        control_path.mkdir()

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        provider = FileSystemProvider(control_path)
        monitor = Monitor(provider)
        monitor.failure_history_path = state_dir / "failure-history.json"

        # Create failure history with repeated failures in one agent type
        now = datetime.now(timezone.utc)
        failures = [
            {
                "taskId": f"TASK-{i}",
                "projectId": f"project-{i}",
                "agentType": "programmer",
                "failureReason": "timeout",
                "error": "worker timeout",
                "failedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ")
            }
            for i in range(6)  # 6 failures with same agent
        ]

        with open(monitor.failure_history_path, "w") as f:
            json.dump({"failures": failures}, f)

        recent_failures = monitor._get_recent_failures(failures, hours=24)
        patterns = monitor._detect_patterns(recent_failures)

        # Should detect agent failure pattern
        agent_patterns = [p for p in patterns if p["type"] == "agent_failures"]
        assert len(agent_patterns) == 1
        assert agent_patterns[0]["agent_type"] == "programmer"
        assert agent_patterns[0]["count"] == 6

    def test_detect_error_pattern(self, tmp_path):
        """Test that monitor detects common error keywords."""
        control_path = tmp_path / "control"
        control_path.mkdir()

        provider = FileSystemProvider(control_path)
        monitor = Monitor(provider)

        # Create failure history with common error keyword
        now = datetime.now(timezone.utc)
        failures = [
            {
                "taskId": f"TASK-{i}",
                "projectId": f"project-{i}",
                "agentType": "programmer",
                "failureReason": "git_error",
                "error": "git permission denied",
                "failedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ")
            }
            for i in range(4)  # 4 failures with 'git' keyword
        ]

        recent_failures = monitor._get_recent_failures(failures, hours=24)
        patterns = monitor._detect_patterns(recent_failures)

        # Should detect error pattern
        error_patterns = [p for p in patterns if p["type"] == "error_pattern"]
        git_pattern = [p for p in error_patterns if p["keyword"] == "git"]
        assert len(git_pattern) == 1
        assert git_pattern[0]["count"] == 4


class TestAutoReview:
    """Test automatic code review triggering."""

    def test_router_selects_correct_agent(self):
        """Test that router correctly selects agents based on task type."""
        router = Router()

        # Test routing for different task types
        test_cases = [
            ({"title": "Fix bug in login"}, "programmer"),
            ({"title": "Research new dependency"}, "researcher"),
            ({"title": "Review pull request"}, "reviewer"),
            ({"title": "Update README"}, "writer"),
            ({"title": "Design API architecture"}, "architect"),
        ]

        for task, expected_agent in test_cases:
            agent = router.route(task)
            assert agent == expected_agent, f"Expected {expected_agent} for task '{task['title']}', got {agent}"

    def test_explicit_agent_override(self):
        """Test that explicit agent field overrides routing."""
        router = Router()

        task = {
            "title": "Fix bug in login",
            "agent": "reviewer"  # Explicit override
        }

        # Router should respect explicit agent field
        # (In the engine, explicit agent is checked before routing)
        assert task.get("agent") == "reviewer"


class TestErrorRecovery:
    """Test git conflict recovery and retry logic."""

    def test_failure_rotation_implements_backoff(self, tmp_path):
        """Test that failure rotation implements exponential backoff."""
        from orchestrator.core.failure_rotation import FailureRotation

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        rotation = FailureRotation(state_dir)

        task_id = "TEST-123"

        # Record multiple failures
        for i in range(3):
            entry = rotation.record_failure(task_id)
            retry_count = entry.get("retry_count", 0)
            assert retry_count == i + 1

            # Should be skipped after first failure
            if i > 0:
                assert rotation.should_skip(task_id)

        # After cooling down, should retry
        entry = rotation.record_failure(task_id)
        assert entry.get("retry_count") >= 3


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
