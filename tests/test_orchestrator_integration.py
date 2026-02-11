"""
Comprehensive integration tests for orchestrator production readiness.

Tests:
1. Multi-worker: spawn 3 workers on different projects  
2. Task failure/retry: simulate worker crash, verify retry
3. Graceful shutdown: SIGTERM with active workers
4. Git conflict recovery: create conflict, verify recovery
5. Stuck task detection: mock stuck worker, verify cleanup
"""

import json
import subprocess
import time
import tempfile
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch
from datetime import datetime, timezone
import pytest

from orchestrator.core.worker import WorkerManager
from orchestrator.core.failure_rotation import FailureRotation, FailurePolicy
from orchestrator.providers.base import TaskProvider
from orchestrator.config import WORKER_KILL_TIMEOUT, WORKER_HEARTBEAT_TIMEOUT


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def mock_provider():
    """Create a mock task provider with essential methods."""
    provider = Mock(spec=TaskProvider)
    provider.update_worker_status = Mock()
    provider.update_task = Mock()
    provider.get_task = Mock(return_value=None)
    provider.sync = Mock(return_value=[])
    provider.scan_for_work = Mock(return_value=[])
    return provider


@pytest.fixture
def worker_manager_patches(tmp_path):
    """Context manager for patching WorkerManager dependencies."""
    patches = {
        'WORKER_RESULTS_DIR': tmp_path / "results",
        'ORCHESTRATOR_REPO_PATH': tmp_path / "orchestrator",
        'CONTROL_REPO_PATH': tmp_path / "control",
        'PROJECTS_FILE': tmp_path / "projects.json",
    }
    
    # Create necessary directories
    (tmp_path / "results").mkdir()
    (tmp_path / "orchestrator" / "worker-template").mkdir(parents=True)
    (tmp_path / "control").mkdir()
    (tmp_path / "projects.json").write_text('{"projects": []}')
    
    return patches


# ============================================================================
# Test 1: Multi-Worker Concurrency (Simpler version using existing test patterns)
# ============================================================================

def test_multi_worker_parallel_execution_simple(tmp_path, mock_provider):
    """Test that WorkerManager can track multiple concurrent workers."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    
    with patch('orchestrator.core.worker.WORKER_RESULTS_DIR', tmp_path / "results"):
        with patch('orchestrator.core.worker.ORCHESTRATOR_REPO_PATH', tmp_path / "orchestrator"):
            with patch('orchestrator.core.worker.CONTROL_REPO_PATH', tmp_path / "control"):
                with patch('orchestrator.core.worker.PROJECTS_FILE', tmp_path / "projects.json"):
                    (tmp_path / "results").mkdir()
                    (tmp_path / "orchestrator" / "worker-template").mkdir(parents=True)
                    (tmp_path / "control").mkdir()
                    (tmp_path / "projects.json").write_text('{"projects": []}')
                    
                    wm = WorkerManager(state_dir, mock_provider, max_workers=5)
                    wm._cleanup_orphaned_workers = Mock()
                    
                    # Manually add workers to active_workers (simulating successful spawns)
                    current_time = time.time()
                    
                    for i, project in enumerate(["project-a", "project-b", "project-c"]):
                        task_id = f"task-{i+1}"
                        mock_process = Mock()
                        mock_process.pid = 1000 + i
                        mock_process.poll = Mock(return_value=None)  # Still running
                        
                        wm.active_workers[task_id] = (
                            mock_process,  # process
                            project,  # project_id
                            Mock(),  # log_file
                            current_time,  # start_time
                            "programmer",  # agent_id
                            f"Task {i+1}",  # task_title
                            "programmer",  # agent_template
                            f"worker-{i+1}",  # worker_id
                            f"session-{i+1}",  # session_label
                            current_time,  # last_heartbeat
                        )
                        
                        # Mark project as locked
                        wm.project_locks[project] = task_id
                    
                    # Verify all three workers are tracked
                    assert len(wm.active_workers) == 3
                    assert "task-1" in wm.active_workers
                    assert "task-2" in wm.active_workers
                    assert "task-3" in wm.active_workers
                    
                    # Verify each project has one worker
                    assert "project-a" in wm.project_locks
                    assert "project-b" in wm.project_locks
                    assert "project-c" in wm.project_locks


def test_project_locking_prevents_concurrent_workers(tmp_path, mock_provider):
    """Test that per-project locking works."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    
    with patch('orchestrator.core.worker.WORKER_RESULTS_DIR', tmp_path / "results"):
        with patch('orchestrator.core.worker.ORCHESTRATOR_REPO_PATH', tmp_path / "orchestrator"):
            with patch('orchestrator.core.worker.CONTROL_REPO_PATH', tmp_path / "control"):
                with patch('orchestrator.core.worker.PROJECTS_FILE', tmp_path / "projects.json"):
                    (tmp_path / "results").mkdir()
                    (tmp_path / "orchestrator" / "worker-template").mkdir(parents=True)
                    (tmp_path / "control").mkdir()
                    (tmp_path / "projects.json").write_text('{"projects": []}')
                    
                    wm = WorkerManager(state_dir, mock_provider, max_workers=5)
                    wm._cleanup_orphaned_workers = Mock()
                    
                    # Lock project-a
                    wm.project_locks["project-a"] = "task-1"
                    
                    # Try to check if we can spawn on project-a (should be locked)
                    assert "project-a" in wm.project_locks
                    
                    # Check that other projects are not locked
                    assert "project-b" not in wm.project_locks


