import logging
import sys
import os
import signal

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

    # Initialize memory monitoring early to catch startup memory issues
    from orchestrator.utils.memory_monitor import init_memory_monitoring
    from orchestrator.utils.settings import get_setting
    
    # Get memory settings (with sensible defaults for 4GB systems)
    min_free_mb = int(get_setting("memory_min_free_mb_for_spawn", 500))
    critical_free_mb = int(get_setting("memory_critical_free_mb", 200))
    enable_watchdog = bool(get_setting("memory_watchdog_enabled", True))
    watchdog_interval = int(get_setting("memory_watchdog_interval", 30))
    
    memory_monitor = init_memory_monitoring(
        min_free_mb_for_spawn=min_free_mb,
        critical_free_mb=critical_free_mb,
        enable_watchdog=enable_watchdog,
        watchdog_interval=watchdog_interval,
    )

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

    # Register signal handlers for graceful shutdown
    def signal_handler(signum, frame):
        """Handle shutdown signals (SIGTERM, SIGINT)."""
        sig_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
        logging.info(f"Received {sig_name}, initiating graceful shutdown...")
        try:
            orchestrator.shutdown(timeout=300.0)  # 5 minute timeout
            # Stop memory watchdog
            memory_monitor.stop_watchdog()
        except Exception as e:
            logging.error(f"Error during shutdown: {e}", exc_info=True)
        finally:
            sys.exit(0)
    
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Start local Dashboard API (for repo onboarding, etc.)
    try:
        from orchestrator.api.server import DashboardAPIServer

        DashboardAPIServer(orchestrator).start()
    except Exception as e:
        logging.getLogger(__name__).error(f"Failed to start Dashboard API: {e}")

    try:
        orchestrator.loop()
    except KeyboardInterrupt:
        # This should not be reached since SIGINT is caught by signal handler,
        # but keep as fallback
        logging.info("Orchestrator stopped by user.")
        try:
            orchestrator.shutdown(timeout=300.0)
            memory_monitor.stop_watchdog()
        except Exception as e:
            logging.error(f"Error during shutdown: {e}", exc_info=True)
        sys.exit(0)


if __name__ == "__main__":
    main()
