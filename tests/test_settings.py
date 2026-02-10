import json
from pathlib import Path
from orchestrator.utils.settings import load_settings, save_settings, get_setting, set_setting, reset_settings

def test_load_settings_empty(temp_settings):
    """Test loading settings when the file does not exist."""
    assert load_settings() == {}

def test_save_and_load_settings(temp_settings):
    """Test saving and then loading settings."""
    settings = {"key1": "value1", "key2": 123}
    save_settings(settings)
    assert load_settings() == settings

def test_get_setting_default(temp_settings):
    """Test get_setting with a default value when key is missing."""
    assert get_setting("missing_key", "default_val") == "default_val"
    assert get_setting("missing_key") is None

def test_set_and_get_setting(temp_settings):
    """Test setting a value and then getting it."""
    set_setting("new_key", "new_value")
    assert get_setting("new_key") == "new_value"

def test_load_settings_invalid_json(temp_settings):
    """Test loading settings when the file contains invalid JSON."""
    temp_settings.write_text("invalid json")
    assert load_settings() == {}

def test_reset_settings(temp_settings, tmp_path):
    """Test reset_settings deletes settings file and state files."""
    # Create settings file
    set_setting("test_key", "test_value")
    assert temp_settings.exists()
    
    # Create state directory and files
    state_dir = Path("state")
    state_dir.mkdir(exist_ok=True)
    state_file1 = state_dir / "test-state.json"
    state_file2 = state_dir / "another-state.json"
    state_file1.write_text('{"test": "data"}')
    state_file2.write_text('{"more": "data"}')
    
    # Run reset
    results = reset_settings()
    
    # Verify settings file deleted
    assert results["settings_file"] is True
    assert not temp_settings.exists()
    
    # Verify our test state files were included in deletion
    assert "test-state.json" in results["state_files"]
    assert "another-state.json" in results["state_files"]
    assert not state_file1.exists()
    assert not state_file2.exists()
    
    # Worker state check (may not exist in test environment)
    assert "worker_state" in results

def test_reset_settings_no_files(temp_settings):
    """Test reset_settings when no files exist."""
    # Ensure settings file doesn't exist
    if temp_settings.exists():
        temp_settings.unlink()
    
    results = reset_settings()
    
    # Should report nothing was deleted
    assert results["settings_file"] is False
    assert results["state_files"] == []
    assert results["worker_state"] is False
