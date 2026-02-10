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
    state_dir = temp_control_repo / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    
    wm = WorkerManager(state_dir, provider)
    
    task = {"id": "test-task"}
    project_id = "test-project"
    
    # Create project dir
    project_dir = temp_control_repo.parent / project_id
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / ".git").mkdir()
    
    # Mock subprocess.run for git pull
    mock_run.return_value = MagicMock(returncode=0)
    # Mock subprocess.Popen for openclaw
    mock_popen.return_value = MagicMock(pid=1234)
    
    with patch("orchestrator.services.prompter.Prompter.build_task_prompt", return_value="fake prompt"):
        wm._async_spawn_openclaw_flow(task, project_id, "task-runner", "fake rules")
        
        # Check if Popen was called with the custom path
        args, kwargs = mock_popen.call_args
        cmd = args[0]
        assert cmd[0] == custom_path
        assert cmd[1] == "agent"
        assert "--agent" in cmd
        assert "-m" in cmd

@patch("subprocess.Popen")
@patch("subprocess.run")
def test_worker_manager_openclaw_model_from_identity(mock_run, mock_popen, temp_control_repo, temp_settings):
    """Test that WorkerManager passes --model based on agents/<type>/IDENTITY.md."""
    provider = MagicMock()
    state_dir = temp_control_repo / "state"
    wm = WorkerManager(state_dir, provider)

    task = {"id": "test-task-model"}
    project_id = "test-project"

    # Create project dir
    project_dir = temp_control_repo.parent / project_id
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / ".git").mkdir()

    # Mock subprocess.run for git pull
    mock_run.return_value = MagicMock(returncode=0)
    # Mock subprocess.Popen for openclaw
    mock_popen.return_value = MagicMock(pid=1234)

    with patch("orchestrator.services.prompter.Prompter.build_task_prompt", return_value="fake prompt"):
        wm._async_spawn_openclaw_flow(task, project_id, "architect", "fake rules")

        args, _kwargs = mock_popen.call_args
        cmd = args[0]

        # Architect template uses Higher tier (Opus)
        assert "--model" in cmd
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "opus"


@patch("subprocess.Popen")
@patch("subprocess.run")
def test_worker_manager_openclaw_model_override(mock_run, mock_popen, temp_control_repo, temp_settings):
    """Test that a global model override forces all agents to use a specific model."""
    set_setting("openclaw_model_override", "opus")

    provider = MagicMock()
    state_dir = temp_control_repo / "state"
    wm = WorkerManager(state_dir, provider)

    task = {"id": "test-task-model-override"}
    project_id = "test-project"

    # Create project dir
    project_dir = temp_control_repo.parent / project_id
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / ".git").mkdir(exist_ok=True)

    mock_run.return_value = MagicMock(returncode=0)
    mock_popen.return_value = MagicMock(pid=1234)

    with patch("orchestrator.services.prompter.Prompter.build_task_prompt", return_value="fake prompt"):
        # Programmer template normally maps to default (no --model), but override should force opus.
        wm._async_spawn_openclaw_flow(task, project_id, "programmer", "fake rules")

        args, _kwargs = mock_popen.call_args
        cmd = args[0]

        assert "--model" in cmd
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "opus"


@patch("subprocess.Popen")
@patch("subprocess.run")
def test_worker_manager_openclaw_not_found(mock_run, mock_popen, temp_control_repo, temp_settings):
    """Test that WorkerManager handles missing openclaw executable gracefully."""
    set_setting("openclaw_executable", "/non/existent/path")
    
    provider = MagicMock()
    state_dir = temp_control_repo / "state"
    wm = WorkerManager(state_dir, provider)
    
    task = {"id": "test-task-fail"}
    project_id = "test-project"
    
    # Mock subprocesses
    mock_run.return_value = MagicMock(returncode=0)
    mock_popen.side_effect = FileNotFoundError("[Errno 2] No such file or directory: '/non/existent/path'")
    
    with patch("orchestrator.services.prompter.Prompter.build_task_prompt", return_value="fake prompt"):
        wm._async_spawn_openclaw_flow(task, project_id, "task-runner", "fake rules")
        
        # Verify failure was handled
        provider.update_task.assert_called_with("test-task-fail", {"workState": "failed"})
        # State should be cleared (no worker-state.json file)
        assert not wm.STATE_FILE.exists()
