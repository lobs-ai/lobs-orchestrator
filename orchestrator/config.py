import os
from pathlib import Path

# Base directory for all lobs related repos
BASE_DIR = Path("/Users/rafe/other/lobs")

# Repositories
CONTROL_REPO_PATH = BASE_DIR / "lobs-control"
ORCHESTRATOR_REPO_PATH = BASE_DIR / "lobs-orchestrator"

# State files within control repo
TASKS_DIR = CONTROL_REPO_PATH / "state" / "tasks"
TASKS_FILE = CONTROL_REPO_PATH / "state" / "tasks.json" # Derived/legacy
WORKER_STATUS_JSON = CONTROL_REPO_PATH / "state" / "worker-status.json"
WORKER_HISTORY_JSON = CONTROL_REPO_PATH / "state" / "worker-history.json"
WORKER_EVENTS_JSONL = CONTROL_REPO_PATH / "state" / "worker-events.jsonl"
CONTROL_OPS_DIR = CONTROL_REPO_PATH / "state" / "control-ops"
WORKER_RESULTS_DIR = CONTROL_REPO_PATH / "state" / "worker-results"

# Project context files (expected in project root)
PROJECT_CONTEXT_FILES = ["PRODUCT.md", "UX_PRINCIPLES.md", "ARCHITECTURE.md"]

# Polling interval in seconds
POLL_INTERVAL = 10 # Reduced for better responsiveness

# Domain locks directory
LOCKS_DIR = ORCHESTRATOR_REPO_PATH / "locks"