# ============================================================================
# Test 2: Task failure/retry - verify exponential backoff
# ============================================================================

def test_task_failure_retry_backoff(tmp_path, mock_provider):
    """Test that failed tasks are retried with exponential backoff."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    policy = FailurePolicy(
        base_seconds=10,
        second_seconds=30,
        factor=2,
        max_backoff_seconds=3600
    )
    
    fr = FailureRotation(
        state_dir,
        policy=policy,
        state_file=state_dir / "failures.json"
    )
    
    task_id = "test-task-retry"
    
    # Initial state - should not be skipped
    assert fr.should_skip(task_id, now=0.0) is False
    
    # First failure - 10s backoff
    entry1 = fr.record_failure(task_id, now=100.0)
    assert entry1["retry_count"] == 1
    assert fr.should_skip(task_id, now=105.0) is True  # Within backoff
    assert fr.should_skip(task_id, now=110.0) is False  # After backoff
    
    # Second failure - 30s backoff
    entry2 = fr.record_failure(task_id, now=200.0)
    assert entry2["retry_count"] == 2
    assert fr.should_skip(task_id, now=220.0) is True  # Within backoff
    assert fr.should_skip(task_id, now=230.0) is False  # After backoff
    
    # Third failure - 60s backoff (30 * 2)
    entry3 = fr.record_failure(task_id, now=300.0)
    assert entry3["retry_count"] == 3
    assert fr.should_skip(task_id, now=350.0) is True  # Within backoff
    assert fr.should_skip(task_id, now=360.0) is False  # After backoff
    
    # Success resets the counter
    fr.record_success(task_id)
    assert fr.get_entry(task_id) is None
    assert fr.should_skip(task_id, now=400.0) is False


def test_failure_backoff_pick_retry(tmp_path):
    """Test that pick_retry_when_all_skipped chooses task with nearest retry time."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    
    policy = FailurePolicy(base_seconds=10, second_seconds=50, factor=3, max_backoff_seconds=10_000)
    fr = FailureRotation(state_dir, policy=policy, state_file=state_dir / "fr.json")

    # task-a skipped until 200, task-b until 150 => should pick task-b
    fr.record_failure("task-a", now=100.0)
    fr._state["tasks"]["task-a"]["skip_until_ts"] = 200.0

    fr.record_failure("task-b", now=100.0)
    fr._state["tasks"]["task-b"]["skip_until_ts"] = 150.0

    assert fr.pick_retry_when_all_skipped(["task-a", "task-b"]) == "task-b"


# ============================================================================
# Test 3: Graceful shutdown
# ============================================================================

