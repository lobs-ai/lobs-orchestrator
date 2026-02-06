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
    try:
        orchestrator.loop()
    except KeyboardInterrupt:
        logging.info("Orchestrator stopped by user.")
        sys.exit(0)


if __name__ == "__main__":
    main()
