import json
import shutil
from pathlib import Path
from typing import Any

SETTINGS_FILE = Path(".lobs_settings.json")

def load_settings() -> dict[str, Any]:
    if not SETTINGS_FILE.exists():
        return {}
    try:
        with open(SETTINGS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_settings(settings: dict[str, Any]) -> None:
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)

def get_setting(key: str, default: Any = None) -> Any:
    return load_settings().get(key, default)

def set_setting(key: str, value: Any) -> None:
    settings = load_settings()
    settings[key] = value
    save_settings(settings)

def reset_settings() -> dict[str, bool]:
    """
    Reset all settings and state for fresh onboarding.
    Returns dict of what was reset.
    """
    results = {}
    
    # Reset settings file
    if SETTINGS_FILE.exists():
        SETTINGS_FILE.unlink()
        results["settings_file"] = True
    else:
        results["settings_file"] = False
    
    # Reset setup state file
    setup_state_file = Path(".lobs_setup_state.json")
    if setup_state_file.exists():
        setup_state_file.unlink()
        results["setup_state_file"] = True
    else:
        results["setup_state_file"] = False
    
    # Reset orchestrator state directory
    state_dir = Path("state")
    if state_dir.exists() and state_dir.is_dir():
        # Clear all JSON files in state directory
        state_files_removed = []
        for state_file in state_dir.glob("*.json"):
            state_file.unlink()
            state_files_removed.append(state_file.name)
        results["state_files"] = state_files_removed
    else:
        results["state_files"] = []
    
    # Reset worker state (OpenClaw)
    worker_state_file = Path.home() / ".openclaw" / "worker-state.json"
    if worker_state_file.exists():
        worker_state_file.unlink()
        results["worker_state"] = True
    else:
        results["worker_state"] = False
    
    return results
