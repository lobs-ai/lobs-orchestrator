from datetime import datetime, timezone

import pytest

from orchestrator.utils.work_windows import is_window_active, prioritize_tasks_by_work_windows


def _dt(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "now_utc,expected",
    [
        # Monday 08:30 ET (13:30 UTC) in a 08-10 ET window
        (_dt(2026, 2, 2, 13, 30), True),
        # Monday 10:00 ET (15:00 UTC) end is exclusive
        (_dt(2026, 2, 2, 15, 0), False),
    ],
)
def test_is_window_active_basic(now_utc, expected):
    window = {"days": ["mon"], "startHour": 8, "endHour": 10, "tz": "America/New_York"}
    assert is_window_active(window, now_utc=now_utc) is expected


def test_is_window_active_wraps_midnight():
    window = {"days": ["mon"], "startHour": 22, "endHour": 2, "tz": "America/New_York"}

    # Monday 23:00 ET (Tuesday 04:00 UTC) should be active
    assert is_window_active(window, now_utc=_dt(2026, 2, 3, 4, 0)) is True

    # Tuesday 01:00 ET (Tuesday 06:00 UTC) should be active (carryover from Monday)
    assert is_window_active(window, now_utc=_dt(2026, 2, 3, 6, 0)) is True

    # Tuesday 02:00 ET (Tuesday 07:00 UTC) is end exclusive -> inactive
    assert is_window_active(window, now_utc=_dt(2026, 2, 3, 7, 0)) is False


def test_prioritize_tasks_by_work_windows_interleaves_round_robin():
    projects = [
        {
            "id": "a",
            "workWindows": [{"days": ["daily"], "startHour": 0, "endHour": 23, "tz": "UTC"}],
        },
        {
            "id": "b",
            "workWindows": [{"days": ["daily"], "startHour": 0, "endHour": 23, "tz": "UTC"}],
        },
        {"id": "c"},
    ]

    tasks = [
        {"id": "t1", "projectId": "a"},
        {"id": "t2", "projectId": "a"},
        {"id": "t3", "projectId": "b"},
        {"id": "t4", "projectId": "c"},
        {"id": "t5", "projectId": "b"},
    ]

    prioritized, active = prioritize_tasks_by_work_windows(tasks, projects, now_utc=_dt(2026, 2, 2, 12, 0), rr_offset=0)
    assert active == ["a", "b"]
    assert [t["id"] for t in prioritized] == ["t1", "t3", "t2", "t5", "t4"]

    prioritized2, active2 = prioritize_tasks_by_work_windows(tasks, projects, now_utc=_dt(2026, 2, 2, 12, 0), rr_offset=1)
    assert active2 == ["b", "a"]
    assert [t["id"] for t in prioritized2] == ["t3", "t1", "t5", "t2", "t4"]
