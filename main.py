import logging
import sys
import argparse
from orchestrator.engine import Orchestrator
from orchestrator.settings import get_setting, set_setting

def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout)
        ]
    )

def main():
    parser = argparse.ArgumentParser(description="Lobs Orchestrator")
    parser.add_argument("--provider", help="Set the task provider (e.g., local)")
    parser.add_argument("--base-dir", help="Set the base directory for repositories")
    parser.add_argument("--control-repo", help="Set the path to the control repository")
    
    args = parser.parse_args()

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

    setup_logging()
    
    provider_name = get_setting("provider", "local")
    
    if provider_name == "local":
        from orchestrator.providers.local import LocalTaskProvider
        provider = LocalTaskProvider()
    else:
        logging.error(f"Unknown provider: {provider_name}")
        sys.exit(1)
    
    orchestrator = Orchestrator(provider)
    try:
        orchestrator.loop()
    except KeyboardInterrupt:
        logging.info("Orchestrator stopped by user.")
        sys.exit(0)

if __name__ == "__main__":
    main()
