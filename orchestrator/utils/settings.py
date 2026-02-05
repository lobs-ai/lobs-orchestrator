import json
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
