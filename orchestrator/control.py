import json
import os
import subprocess
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any
from .config import CONTROL_REPO_PATH, TASKS_FILE

logger = logging.getLogger(__name__)

class ControlManager:
    """
    Exactly one writer for the control repo.
    Reads requests from state/control-ops/, applies them, and pushes.
    """
    def __init__(self):
        from .config import CONTROL_OPS_DIR
        self.ops_dir = CONTROL_OPS_DIR
        self.ops_dir.mkdir(parents=True, exist_ok=True)

    def pull(self):
        try:
            subprocess.run(["git", "pull", "--rebase"], cwd=CONTROL_REPO_PATH, check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            logger.error(f"Git pull failed: {e.stderr.decode()}")

    def push(self, message: str):
        try:
            subprocess.run(["git", "add", "."], cwd=CONTROL_REPO_PATH, check=True, capture_output=True)
            # Check if there are changes to commit
            status = subprocess.run(["git", "status", "--porcelain"], cwd=CONTROL_REPO_PATH, check=True, capture_output=True).stdout.decode()
            if not status:
                return
            
            subprocess.run(["git", "commit", "-m", message], cwd=CONTROL_REPO_PATH, check=True, capture_output=True)
            subprocess.run(["git", "push"], cwd=CONTROL_REPO_PATH, check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            logger.error(f"Git push failed: {e.stderr.decode()}")

    def process_ops(self):
        ops_files = sorted(self.ops_dir.glob("*.json"))
        if not ops_files:
            return

        # Ensure we have the latest control state before processing
        self.pull()
        
        applied_ops = []
        for op_file in ops_files:
            try:
                with open(op_file, 'r') as f:
                    op = json.load(f)
                
                self.apply_op(op)
                applied_ops.append(op_file)
            except Exception as e:
                logger.error(f"Failed to apply op {op_file}: {e}")

        if applied_ops:
            for op_file in applied_ops:
                op_file.unlink()
            
            # Commit and push control state updates
            self.push(f"lobs: apply {len(applied_ops)} control ops")

    def apply_op(self, op: Dict[str, Any]):
        op_type = op.get("type")
        if op_type == "update_task":
            self._update_task(op["task_id"], op["updates"])
        elif op_type == "update_worker_status":
            self._update_worker_status(op["updates"])
        else:
            logger.warning(f"Unknown op type: {op_type}")

    def _update_task(self, task_id: str, updates: Dict[str, Any]):
        from .config import TASKS_DIR, TASKS_FILE
        
        # 1. Update individual task file (Preferred)
        task_path = TASKS_DIR / f"{task_id}.json"
        if task_path.exists():
            with open(task_path, 'r') as f:
                task = json.load(f)
            
            task.update(updates)
            task["updatedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            
            with open(task_path, 'w') as f:
                json.dump(task, f, indent=2)
                f.write("\n")
            logger.info(f"Updated task file {task_path}")
        
        # 2. Update legacy aggregate file (Compatibility)
        if TASKS_FILE.exists():
            with open(TASKS_FILE, 'r') as f:
                data = json.load(f)
            
            found = False
            for task in data.get("tasks", []):
                if task["id"] == task_id:
                    task.update(updates)
                    task["updatedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    found = True
                    break
            
            if found:
                with open(TASKS_FILE, 'w') as f:
                    json.dump(data, f, indent=2)
                    f.write("\n")
                logger.info(f"Updated legacy tasks file {TASKS_FILE}")

    def _update_worker_status(self, updates: Dict[str, Any]):
        from .config import WORKER_STATUS_JSON
        if not WORKER_STATUS_JSON.exists():
            status = {}
        else:
            with open(WORKER_STATUS_JSON, 'r') as f:
                status = json.load(f)
        
        status.update(updates)
        with open(WORKER_STATUS_JSON, 'w') as f:
            json.dump(status, f, indent=2)

    @staticmethod
    def request_op(op: Dict[str, Any]):
        """
        Static method for other components to request an operation.
        """
        from .config import CONTROL_OPS_DIR
        CONTROL_OPS_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = int(time.time() * 1000)
        op_file = CONTROL_OPS_DIR / f"op_{timestamp}.json"
        with open(op_file, 'w') as f:
            json.dump(op, f, indent=2)
            f.write("\n")
