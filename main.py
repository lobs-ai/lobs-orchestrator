import logging
import sys
from orchestrator.engine import Orchestrator

def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout)
        ]
    )

if __name__ == "__main__":
    setup_logging()
    orchestrator = Orchestrator()
    try:
        orchestrator.loop()
    except KeyboardInterrupt:
        logging.info("Orchestrator stopped by user.")
        sys.exit(0)
