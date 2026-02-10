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
    """Policy for exponential backoff after task failures.

    backoff schedule (seconds) for consecutive failures:
      1st retry:  60s
      2nd retry:  300s
      3rd retry:  900s
      4th retry:  2700s
      ... grows by factor=3 thereafter, capped by max_backoff_seconds.
    """

    base_seconds: int = 60
    second_seconds: int = 300
    factor: int = 3
    max_backoff_seconds: int = 6 * 60 * 60


class FailureRotation:
    """Tracks per-task consecutive failures and enforces exponential backoff.

    Persistence:
      - Stored in a JSON file under the orchestrator state dir for restart safety.

    Semantics:
      - retry_count increments on consecutive failures for the same task id.
      - retry_count resets (entry removed) on success.
      - after each failure, the task is skipped until now + backoff(retry_count).
      - if all eligible tasks are skipped, caller may choose to override cooldown and
        retry one skipped task to avoid deadlock.

    State fields per task:
      - retry_count: int
      - last_failure_ts: float
      - skip_until_ts: float

    Backwards compat:
      - We still write `streak` as an alias for `retry_count` since older logs/tests
        referenced it.
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
            # Backwards compatible settings:
            # - failure_rotation_threshold / failure_rotation_cooldown_seconds used to gate skipping.
            #   Now we always apply backoff, so these are ignored.
            base_seconds = int(get_setting("failure_backoff_base_seconds", 60))
            second_seconds = int(get_setting("failure_backoff_second_seconds", 5 * 60))
            factor = int(get_setting("failure_backoff_factor", 3))
            max_backoff_seconds = int(get_setting("failure_backoff_max_seconds", 6 * 60 * 60))
            policy = FailurePolicy(
                base_seconds=base_seconds,
                second_seconds=second_seconds,
                factor=factor,
                max_backoff_seconds=max_backoff_seconds,
            )
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

    def _compute_backoff_seconds(self, retry_count: int) -> int:
        """Compute exponential backoff for the given consecutive retry_count."""
        if retry_count <= 1:
            backoff = self.policy.base_seconds
        elif retry_count == 2:
            backoff = self.policy.second_seconds
        else:
            # 3rd failure uses `second_seconds * factor^(retry_count-2)`.
            backoff = int(self.policy.second_seconds * (self.policy.factor ** (retry_count - 2)))

        return int(min(backoff, self.policy.max_backoff_seconds))

    def record_failure(self, task_id: str, now: Optional[float] = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        tasks = self._state.setdefault("tasks", {})
        entry = tasks.get(task_id) or {}

        retry_count = int(entry.get("retry_count") or entry.get("streak") or 0) + 1
        entry["retry_count"] = retry_count
        # Backwards-compatible alias
        entry["streak"] = retry_count

        entry["last_failure_ts"] = now
        entry["skip_until_ts"] = now + self._compute_backoff_seconds(retry_count)

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
