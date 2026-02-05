from pathlib import Path
from .utils.settings import get_setting

# Base directory for all lobs related repos
BASE_DIR = Path(get_setting("base_dir", "/Users/rafe/other/lobs"))

# Repositories
CONTROL_REPO_PATH = Path(get_setting("control_repo_path", str(BASE_DIR / "lobs-control")))
ORCHESTRATOR_REPO_PATH = Path(get_setting("orchestrator_repo_path", "/Users/rafe/other/lobs/lobs-orchestrator"))

# State files within control repo
TASKS_DIR = CONTROL_REPO_PATH / "state" / "tasks"
TASKS_FILE = CONTROL_REPO_PATH / "state" / "tasks.json"
WORKER_STATUS_JSON = CONTROL_REPO_PATH / "state" / "worker-status.json"
CONTROL_OPS_DIR = CONTROL_REPO_PATH / "state" / "control-ops"
WORKER_RESULTS_DIR = CONTROL_REPO_PATH / "state" / "worker-results"
PROJECTS_FILE = CONTROL_REPO_PATH / "state" / "projects.json"

# Polling interval in seconds
POLL_INTERVAL = get_setting("poll_interval", 10)

# Domain locks directory
LOCKS_DIR = ORCHESTRATOR_REPO_PATH / "locks"

PROJECT_CONTEXT_FILES = ["PRODUCT.md", "UX_PRINCIPLES.md", "ARCHITECTURE.md"]
