import logging
import time
import json
import subprocess
import hashlib
import re
from pathlib import Path
from typing import Any, Optional
from datetime import datetime, timezone, timedelta

from orchestrator.providers.base import TaskProvider
from orchestrator.config import WORKER_STATUS_JSON, CONTROL_REPO_PATH, BASE_DIR, STATE_DIR
from orchestrator.core.agents import AgentManager
from orchestrator.utils.settings import get_setting

logger = logging.getLogger(__name__)

class Monitor:
    """
    Observer component that monitors system state, cron jobs, and project health.
    Generates suggestions or alerts based on observations.
    """

    SUGGESTION_HISTORY_PATH = STATE_DIR / "suggestion-history.json"
    SUGGESTION_DEDUP_HOURS = 48
    SUGGESTION_MAX_PER_PROJECT_PER_DAY = 3

    def __init__(self, provider: TaskProvider):
        self.provider = provider
        self.last_check = 0
        self.check_interval = 600  # 10 minutes
        self.last_proactive_check = 0
        self.proactive_interval = 3600  # 1 hour

        # Lightweight agent for proactive analysis.
        template_dir = (STATE_DIR.parent / "worker-template").resolve()
        # STATE_DIR is orchestrator_repo/state, so parent is orchestrator repo.
        self.agent_manager = AgentManager(template_dir)
        self._suggester_provisioned = False

    def tick(self) -> None:
        """Periodic check for system health and inbox processing."""
        now = time.time()
        if now - self.last_check < self.check_interval:
            return

        logger.info("Running periodic system monitoring and inbox processing...")
        self.check_stuck_tasks()
        
        if now - self.last_proactive_check >= self.proactive_interval:
            self.generate_proactive_suggestions()
            self.last_proactive_check = now

        self.process_inbox()
        
        self.last_check = now

    def process_inbox(self) -> None:
        """Scan and respond to items in the inbox intended for the system."""
        items = self.provider.get_inbox_items()

        # Persist dismissals for proactive suggestions.
        history = self._load_suggestion_history()
        changed = False
        for item in items:
            if item.get("type") == "suggestion" and item.get("status") == "dismissed":
                h = item.get("suggestionHash") or item.get("dismissKey")
                if h:
                    for s in history.get("suggestions", []):
                        if s.get("hash") == h and s.get("dismissed") is not True:
                            s["dismissed"] = True
                            s["dismissedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                            changed = True
        if changed:
            self._save_suggestion_history(history)

        for item in items:
            if item.get("status") == "resolved":
                continue

            # If the item is assigned to the system and needs a response
            if item.get("responder") == "system" and item.get("actionRequired"):
                self._respond_to_inbox_item(item)

    def _respond_to_inbox_item(self, item: dict[str, Any]) -> None:
        """Handle a specific inbox item."""
        logger.info(f"System responding to inbox item: {item['id']}")
        
        item_type = item.get("type")
        if item_type == "request":
            # For example, if it's a request to analyze something
            logger.info(f"Processing system request: {item.get('title')}")
            # Potentially spawn a task or update state
            self.provider.update_inbox_item(item["id"], {
                "status": "resolved", 
                "response": "Acknowledged and processed by system monitor."
            })
        elif item_type == "suggestion" and item.get("autoApprove"):
            # Turn suggestion into a real task
            new_task = {
                "id": f"task_from_inbox_{item['id']}",
                "projectId": item.get("projectId", "default"),
                "title": item.get("title"),
                "notes": item.get("body"),
                "status": "active",
                "workState": "not_started"
            }
            self.provider.update_task(new_task["id"], new_task)
            self.provider.update_inbox_item(item["id"], {"status": "resolved", "taskId": new_task["id"]})

    def check_stuck_tasks(self) -> None:
        """Verify that running tasks haven't exceeded timeout."""
        if not WORKER_STATUS_JSON.exists():
            return

        try:
            with open(WORKER_STATUS_JSON, "r") as f:
                status = json.load(f)
            
            current_task = status.get("currentTask")
            started_at_str = status.get("startedAt")
            
            if not current_task or not started_at_str:
                return

            started_at = datetime.fromisoformat(started_at_str.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            
            delta = (now - started_at).total_seconds()
            if delta > 3600:  # 1 hour timeout (increased from 30m)
                logger.warning(f"Task {current_task} is running long ({int(delta/60)}m). Notifying inbox.")
                
                # Check if we already alerted
                alerts = self.provider.get_active_alerts()
                if any(a.get("taskId") == current_task for a in alerts):
                    return

                self.provider.add_inbox_item({
                    "id": f"stale_worker_{int(time.time())}",
                    "title": "Alert: Task Timeout",
                    "body": f"The task {current_task} has been running for {int(delta/60)} minutes.",
                    "type": "alert",
                    "severity": "medium",
                    "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                })
        except Exception as e:
            logger.error(f"Failed to check stuck tasks: {e}")

    # =========================================================================
    # Proactive Suggestions (LLM-powered)
    # =========================================================================

    def _load_suggestion_history(self) -> dict[str, Any]:
        if not self.SUGGESTION_HISTORY_PATH.exists():
            return {"suggestions": []}
        try:
            with open(self.SUGGESTION_HISTORY_PATH, "r") as f:
                data = json.load(f)
            if not isinstance(data, dict) or "suggestions" not in data:
                return {"suggestions": []}
            if not isinstance(data["suggestions"], list):
                return {"suggestions": []}
            return data
        except Exception as e:
            logger.warning(f"Failed to read suggestion history: {e}")
            return {"suggestions": []}

    def _save_suggestion_history(self, data: dict[str, Any]) -> None:
        self.SUGGESTION_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(self.SUGGESTION_HISTORY_PATH, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")

    def _hash_suggestion(self, project_id: str, title: str) -> str:
        key = f"{project_id.strip().lower()}::{title.strip().lower()}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

    def _extract_json_array(self, text: str) -> Optional[list[dict[str, Any]]]:
        """Best-effort extraction of a JSON array from an LLM/CLI response."""
        text = text.strip()
        # Fast path: already JSON
        try:
            obj = json.loads(text)
            if isinstance(obj, list):
                return obj
        except Exception:
            pass

        # Heuristic: find the outermost JSON array
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1 or end <= start:
            return None

        blob = text[start : end + 1]
        try:
            obj = json.loads(blob)
            if isinstance(obj, list):
                return obj
        except Exception:
            return None
        return None

    def _read_project_context_files(self, repo_path: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        for name in ("README.md", "ARCHITECTURE.md"):
            p = repo_path / name
            if p.exists():
                try:
                    out[name] = p.read_text(errors="ignore")[:12000]
                except Exception:
                    continue
        return out

    def _get_recent_commits(self, repo_path: Path, since_hours: int = 24) -> str:
        since = f"{since_hours} hours ago"
        try:
            result = subprocess.run(
                [
                    "git",
                    "log",
                    f"--since={since}",
                    "--pretty=format:%h | %ad | %s",
                    "--date=iso",
                    "-n",
                    "50",
                ],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            return (result.stdout or "").strip()
        except Exception:
            return ""

    def _read_local_open_tasks(self) -> list[dict[str, Any]]:
        tasks_dir = CONTROL_REPO_PATH / "state" / "tasks"
        if not tasks_dir.exists():
            return []

        tasks: list[dict[str, Any]] = []
        for task_path in tasks_dir.glob("*.json"):
            try:
                with open(task_path, "r") as f:
                    t = json.load(f)
                if t.get("status") != "active":
                    continue
                if t.get("workState") == "completed":
                    continue
                tasks.append(t)
            except Exception:
                continue
        return tasks

    def _format_tasks_summary(self, tasks: list[dict[str, Any]], max_items: int = 30) -> str:
        def _age_days(created_at: str) -> Optional[int]:
            try:
                dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                return int((datetime.now(timezone.utc) - dt).total_seconds() // 86400)
            except Exception:
                return None

        rows = []
        for t in tasks:
            created = t.get("createdAt") or t.get("updatedAt") or ""
            age = _age_days(created) if created else None
            age_str = f"{age}d" if age is not None else "?"
            rows.append(
                {
                    "id": t.get("id", ""),
                    "projectId": t.get("projectId", ""),
                    "title": t.get("title") or t.get("prompt") or "(no title)",
                    "ageDays": age,
                    "ageStr": age_str,
                    "workState": t.get("workState", ""),
                    "kind": t.get("kind", "task"),
                }
            )

        rows.sort(key=lambda r: (r["projectId"], -(r["ageDays"] or 0)))
        lines = []
        for r in rows[:max_items]:
            lines.append(
                f"- [{r['projectId']}] {r['id']} ({r['kind']}/{r['workState']}, age {r['ageStr']}): {r['title']}"
            )
        return "\n".join(lines)

    def _should_dedup(self, suggestion_hash: str, history: dict[str, Any], now: datetime) -> bool:
        cutoff = now - timedelta(hours=self.SUGGESTION_DEDUP_HOURS)
        for s in history.get("suggestions", []):
            if s.get("hash") != suggestion_hash:
                continue
            if s.get("dismissed") is True:
                return True
            try:
                created = datetime.fromisoformat(s.get("createdAt").replace("Z", "+00:00"))
                if created >= cutoff:
                    return True
            except Exception:
                # If we can't parse, err on dedup.
                return True
        return False

    def _count_recent_project_suggestions(self, project_id: str, history: dict[str, Any], now: datetime) -> int:
        cutoff = now - timedelta(hours=24)
        n = 0
        for s in history.get("suggestions", []):
            if s.get("projectId") != project_id:
                continue
            if s.get("dismissed") is True:
                continue
            try:
                created = datetime.fromisoformat(s.get("createdAt").replace("Z", "+00:00"))
                if created >= cutoff:
                    n += 1
            except Exception:
                continue
        return n

    def _spawn_suggester(self, prompt: str) -> str:
        """Invoke OpenClaw suggester agent and return raw stdout."""
        agent_id = "suggester"

        # One-time provisioning/registration.
        if not self._suggester_provisioned:
            needs_provision = not self.agent_manager.aux_agent_exists(agent_id)
            needs_registration = not self.agent_manager.is_agent_registered_by_id(agent_id)
            if needs_provision or needs_registration:
                if not self.agent_manager.provision_aux_agent(
                    agent_id,
                    name="Lobs Suggester",
                    model="anthropic/claude-haiku-4-5",
                    emoji="💡",
                    register=True,
                ):
                    raise RuntimeError("Failed to provision suggester agent")

                # New agent registrations require a gateway restart.
                if needs_registration:
                    logger.warning("New suggester agent registered - restarting gateway...")
                    if not self.agent_manager.restart_gateway():
                        raise RuntimeError("Gateway restart failed after suggester registration")
                    time.sleep(3)

            self._suggester_provisioned = True

        executable = get_setting("openclaw_executable", "openclaw")
        cmd = [executable, "agent", "--agent", agent_id, "-m", prompt]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
        return (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")

    def generate_proactive_suggestions(self) -> None:
        """Spawn a cheap/fast LLM agent to analyze recent activity and suggest actionable next steps."""
        now = datetime.now(timezone.utc)
        now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        history = self._load_suggestion_history()

        # Dedup against current inbox too.
        existing_inbox = self.provider.get_inbox_items()
        existing_hashes = {
            i.get("suggestionHash")
            for i in existing_inbox
            if i.get("type") == "suggestion" and i.get("status") not in ("resolved", "dismissed")
        }

        projects = [p for p in self.provider.get_projects() if not p.get("archived")]
        open_tasks = self._read_local_open_tasks()
        tasks_summary = self._format_tasks_summary(open_tasks)

        for project in projects:
            project_id = project.get("id")
            if not project_id:
                continue

            # Per-project daily cap
            if self._count_recent_project_suggestions(project_id, history, now) >= self.SUGGESTION_MAX_PER_PROJECT_PER_DAY:
                continue

            repo_path = Path(project.get("repoPath") or (BASE_DIR / project_id)).resolve()
            if not repo_path.exists():
                continue

            recent_commits = self._get_recent_commits(repo_path)
            ctx_files = self._read_project_context_files(repo_path)

            prompt = "\n".join(
                [
                    "You are an assistant that generates *actionable, context-aware* engineering suggestions.",
                    "Analyze the project context and output ONLY valid JSON.",
                    "",
                    "Return a JSON array of up to 3 objects. Each object must have:",
                    "- title (string, short)",
                    "- body (string, 2-6 sentences, actionable, include any task IDs you reference)",
                    "- priority (low|medium|high)",
                    "- tags (array of short strings)",
                    "",
                    "Hard rules:",
                    "- Do not suggest anything that requires unknown credentials.",
                    "- Avoid vague advice; every suggestion should be convertible into a concrete task.",
                    "- Prefer referencing concrete evidence (task age, commit patterns, missing tests).",
                    "",
                    f"PROJECT_ID: {project_id}",
                    f"NOW_UTC: {now_iso}",
                    "",
                    "RECENT_COMMITS_LAST_24H:",
                    recent_commits or "(none)",
                    "",
                    "OPEN_TASKS_SUMMARY:",
                    tasks_summary or "(none)",
                    "",
                    "PROJECT_CONTEXT_FILES:",
                    *(
                        [f"## {name}\n{content}" for name, content in ctx_files.items()]
                        if ctx_files
                        else ["(no README.md or ARCHITECTURE.md found)"]
                    ),
                ]
            )

            try:
                raw = self._spawn_suggester(prompt)
            except Exception as e:
                logger.error(f"Suggester failed for {project_id}: {e}")
                continue

            suggestions = self._extract_json_array(raw)
            if not suggestions:
                logger.warning(f"No parseable suggestions for {project_id}")
                continue

            # Limit and validate
            added = 0
            for s in suggestions[: self.SUGGESTION_MAX_PER_PROJECT_PER_DAY]:
                title = str(s.get("title") or "").strip()
                body = str(s.get("body") or "").strip()
                priority = str(s.get("priority") or "medium").strip().lower()
                tags = s.get("tags") if isinstance(s.get("tags"), list) else []

                if not title or not body:
                    continue
                if priority not in ("low", "medium", "high"):
                    priority = "medium"

                suggestion_hash = self._hash_suggestion(project_id, title)

                if suggestion_hash in existing_hashes:
                    continue
                if self._should_dedup(suggestion_hash, history, now):
                    continue

                if self._count_recent_project_suggestions(project_id, history, now) >= self.SUGGESTION_MAX_PER_PROJECT_PER_DAY:
                    break

                item_id = f"suggest_{project_id}_{suggestion_hash}_{int(time.time())}"
                self.provider.add_inbox_item(
                    {
                        "id": item_id,
                        "title": title,
                        "body": body,
                        "type": "suggestion",
                        "projectId": project_id,
                        "priority": priority,
                        "tags": tags,
                        "suggestionHash": suggestion_hash,
                        "dismissKey": suggestion_hash,
                        "dismissInstructions": "Set status='dismissed' on this inbox item to stop repeats; the monitor will persist that preference.",
                        "createdAt": now_iso,
                    }
                )

                history.setdefault("suggestions", []).append(
                    {
                        "hash": suggestion_hash,
                        "title": title,
                        "projectId": project_id,
                        "createdAt": now_iso,
                        "dismissed": False,
                    }
                )
                self._save_suggestion_history(history)

                existing_hashes.add(suggestion_hash)
                added += 1

            if added:
                logger.info(f"Added {added} proactive suggestions for {project_id}")
