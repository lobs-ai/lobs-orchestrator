import argparse
import logging
import sys
from orchestrator.utils.settings import set_setting, get_setting, reset_settings

def parse_args():
    parser = argparse.ArgumentParser(description="Lobs Orchestrator")
    parser.add_argument("--provider", help="Set the task provider (e.g., local)")
    parser.add_argument("--base-dir", help="Set the base directory for repositories")
    parser.add_argument("--control-repo", help="Set the path to the control repository")
    parser.add_argument("--orchestrator-repo", help="Set the path to the orchestrator repository")
    parser.add_argument("--poll-interval", type=int, help="Polling interval in seconds")
    parser.add_argument("--openclaw-executable", help="Path to openclaw executable")
    parser.add_argument("--max-concurrent-workers", type=int, help="Maximum concurrent workers (default: 3)")

    # Ollama settings
    parser.add_argument("--ollama-url", help="Ollama API URL (default: http://localhost:11434)")
    parser.add_argument("--ollama-model", help="Ollama model to use (e.g., llama3.1, codellama)")
    parser.add_argument("--ollama-keep-alive", help="How long to keep model loaded (e.g., 5m, 1h, -1)")

    # Setup and configuration
    parser.add_argument("--show", action="store_true", help="Show current settings")
    parser.add_argument("--setup-status", action="store_true", help="Show setup status and what's configured vs missing")
    parser.add_argument("--setup-wizard", action="store_true", help="Run interactive setup wizard")
    parser.add_argument("--configure", choices=["base_dir", "control_repo", "orchestrator_repo", "openclaw_executable"], 
                        help="Configure a specific setup step")
    parser.add_argument("--reset", action="store_true", help="Reset all settings and state for fresh onboarding")

    return parser.parse_args()

def show_settings():
    from orchestrator import config
    from orchestrator.utils.executables import which, get_node_version
    
    print("\nCurrent Configuration:")
    print(f"  Provider:             {get_setting('provider', 'local')}")
    print(f"  Base Directory:       {config.BASE_DIR}")
    print(f"  Control Repo:         {config.CONTROL_REPO_PATH}")
    print(f"  Orchestrator Repo:    {config.ORCHESTRATOR_REPO_PATH}")
    print(f"  Poll Interval:        {config.POLL_INTERVAL}s")
    print(f"  Max Concurrent Workers: {config.MAX_WORKERS}")
    print(f"  Worker State:         ~/.openclaw/worker-state.json")
    print(f"  OpenClaw Executable:  {get_setting('openclaw_executable', 'openclaw')}")
    print("")
    
    print("Prerequisites:")
    skip_all = get_setting("skip_prereqs", False)
    skip_list = get_setting("skip_prereqs_list", [])
    strict_mode = get_setting("strict_prereqs", False)
    
    if skip_all:
        print(f"  Mode:                 ⚠️  Skip all checks (skip_prereqs: true)")
    elif skip_list:
        print(f"  Mode:                 ⚠️  Skip specific: {', '.join(skip_list)}")
    elif strict_mode:
        print(f"  Mode:                 🔒 Strict (fail on missing tools)")
    else:
        print(f"  Mode:                 ✨ Graceful degradation (default)")
    print("")
    
    print("Node.js Configuration:")
    node_path = which('node')
    if node_path:
        node_version = get_node_version()
        print(f"  Node.js:              ✅ {node_version or 'found'}")
        print(f"  Path:                 {node_path}")
        
        npm_path = which('npm')
        if npm_path:
            print(f"  npm:                  ✅ found at {npm_path}")
        else:
            print(f"  npm:                  ⚠️  Not found - install with Node.js")
    else:
        print(f"  Node.js:              ⚠️  Not detected")
        print(f"  Note:                 JavaScript/TypeScript features disabled")
        print(f"  Install:              https://nodejs.org")
    print("")
    
    print("Ollama Configuration:")
    print(f"  Ollama URL:           {get_setting('ollama_url', 'http://localhost:11434')}")
    print(f"  Ollama Model:         {get_setting('ollama_model', 'llama3.1')}")
    print(f"  Keep Alive:           {get_setting('ollama_keep_alive', '5m')}")

    # Check if Ollama is available (optional - requires requests module)
    try:
        from orchestrator.core.ollama_client import OllamaClient
        client = OllamaClient(
            base_url=get_setting('ollama_url', 'http://localhost:11434'),
            model=get_setting('ollama_model', 'llama3.1')
        )
        if client.is_available():
            models = client.list_models()
            print(f"  Status:               ✅ Running")
            print(f"  Available Models:     {', '.join(models) if models else 'None'}")
        else:
            print(f"  Status:               ❌ Not running")
            print(f"  Note:                 Install from https://ollama.com")
    except ImportError:
        print(f"  Status:               (install 'requests' to check)")
    except Exception as e:
        print(f"  Status:               Error checking: {str(e)[:50]}")
    print("")

