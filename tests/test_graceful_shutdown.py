"""Tests for graceful shutdown with multiple active workers."""
import json
import subprocess
import time
import tempfile
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch
import pytest

from orchestrator.core.worker import WorkerManager
from orchestrator.core.failure_rotation import FailureRotation
from orchestrator.providers.base import TaskProvider


class MockProcess:
    """Mock process for testing."""
    def __init__(self, pid: int, terminate_delay: float = 0.1, should_timeout: bool = False):
        self.pid = pid
        self.terminate_delay = terminate_delay
        self.should_timeout = should_timeout
        self._terminated = False
        self._killed = False
        self.returncode = None
        
    def poll(self):
        """Check if process is still running."""
        if self._terminated or self._killed:
            return 0
        return None
    
    def terminate(self):
        """Gracefully terminate the process."""
        self._terminated = True
        if not self.should_timeout:
            time.sleep(self.terminate_delay)
            self.returncode = 0
    
    def kill(self):
        """Force kill the process."""
        self._killed = True
        self.returncode = -9
    
    def wait(self, timeout=None):
        """Wait for process to complete."""
        if self.should_timeout and not self._killed:
            raise subprocess.TimeoutExpired(cmd="test", timeout=timeout)
        if self._terminated or self._killed:
            return self.returncode
        time.sleep(self.terminate_delay)
        return self.returncode


class MockTaskProvider(TaskProvider):
    """Mock provider for testing."""
    def __init__(self):
        self.tasks = {}
        self.worker_status = {}
        self.sync_calls = []
    
    def get_tasks(self):
        return list(self.tasks.values())
    
    def get_task(self, task_id):
        return self.tasks.get(task_id)
    
    def update_task(self, task_id, updates):
        if task_id in self.tasks:
            self.tasks[task_id].update(updates)
    
    def update_worker_status(self, status):
        self.worker_status.update(status)
    
    def sync(self):
        return self.sync_calls
    
    def get_projects(self):
        return []
    
    def get_engineering_rules(self):
        return ""


