import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def parse_worker_result(task_id: str, results_dir: Path) -> dict[str, Any] | None:
    """
    Parses worker output from the results directory.
    Expected to find task_id.md or task_id.json.
    """
    json_path = results_dir / f"{task_id}.json"
    if json_path.exists():
        try:
            with open(json_path, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to parse result JSON for {task_id}: {e}")

    return None