def apply_args(args):
    # Update settings if provided via CLI
    if args.provider:
        set_setting("provider", args.provider)
        logging.info(f"Provider set to: {args.provider}")
    
    if args.base_dir:
        set_setting("base_dir", args.base_dir)
        logging.info(f"Base directory set to: {args.base_dir}")

    if args.control_repo:
        set_setting("control_repo_path", args.control_repo)
        logging.info(f"Control repo path set to: {args.control_repo}")

    if args.orchestrator_repo:
        set_setting("orchestrator_repo_path", args.orchestrator_repo)
        logging.info(f"Orchestrator repo path set to: {args.orchestrator_repo}")

    if args.poll_interval:
        set_setting("poll_interval", args.poll_interval)
        logging.info(f"Poll interval set to: {args.poll_interval}")

    if args.openclaw_executable:
        set_setting("openclaw_executable", args.openclaw_executable)
        logging.info(f"OpenClaw executable set to: {args.openclaw_executable}")

    if args.max_concurrent_workers:
        set_setting("max_concurrent_workers", args.max_concurrent_workers)
        logging.info(f"Max concurrent workers set to: {args.max_concurrent_workers}")

    # Ollama settings
    if args.ollama_url:
        set_setting("ollama_url", args.ollama_url)
        logging.info(f"Ollama URL set to: {args.ollama_url}")

    if args.ollama_model:
        set_setting("ollama_model", args.ollama_model)
        logging.info(f"Ollama model set to: {args.ollama_model}")

    if args.ollama_keep_alive:
        set_setting("ollama_keep_alive", args.ollama_keep_alive)
        logging.info(f"Ollama keep_alive set to: {args.ollama_keep_alive}")

def setup_orchestrator():
    args = parse_args()
    
    # Handle reset first (exits after completion)
    if args.reset:
        perform_reset()
        sys.exit(0)
    
    # Handle setup commands (exit after completion)
    if args.setup_status:
        from orchestrator.utils.setup_wizard import show_setup_status
        show_setup_status()
        sys.exit(0)
    
    if args.setup_wizard:
        from orchestrator.utils.setup_wizard import run_setup_wizard
        run_setup_wizard()
        sys.exit(0)
    
    if args.configure:
        from orchestrator.utils.setup_wizard import configure_step
        configure_step(args.configure)
        sys.exit(0)
    
    apply_args(args)
    if args.show:
        show_settings()
        sys.exit(0)
    return args

def perform_reset():
    """Reset all settings and state for fresh onboarding."""
    print("\n🔄 Resetting Lobs Orchestrator Settings & State")
    print("=" * 50)
    print("\nThis will delete:")
    print("  • .lobs_settings.json")
    print("  • .lobs_setup_state.json")
    print("  • state/*.json (orchestrator state)")
    print("  • ~/.openclaw/worker-state.json")
    print("")
    
    confirm = input("Are you sure you want to reset? [y/N]: ").strip().lower()
    if confirm not in ('y', 'yes'):
        print("\n❌ Reset cancelled.")
        return
    
    print("\n⏳ Resetting...")
    results = reset_settings()
    
    print("\n✅ Reset Complete\n")
    print("Results:")
    
    # Settings file
    if results.get("settings_file"):
        print("  ✅ Deleted .lobs_settings.json")
    else:
        print("  ℹ️  No .lobs_settings.json found")
    
    # Setup state file
    if results.get("setup_state_file"):
        print("  ✅ Deleted .lobs_setup_state.json")
    else:
        print("  ℹ️  No .lobs_setup_state.json found")
    
    # State files
    state_files = results.get("state_files", [])
    if state_files:
        print(f"  ✅ Deleted {len(state_files)} state file(s):")
        for fname in state_files:
            print(f"     • {fname}")
    else:
        print("  ℹ️  No state files found")
    
    # Worker state
    if results.get("worker_state"):
        print("  ✅ Deleted ~/.openclaw/worker-state.json")
    else:
        print("  ℹ️  No worker state found")
    
    print("\n🎉 Orchestrator reset to fresh state.")
    print("You can now run setup wizard with:")
    print("  python3 main.py --setup-wizard")
    print("")
