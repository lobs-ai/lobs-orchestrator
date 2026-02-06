import json
from orchestrator.utils.settings import load_settings, save_settings, get_setting, set_setting

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
