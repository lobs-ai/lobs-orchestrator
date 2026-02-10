"""Tests for setup wizard and setup state tracking."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestrator.utils.setup_wizard import (
    SetupState,
    check_step_status,
    validate_base_dir,
    validate_git_repo,
    validate_openclaw_executable,
)


@pytest.fixture
def temp_setup_state(tmp_path, monkeypatch):
    """Create a temporary setup state file."""
    state_file = tmp_path / ".lobs_setup_state.json"
    monkeypatch.setattr("orchestrator.utils.setup_wizard.SETUP_STATE_FILE", state_file)
    return state_file


def test_setup_state_init_creates_default_state(temp_setup_state):
    """Test that SetupState initializes with default state."""
    state = SetupState()
    assert state.state["version"] == 1
    assert state.state["completed_steps"] == []
    assert state.state["skipped_steps"] == []


def test_setup_state_mark_completed(temp_setup_state):
    """Test marking a step as completed."""
    state = SetupState()
    state.mark_completed("base_dir")
    
    assert state.is_completed("base_dir")
    assert not state.is_skipped("base_dir")
    
    # Check that it's persisted
    state2 = SetupState()
    assert state2.is_completed("base_dir")


def test_setup_state_mark_skipped(temp_setup_state):
    """Test marking a step as skipped."""
    state = SetupState()
    state.mark_skipped("control_repo")
    
    assert state.is_skipped("control_repo")
    assert not state.is_completed("control_repo")
    
    # Check that it's persisted
    state2 = SetupState()
    assert state2.is_skipped("control_repo")


def test_setup_state_completed_removes_from_skipped(temp_setup_state):
    """Test that marking as completed removes from skipped."""
    state = SetupState()
    state.mark_skipped("base_dir")
    assert state.is_skipped("base_dir")
    
    state.mark_completed("base_dir")
    assert state.is_completed("base_dir")
    assert not state.is_skipped("base_dir")


def test_setup_state_reset(temp_setup_state):
    """Test resetting setup state."""
    state = SetupState()
    state.mark_completed("base_dir")
    state.mark_skipped("control_repo")
    
    state.reset()
    
    assert not state.is_completed("base_dir")
    assert not state.is_skipped("control_repo")
    assert state.state["completed_steps"] == []
    assert state.state["skipped_steps"] == []


def test_validate_base_dir_valid(tmp_path):
    """Test validating a valid base directory."""
    valid, msg = validate_base_dir(str(tmp_path))
    assert valid is True
    assert str(tmp_path) in msg


def test_validate_base_dir_nonexistent():
    """Test validating a nonexistent directory."""
    valid, msg = validate_base_dir("/nonexistent/path")
    assert valid is False
    assert "does not exist" in msg


def test_validate_base_dir_not_directory(tmp_path):
    """Test validating a file instead of directory."""
    file_path = tmp_path / "file.txt"
    file_path.write_text("test")
    
    valid, msg = validate_base_dir(str(file_path))
    assert valid is False
    assert "not a directory" in msg


def test_validate_git_repo_valid(tmp_path):
    """Test validating a valid git repository."""
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    
    valid, msg = validate_git_repo(str(tmp_path))
    assert valid is True
    assert str(tmp_path) in msg


def test_validate_git_repo_no_git_dir(tmp_path):
    """Test validating a directory without .git."""
    valid, msg = validate_git_repo(str(tmp_path))
    assert valid is False
    assert "Not a git repository" in msg


def test_validate_git_repo_nonexistent():
    """Test validating a nonexistent git repository."""
    valid, msg = validate_git_repo("/nonexistent/repo")
    assert valid is False
    assert "does not exist" in msg


def test_validate_openclaw_executable_in_path():
    """Test validating openclaw when it's in PATH."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "/usr/local/bin/openclaw\n"
        
        valid, msg = validate_openclaw_executable("openclaw")
        assert valid is True
        assert "/usr/local/bin/openclaw" in msg


def test_validate_openclaw_executable_not_in_path():
    """Test validating openclaw when it's not in PATH."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 1
        
        valid, msg = validate_openclaw_executable("openclaw")
        assert valid is False
        assert "not found in PATH" in msg


def test_validate_openclaw_executable_absolute_path(tmp_path):
    """Test validating an absolute path to openclaw."""
    exe_path = tmp_path / "openclaw"
    exe_path.write_text("#!/bin/bash\necho 'openclaw'")
    exe_path.chmod(0o755)
    
    valid, msg = validate_openclaw_executable(str(exe_path))
    assert valid is True
    assert str(exe_path) in msg


def test_validate_openclaw_executable_not_executable(tmp_path):
    """Test validating a file that's not executable."""
    exe_path = tmp_path / "openclaw"
    exe_path.write_text("#!/bin/bash\necho 'openclaw'")
    exe_path.chmod(0o644)  # Not executable
    
    valid, msg = validate_openclaw_executable(str(exe_path))
    assert valid is False
    assert "not executable" in msg


def test_check_step_status_base_dir_complete(temp_setup_state, tmp_path):
    """Test check_step_status for a completed base_dir."""
    with patch("orchestrator.utils.setup_wizard.get_setting") as mock_get:
        mock_get.return_value = str(tmp_path)
        
        status, msg = check_step_status("base_dir")
        assert status == "complete"
        assert "✅" in msg


def test_check_step_status_incomplete(temp_setup_state):
    """Test check_step_status for an incomplete step."""
    with patch("orchestrator.utils.setup_wizard.get_setting") as mock_get:
        mock_get.return_value = None
        
        status, msg = check_step_status("base_dir")
        assert status == "incomplete"
        assert "❌" in msg


def test_check_step_status_invalid(temp_setup_state):
    """Test check_step_status for an invalid configuration."""
    with patch("orchestrator.utils.setup_wizard.get_setting") as mock_get:
        mock_get.return_value = "/nonexistent/path"
        
        status, msg = check_step_status("base_dir")
        assert status == "invalid"
        assert "⚠️" in msg


def test_check_step_status_skipped(temp_setup_state):
    """Test check_step_status for a skipped step."""
    state = SetupState()
    state.mark_skipped("base_dir")
    
    status, msg = check_step_status("base_dir")
    assert status == "skipped"
    assert "skipped" in msg.lower()
