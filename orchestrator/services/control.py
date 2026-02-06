import json
import subprocess
import time
import logging
import random
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.config import (
    CONTROL_REPO_PATH,
    CONTROL_OPS_DIR,
    PROJECTS_FILE,
    TASKS_DIR,
    TASKS_FILE,
    WORKER_STATUS_JSON
)

logger = logging.getLogger(__name__)


class ControlManager:
    """
    Exactly one writer for the control repo.
    Reads requests from state/control-ops/, applies them, and pushes.
    """

    @property
    def ops_dir(self) -> Path:
        return CONTROL_OPS_DIR

    def pull(self) -> None:
        try:
            subprocess.run(
                ["git", "pull", "--rebase"],
                cwd=CONTROL_REPO_PATH,
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as e:
            logger.error(f"Git pull failed: {e.stderr.decode()}")

    def push(self, message: str) -> None:
        try:
            # Check if there are changes to commit
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=CONTROL_REPO_PATH,
                check=True,
                capture_output=True,
            ).stdout.decode().strip()
            
            if not status:
                return

            subprocess.run(
                ["git", "add", "."],
                cwd=CONTROL_REPO_PATH,
                check=True,
                capture_output=True,
            )

            subprocess.run(
                ["git", "commit", "-m", message],
                cwd=CONTROL_REPO_PATH,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "push"], cwd=CONTROL_REPO_PATH, check=True, capture_output=True
            )
        except subprocess.CalledProcessError as e:
            logger.error(f"Git push failed: {e.stderr.decode()}")

    def process_ops(self) -> list[dict[str, Any]]:
        self.ops_dir.mkdir(parents=True, exist_ok=True)
        ops_files = sorted(self.ops_dir.glob("*.json"))
        if not ops_files:
            return []

        # Ensure we have the latest control state before processing
        self.pull()

        applied_ops = []
        messages = []
        for op_file in ops_files:
            try:
                with open(op_file, "r") as f:
                    op = json.load(f)

                if op.get("type") == "message":
                    messages.append(op)
                else:
                    self.apply_op(op)
                applied_ops.append(op_file)
            except Exception as e:
                logger.error(f"Failed to apply op {op_file}: {e}")

        if applied_ops:
            for op_file in applied_ops:
                op_file.unlink()

            # Commit and push control state updates
            self.push(f"lobs: apply {len(applied_ops)} control ops")
        
        return messages

    def apply_op(self, op: dict[str, Any]) -> None:
        op_type = op.get("type")
        if op_type == "update_task":
            self._update_item(op["task_id"], op["updates"], kind=op.get("kind", "task"))
        elif op_type == "update_project":
            self._update_project(op["project_id"], op["updates"])
        elif op_type == "update_worker_status":
            self._update_worker_status(op["updates"])
        elif op_type == "update_alert":
            self._update_alert(op["alert_id"], op["updates"])
        elif op_type == "update_inbox_item":
            self._update_inbox_item(op["item_id"], op["updates"])
        elif op_type == "add_inbox_item":
            self._add_inbox_item(op["item"])
        else:
            logger.warning(f"Unknown op type: {op_type}")

    def _update_item(self, item_id: str, updates: dict[str, Any], kind: str = "task") -> None:
        from orchestrator.config import CONTROL_REPO_PATH, TASKS_DIR, TASKS_FILE
        
        item_path = None
        if kind == "task":
            item_path = TASKS_DIR / f"{item_id}.json"
        elif kind == "research_request":
            # Research requests are in state/research/*/requests/*.json
            research_dir = CONTROL_REPO_PATH / "state" / "research"
            for req_dir in research_dir.glob("*/requests"):
                p = req_dir / f"{item_id}.json"
                if p.exists():
                    item_path = p
                    break
        elif kind == "inbox_response":
            item_path = CONTROL_REPO_PATH / "state" / "inbox-responses" / f"{item_id}.json"
            if not item_path.exists():
                # Check rglob for nested inbox-responses
                for p in (CONTROL_REPO_PATH / "state" / "inbox-responses").rglob(f"{item_id}.json"):
                    item_path = p
                    break
        
        if item_path and item_path.exists():
            try:
                with open(item_path, "r") as f:
                    data = json.load(f)

                data.update(updates)
                data["updatedAt"] = datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )

                with open(item_path, "w") as f:
                    json.dump(data, f, indent=2)
                    f.write("\n")
                logger.info(f"Updated {kind} file {item_path}")
            except Exception as e:
                logger.error(f"Failed to update {kind} item {item_id}: {e}")

        # Fallback for legacy tasks.json
        if kind == "task" and TASKS_FILE.exists():
            try:
                with open(TASKS_FILE, "r") as f:
                    data = json.load(f)

                found = False
                for task in data.get("tasks", []):
                    if task["id"] == item_id:
                        task.update(updates)
                        task["updatedAt"] = datetime.now(timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        )
                        found = True
                        break

                if found:
                    with open(TASKS_FILE, "w") as f:
                        json.dump(data, f, indent=2)
                        f.write("\n")
                    logger.info(f"Updated legacy tasks file {TASKS_FILE}")
            except Exception as e:
                logger.error(f"Failed to update legacy tasks file: {e}")

    def _update_worker_status(self, updates: dict[str, Any]) -> None:
        from orchestrator.config import WORKER_STATUS_JSON

        if not WORKER_STATUS_JSON.exists():
            status = {}
        else:
            with open(WORKER_STATUS_JSON, "r") as f:
                status = json.load(f)

        status.update(updates)
        with open(WORKER_STATUS_JSON, "w") as f:
            json.dump(status, f, indent=2)

    @staticmethod
    def request_op(op: dict[str, Any]) -> None:
        """
        Static method for other components to request an operation.
        """
        from orchestrator.config import CONTROL_OPS_DIR
        import random
        import string

        CONTROL_OPS_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = int(time.time() * 1000)
        rand = ''.join(random.choices(string.ascii_lowercase + string.digits, k=4))
        op_file = CONTROL_OPS_DIR / f"op_{timestamp}_{rand}.json"
        with open(op_file, "w") as f:
            json.dump(op, f, indent=2)
            f.write("\n")
