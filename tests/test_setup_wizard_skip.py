"""Tests for setup wizard skip/later functionality."""

from pathlib import Path
from unittest.mock import patch
from io import StringIO

import pytest

from orchestrator.utils.setup_wizard import run_interactive_step, SetupState


@pytest.fixture
def temp_setup_state(tmp_path, monkeypatch):
    """Create a temporary setup state file."""
    state_file = tmp_path / ".lobs_setup_state.json"
    monkeypatch.setattr("orchestrator.utils.setup_wizard.SETUP_STATE_FILE", state_file)
    
    # Also patch settings file
    settings_file = tmp_path / ".lobs_settings.json"
    monkeypatch.setattr("orchestrator.utils.settings.SETTINGS_FILE", settings_file)
    
    return state_file


def test_skip_keyword_marks_step_as_skipped(temp_setup_state, monkeypatch):
    """Test that entering 'skip' marks a step as skipped."""
    # Mock user input to enter 'skip'
    monkeypatch.setattr('builtins.input', lambda _: 'skip')
    
    result = run_interactive_step('base_dir')
    
    assert result is False  # Step was not completed
    
    state = SetupState()
    assert state.is_skipped('base_dir')
    assert not state.is_completed('base_dir')


def test_later_keyword_marks_step_as_skipped(temp_setup_state, monkeypatch):
    """Test that entering 'later' marks a step as skipped."""
    # Mock user input to enter 'later'
    monkeypatch.setattr('builtins.input', lambda _: 'later')
    
    result = run_interactive_step('base_dir')
    
    assert result is False  # Step was not completed
    
    state = SetupState()
    assert state.is_skipped('base_dir')
    assert not state.is_completed('base_dir')


def test_skip_control_repo_step(temp_setup_state, monkeypatch):
    """Test that control_repo can be skipped."""
    monkeypatch.setattr('builtins.input', lambda _: 'skip')
    
    result = run_interactive_step('control_repo')
    
    assert result is False
    
    state = SetupState()
    assert state.is_skipped('control_repo')


def test_later_control_repo_step(temp_setup_state, monkeypatch):
    """Test that control_repo can be deferred with 'later'."""
    monkeypatch.setattr('builtins.input', lambda _: 'later')
    
    result = run_interactive_step('control_repo')
    
    assert result is False
    
    state = SetupState()
    assert state.is_skipped('control_repo')


def test_skip_orchestrator_repo_step(temp_setup_state, monkeypatch):
    """Test that orchestrator_repo can be skipped."""
    monkeypatch.setattr('builtins.input', lambda _: 'skip')
    
    result = run_interactive_step('orchestrator_repo')
    
    assert result is False
    
    state = SetupState()
    assert state.is_skipped('orchestrator_repo')


def test_later_orchestrator_repo_step(temp_setup_state, monkeypatch):
    """Test that orchestrator_repo can be deferred with 'later'."""
    monkeypatch.setattr('builtins.input', lambda _: 'later')
    
    result = run_interactive_step('orchestrator_repo')
    
    assert result is False
    
    state = SetupState()
    assert state.is_skipped('orchestrator_repo')


def test_skip_openclaw_executable_step(temp_setup_state, monkeypatch):
    """Test that openclaw_executable can be skipped."""
    monkeypatch.setattr('builtins.input', lambda _: 'skip')
    
    result = run_interactive_step('openclaw_executable')
    
    assert result is False
    
    state = SetupState()
    assert state.is_skipped('openclaw_executable')


def test_later_openclaw_executable_step(temp_setup_state, monkeypatch):
    """Test that openclaw_executable can be deferred with 'later'."""
    monkeypatch.setattr('builtins.input', lambda _: 'later')
    
    result = run_interactive_step('openclaw_executable')
    
    assert result is False
    
    state = SetupState()
    assert state.is_skipped('openclaw_executable')


def test_skip_is_case_insensitive(temp_setup_state, monkeypatch):
    """Test that 'SKIP', 'Skip', etc. all work."""
    monkeypatch.setattr('builtins.input', lambda _: 'SKIP')
    
    result = run_interactive_step('base_dir')
    
    assert result is False
    
    state = SetupState()
    assert state.is_skipped('base_dir')


def test_later_is_case_insensitive(temp_setup_state, monkeypatch):
    """Test that 'LATER', 'Later', etc. all work."""
    monkeypatch.setattr('builtins.input', lambda _: 'LATER')
    
    result = run_interactive_step('base_dir')
    
    assert result is False
    
    state = SetupState()
    assert state.is_skipped('base_dir')
