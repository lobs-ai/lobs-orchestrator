import sys
from unittest.mock import patch, MagicMock
from orchestrator.utils.cli import parse_args, apply_args, perform_reset
from orchestrator.utils.settings import get_setting, set_setting

def test_parse_args_all_flags():
    """Test that all CLI flags are correctly parsed."""
    test_args = [
        "main.py",
        "--provider", "anthropic",
        "--base-dir", "/tmp/base",
        "--control-repo", "/tmp/control",
        "--orchestrator-repo", "/tmp/orch",
        "--poll-interval", "30",
        "--openclaw-executable", "/usr/local/bin/openclaw"
    ]
    with patch.object(sys, 'argv', test_args):
        args = parse_args()
        assert args.provider == "anthropic"
        assert args.base_dir == "/tmp/base"
        assert args.control_repo == "/tmp/control"
        assert args.orchestrator_repo == "/tmp/orch"
        assert args.poll_interval == 30
        assert args.openclaw_executable == "/usr/local/bin/openclaw"

def test_parse_args_reset_flag():
    """Test that --reset flag is correctly parsed."""
    test_args = ["main.py", "--reset"]
    with patch.object(sys, 'argv', test_args):
        args = parse_args()
        assert args.reset is True

def test_apply_args(temp_settings):
    """Test that apply_args correctly updates settings."""
    class Args:
        provider = "openai"
        base_dir = "/tmp/new_base"
        control_repo = None
        orchestrator_repo = None
        poll_interval = 15
        openclaw_executable = None
        ollama_url = None
        ollama_model = None
        ollama_keep_alive = None
        show = False

    apply_args(Args())
    
    assert get_setting("provider") == "openai"
    assert get_setting("base_dir") == "/tmp/new_base"
    assert get_setting("poll_interval") == 15
    # Non-provided args should not change settings (or remain None/default)
    assert get_setting("control_repo_path") is None

def test_perform_reset_cancelled(temp_settings, monkeypatch, capsys):
    """Test that perform_reset can be cancelled."""
    # Mock user input to cancel
    monkeypatch.setattr('builtins.input', lambda _: 'n')
    
    # Create a settings file
    set_setting("test_key", "test_value")
    
    # Run reset (should cancel)
    perform_reset()
    
    # Verify settings file still exists
    from orchestrator.utils.settings import SETTINGS_FILE
    assert SETTINGS_FILE.exists()
    
    # Check output
    captured = capsys.readouterr()
    assert "Reset cancelled" in captured.out

def test_perform_reset_confirmed(temp_settings, monkeypatch, capsys):
    """Test that perform_reset works when confirmed."""
    # Mock user input to confirm
    monkeypatch.setattr('builtins.input', lambda _: 'y')
    
    # Create a settings file
    set_setting("test_key", "test_value")
    
    # Run reset (should proceed)
    perform_reset()
    
    # Verify settings file deleted
    from orchestrator.utils.settings import SETTINGS_FILE
    assert not SETTINGS_FILE.exists()
    
    # Check output
    captured = capsys.readouterr()
    assert "Reset Complete" in captured.out
    assert "Deleted .lobs_settings.json" in captured.out or "No .lobs_settings.json found" in captured.out
