"""Tests for the deterministic Observer (proactive work discovery)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orchestrator.core.observer import (
    Observer,
    Opportunity,
    OpportunityPriority,
    ProactiveSettings,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_observer(
    tmp_path: Path,
    *,
    enabled: bool = True,
    scan_interval: int = 0,
    quiet_start: str | None = None,
    quiet_end: str | None = None,
    now: datetime | None = None,
    **kwargs,
) -> Observer:
    """Build an Observer with sensible test defaults."""
    settings = ProactiveSettings(
        enabled=enabled,
        scan_interval_seconds=scan_interval,
        quiet_hours_start=quiet_start,
        quiet_hours_end=quiet_end,
        **kwargs,
    )
    state_path = tmp_path / "observer-state.json"
    now_dt = now or datetime.now(timezone.utc)
    return Observer(settings=settings, state_path=state_path, now_fn=lambda: now_dt)


def _make_repo(tmp_path: Path, name: str = "repo") -> Path:
    """Create a minimal repo directory and return its path."""
    repo = tmp_path / name
    repo.mkdir(parents=True, exist_ok=True)
    return repo


def _project(pid: str, repo_path: Path) -> dict:
    return {"id": pid, "repoPath": str(repo_path)}


# ===================================================================
# Scan: TODO / FIXME / HACK markers (programmer)
# ===================================================================


class TestCodeImprovements:
    def test_finds_todo_comments(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "main.py").write_text("x = 1  # TODO: refactor this\n")

        obs = _make_observer(tmp_path)
        ops = obs.scan_for_code_improvements("proj", repo)

        assert len(ops) == 1
        assert ops[0].kind == "todo_comment"
        assert ops[0].agent_type == "programmer"
        assert "TODO" in ops[0].title
        assert ops[0].line_number == 1

    def test_finds_fixme_and_hack(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "app.py").write_text(
            "a = 1\n"
            "# FIXME: broken\n"
            "# HACK: workaround\n"
        )

        obs = _make_observer(tmp_path)
        ops = obs.scan_for_code_improvements("proj", repo)

        kinds = {op.evidence.split(":")[0].strip() for op in ops}
        assert "FIXME" in kinds
        assert "HACK" in kinds

    def test_skips_non_code_files(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "notes.txt").write_text("TODO: remember to buy milk\n")

        obs = _make_observer(tmp_path)
        ops = obs.scan_for_code_improvements("proj", repo)
        assert ops == []

    def test_respects_max_per_file(self, tmp_path):
        """At most 5 TODOs per file are reported."""
        repo = _make_repo(tmp_path)
        lines = [f"# TODO: item {i}\n" for i in range(20)]
        (repo / "big.py").write_text("".join(lines))

        obs = _make_observer(tmp_path)
        ops = obs.scan_for_code_improvements("proj", repo)

        # _find_todos caps at 5 per file
        assert len(ops) <= 5

    def test_caps_at_20_opportunities(self, tmp_path):
        repo = _make_repo(tmp_path)
        # Create many files, each with a TODO
        for i in range(30):
            (repo / f"f{i}.py").write_text(f"# TODO: fix {i}\n")

        obs = _make_observer(tmp_path)
        ops = obs.scan_for_code_improvements("proj", repo)
        assert len(ops) <= 20

    def test_ignores_dot_and_build_dirs(self, tmp_path):
        repo = _make_repo(tmp_path)
        hidden = repo / ".hidden"
        hidden.mkdir()
        (hidden / "secret.py").write_text("# TODO: hidden\n")

        node_modules = repo / "node_modules"
        node_modules.mkdir()
        (node_modules / "dep.js").write_text("// TODO: dep\n")

        obs = _make_observer(tmp_path)
        ops = obs.scan_for_code_improvements("proj", repo)
        assert ops == []


# ===================================================================
# Scan: stale / unpinned dependencies (researcher)
# ===================================================================


class TestStaleDeps:
    def test_detects_unpinned_python_deps(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "requirements.txt").write_text("requests\nflask>=2.0\n")

        obs = _make_observer(tmp_path)
        ops = obs.scan_for_stale_deps("proj", repo)

        assert len(ops) == 1
        assert ops[0].kind == "unpinned_dependencies"
        assert ops[0].agent_type == "researcher"

    def test_all_pinned_no_opportunity(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "requirements.txt").write_text("requests==2.31.0\nflask==3.0.0\n")

        obs = _make_observer(tmp_path)
        ops = obs.scan_for_stale_deps("proj", repo)
        assert ops == []

    def test_comments_and_blank_lines_ignored(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "requirements.txt").write_text(
            "# pinned deps\n\nrequests==2.31.0\n"
        )

        obs = _make_observer(tmp_path)
        ops = obs.scan_for_stale_deps("proj", repo)
        assert ops == []

    def test_no_requirements_no_opportunity(self, tmp_path):
        repo = _make_repo(tmp_path)
        obs = _make_observer(tmp_path)
        ops = obs.scan_for_stale_deps("proj", repo)
        assert ops == []


# ===================================================================
# Scan: quality issues (reviewer)
# ===================================================================


class TestQualityIssues:
    def test_detects_missing_tests_dir(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "pyproject.toml").write_text("[project]\nname='x'\n")

        obs = _make_observer(tmp_path)
        ops = obs.scan_for_quality_issues("proj", repo)

        assert any(op.kind == "missing_tests" for op in ops)

    def test_no_issue_when_tests_exist(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "pyproject.toml").write_text("[project]\nname='x'\n")
        (repo / "tests").mkdir()

        obs = _make_observer(tmp_path)
        ops = obs.scan_for_quality_issues("proj", repo)

        assert not any(op.kind == "missing_tests" for op in ops)

    def test_non_python_repo_skipped(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "main.go").write_text("package main\n")

        obs = _make_observer(tmp_path)
        ops = obs.scan_for_quality_issues("proj", repo)
        assert ops == []


# ===================================================================
# Scan: doc drift (writer)
# ===================================================================


class TestDocDrift:
    def test_detects_missing_readme(self, tmp_path):
        repo = _make_repo(tmp_path)
        now = datetime.now(timezone.utc)

        obs = _make_observer(tmp_path, now=now)
        ops = obs.scan_for_doc_drift("proj", repo, now)

        assert any(op.kind == "missing_readme" for op in ops)

    def test_no_issue_when_readme_exists(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "README.md").write_text("# My Project\n")
        now = datetime.now(timezone.utc)

        obs = _make_observer(tmp_path, now=now)
        ops = obs.scan_for_doc_drift("proj", repo, now)

        assert not any(op.kind == "missing_readme" for op in ops)

    def test_detects_empty_docs_dir(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "README.md").write_text("# Hi\n")
        (repo / "docs").mkdir()
        now = datetime.now(timezone.utc)

        obs = _make_observer(tmp_path, now=now)
        ops = obs.scan_for_doc_drift("proj", repo, now)

        assert any(op.kind == "empty_docs_dir" for op in ops)


# ===================================================================
# Scan: tech debt / large files (architect)
# ===================================================================


class TestTechDebt:
    def test_detects_large_file(self, tmp_path):
        repo = _make_repo(tmp_path)
        # Write a file that exceeds the threshold
        (repo / "big.py").write_text("\n".join(f"line{i}" for i in range(600)))

        obs = _make_observer(tmp_path, large_file_line_threshold=500)
        ops = obs.scan_for_tech_debt("proj", repo)

        assert len(ops) == 1
        assert ops[0].kind == "complexity_hotspot"
        assert ops[0].agent_type == "architect"
        assert "600" in ops[0].title

    def test_no_issue_for_small_files(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "small.py").write_text("x = 1\n")

        obs = _make_observer(tmp_path, large_file_line_threshold=500)
        ops = obs.scan_for_tech_debt("proj", repo)
        assert ops == []

    def test_reports_top_5_only(self, tmp_path):
        repo = _make_repo(tmp_path)
        for i in range(8):
            (repo / f"big{i}.py").write_text("\n".join(f"l{j}" for j in range(600 + i)))

        obs = _make_observer(tmp_path, large_file_line_threshold=500)
        ops = obs.scan_for_tech_debt("proj", repo)
        assert len(ops) <= 5


# ===================================================================
# Full scan: dedup, rate limiting, quiet hours
# ===================================================================


class TestFullScan:
    def test_disabled_returns_empty(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "main.py").write_text("# TODO: x\n")

        obs = _make_observer(tmp_path, enabled=False)
        ops = obs.scan_for_opportunities([_project("proj", repo)])
        assert ops == []

    def test_quiet_hours_returns_empty(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "main.py").write_text("# TODO: x\n")

        # Set quiet hours that encompass the "now" time
        now = datetime(2025, 1, 15, 23, 30, tzinfo=timezone.utc)
        obs = _make_observer(
            tmp_path,
            quiet_start="22:00",
            quiet_end="07:00",
            now=now,
        )
        ops = obs.scan_for_opportunities([_project("proj", repo)])
        assert ops == []

    def test_dedup_prevents_repeat_opportunities(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "main.py").write_text("# TODO: fix this\n")

        obs = _make_observer(tmp_path, scan_interval=0)
        projects = [_project("proj", repo)]

        first = obs.scan_for_opportunities(projects)
        assert len(first) >= 1

        # Second scan within same observer should dedup
        second = obs.scan_for_opportunities(projects)
        assert len(second) == 0

    def test_skips_archived_projects(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "main.py").write_text("# TODO: fix\n")

        obs = _make_observer(tmp_path)
        projects = [{"id": "proj", "repoPath": str(repo), "archived": True}]
        ops = obs.scan_for_opportunities(projects)
        assert ops == []

    def test_skips_nonexistent_repo(self, tmp_path):
        obs = _make_observer(tmp_path)
        projects = [{"id": "proj", "repoPath": str(tmp_path / "nonexistent")}]
        ops = obs.scan_for_opportunities(projects)
        assert ops == []

    def test_scan_interval_rate_limiting(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "main.py").write_text("# TODO: a\n")

        now = datetime(2025, 1, 15, 12, 0, tzinfo=timezone.utc)
        obs = _make_observer(tmp_path, scan_interval=3600, now=now)

        projects = [_project("proj", repo)]

        # First scan succeeds
        first = obs.scan_for_opportunities(projects)
        assert len(first) >= 1

        # Immediate second scan is rate-limited (scan_due returns False)
        second = obs.scan_for_opportunities(projects)
        assert len(second) == 0


# ===================================================================
# Prioritization
# ===================================================================


class TestPrioritization:
    def test_higher_priority_opportunities_come_first(self, tmp_path):
        obs = _make_observer(tmp_path)

        ops = [
            Opportunity(
                project_id="p",
                agent_type="writer",
                kind="low_prio",
                title="Low",
                description="d",
                priority=OpportunityPriority.LOW,
            ),
            Opportunity(
                project_id="p",
                agent_type="researcher",
                kind="urgent_prio",
                title="Urgent",
                description="d",
                priority=OpportunityPriority.URGENT,
            ),
        ]

        prioritized = obs._prioritize(ops)
        assert prioritized[0].kind == "urgent_prio"
        assert prioritized[1].kind == "low_prio"

    def test_researcher_weighted_higher_than_writer(self, tmp_path):
        obs = _make_observer(tmp_path)

        ops = [
            Opportunity(
                project_id="p",
                agent_type="writer",
                kind="writer_work",
                title="Writer",
                description="d",
                priority=OpportunityPriority.NORMAL,
            ),
            Opportunity(
                project_id="p",
                agent_type="researcher",
                kind="researcher_work",
                title="Researcher",
                description="d",
                priority=OpportunityPriority.NORMAL,
            ),
        ]

        prioritized = obs._prioritize(ops)
        # Researcher gets 1.15x weight, writer gets 0.95x → researcher first
        assert prioritized[0].kind == "researcher_work"


# ===================================================================
# Opportunity ID generation
# ===================================================================


class TestOpportunityId:
    def test_stable_id_for_same_inputs(self, tmp_path):
        obs = _make_observer(tmp_path)

        op = Opportunity(
            project_id="p",
            agent_type="programmer",
            kind="todo_comment",
            title="Fix TODO in main.py:10",
            description="d",
            priority=OpportunityPriority.NORMAL,
            file_path="main.py",
            line_number=10,
        )

        id1 = obs._make_opportunity_id(op)
        id2 = obs._make_opportunity_id(op)
        assert id1 == id2

    def test_different_for_different_inputs(self, tmp_path):
        obs = _make_observer(tmp_path)

        op1 = Opportunity(
            project_id="p",
            agent_type="programmer",
            kind="todo_comment",
            title="A",
            description="d",
            priority=OpportunityPriority.NORMAL,
        )
        op2 = Opportunity(
            project_id="p",
            agent_type="programmer",
            kind="todo_comment",
            title="B",
            description="d",
            priority=OpportunityPriority.NORMAL,
        )

        assert obs._make_opportunity_id(op1) != obs._make_opportunity_id(op2)

    def test_ai_prefix_works(self, tmp_path):
        """AI-prefixed kinds generate valid IDs without collisions."""
        obs = _make_observer(tmp_path)

        det = Opportunity(
            project_id="p",
            agent_type="programmer",
            kind="todo_comment",
            title="Same Title",
            description="d",
            priority=OpportunityPriority.NORMAL,
        )
        ai = Opportunity(
            project_id="p",
            agent_type="programmer",
            kind="ai_todo_comment",
            title="Same Title",
            description="d",
            priority=OpportunityPriority.NORMAL,
        )

        assert obs._make_opportunity_id(det) != obs._make_opportunity_id(ai)
