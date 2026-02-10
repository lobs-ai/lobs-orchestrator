"""Tests for prerequisite skip functionality."""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestrator.utils.executables import which, check_node_available


@pytest.fixture
def temp_settings(tmp_path, monkeypatch):
    """Create a temporary settings file and configure it."""
    settings_file = tmp_path / ".lobs_settings.json"
    monkeypatch.setattr("orchestrator.utils.settings.SETTINGS_FILE", settings_file)
    return settings_file


def write_settings(settings_file: Path, data: dict):
    """Helper to write settings JSON."""
    with open(settings_file, "w") as f:
        json.dump(data, f)


def test_which_normal_detection():
    """Test that which() works normally when tool is in PATH."""
    # Test with a tool that should always be available
    result = which("python3")
    assert result is not None
    assert "python3" in result


def test_which_skip_all(temp_settings):
    """Test that skip_prereqs=true bypasses all checks."""
    write_settings(temp_settings, {"skip_prereqs": True})
    
    # Should return a fake path for any executable
    result = which("nonexistent-tool-xyz")
    assert result is not None
    assert "nonexistent-tool-xyz" in result


def test_which_skip_specific(temp_settings):
    """Test that skip_prereqs_list bypasses specific tools."""
    write_settings(temp_settings, {"skip_prereqs_list": ["node", "gh"]})
    
    # Should skip node
    result = which("node")
    assert result is not None
    
    # Should skip gh
    result = which("gh")
    assert result is not None
    
    # Should NOT skip other tools (will return None if not found)
    result = which("totally-fake-tool-12345")
    assert result is None


def test_which_no_skip(temp_settings):
    """Test that without skip settings, detection works normally."""
    write_settings(temp_settings, {})
    
    # Should return None for nonexistent tool
    result = which("nonexistent-tool-xyz")
    assert result is None


def test_check_node_available_skip_all(temp_settings):
    """Test that check_node_available respects skip_prereqs."""
    write_settings(temp_settings, {"skip_prereqs": True})
    
    # Should return True even if node isn't available
    result = check_node_available()
    assert result is True


def test_check_node_available_skip_specific(temp_settings):
    """Test that check_node_available respects skip_prereqs_list."""
    write_settings(temp_settings, {"skip_prereqs_list": ["node"]})
    
    # Should return True when node is in skip list
    result = check_node_available()
    assert result is True


def test_skip_list_not_list(temp_settings):
    """Test that non-list skip_prereqs_list is handled gracefully."""
    write_settings(temp_settings, {"skip_prereqs_list": "not-a-list"})
    
    # Should not crash, just treat as no skip
    result = which("node")
    # Result depends on whether node is actually installed
    assert isinstance(result, (str, type(None)))
