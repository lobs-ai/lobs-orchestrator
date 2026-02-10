import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from orchestrator.utils.settings import get_setting

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FailurePolicy:
    threshold: int
    cooldown_seconds: int


class FailureRotation:
    """Tracks per-task consecutive failure streaks and enforces temporary skipping.

    Persistence:
      - Stored in a JSON file under the orchestrator state dir for restart safety.

    Semantics:
      - streak increments on consecutive failures for the same task id.
      - streak resets (entry removed) on success.
      - once streak >= threshold, task is skipped until skip_until (now + cooldown).
      - if all eligible tasks are skipped, caller may choose to override cooldown and
        retry one skipped task to avoid deadlock.
    """

    def __init__(
        self,
        state_dir: Path,
        policy: Optional[FailurePolicy] = None,
        state_file: Optional[Path] = None,
    ):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

        if policy is None:
            threshold = int(get_setting("failure_rotation_threshold", 3))
            cooldown_seconds = int(get_setting("failure_rotation_cooldown_seconds", 30 * 60))
            policy = FailurePolicy(threshold=threshold, cooldown_seconds=cooldown_seconds)
        self.policy = policy

        self.state_file = state_file or (self.state_dir / "failure-rotation.json")
        self._state: dict[str, Any] = {"version": 1, "tasks": {}}
        self._load()

    # ---------------------------------------------------------------------
    # Persistence
    # ---------------------------------------------------------------------

    def _load(self) -> None:
        if not self.state_file.exists():
            return
        try:
            with open(self.state_file, "r") as f:
                data = json.load(f)
            if isinstance(data, dict) and "tasks" in data:
                self._state = data
        except Exception as e:
            logger.warning(f"Failed to load failure rotation state: {e}")

    def _save(self) -> None:
        tmp = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
        try:
            with open(tmp, "w") as f:
                json.dump(self._state, f, indent=2, sort_keys=True)
            tmp.replace(self.state_file)
        except Exception as e:
            logger.warning(f"Failed to save failure rotation state: {e}")
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass

    # ---------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------

    def record_success(self, task_id: str) -> None:
        tasks = self._state.setdefault("tasks", {})
        if task_id in tasks:
            del tasks[task_id]
            self._save()

    def record_failure(self, task_id: str, now: Optional[float] = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        tasks = self._state.setdefault("tasks", {})
        entry = tasks.get(task_id) or {}

        streak = int(entry.get("streak", 0)) + 1
        entry["streak"] = streak
        entry["last_failure_ts"] = now

        if streak >= self.policy.threshold:
            entry["skip_until_ts"] = now + self.policy.cooldown_seconds

        tasks[task_id] = entry
        self._save()
        return entry

    def get_entry(self, task_id: str) -> Optional[dict[str, Any]]:
        entry = self._state.get("tasks", {}).get(task_id)
        return dict(entry) if isinstance(entry, dict) else None

    def should_skip(self, task_id: str, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        entry = self._state.get("tasks", {}).get(task_id)
        if not entry:
            return False

        streak = int(entry.get("streak", 0))
        if streak < self.policy.threshold:
            return False

        skip_until = entry.get("skip_until_ts")
        if skip_until is None:
            return False

        return now < float(skip_until)

    def pick_retry_when_all_skipped(self, task_ids: list[str]) -> Optional[str]:
        """Pick a task id to retry when everything is skipped.

        Chooses the one with the earliest skip_until (or oldest failure).
        """
        best_id = None
        best_key = None

        tasks = self._state.get("tasks", {})
        for tid in task_ids:
            entry = tasks.get(tid) or {}
            key = (
                float(entry.get("skip_until_ts") or float("inf")),
                float(entry.get("last_failure_ts") or 0.0),
            )
            if best_key is None or key < best_key:
                best_key = key
                best_id = tid

        return best_id
