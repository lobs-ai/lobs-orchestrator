import json
import subprocess
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ControlManager:
    """
    Exactly one writer for the control repo.
    Reads requests from state/control-ops/, applies them, and pushes.
    """

    @property
    def ops_dir(self) -> Path:
        from orchestrator.config import CONTROL_OPS_DIR
        return CONTROL_OPS_DIR

    def pull(self) -> None:
        try:
            from orchestrator.config import CONTROL_REPO_PATH
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
            from orchestrator.config import CONTROL_REPO_PATH
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

    def process_ops(self) -> None:
        self.ops_dir.mkdir(parents=True, exist_ok=True)
        ops_files = sorted(self.ops_dir.glob("*.json"))
        if not ops_files:
            return

        # Ensure we have the latest control state before processing
        self.pull()

        applied_ops = []
        for op_file in ops_files:
            try:
                with open(op_file, "r") as f:
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

    def apply_op(self, op: dict[str, Any]) -> None:
        op_type = op.get("type")
        if op_type == "update_task":
            self._update_task(op["task_id"], op["updates"])
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

    def _update_project(self, project_id: str, updates: dict[str, Any]) -> None:
        from orchestrator.config import PROJECTS_FILE
        
        if not PROJECTS_FILE.exists():
            data = {"projects": []}
        else:
            with open(PROJECTS_FILE, "r") as f:
                data = json.load(f)

        found = False
        for project in data.get("projects", []):
            if project["id"] == project_id:
                project.update(updates)
                found = True
                break
        
        if not found:
            if "id" not in updates:
                updates["id"] = project_id
            data["projects"].append(updates)

        with open(PROJECTS_FILE, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        logger.info(f"Updated projects file with {project_id}")

    def _add_inbox_item(self, item: dict[str, Any]) -> None:
        from orchestrator.config import CONTROL_REPO_PATH
        inbox_dir = CONTROL_REPO_PATH / "state" / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        
        timestamp = int(time.time() * 1000)
        item_id = item.get("id", f"inbox_{timestamp}")
        item_path = inbox_dir / f"{item_id}.json"
        
        # Ensure item has basic fields
        if "createdAt" not in item:
            item["createdAt"] = datetime.now(timezone.utc).isoformat()
        
        with open(item_path, "w") as f:
            json.dump(item, f, indent=2)
            f.write("\n")
        logger.info(f"Added inbox item file {item_path}")

    def _update_inbox_item(self, item_id: str, updates: dict[str, Any]) -> None:
        from orchestrator.config import CONTROL_REPO_PATH
        inbox_dir = CONTROL_REPO_PATH / "state" / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        
        item_path = inbox_dir / f"{item_id}.json"
        if item_path.exists():
            with open(item_path, "r") as f:
                item = json.load(f)
        else:
            item = {"id": item_id, "createdAt": datetime.now(timezone.utc).isoformat()}

        item.update(updates)
        item["updatedAt"] = datetime.now(timezone.utc).isoformat()

        with open(item_path, "w") as f:
            json.dump(item, f, indent=2)
            f.write("\n")
        logger.info(f"Updated inbox item file {item_path}")

    def _update_alert(self, alert_id: str, updates: dict[str, Any]) -> None:
        from orchestrator.config import CONTROL_REPO_PATH
        alerts_dir = CONTROL_REPO_PATH / "state" / "alerts"
        alerts_dir.mkdir(parents=True, exist_ok=True)
        
        alert_path = alerts_dir / f"{alert_id}.json"
        if alert_path.exists():
            with open(alert_path, "r") as f:
                alert = json.load(f)
        else:
            alert = {"id": alert_id, "createdAt": datetime.now(timezone.utc).isoformat()}

        alert.update(updates)
        alert["updatedAt"] = datetime.now(timezone.utc).isoformat()

        with open(alert_path, "w") as f:
            json.dump(alert, f, indent=2)
            f.write("\n")
        logger.info(f"Updated alert file {alert_path}")

    def _update_task(self, task_id: str, updates: dict[str, Any]) -> None:
        from orchestrator.config import TASKS_DIR, TASKS_FILE

        # 1. Update individual task file (Preferred)
        task_path = TASKS_DIR / f"{task_id}.json"
        if task_path.exists():
            with open(task_path, "r") as f:
                task = json.load(f)

            task.update(updates)
            task["updatedAt"] = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )

            with open(task_path, "w") as f:
                json.dump(task, f, indent=2)
                f.write("\n")
            logger.info(f"Updated task file {task_path}")

        # 2. Update legacy aggregate file (Compatibility)
        if TASKS_FILE.exists():
            with open(TASKS_FILE, "r") as f:
                data = json.load(f)

            found = False
            for task in data.get("tasks", []):
                if task["id"] == task_id:
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
