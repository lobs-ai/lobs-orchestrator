import importlib
import os
from pathlib import Path
from orchestrator import config
from orchestrator.utils.settings import set_setting

def test_config_paths_derived_from_base_dir(temp_settings):
    """Test that paths are correctly derived when BASE_DIR is changed."""
    new_base = Path("/tmp/fake_lobs").resolve()
    set_setting("base_dir", str(new_base))
    
    # Reload the config module to pick up the new setting
    importlib.reload(config)
    
    assert config.BASE_DIR == new_base
    assert config.CONTROL_REPO_PATH == new_base / "lobs-control"
    assert config.TASKS_FILE == new_base / "lobs-control" / "state" / "tasks.json"

def test_config_override_control_repo(temp_settings):
    """Test that CONTROL_REPO_PATH can be overridden independently."""
    custom_control = Path("/tmp/custom_control").resolve()
    set_setting("control_repo_path", str(custom_control))
    
    importlib.reload(config)
    
    assert config.CONTROL_REPO_PATH == custom_control
    # TASKS_FILE should now be relative to custom_control
    assert config.TASKS_FILE == custom_control / "state" / "tasks.json"

def test_poll_interval_setting(temp_settings):
    """Test that POLL_INTERVAL setting is respected."""
    set_setting("poll_interval", 5)
    
    importlib.reload(config)
    
    assert config.POLL_INTERVAL == 5
