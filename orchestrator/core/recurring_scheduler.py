from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable

from croniter import croniter
from zoneinfo import ZoneInfo

from orchestrator.utils.settings import get_setting

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecurringTaskSpec:
    title: str
    agent: str
    project_id: str
    notes: str | None = None
    extra: dict[str, Any] | None = None


@dataclass(frozen=True)
class RecurringItem:
    id: str
    name: str
    schedule: str
    tz: str
    enabled: bool
    task: RecurringTaskSpec


class RecurringScheduler:
    """Cron-like recurring task scheduler.

    - Configuration comes from settings key: "recurring" (list of dicts)
    - Persists last-run times to a local state file
    - Creates tasks by writing JSON task files into the control repo tasks dir

    This is intentionally filesystem-based for restart safety.
    """

    def __init__(self, tasks_dir: Path, state_path: Path):
        self._tasks_dir = tasks_dir
        self._state_path = state_path
        self._state = self._load_state()

    # -----------------
    # State management
    # -----------------
    def _load_state(self) -> dict[str, Any]:
        if not self._state_path.exists():
            return {"last_run": {}}
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception:
            return {"last_run": {}}

    def _save_state(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self._state_path)

    def get_last_run(self, recurring_id: str) -> datetime | None:
        iso = (self._state.get("last_run", {}) or {}).get(recurring_id)
        if not iso:
            return None
        try:
            # stored as UTC
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None

    def set_last_run(self, recurring_id: str, when_utc: datetime) -> None:
        if when_utc.tzinfo is None:
            when_utc = when_utc.replace(tzinfo=timezone.utc)
        when_utc = when_utc.astimezone(timezone.utc)
        self._state.setdefault("last_run", {})[recurring_id] = when_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    # -----------------
    # Config parsing
    # -----------------
    def load_items(self) -> list[RecurringItem]:
        raw = get_setting("recurring", [])
        if not raw:
            return []
        if not isinstance(raw, list):
            logger.warning("Invalid recurring config: expected list")
            return []

        items: list[RecurringItem] = []
        for i, entry in enumerate(raw):
            if not isinstance(entry, dict):
                logger.warning(f"Invalid recurring entry at index {i}: expected dict")
                continue
            try:
                task_raw = entry.get("task") or {}
                # Allow extra task fields (e.g., outputPathTemplate, pipelineMeta, etc.)
                extra = {}
                if isinstance(task_raw, dict):
                    for k, v in task_raw.items():
                        if k in {"title", "agent", "projectId", "notes"}:
                            continue
                        extra[k] = v

                task = RecurringTaskSpec(
                    title=str(task_raw.get("title") or "").strip(),
                    agent=str(task_raw.get("agent") or "").strip(),
                    project_id=str(task_raw.get("projectId") or "").strip(),
                    notes=(str(task_raw.get("notes")) if task_raw.get("notes") is not None else None),
                    extra=(extra or None),
                )
                item = RecurringItem(
                    id=str(entry.get("id") or "").strip(),
                    name=str(entry.get("name") or "").strip(),
                    schedule=str(entry.get("schedule") or "").strip(),
                    tz=str(entry.get("tz") or "UTC").strip(),
                    enabled=bool(entry.get("enabled", True)),
                    task=task,
                )
                if not item.id or not item.schedule or not item.task.title or not item.task.agent or not item.task.project_id:
                    raise ValueError("missing required fields")

                # Validate tz and cron early so we fail fast but don't crash engine.
                ZoneInfo(item.tz)
                croniter(item.schedule, datetime.now())

                items.append(item)
            except Exception as e:
                logger.warning(f"Invalid recurring entry {entry.get('id') or i}: {e}")

        return items

    # -----------------
    # Task creation
    # -----------------
    def _existing_recurring_task_ids_for_date(self, recurring_id: str, date_iso: str) -> set[str]:
        """Return ids of tasks that match (recurring_id, date_iso)."""
        if not self._tasks_dir.exists():
            return set()

        matches: set[str] = set()
        for p in self._tasks_dir.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                meta = data.get("recurringMeta") or {}
                if meta.get("id") == recurring_id and meta.get("date") == date_iso:
                    matches.add(str(data.get("id") or p.stem))
            except Exception:
                continue
        return matches

    def _create_task(self, item: RecurringItem, now_utc: datetime, date_iso: str) -> str:
        task_id = str(uuid.uuid4()).upper()
        now_iso = now_utc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        task: dict[str, Any] = {
            "id": task_id,
            "title": item.task.title,
            "notes": item.task.notes or "",
            "projectId": item.task.project_id,
            "status": "active",
            "owner": "lobs",
            "reviewState": "approved",
            "workState": "not_started",
            "createdAt": now_iso,
            "updatedAt": now_iso,
            "tags": ["recurring", f"recurring:{item.id}"],
            "agent": item.task.agent,
            "recurringMeta": {
                "id": item.id,
                "name": item.name,
                "date": date_iso,
                "schedule": item.schedule,
                "tz": item.tz,
            },
        }

        # Merge any extra task fields from config.
        if item.task.extra:
            for k, v in item.task.extra.items():
                # Don't allow clobbering required keys.
                if k in {"id", "recurringMeta", "createdAt", "updatedAt"}:
                    continue
                task[k] = v

        # If configured with pipelineMeta but without date, fill it in.
        if isinstance(task.get("pipelineMeta"), dict):
            pm = task.get("pipelineMeta")
            if pm.get("pipelineId") and pm.get("stageId") and not pm.get("date"):
                pm["date"] = date_iso

        self._tasks_dir.mkdir(parents=True, exist_ok=True)
        out = self._tasks_dir / f"{task_id}.json"
        out.write_text(json.dumps(task, indent=2) + "\n", encoding="utf-8")
        return task_id

    # -----------------
    # Scheduling logic
    # -----------------
    def next_run_time(self, item: RecurringItem, *, now_utc: datetime | None = None) -> datetime:
        """Compute the next run time (local timezone) after the last run."""
        if now_utc is None:
            now_utc = datetime.now(timezone.utc)
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)

        tz = ZoneInfo(item.tz)
        last_run = self.get_last_run(item.id)

        if last_run is None:
            # Treat as if we last checked shortly before now so we can fire immediately
            # when starting up near a scheduled boundary.
            base = now_utc - timedelta(minutes=1)
        else:
            base = last_run

        base_local = base.astimezone(tz)
        it = croniter(item.schedule, base_local)
        return it.get_next(datetime)

    def is_due(self, item: RecurringItem, *, now_utc: datetime | None = None) -> bool:
        if now_utc is None:
            now_utc = datetime.now(timezone.utc)
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)

        tz = ZoneInfo(item.tz)
        now_local = now_utc.astimezone(tz)
        next_local = self.next_run_time(item, now_utc=now_utc)
        return next_local <= now_local

    def tick(self, *, now_utc: datetime | None = None) -> list[str]:
        """Check config and create any due recurring tasks.

        Returns list of created task ids.
        """
        if now_utc is None:
            now_utc = datetime.now(timezone.utc)
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)
        now_utc = now_utc.astimezone(timezone.utc)

        created: list[str] = []

        items = self.load_items()
        for item in items:
            if not item.enabled:
                continue

            try:
                if not self.is_due(item, now_utc=now_utc):
                    continue

                tz = ZoneInfo(item.tz)
                today_local = now_utc.astimezone(tz).date().isoformat()

                existing = self._existing_recurring_task_ids_for_date(item.id, today_local)
                if existing:
                    logger.info(
                        f"[RECURRING] Skip '{item.id}' ({item.name}) - already exists for {today_local}: {sorted(existing)[:1][0][:8]}..."
                    )
                    # Still advance last_run so we don't repeatedly try within the same minute.
                    self.set_last_run(item.id, now_utc)
                    continue

                task_id = self._create_task(item, now_utc, today_local)
                created.append(task_id)
                self.set_last_run(item.id, now_utc)

                logger.info(
                    f"[RECURRING] Created task {task_id[:8]} for '{item.id}' ({item.name}) @ {today_local}"
                )
            except Exception as e:
                logger.error(f"[RECURRING] Failed processing '{item.id}': {e}", exc_info=True)

        # Persistence is handled by tick_with_persistence(), which detects state changes.
        return created

    def tick_with_persistence(self, *, now_utc: datetime | None = None) -> list[str]:
        """Like tick(), but always saves state if any due item was evaluated.

        Engine integration should call this.
        """
        before = json.dumps(self._state, sort_keys=True)
        created = self.tick(now_utc=now_utc)
        after = json.dumps(self._state, sort_keys=True)
        if before != after:
            self._save_state()
        return created

    def run_now(self, recurring_id: str, *, now_utc: datetime | None = None) -> str | None:
        """Manually trigger a recurring item by id.

        Returns created task id, or None if not found/disabled/duplicate.
        """
        if now_utc is None:
            now_utc = datetime.now(timezone.utc)
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)
        now_utc = now_utc.astimezone(timezone.utc)

        item = next((i for i in self.load_items() if i.id == recurring_id), None)
        if not item:
            return None
        if not item.enabled:
            return None

        tz = ZoneInfo(item.tz)
        today_local = now_utc.astimezone(tz).date().isoformat()
        existing = self._existing_recurring_task_ids_for_date(item.id, today_local)
        if existing:
            self.set_last_run(item.id, now_utc)
            self._save_state()
            return None

        task_id = self._create_task(item, now_utc, today_local)
        self.set_last_run(item.id, now_utc)
        self._save_state()
        logger.info(f"[RECURRING] Manually created task {task_id[:8]} for '{item.id}'")
        return task_id
