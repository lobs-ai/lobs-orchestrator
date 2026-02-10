from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Any, Iterable

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


_DAY_ALIASES = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tues": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}


@dataclass(frozen=True)
class WorkWindow:
    days: tuple[str, ...]
    start_hour: int
    end_hour: int
    tz: str


def _parse_window(raw: dict[str, Any]) -> WorkWindow | None:
    days_raw = raw.get("days")
    start = raw.get("startHour")
    end = raw.get("endHour")
    tz = raw.get("tz")

    if not isinstance(days_raw, list) or not all(isinstance(d, str) for d in days_raw):
        return None
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    if not isinstance(tz, str) or not tz:
        return None
    if start < 0 or start > 23 or end < 0 or end > 23:
        return None

    days = tuple(d.strip().lower() for d in days_raw if d.strip())
    if not days:
        return None

    return WorkWindow(days=days, start_hour=start, end_hour=end, tz=tz)


def _days_match(days: Iterable[str], weekday: int) -> bool:
    normalized = [d.strip().lower() for d in days]
    if any(d in ("daily", "all") for d in normalized):
        return True

    for d in normalized:
        if d in _DAY_ALIASES and _DAY_ALIASES[d] == weekday:
            return True
    return False


def is_window_active(raw_window: dict[str, Any], now_utc: datetime | None = None) -> bool:
    """Return True if the work window is active at `now_utc`.

    Rules:
    - days: ["mon", ...] or ["daily"]
    - startHour/endHour are 0-23
    - tz is an IANA timezone string
    - If endHour <= startHour, the window is treated as spanning midnight.
    - Start is inclusive, end is exclusive.

    Invalid windows are treated as inactive.
    """

    window = _parse_window(raw_window)
    if not window:
        return False

    if ZoneInfo is None:
        return False

    try:
        tzinfo = ZoneInfo(window.tz)
    except Exception:
        return False

    if now_utc is None:
        now_utc = datetime.utcnow().replace(tzinfo=ZoneInfo("UTC"))
    elif now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")

    local = now_utc.astimezone(tzinfo)
    weekday = local.weekday()

    start_t = time(window.start_hour, 0, 0)
    end_t = time(window.end_hour, 0, 0)

    # Non-wrapping window (same-day)
    if window.end_hour > window.start_hour:
        if not _days_match(window.days, weekday):
            return False
        return start_t <= local.time() < end_t

    # Wrapping window (spans midnight): active if (day matches and time >= start)
    # or (previous day matches and time < end)
    if _days_match(window.days, weekday) and local.time() >= start_t:
        return True

    prev_weekday = (weekday - 1) % 7
    if _days_match(window.days, prev_weekday) and local.time() < end_t:
        return True

    return False


def get_active_window_project_ids(projects: list[dict[str, Any]], now_utc: datetime | None = None) -> list[str]:
    active: list[str] = []
    for p in projects:
        pid = p.get("id")
        if not isinstance(pid, str) or not pid:
            continue
        windows = p.get("workWindows")
        if not isinstance(windows, list):
            continue
        for w in windows:
            if isinstance(w, dict) and is_window_active(w, now_utc=now_utc):
                active.append(pid)
                break
    return active


def prioritize_tasks_by_work_windows(
    tasks: list[dict[str, Any]],
    projects: list[dict[str, Any]],
    now_utc: datetime | None = None,
    rr_offset: int = 0,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (prioritized_tasks, active_project_ids).

    - If no active windows: tasks returned unchanged.
    - If multiple active projects: tasks are interleaved round-robin across active projects,
      starting from rr_offset.
    - Tasks from non-active projects retain their relative order and are appended after.
    """

    active_projects = get_active_window_project_ids(projects, now_utc=now_utc)
    if not active_projects:
        return tasks, []

    # De-dupe while preserving order
    seen: set[str] = set()
    active_projects = [p for p in active_projects if not (p in seen or seen.add(p))]

    if rr_offset and active_projects:
        rr_offset = rr_offset % len(active_projects)
        active_projects = active_projects[rr_offset:] + active_projects[:rr_offset]

    by_project: dict[str, list[dict[str, Any]]] = {pid: [] for pid in active_projects}
    non_active: list[dict[str, Any]] = []

    for t in tasks:
        pid = t.get("projectId")
        if isinstance(pid, str) and pid in by_project:
            by_project[pid].append(t)
        else:
            non_active.append(t)

    prioritized: list[dict[str, Any]] = []
    while True:
        progressed = False
        for pid in active_projects:
            bucket = by_project.get(pid)
            if bucket:
                prioritized.append(bucket.pop(0))
                progressed = True
        if not progressed:
            break

    prioritized.extend(non_active)
    return prioritized, active_projects
