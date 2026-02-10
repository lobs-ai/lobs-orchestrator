"""Tests for worker health monitoring."""
import time
import tempfile
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch
import pytest

from orchestrator.core.worker import WorkerManager
from orchestrator.config import WORKER_WARNING_TIMEOUT, WORKER_KILL_TIMEOUT, WORKER_HEARTBEAT_TIMEOUT


@pytest.fixture
def temp_state_dir():
    """Create a temporary state directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def mock_provider():
    """Create a mock task provider."""
    provider = Mock()
    provider.update_worker_status = Mock()
    provider.update_task = Mock()
    provider.get_task = Mock(return_value=None)
    return provider


def test_worker_health_timeouts_configured():
    """Test that worker health timeouts are properly configured."""
    assert WORKER_WARNING_TIMEOUT > 0
    assert WORKER_KILL_TIMEOUT > WORKER_WARNING_TIMEOUT
    assert WORKER_HEARTBEAT_TIMEOUT > 0
    assert isinstance(WORKER_WARNING_TIMEOUT, int)
    assert isinstance(WORKER_KILL_TIMEOUT, int)
    assert isinstance(WORKER_HEARTBEAT_TIMEOUT, int)


def test_worker_heartbeat_tracking(temp_state_dir, mock_provider):
    """Test that worker heartbeat is tracked in active_workers."""
    manager = WorkerManager(
        state_dir=temp_state_dir,
        provider=mock_provider,
        max_workers=1,
    )
    
    # Create a mock process
    mock_process = Mock()
    mock_process.pid = 12345
    mock_process.poll = Mock(return_value=None)  # Still running
    
    # Add a worker to active_workers
    task_id = "test-task-123"
    project_id = "test-project"
    current_time = time.time()
    
    manager.active_workers[task_id] = (
        mock_process,
        project_id,
        Mock(),  # log_file
        current_time - 100,  # start_time (100 seconds ago)
        "programmer",  # agent_id
        "Test Task",  # task_title
        "programmer",  # agent_template
        "worker-123",  # worker_id
        "worker-session-123",  # session_label
        current_time - 50,  # last_heartbeat (50 seconds ago)
    )
    
    # Verify tuple structure includes last_heartbeat
    worker_data = manager.active_workers[task_id]
    assert len(worker_data) == 10  # Should have 10 elements including last_heartbeat
    assert worker_data[9] == current_time - 50  # last_heartbeat is the 10th element


def test_heartbeat_updated_on_status_update(temp_state_dir, mock_provider):
    """Test that heartbeat is updated when status is updated."""
    manager = WorkerManager(
        state_dir=temp_state_dir,
        provider=mock_provider,
        max_workers=1,
    )
    
    # Create a mock process
    mock_process = Mock()
    mock_process.pid = 12345
    mock_process.poll = Mock(return_value=None)  # Still running
    
    # Add a worker to active_workers
    task_id = "test-task-123"
    project_id = "test-project"
    old_time = time.time() - 100
    
    manager.active_workers[task_id] = (
        mock_process,
        project_id,
        Mock(),  # log_file
        old_time,  # start_time
        "programmer",  # agent_id
        "Test Task",  # task_title
        "programmer",  # agent_template
        "worker-123",  # worker_id
        "worker-session-123",  # session_label
        old_time,  # last_heartbeat (old)
    )
    
    # Update provider status (should update heartbeat)
    time.sleep(0.1)  # Small delay to ensure time changes
    manager._update_provider_status()
    
    # Verify heartbeat was updated
    worker_data = manager.active_workers[task_id]
    new_heartbeat = worker_data[9]
    assert new_heartbeat > old_time  # Heartbeat should be newer


def test_stuck_worker_detection_runtime(temp_state_dir, mock_provider):
    """Test detection of workers that exceed maximum runtime."""
    manager = WorkerManager(
        state_dir=temp_state_dir,
        provider=mock_provider,
        max_workers=1,
    )
    
    # Create a mock process
    mock_process = Mock()
    mock_process.pid = 12345
    mock_process.poll = Mock(return_value=None)  # Still running
    mock_process.terminate = Mock()
    mock_process.kill = Mock()
    mock_process.wait = Mock()
    
    # Add a worker that started more than WORKER_KILL_TIMEOUT seconds ago
    task_id = "test-task-stuck"
    project_id = "test-project"
    current_time = time.time()
    stuck_start_time = current_time - WORKER_KILL_TIMEOUT - 10  # 10 seconds over limit
    
    manager.active_workers[task_id] = (
        mock_process,
        project_id,
        Mock(),  # log_file
        stuck_start_time,  # start_time (old)
        "programmer",  # agent_id
        "Stuck Task",  # task_title
        "programmer",  # agent_template
        "worker-stuck",  # worker_id
        "worker-session-stuck",  # session_label
        stuck_start_time,  # last_heartbeat (old but doesn't matter - runtime check triggers first)
    )
    
    # Mock finalize and failure handling
    manager.finalize_project_changes_wip = Mock(return_value=True)
    manager._capture_worker_usage = Mock()
    manager._cleanup_worker_session = Mock()
    manager.escalation = Mock()
    manager.escalation.process_failure = Mock()
    
    # Verify worker is in active_workers before check
    assert task_id in manager.active_workers
    
    # Check workers - should detect stuck worker and terminate it
    manager.check_workers()
    
    # Verify worker was terminated
    mock_process.terminate.assert_called_once()
    
    # Verify WIP finalization was called (to clean up repo)
    # Note: May be called multiple times during stuck worker handling
    assert manager.finalize_project_changes_wip.call_count >= 1
    
    # Verify the finalize call included the stuck reason
    finalize_calls = manager.finalize_project_changes_wip.call_args_list
    stuck_finalize_call = [c for c in finalize_calls if 'stuck' in c.kwargs.get('failure_reason', '')]
    assert len(stuck_finalize_call) > 0, "Expected at least one finalize call with 'stuck' in failure_reason"
    
    # Verify provider was notified of failure
    mock_provider.update_task.assert_called()
    
    # Check that failure reason contains stuck indicator
    call_args = mock_provider.update_task.call_args
    if call_args:
        updates = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get('updates', {})
        assert 'failureReason' in updates
        # Failure reason should indicate it was stuck
        assert 'stuck' in updates['failureReason'].lower() or 'exceeded' in updates['failureReason'].lower()


def test_stuck_worker_detection_heartbeat(temp_state_dir, mock_provider):
    """Test detection of workers with stale heartbeat."""
    manager = WorkerManager(
        state_dir=temp_state_dir,
        provider=mock_provider,
        max_workers=1,
    )
    
    # Create a mock process
    mock_process = Mock()
    mock_process.pid = 12345
    mock_process.poll = Mock(return_value=None)  # Still running
    mock_process.terminate = Mock()
    mock_process.kill = Mock()
    mock_process.wait = Mock()
    
    # Add a worker with old heartbeat but recent start time
    task_id = "test-task-no-heartbeat"
    project_id = "test-project"
    current_time = time.time()
    
    manager.active_workers[task_id] = (
        mock_process,
        project_id,
        Mock(),  # log_file
        current_time - 600,  # start_time (10 minutes ago - under kill timeout)
        "programmer",  # agent_id
        "No Heartbeat Task",  # task_title
        "programmer",  # agent_template
        "worker-no-hb",  # worker_id
        "worker-session-no-hb",  # session_label
        current_time - WORKER_HEARTBEAT_TIMEOUT - 10,  # last_heartbeat (stale)
    )
    
    # Mock finalize and failure handling
    manager.finalize_project_changes_wip = Mock(return_value=True)
    manager._capture_worker_usage = Mock()
    manager._cleanup_worker_session = Mock()
    manager.escalation = Mock()
    manager.escalation.process_failure = Mock()
    
    # Verify worker is in active_workers before check
    assert task_id in manager.active_workers
    
    # Check workers - should detect stuck worker and terminate it
    manager.check_workers()
    
    # Verify worker was terminated
    mock_process.terminate.assert_called_once()
    
    # Verify WIP finalization was called
    # Note: May be called multiple times during stuck worker handling
    assert manager.finalize_project_changes_wip.call_count >= 1
    
    # Verify the finalize call included the stuck/heartbeat reason
    finalize_calls = manager.finalize_project_changes_wip.call_args_list
    stuck_finalize_call = [c for c in finalize_calls if 'stuck' in c.kwargs.get('failure_reason', '')]
    assert len(stuck_finalize_call) > 0, "Expected at least one finalize call with 'stuck' in failure_reason"
    
    # Verify provider was notified of failure
    mock_provider.update_task.assert_called()
    
    # Check that failure reason contains heartbeat indicator
    call_args = mock_provider.update_task.call_args
    if call_args:
        updates = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get('updates', {})
        assert 'failureReason' in updates
        # Failure reason should indicate it was stuck due to heartbeat
        assert 'stuck' in updates['failureReason'].lower() and 'heartbeat' in updates['failureReason'].lower()


def test_healthy_worker_not_terminated(temp_state_dir, mock_provider):
    """Test that healthy workers are not terminated."""
    manager = WorkerManager(
        state_dir=temp_state_dir,
        provider=mock_provider,
        max_workers=1,
    )
    
    # Create a mock process
    mock_process = Mock()
    mock_process.pid = 12345
    mock_process.poll = Mock(return_value=None)  # Still running
    mock_process.terminate = Mock()
    
    # Add a healthy worker (recent start, recent heartbeat)
    task_id = "test-task-healthy"
    project_id = "test-project"
    current_time = time.time()
    
    manager.active_workers[task_id] = (
        mock_process,
        project_id,
        Mock(),  # log_file
        current_time - 100,  # start_time (100 seconds ago)
        "programmer",  # agent_id
        "Healthy Task",  # task_title
        "programmer",  # agent_template
        "worker-healthy",  # worker_id
        "worker-session-healthy",  # session_label
        current_time - 10,  # last_heartbeat (10 seconds ago)
    )
    
    # Check workers - should NOT terminate healthy worker
    manager.check_workers()
    
    # Verify worker was NOT terminated
    mock_process.terminate.assert_not_called()
    
    # Verify worker is still in active_workers
    assert task_id in manager.active_workers


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
