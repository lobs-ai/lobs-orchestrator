import sys
from unittest.mock import patch
from orchestrator.utils.cli import parse_args, apply_args
from orchestrator.utils.settings import get_setting

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
