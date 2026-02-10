from pathlib import Path
from orchestrator.utils.settings import get_setting

# Base directory for all lobs related repos
BASE_DIR = Path(get_setting("base_dir", "/Users/rafe/other/lobs")).resolve()

# Repositories
CONTROL_REPO_PATH = Path(get_setting("control_repo_path", str(BASE_DIR / "lobs-control"))).resolve()
ORCHESTRATOR_REPO_PATH = Path(get_setting("orchestrator_repo_path", str(Path(__file__).parent.parent))).resolve()

# State files within control repo
TASKS_DIR = (CONTROL_REPO_PATH / "state" / "tasks").resolve()
TASKS_FILE = (CONTROL_REPO_PATH / "state" / "tasks.json").resolve()
WORKER_STATUS_JSON = (CONTROL_REPO_PATH / "state" / "worker-status.json").resolve()
CONTROL_OPS_DIR = (CONTROL_REPO_PATH / "state" / "control-ops").resolve()
WORKER_RESULTS_DIR = (CONTROL_REPO_PATH / "state" / "worker-results").resolve()
PROJECTS_FILE = (CONTROL_REPO_PATH / "state" / "projects.json").resolve()

# Polling interval in seconds
POLL_INTERVAL = get_setting("poll_interval", 10)

# Maximum number of concurrent workers
MAX_WORKERS = get_setting("max_workers", 5)

# State directory (for compatibility; worker state is in ~/.openclaw/worker-state.json)
STATE_DIR = (ORCHESTRATOR_REPO_PATH / "state").resolve()

PROJECT_CONTEXT_FILES = ["PRODUCT.md", "UX_PRINCIPLES.md", "ARCHITECTURE.md"]
