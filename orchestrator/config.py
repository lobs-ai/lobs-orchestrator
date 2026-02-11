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

# Central output directories (within control repo)
RESEARCH_DIR = (CONTROL_REPO_PATH / "state" / "research").resolve()
REPORTS_DIR = (CONTROL_REPO_PATH / "state" / "reports").resolve()
DESIGNS_DIR = (CONTROL_REPO_PATH / "state" / "designs").resolve()
PIPELINES_DIR = (CONTROL_REPO_PATH / "state" / "pipelines").resolve()

# Polling interval in seconds
POLL_INTERVAL = get_setting("poll_interval", 10)

# Maximum number of concurrent workers (default: 3)
# Controls resource usage and API costs by limiting how many workers can run simultaneously
MAX_WORKERS = get_setting("max_concurrent_workers", 3)

# Worker health monitoring timeouts (in seconds)
WORKER_WARNING_TIMEOUT = get_setting("worker_warning_timeout_sec", 1800)  # 30 minutes
WORKER_KILL_TIMEOUT = get_setting("worker_kill_timeout_sec", 3600)  # 1 hour
WORKER_HEARTBEAT_TIMEOUT = get_setting("worker_heartbeat_timeout_sec", 300)  # 5 minutes

# State directory (for compatibility; runtime state is in orchestrator repo)
STATE_DIR = (ORCHESTRATOR_REPO_PATH / "state").resolve()

# Recurring scheduler state lives in control repo so it is restart-safe and shared across machines.
RECURRING_STATE_FILE = (CONTROL_REPO_PATH / "state" / "recurring-state.json").resolve()

PROJECT_CONTEXT_FILES = ["PRODUCT.md", "UX_PRINCIPLES.md", "ARCHITECTURE.md"]