@pytest.fixture
def temp_state_dir():
    """Create a temporary state directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def mock_provider():
    """Create a mock provider."""
    return MockTaskProvider()


@pytest.fixture
def worker_manager(temp_state_dir, mock_provider):
    """Create a WorkerManager for testing."""
    failure_rotation = FailureRotation(temp_state_dir)
    manager = WorkerManager(
        state_dir=temp_state_dir,
        provider=mock_provider,
        failure_rotation=failure_rotation,
        max_workers=3,
    )
    return manager


def test_shutdown_no_workers(worker_manager):
    """Test shutdown with no active workers."""
    # Should complete immediately
    start = time.time()
    worker_manager.shutdown(timeout=5.0)
    elapsed = time.time() - start
    
    # Should take less than 1 second
    assert elapsed < 1.0


def test_shutdown_single_worker_completes(worker_manager, temp_state_dir):
    """Test shutdown with one worker that completes naturally."""
    # Create a mock active worker
    task_id = "task-001"
    project_id = "test-project"
    
    mock_process = MockProcess(pid=12345, terminate_delay=0.5)
    log_file = (temp_state_dir / f"{task_id}.log").open("w")
    
    worker_manager.active_workers[task_id] = (
        mock_process,
        project_id,
        log_file,
        time.time(),
        "programmer",
        "Test task",
        "programmer",
        "worker-001",
        "session-001",
    )
    worker_manager.project_locks[project_id] = task_id
    
    # Mock the check_workers method to simulate completion
    original_check = worker_manager.check_workers
    check_count = [0]
    
    def mock_check():
        check_count[0] += 1
        if check_count[0] >= 2:
            # Simulate worker completion
            mock_process.returncode = 0
            if task_id in worker_manager.active_workers:
                del worker_manager.active_workers[task_id]
                log_file.close()
                if project_id in worker_manager.project_locks:
                    del worker_manager.project_locks[project_id]
    
    worker_manager.check_workers = mock_check
    
    # Shutdown should complete when worker finishes
    start = time.time()
    worker_manager.shutdown(timeout=10.0)
    elapsed = time.time() - start
    
    # Should complete quickly since worker finishes naturally
    assert elapsed < 5.0
    assert len(worker_manager.active_workers) == 0
    assert len(worker_manager.project_locks) == 0


def test_shutdown_multiple_workers_force_terminate(worker_manager, temp_state_dir, mock_provider):
    """Test shutdown with multiple workers that need force termination."""
    # Create multiple mock active workers
    workers = [
        ("task-001", "project-A", 12345, False),  # Will terminate gracefully
        ("task-002", "project-B", 12346, True),   # Will timeout on terminate
        ("task-003", "project-C", 12347, False),  # Will terminate gracefully
    ]
    
    for task_id, project_id, pid, should_timeout in workers:
        mock_process = MockProcess(pid=pid, should_timeout=should_timeout)
        log_file = (temp_state_dir / f"{task_id}.log").open("w")
        
        worker_manager.active_workers[task_id] = (
            mock_process,
            project_id,
            log_file,
            time.time(),
            "programmer",
            f"Test task {task_id}",
            "programmer",
            f"worker-{pid}",
            f"session-{pid}",
        )
        worker_manager.project_locks[project_id] = task_id
        mock_provider.tasks[task_id] = {"id": task_id, "title": f"Test task {task_id}"}
    
    # Mock finalization and cleanup methods
    with patch.object(worker_manager, 'finalize_project_changes_wip') as mock_finalize, \
         patch.object(worker_manager, '_capture_worker_usage') as mock_capture, \
         patch.object(worker_manager, '_cleanup_worker_session') as mock_cleanup:
        
        # Shutdown with very short timeout to force termination
        worker_manager.shutdown(timeout=0.1)
        
        # All workers should be terminated
        assert len(worker_manager.active_workers) == 0
        assert len(worker_manager.project_locks) == 0
        
        # All workers should have been finalized
        assert mock_finalize.call_count == 3
        
        # All workers should have usage captured
        assert mock_capture.call_count == 3
        
        # All workers should have sessions cleaned up
        assert mock_cleanup.call_count == 3
        
        # All tasks should be marked as failed
        for task_id, _, _, _ in workers:
            task = mock_provider.tasks.get(task_id)
            assert task is not None
            assert task.get("workState") == "failed"


def test_shutdown_with_pending_workers(worker_manager):
    """Test shutdown with pending (syncing/finalizing) workers."""
    # Add some pending workers
    worker_manager.pending_workers.add("task-001")
    worker_manager.pending_workers.add("task-002")
    
    # Shutdown should clear pending workers
    worker_manager.shutdown(timeout=5.0)
    
    assert len(worker_manager.pending_workers) == 0


def test_shutdown_mixed_workers(worker_manager, temp_state_dir, mock_provider):
    """Test shutdown with both active and pending workers."""
    # Add active worker
    task_id = "task-active"
    project_id = "project-A"
    mock_process = MockProcess(pid=12345, should_timeout=True)
    log_file = (temp_state_dir / f"{task_id}.log").open("w")
    
    worker_manager.active_workers[task_id] = (
        mock_process,
        project_id,
        log_file,
        time.time(),
        "programmer",
        "Active task",
        "programmer",
        "worker-001",
        "session-001",
    )
    worker_manager.project_locks[project_id] = task_id
    mock_provider.tasks[task_id] = {"id": task_id, "title": "Active task"}
    
    # Add pending workers
    worker_manager.pending_workers.add("task-pending-1")
    worker_manager.pending_workers.add("task-pending-2")
    
    # Mock cleanup methods
    with patch.object(worker_manager, 'finalize_project_changes_wip'), \
         patch.object(worker_manager, '_capture_worker_usage'), \
         patch.object(worker_manager, '_cleanup_worker_session'):
        
        # Shutdown
        worker_manager.shutdown(timeout=0.1)
        
        # All workers should be cleaned up
        assert len(worker_manager.active_workers) == 0
        assert len(worker_manager.pending_workers) == 0
        assert len(worker_manager.project_locks) == 0


def test_shutdown_timeout_calculation(worker_manager, temp_state_dir):
    """Test that shutdown respects the timeout parameter."""
    # Create a mock active worker
    task_id = "task-001"
    project_id = "test-project"
    
    mock_process = MockProcess(pid=12345)
    log_file = (temp_state_dir / f"{task_id}.log").open("w")
    
    worker_manager.active_workers[task_id] = (
        mock_process,
        project_id,
        log_file,
        time.time(),
        "programmer",
        "Test task",
        "programmer",
        "worker-001",
        "session-001",
    )
    
    # Mock check_workers to never complete
    worker_manager.check_workers = lambda: None
    
    # Mock cleanup methods
    with patch.object(worker_manager, 'finalize_project_changes_wip'), \
         patch.object(worker_manager, '_capture_worker_usage'), \
         patch.object(worker_manager, '_cleanup_worker_session'):
        
        # Shutdown with 1 second timeout
        start = time.time()
        worker_manager.shutdown(timeout=1.0)
        elapsed = time.time() - start
        
        # Should timeout after approximately 1 second (allow some slack)
        assert 0.9 <= elapsed <= 2.0
        
        # Worker should be force terminated
        assert len(worker_manager.active_workers) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
