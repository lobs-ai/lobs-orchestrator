import pytest
import json
from pathlib import Path
from orchestrator.utils.settings import SETTINGS_FILE

@pytest.fixture
def temp_settings(tmp_path, monkeypatch):
    """Fixture to use a temporary settings file for testing."""
    test_settings_file = tmp_path / ".lobs_settings.json"
    # Monkeypatch the SETTINGS_FILE in the settings module
    monkeypatch.setattr("orchestrator.utils.settings.SETTINGS_FILE", test_settings_file)
    return test_settings_file

@pytest.fixture
def temp_control_repo(tmp_path, temp_settings, monkeypatch):
    """Fixture to create a temporary control repo structure."""
    control_dir = tmp_path / "lobs-control"
    control_dir.mkdir()
    
    # Create necessary subdirectories
    (control_dir / "state" / "tasks").mkdir(parents=True)
    (control_dir / "state" / "control-ops").mkdir(parents=True)
    (control_dir / "state" / "inbox").mkdir(parents=True)
    (control_dir / "bin").mkdir(parents=True)
    
    # Update settings to point to this temp repo
    set_setting("base_dir", str(tmp_path))
    set_setting("control_repo_path", str(control_dir))
    
    # Reload config to ensure it uses these new settings
    from orchestrator import config
    import importlib
    importlib.reload(config)
    
    # Also reload services that import from config at top level
    import orchestrator.services.control
    importlib.reload(orchestrator.services.control)
    import orchestrator.services.scanner
    importlib.reload(orchestrator.services.scanner)
    import orchestrator.core.worker
    importlib.reload(orchestrator.core.worker)

    # Reload onboarding module (it imports config at module import time)
    import orchestrator.services.github_onboarding
    importlib.reload(orchestrator.services.github_onboarding)

    return control_dir

from orchestrator.utils.settings import set_setting
