"""
Test per-project worker locking.

Verifies that only one worker can be active per project at a time,
while different projects can run workers in parallel.
"""

import pytest
import time
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from orchestrator.core.worker import WorkerManager
from orchestrator.providers.base import TaskProvider


@pytest.fixture
def mock_provider():
    """Create a mock task provider."""
    provider = Mock(spec=TaskProvider)
    provider.update_worker_status = Mock()
    provider.update_task = Mock()
    provider.get_task = Mock(return_value=None)
    return provider


@pytest.fixture
def worker_manager(tmp_path, mock_provider):
    """Create a WorkerManager instance with mocked dependencies."""
    with patch('orchestrator.core.worker.WORKER_RESULTS_DIR', tmp_path / "results"):
        with patch('orchestrator.core.worker.ORCHESTRATOR_REPO_PATH', tmp_path / "orchestrator"):
            with patch('orchestrator.core.worker.CONTROL_REPO_PATH', tmp_path / "control"):
                with patch('orchestrator.core.worker.PROJECTS_FILE', tmp_path / "projects.json"):
                    # Create necessary directories
                    (tmp_path / "results").mkdir(parents=True)
                    (tmp_path / "orchestrator" / "worker-template").mkdir(parents=True)
                    (tmp_path / "control").mkdir(parents=True)
                    
                    # Create empty projects.json
                    (tmp_path / "projects.json").write_text('{"projects": []}')
                    
                    wm = WorkerManager(tmp_path / "state", mock_provider)
                    wm._cleanup_orphaned_workers = Mock()  # Skip cleanup on init
                    return wm


def test_single_project_lock(worker_manager, mock_provider):
    """Test that only one worker can run per project."""
    task1 = {
        "id": "task-1",
        "title": "Task 1",
        "kind": "task",
    }
    task2 = {
        "id": "task-2", 
        "title": "Task 2",
        "kind": "task",
    }
    project_id = "project-a"
    
    # Mock the async spawn to just add to pending immediately
    with patch.object(worker_manager.executor, 'submit') as mock_submit:
        # First spawn should succeed
        result1 = worker_manager.spawn_worker(task1, project_id)
        assert result1 is True, "First worker should spawn successfully"
        assert project_id not in worker_manager.project_locks, "Lock not yet acquired (pending)"
        assert "task-1" in worker_manager.pending_workers
        
        # Simulate lock being acquired when worker starts
        worker_manager.project_locks[project_id] = "task-1"
        worker_manager.pending_workers.discard("task-1")
        worker_manager.active_workers["task-1"] = (
            Mock(), project_id, Mock(), time.time(), "programmer", "Task 1", "programmer"
        )
        
        # Second spawn for same project should fail (queued)
        result2 = worker_manager.spawn_worker(task2, project_id)
        assert result2 is False, "Second worker should be queued (project locked)"
        assert "task-2" not in worker_manager.pending_workers
        assert "task-2" not in worker_manager.active_workers
        
        # Verify lock is still held by task-1
        assert worker_manager.project_locks[project_id] == "task-1"


def test_different_projects_parallel(worker_manager, mock_provider):
    """Test that different projects can run workers in parallel."""
    task1 = {
        "id": "task-1",
        "title": "Task 1",
        "kind": "task",
    }
    task2 = {
        "id": "task-2",
        "title": "Task 2", 
        "kind": "task",
    }
    
    with patch.object(worker_manager.executor, 'submit') as mock_submit:
        # Spawn worker for project-a
        result1 = worker_manager.spawn_worker(task1, "project-a")
        assert result1 is True
        
        # Simulate lock acquisition for project-a
        worker_manager.project_locks["project-a"] = "task-1"
        worker_manager.pending_workers.discard("task-1")
        worker_manager.active_workers["task-1"] = (
            Mock(), "project-a", Mock(), time.time(), "programmer", "Task 1", "programmer"
        )
        
        # Spawn worker for project-b should also succeed (different project)
        result2 = worker_manager.spawn_worker(task2, "project-b")
        assert result2 is True, "Worker for different project should spawn"
        
        # Both locks should exist
        assert "project-a" in worker_manager.project_locks
        assert "task-2" in worker_manager.pending_workers


def test_lock_released_on_completion(worker_manager, mock_provider):
    """Test that project lock is released when worker completes."""
    task = {
        "id": "task-1",
        "title": "Task 1",
        "kind": "task",
    }
    project_id = "project-a"
    
    # Setup a completed worker
    mock_process = Mock()
    mock_process.poll.return_value = 0  # Completed successfully
    mock_log = Mock()
    
    worker_manager.project_locks[project_id] = "task-1"
    worker_manager.active_workers["task-1"] = (
        mock_process, project_id, mock_log, time.time(), "programmer", "Task 1", "programmer"
    )
    
    # Check workers (should detect completion)
    worker_manager.check_workers()
    
    # Lock should be released
    assert project_id not in worker_manager.project_locks, "Lock should be released after completion"
    assert "task-1" not in worker_manager.active_workers


def test_lock_released_on_failure(worker_manager, mock_provider):
    """Test that project lock is released when worker fails."""
    task = {
        "id": "task-1",
        "title": "Task 1",
        "kind": "task",
    }
    project_id = "project-a"
    
    # Setup a failed worker
    mock_process = Mock()
    mock_process.poll.return_value = 1  # Failed
    mock_log = Mock()
    
    worker_manager.project_locks[project_id] = "task-1"
    worker_manager.active_workers["task-1"] = (
        mock_process, project_id, mock_log, time.time(), "programmer", "Task 1", "programmer"
    )
    
    # Mock the finalization methods
    with patch.object(worker_manager, 'finalize_project_changes_wip', return_value=True):
        with patch.object(worker_manager, 'handle_worker_failure'):
            # Check workers (should detect failure)
            worker_manager.check_workers()
    
    # Lock should be released
    assert project_id not in worker_manager.project_locks, "Lock should be released after failure"
    assert "task-1" not in worker_manager.active_workers


def test_lock_reacquired_on_restart(worker_manager, mock_provider):
    """Test that project lock is re-acquired when re-adopting a worker on restart."""
    # Simulate state file from previous run
    state = {
        "active": True,
        "task_id": "task-1",
        "project_id": "project-a",
        "pid": 12345,
        "state": "running",
        "agent_id": "programmer",
        "agent_template": "programmer",
        "task_title": "Task 1",
    }
    
    with patch.object(worker_manager, '_load_state', return_value=state):
        with patch.object(worker_manager, '_is_process_alive', return_value=True):
            with patch('builtins.open', MagicMock()):
                # Re-run cleanup which should re-adopt the worker
                worker_manager._cleanup_orphaned_workers()
    
    # Lock should be re-acquired
    assert "project-a" in worker_manager.project_locks
    assert worker_manager.project_locks["project-a"] == "task-1"
    assert "task-1" in worker_manager.active_workers


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
