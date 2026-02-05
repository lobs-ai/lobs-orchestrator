import time
import logging
from .config import POLL_INTERVAL, LOCKS_DIR
from .control import ControlManager
from .worker import WorkerManager
from .scanner import Scanner
from .reconciler import Reconciler

logger = logging.getLogger(__name__)

class Orchestrator:
    def __init__(self):
        self.control = ControlManager()
        self.worker_manager = WorkerManager(LOCKS_DIR)
        self.scanner = Scanner()
        self.reconciler = Reconciler()
        self.last_reconcile = 0
        self.reconcile_interval = 300 # 5 minutes

    def run_once(self):
        # 1. Process pending control operations (exactly one writer pattern)
        self.control.process_ops()

        # 2. Check active workers and release locks
        self.worker_manager.check_workers()

        # 3. Periodic reconciliation (Self-healing)
        now = time.time()
        if now - self.last_reconcile > self.reconcile_interval:
            logger.info("Starting periodic reconciliation...")
            # Ideally we'd discover projects dynamically
            self.reconciler.reconcile(["lobs-dashboard", "flock"]) 
            self.last_reconcile = now

        # 4. Scan for new work
        facts = self.scanner.scan()

        # 4. Decision logic
        # Handle explicit worker requests from dashboard
        if facts["pending_request"]:
            logger.info("Worker request detected from dashboard.")
            # Consume the request
            try:
                (CONTROL_REPO_PATH / "state" / "worker-request.json").unlink()
                logger.info("Consumed worker-request.json")
            except FileNotFoundError:
                pass

        eligible_tasks = facts["eligible_tasks"]
        for task in eligible_tasks:
            project_id = task.get("projectId", "default")
            task_id = task["id"]
            
            if not self.worker_manager.is_domain_locked(project_id):
                logger.info(f"Assigning task {task_id} to project {project_id}")
                self.worker_manager.spawn_worker(task, project_id)
                
                # Update task state to in_progress
                ControlManager.request_op({
                    "type": "update_task",
                    "task_id": task_id,
                    "updates": {"workState": "in_progress"}
                })
                
                # If we handled a request, we might want to consume it.
                # For now, one worker at a time per domain.
                # We only spawn one worker per run_once to avoid overwhelming.
                break 

    def loop(self):
        logger.info("Orchestrator loop started.")
        while True:
            try:
                self.run_once()
            except Exception as e:
                logger.error(f"Error in orchestrator loop: {e}", exc_info=True)
            
            time.sleep(POLL_INTERVAL)
