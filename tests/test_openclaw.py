import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path
from orchestrator.core.worker import WorkerManager
from orchestrator.utils.settings import set_setting

def test_openclaw_executable_default(temp_settings):
    """Test that openclaw defaults to 'openclaw' if not set."""
    from orchestrator.utils.settings import get_setting
    assert get_setting("openclaw_executable", "openclaw") == "openclaw"

def test_openclaw_executable_custom(temp_settings):
    """Test that a custom openclaw executable can be set."""
    custom_path = "/usr/local/bin/my-openclaw"
    set_setting("openclaw_executable", custom_path)
    from orchestrator.utils.settings import get_setting
    assert get_setting("openclaw_executable") == custom_path

@patch("subprocess.Popen")
@patch("subprocess.run")
def test_worker_manager_uses_custom_openclaw(mock_run, mock_popen, temp_control_repo, temp_settings):
    """Test that WorkerManager uses the configured openclaw executable."""
    custom_path = "/tmp/fake-openclaw"
    set_setting("openclaw_executable", custom_path)
    
    provider = MagicMock()
    # Mock lock_dir as a Path object
    lock_dir = temp_control_repo / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    
    wm = WorkerManager(lock_dir, provider)
    
    task = {"id": "test-task"}
    project_id = "test-project"
    
    # Create project dir
    project_dir = temp_control_repo.parent / project_id
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / ".git").mkdir() # Make it a git repo
    
    # Mock subprocess.run for git pull
    mock_run.return_value = MagicMock(returncode=0)
    # Mock subprocess.Popen for openclaw
    mock_popen.return_value = MagicMock(pid=1234)
    
    # We need to mock Prompter.build_task_prompt as well
    with patch("orchestrator.services.prompter.Prompter.build_task_prompt", return_value="fake prompt"):
        # We need to call the internal _async_spawn_openclaw_flow directly to avoid threading for testing
        wm._async_spawn_openclaw_flow(task, project_id, "task-runner", "fake rules")
        
        # Check if Popen was called with the custom path
        args, kwargs = mock_popen.call_args
        cmd = args[0]
        assert cmd[0] == custom_path
        assert cmd[1] == "agent"
        assert "--agent" in cmd
        assert "-m" in cmd  # -m is short for --message

@patch("subprocess.Popen")
@patch("subprocess.run")
def test_worker_manager_openclaw_not_found(mock_run, mock_popen, temp_control_repo, temp_settings):
    """Test that WorkerManager handles missing openclaw executable gracefully."""
    set_setting("openclaw_executable", "/non/existent/path")
    
    provider = MagicMock()
    lock_dir = temp_control_repo / "locks"
    wm = WorkerManager(lock_dir, provider)
    
    task = {"id": "test-task-fail"}
    project_id = "test-project"
    
    # Mock subprocesses
    mock_run.return_value = MagicMock(returncode=0)
    mock_popen.side_effect = FileNotFoundError("[Errno 2] No such file or directory: '/non/existent/path'")
    
    with patch("orchestrator.services.prompter.Prompter.build_task_prompt", return_value="fake prompt"):
        wm._async_spawn_openclaw_flow(task, project_id, "task-runner", "fake rules")
        
        # Verify failure was handled
        provider.update_task.assert_called_with("test-task-fail", {"workState": "failed"})
        # Verify lock was released
        lock_file = lock_dir / f"{project_id}.lock"
        assert not lock_file.exists()
