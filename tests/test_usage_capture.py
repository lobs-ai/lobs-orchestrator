import pytest
from unittest.mock import MagicMock

from orchestrator.core.worker import WorkerManager


def test_usage_capture_on_worker_failure(temp_control_repo, temp_settings):
    provider = MagicMock()
    state_dir = temp_control_repo / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    wm = WorkerManager(state_dir, provider)

    # Isolate side effects
    wm.escalation = MagicMock()
    wm.failure_rotation = MagicMock()
    wm._cleanup_worker_session = MagicMock()
    wm._capture_worker_usage = MagicMock()

    wm.handle_worker_failure(
        task_id="task-123",
        project_id="proj-abc",
        error_log="boom",
        agent_id="worker",
        start_time=123.0,
    )

    wm._capture_worker_usage.assert_called_once()
    args, kwargs = wm._capture_worker_usage.call_args
    assert args[0] == "worker"
    assert args[1] == "task-123"
    assert args[2] == 123.0
    assert kwargs["succeeded"] is False
