import logging
import sys
import os

# Ensure the current directory is in sys.path for absolute imports
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from orchestrator.utils.cli import setup_orchestrator, show_settings
from orchestrator.utils.settings import get_setting


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def main():
    setup_logging()

    # Setup settings from CLI and file
    setup_orchestrator()
    show_settings()

    # Check node availability at startup
    from orchestrator.utils.executables import check_node_available, get_node_version, which
    try:
        if check_node_available():
            node_version = get_node_version()
            node_path = which('node')
            logging.info(f"Node.js detected: {node_version} at {node_path}")
        else:
            logging.warning("⚠️  Node.js not detected - JavaScript/TypeScript features disabled")
            logging.warning("    Install from: https://nodejs.org")
            logging.info("    Orchestrator will continue without Node.js features")
    except RuntimeError as e:
        logging.error(f"Fatal: {e}")
        logging.error("Set 'strict_prereqs': false in .lobs_settings.json to allow startup without all tools")
        sys.exit(1)

    # Import core components ONLY AFTER settings are updated
    from orchestrator.core.engine import Orchestrator

    provider_name = get_setting("provider", "local")

    if provider_name == "local":
        from orchestrator.providers.local import LocalTaskProvider

        provider = LocalTaskProvider()
    else:
        logging.error(f"Unknown provider: {provider_name}")
        sys.exit(1)

    orchestrator = Orchestrator(provider)

    # Start local Dashboard API (for repo onboarding, etc.)
    try:
        from orchestrator.api.server import DashboardAPIServer

        DashboardAPIServer().start()
    except Exception as e:
        logging.getLogger(__name__).error(f"Failed to start Dashboard API: {e}")

    try:
        orchestrator.loop()
    except KeyboardInterrupt:
        logging.info("Orchestrator stopped by user.")
        sys.exit(0)


if __name__ == "__main__":
    main()