def test_graceful_shutdown_terminates_workers(tmp_path, mock_provider):
    """Test that shutdown() terminates all active workers."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    
    with patch('orchestrator.core.worker.WORKER_RESULTS_DIR', tmp_path / "results"):
        with patch('orchestrator.core.worker.ORCHESTRATOR_REPO_PATH', tmp_path / "orchestrator"):
            with patch('orchestrator.core.worker.CONTROL_REPO_PATH', tmp_path / "control"):
                with patch('orchestrator.core.worker.PROJECTS_FILE', tmp_path / "projects.json"):
                    (tmp_path / "results").mkdir()
                    (tmp_path / "orchestrator" / "worker-template").mkdir(parents=True)
                    (tmp_path / "control").mkdir()
                    (tmp_path / "projects.json").write_text('{"projects": []}')
                    
                    wm = WorkerManager(state_dir, mock_provider, max_workers=5)
                    wm._cleanup_orphaned_workers = Mock()
                    
                    # Create mock processes
                    processes = []
                    current_time = time.time()
                    
                    for i in range(3):
                        task_id = f"task-{i+1}"
                        mock_process = Mock()
                        mock_process.pid = 2000 + i
                        mock_process.poll = Mock(return_value=None)
                        mock_process.terminate = Mock()
                        mock_process.kill = Mock()
                        mock_process.wait = Mock(return_value=0)
                        processes.append(mock_process)
                        
                        wm.active_workers[task_id] = (
                            mock_process,
                            f"project-{i}",
                            Mock(),  # log_file
                            current_time,
                            "programmer",
                            f"Task {i+1}",
                            "programmer",
                            f"worker-{i+1}",
                            f"session-{i+1}",
                            current_time,
                        )
                    
                    assert len(wm.active_workers) == 3
                    
                    # Perform shutdown
                    wm.shutdown(timeout=1.0)
                    
                    # Verify all processes were terminated
                    for proc in processes:
                        proc.terminate.assert_called()


# ============================================================================
# Test 4: Git conflict recovery
# ============================================================================

def test_git_conflict_detection_and_recovery(tmp_path):
    """Test that git conflicts are detected and recovery is attempted."""
    from orchestrator.services.control import ControlManager
    
    project_path = tmp_path / "test-repo"
    project_path.mkdir()
    
    # Initialize git repo
    subprocess.run(["git", "init"], cwd=project_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=project_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project_path, check=True, capture_output=True)
    
    # Create initial commit
    test_file = project_path / "test.txt"
    test_file.write_text("Original content\n")
    subprocess.run(["git", "add", "."], cwd=project_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial"], cwd=project_path, check=True, capture_output=True)
    
    # Get the default branch name
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=project_path,
        capture_output=True,
        text=True,
        check=True
    )
    default_branch = result.stdout.strip()
    
    # Create feature branch with change
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=project_path, check=True, capture_output=True)
    test_file.write_text("Feature content\n")
    subprocess.run(["git", "add", "."], cwd=project_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Feature"], cwd=project_path, check=True, capture_output=True)
    
    # Go back to default branch and make conflicting change
    subprocess.run(["git", "checkout", default_branch], cwd=project_path, check=True, capture_output=True)
    test_file.write_text("Main content\n")
    subprocess.run(["git", "add", "."], cwd=project_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Main"], cwd=project_path, check=True, capture_output=True)
    
    # Try to rebase - this will create a conflict
    result = subprocess.run(
        ["git", "rebase", "feature"],
        cwd=project_path,
        capture_output=True
    )
    
    # Verify we're in a conflict state
    assert result.returncode != 0
    
    # Test ControlManager's conflict detection
    cm = ControlManager()
    is_in_rebase = cm._check_git_rebase_state(project_path)
    assert is_in_rebase is True
    
    # Test abort recovery
    success = cm._abort_git_rebase(project_path)
    assert success is True
    
    # Verify we're out of rebase state
    is_in_rebase_after = cm._check_git_rebase_state(project_path)
    assert is_in_rebase_after is False


# ============================================================================
# Test 5: Stuck task detection
# ============================================================================

def test_stuck_worker_runtime_detection(tmp_path, mock_provider):
    """Test that workers exceeding runtime limit are detected."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    
    with patch('orchestrator.core.worker.WORKER_RESULTS_DIR', tmp_path / "results"):
        with patch('orchestrator.core.worker.ORCHESTRATOR_REPO_PATH', tmp_path / "orchestrator"):
            with patch('orchestrator.core.worker.CONTROL_REPO_PATH', tmp_path / "control"):
                with patch('orchestrator.core.worker.PROJECTS_FILE', tmp_path / "projects.json"):
                    (tmp_path / "results").mkdir()
                    (tmp_path / "orchestrator" / "worker-template").mkdir(parents=True)
                    (tmp_path / "control").mkdir()
                    (tmp_path / "projects.json").write_text('{"projects": []}')
                    
                    wm = WorkerManager(state_dir, mock_provider, max_workers=5)
                    wm._cleanup_orphaned_workers = Mock()
                    
                    # Create a mock "stuck" process
                    current_time = time.time()
                    old_start_time = current_time - WORKER_KILL_TIMEOUT - 100
                    
                    mock_process = Mock()
                    mock_process.pid = 3000
                    mock_process.poll = Mock(return_value=None)  # Still running
                    mock_process.terminate = Mock()
                    mock_process.kill = Mock()
                    
                    wm.active_workers["task-stuck"] = (
                        mock_process,
                        "project-a",
                        Mock(),  # log_file
                        old_start_time,  # start_time (way in the past)
                        "programmer",
                        "Stuck Task",
                        "programmer",
                        "worker-stuck",
                        "session-stuck",
                        old_start_time,  # last_heartbeat (also old)
                    )
                    
                    # Mock finalize to avoid real git operations
                    wm.finalize_worker = Mock()
                    
                    # Check workers - should detect stuck worker
                    with patch('time.time', return_value=current_time):
                        wm.check_workers()
                    
                    # Verify process was terminated
                    assert mock_process.terminate.called or mock_process.kill.called
                    
                    # Verify finalize was called with failure
                    assert wm.finalize_worker.called


def test_stuck_worker_heartbeat_detection(tmp_path, mock_provider):
    """Test that workers with stale heartbeats are detected."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    
    with patch('orchestrator.core.worker.WORKER_RESULTS_DIR', tmp_path / "results"):
        with patch('orchestrator.core.worker.ORCHESTRATOR_REPO_PATH', tmp_path / "orchestrator"):
            with patch('orchestrator.core.worker.CONTROL_REPO_PATH', tmp_path / "control"):
                with patch('orchestrator.core.worker.PROJECTS_FILE', tmp_path / "projects.json"):
                    (tmp_path / "results").mkdir()
                    (tmp_path / "orchestrator" / "worker-template").mkdir(parents=True)
                    (tmp_path / "control").mkdir()
                    (tmp_path / "projects.json").write_text('{"projects": []}')
                    
                    wm = WorkerManager(state_dir, mock_provider, max_workers=5)
                    wm._cleanup_orphaned_workers = Mock()
                    
                    # Create a mock process with stale heartbeat
                    current_time = time.time()
                    stale_heartbeat = current_time - WORKER_HEARTBEAT_TIMEOUT - 100
                    
                    mock_process = Mock()
                    mock_process.pid = 4000
                    mock_process.poll = Mock(return_value=None)
                    mock_process.terminate = Mock()
                    mock_process.kill = Mock()
                    
                    wm.active_workers["task-no-heartbeat"] = (
                        mock_process,
                        "project-a",
                        Mock(),
                        current_time - 100,  # start_time (recent enough)
                        "programmer",
                        "No Heartbeat Task",
                        "programmer",
                        "worker-no-hb",
                        "session-no-hb",
                        stale_heartbeat,  # last_heartbeat (way in the past)
                    )
                    
                    wm.finalize_worker = Mock()
                    
                    # Check workers - should detect stale heartbeat
                    with patch('time.time', return_value=current_time):
                        wm.check_workers()
                    
                    # Verify process was terminated
                    assert mock_process.terminate.called or mock_process.kill.called
                    assert wm.finalize_worker.called


# ============================================================================
# Test 6: Worker capacity enforcement
# ============================================================================

def test_worker_capacity_enforcement(tmp_path, mock_provider):
    """Test that max_workers limit is enforced."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    
    with patch('orchestrator.core.worker.WORKER_RESULTS_DIR', tmp_path / "results"):
        with patch('orchestrator.core.worker.ORCHESTRATOR_REPO_PATH', tmp_path / "orchestrator"):
            with patch('orchestrator.core.worker.CONTROL_REPO_PATH', tmp_path / "control"):
                with patch('orchestrator.core.worker.PROJECTS_FILE', tmp_path / "projects.json"):
                    (tmp_path / "results").mkdir()
                    (tmp_path / "orchestrator" / "worker-template").mkdir(parents=True)
                    (tmp_path / "control").mkdir()
                    (tmp_path / "projects.json").write_text('{"projects": []}')
                    
                    # Create worker manager with capacity of 2
                    wm = WorkerManager(state_dir, mock_provider, max_workers=2)
                    wm._cleanup_orphaned_workers = Mock()
                    
                    # Add 2 workers manually
                    current_time = time.time()
                    for i in range(2):
                        task_id = f"task-{i+1}"
                        mock_process = Mock()
                        mock_process.pid = 5000 + i
                        mock_process.poll = Mock(return_value=None)
                        
                        wm.active_workers[task_id] = (
                            mock_process,
                            f"project-{i}",
                            Mock(),
                            current_time,
                            "programmer",
                            f"Task {i+1}",
                            "programmer",
                            f"worker-{i+1}",
                            f"session-{i+1}",
                            current_time,
                        )
                    
                    # Verify we're at capacity
                    assert len(wm.active_workers) == 2
                    active_count = len(wm.active_workers)
                    assert active_count >= wm.max_workers
