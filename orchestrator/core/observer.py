"""orchestrator.core.observer

Deterministic, lightweight proactive work discovery.

This module is *not* an LLM suggester. It scans project repositories for
small, concrete opportunities that can be converted into tasks when the
system is idle.

Reference: docs/ARCHITECTURE.md (proactive work discovery).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, asdict
from datetime import date, datetime, time, timedelta
from enum import IntEnum
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from orchestrator.config import BASE_DIR, STATE_DIR
from orchestrator.utils.settings import get_setting
from orchestrator.utils.executables import which

logger = logging.getLogger(__name__)


class OpportunityPriority(IntEnum):
    """Lower number means higher priority."""

    URGENT = 1
    HIGH = 2
    NORMAL = 3
    LOW = 4
    BACKGROUND = 5


@dataclass(frozen=True)
class Opportunity:
    """A single proactive work opportunity discovered by scans."""

    project_id: str
    agent_type: str  # programmer|researcher|reviewer|writer|architect
    kind: str  # e.g. todo_comment|stale_docs|unbounded_dependency
    title: str
    description: str
    priority: OpportunityPriority

    # Optional evidence.
    repo_path: str | None = None
    file_path: str | None = None
    line_number: int | None = None
    evidence: str | None = None

    # A stable identifier used for dedup/rate limiting.
    opportunity_id: str | None = None

    # Computed during prioritization.
    score: float = 0.0


@dataclass
class ProactiveSettings:
    enabled: bool = True

    # Rate limiting and deduping.
    scan_interval_seconds: int = 60 * 60  # 1 hour
    max_opportunities_per_project_per_day: int = 5
    max_opportunities_per_scan: int = 50

    # Quiet hours (local time). If now is within quiet hours, scans return [].
    quiet_hours_start: str | None = None  # "22:00"
    quiet_hours_end: str | None = None  # "07:00"

    # Scan tuning.
    max_files_per_repo: int = 800
    max_file_bytes: int = 200_000

    # Agent-specific toggles.
    run_npm_audit: bool = False
    run_pip_audit: bool = False

    # Heuristics.
    doc_drift_days: int = 14
    large_file_line_threshold: int = 500


def _parse_hhmm(s: str) -> time:
    parts = s.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid HH:MM time: {s}")
    hh, mm = int(parts[0]), int(parts[1])
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"Invalid HH:MM time: {s}")
    return time(hour=hh, minute=mm)


class Observer:
    """Scans project repositories for proactive work opportunities."""

    STATE_FILE = STATE_DIR / "observer-state.json"

    DEFAULT_IGNORE_DIRS = {
        ".git",
        "node_modules",
        "dist",
        "build",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".idea",
        ".vscode",
    }

    CODE_EXTS = {
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".c",
        ".cc",
        ".cpp",
        ".h",
        ".hpp",
        ".cs",
        ".php",
        ".rb",
        ".swift",
    }

    DOC_EXTS = {".md", ".rst", ".txt"}

    TODO_RE = re.compile(r"\b(TODO|FIXME|HACK)\b[:]?(.*)$", re.IGNORECASE)

    def __init__(
        self,
        *,
        settings: ProactiveSettings | None = None,
        state_path: Path | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ):
        self.settings = settings or self._load_settings()
        self.state_path = state_path or self.STATE_FILE
        self._now_fn = now_fn or (lambda: datetime.now().astimezone())

    # ---------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------

    def scan_for_opportunities(self, projects: list[dict[str, Any]]) -> list[Opportunity]:
        """Find proactive work across projects.

        Returns a prioritized list of opportunities. If proactive scanning is
        disabled, in quiet hours, or rate-limited, returns an empty list.
        """

        if not self.settings.enabled:
            return []

        now = self._now_fn()
        if self._in_quiet_hours(now):
            return []

        state = self._load_state()
        if not self._scan_due(state, now):
            return []

        opportunities: list[Opportunity] = []
        for p in projects:
            if p.get("archived"):
                continue
            project_id = str(p.get("id") or "").strip()
            if not project_id:
                continue

            repo_path = Path(p.get("repoPath") or (BASE_DIR / project_id)).resolve()
            if not repo_path.exists() or not repo_path.is_dir():
                continue

            # Per-project daily cap
            if self._count_project_today(project_id, state, now) >= self.settings.max_opportunities_per_project_per_day:
                continue

            try:
                opportunities.extend(self._scan_project(project_id, repo_path, state, now))
            except Exception as e:
                logger.warning(f"[observer] scan failed for {project_id}: {e}")

            if len(opportunities) >= self.settings.max_opportunities_per_scan:
                break

        # Prioritize and persist state.
        opportunities = self._prioritize(opportunities)
        self._save_state_after_scan(state, opportunities, now)
        return opportunities[: self.settings.max_opportunities_per_scan]

    # ---------------------------------------------------------------------
    # Settings / state
    # ---------------------------------------------------------------------

    def _load_settings(self) -> ProactiveSettings:
        raw = get_setting("proactive", None)
        if isinstance(raw, dict):
            # Allow either snake_case or camelCase.
            def g(*keys: str, default: Any = None) -> Any:
                for k in keys:
                    if k in raw:
                        return raw[k]
                return default

            return ProactiveSettings(
                enabled=bool(g("enabled", default=True)),
                scan_interval_seconds=int(g("scan_interval_seconds", "scanIntervalSeconds", default=3600)),
                max_opportunities_per_project_per_day=int(
                    g("max_opportunities_per_project_per_day", "maxOpportunitiesPerProjectPerDay", default=5)
                ),
                max_opportunities_per_scan=int(g("max_opportunities_per_scan", "maxOpportunitiesPerScan", default=50)),
                quiet_hours_start=g("quiet_hours_start", "quietHoursStart", default=None),
                quiet_hours_end=g("quiet_hours_end", "quietHoursEnd", default=None),
                max_files_per_repo=int(g("max_files_per_repo", "maxFilesPerRepo", default=800)),
                max_file_bytes=int(g("max_file_bytes", "maxFileBytes", default=200_000)),
                run_npm_audit=bool(g("run_npm_audit", "runNpmAudit", default=False)),
                run_pip_audit=bool(g("run_pip_audit", "runPipAudit", default=False)),
                doc_drift_days=int(g("doc_drift_days", "docDriftDays", default=14)),
                large_file_line_threshold=int(g("large_file_line_threshold", "largeFileLineThreshold", default=500)),
            )

        # Back-compat: flat settings.
        return ProactiveSettings(
            enabled=bool(get_setting("proactive_enabled", True)),
            scan_interval_seconds=int(get_setting("proactive_scan_interval_seconds", 3600)),
            max_opportunities_per_project_per_day=int(get_setting("proactive_max_per_project_per_day", 5)),
            max_opportunities_per_scan=int(get_setting("proactive_max_per_scan", 50)),
            quiet_hours_start=get_setting("proactive_quiet_hours_start", None),
            quiet_hours_end=get_setting("proactive_quiet_hours_end", None),
        )

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"lastScanAt": None, "seen": {}, "counts": {}}
        try:
            data = json.loads(self.state_path.read_text())
            if not isinstance(data, dict):
                return {"lastScanAt": None, "seen": {}, "counts": {}}
            data.setdefault("seen", {})
            data.setdefault("counts", {})
            return data
        except Exception:
            return {"lastScanAt": None, "seen": {}, "counts": {}}

    def _save_state(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, indent=2) + "\n")

    def _scan_due(self, state: dict[str, Any], now: datetime) -> bool:
        last = state.get("lastScanAt")
        if not last:
            return True
        try:
            last_dt = datetime.fromisoformat(str(last))
        except Exception:
            return True
        return (now - last_dt).total_seconds() >= self.settings.scan_interval_seconds

    def _count_project_today(self, project_id: str, state: dict[str, Any], now: datetime) -> int:
        day = now.date().isoformat()
        return int(state.get("counts", {}).get(day, {}).get(project_id, 0))

    def _in_quiet_hours(self, now: datetime) -> bool:
        start_s = self.settings.quiet_hours_start
        end_s = self.settings.quiet_hours_end
        if not start_s or not end_s:
            return False
        try:
            start_t = _parse_hhmm(start_s)
            end_t = _parse_hhmm(end_s)
        except Exception:
            return False

        cur_t = now.timetz().replace(tzinfo=None)
        if start_t == end_t:
            return False
        if start_t < end_t:
            return start_t <= cur_t < end_t
        # Window spans midnight.
        return cur_t >= start_t or cur_t < end_t

    # ---------------------------------------------------------------------
    # Scanning
    # ---------------------------------------------------------------------

    def _scan_project(
        self, project_id: str, repo_path: Path, state: dict[str, Any], now: datetime
    ) -> list[Opportunity]:
        ops: list[Opportunity] = []

        ops.extend(self.scan_for_code_improvements(project_id, repo_path))  # Programmer
        ops.extend(self.scan_for_stale_deps(project_id, repo_path))  # Researcher
        ops.extend(self.scan_for_quality_issues(project_id, repo_path))  # Reviewer
        ops.extend(self.scan_for_doc_drift(project_id, repo_path, now))  # Writer
        ops.extend(self.scan_for_tech_debt(project_id, repo_path))  # Architect

        # Dedup via state.seen
        seen: dict[str, str] = state.setdefault("seen", {})
        out: list[Opportunity] = []
        for op in ops:
            op_id = op.opportunity_id or self._make_opportunity_id(op)
            if op_id in seen:
                continue
            out.append(
                Opportunity(
                    **{**asdict(op), "opportunity_id": op_id}  # type: ignore[arg-type]
                )
            )
        return out

    def scan_for_code_improvements(self, project_id: str, repo_path: Path) -> list[Opportunity]:
        """Programmer scan: TODO/FIXME/HACK markers."""

        ops: list[Opportunity] = []
        for path in self._iter_repo_files(repo_path):
            if path.suffix.lower() not in self.CODE_EXTS:
                continue
            for line_no, marker, detail in self._find_todos(path):
                title = f"Resolve {marker.upper()} in {path.name}:{line_no}"
                desc = (detail or "").strip()
                if desc:
                    desc = f"Found {marker.upper()} comment: {desc}"
                else:
                    desc = f"Found {marker.upper()} comment; consider addressing or converting to a tracked task."
                ops.append(
                    Opportunity(
                        project_id=project_id,
                        agent_type="programmer",
                        kind="todo_comment",
                        title=title,
                        description=desc,
                        priority=OpportunityPriority.NORMAL,
                        repo_path=str(repo_path),
                        file_path=str(path.relative_to(repo_path)),
                        line_number=line_no,
                        evidence=f"{marker.upper()}: {detail.strip()}" if detail else marker.upper(),
                    )
                )
                if len(ops) >= 20:
                    return ops
        return ops

    def scan_for_stale_deps(self, project_id: str, repo_path: Path) -> list[Opportunity]:
        """Researcher scan: dependency hygiene and (optional) security advisories."""

        ops: list[Opportunity] = []

        # Unpinned Python deps.
        req = repo_path / "requirements.txt"
        if req.exists():
            try:
                text = req.read_text(errors="ignore")
                unpinned = [
                    ln.strip()
                    for ln in text.splitlines()
                    if ln.strip() and not ln.strip().startswith("#") and "==" not in ln and "@" not in ln
                ]
                if unpinned:
                    ops.append(
                        Opportunity(
                            project_id=project_id,
                            agent_type="researcher",
                            kind="unpinned_dependencies",
                            title="Pin versions in requirements.txt",
                            description=(
                                f"Found {len(unpinned)} unpinned dependency entries in requirements.txt. "
                                "Pinning versions improves reproducibility and makes security review easier."
                            ),
                            priority=OpportunityPriority.NORMAL,
                            repo_path=str(repo_path),
                            file_path="requirements.txt",
                            evidence="\n".join(unpinned[:20]),
                        )
                    )
            except Exception:
                pass

        # Optional: npm audit summary.
        pkg_lock = repo_path / "package-lock.json"
        if self.settings.run_npm_audit and pkg_lock.exists() and which("npm"):
            vulns = self._run_npm_audit(repo_path)
            if vulns:
                sev = vulns.get("severity")
                count = vulns.get("count")
                prio = OpportunityPriority.URGENT if sev in ("critical", "high") else OpportunityPriority.HIGH
                ops.append(
                    Opportunity(
                        project_id=project_id,
                        agent_type="researcher",
                        kind="npm_vulnerabilities",
                        title=f"Address npm audit vulnerabilities ({sev}: {count})",
                        description=(
                            "npm audit reported dependency vulnerabilities. Consider updating packages "
                            "or applying recommended fixes."
                        ),
                        priority=prio,
                        repo_path=str(repo_path),
                        file_path="package-lock.json",
                        evidence=json.dumps(vulns.get("summary"), indent=2) if vulns.get("summary") else None,
                    )
                )

        # Optional: pip-audit.
        if self.settings.run_pip_audit and req.exists() and which("pip-audit"):
            audit = self._run_pip_audit(repo_path, req)
            if audit and audit.get("count", 0) > 0:
                ops.append(
                    Opportunity(
                        project_id=project_id,
                        agent_type="researcher",
                        kind="pip_vulnerabilities",
                        title=f"Address pip-audit vulnerabilities ({audit['count']})",
                        description=(
                            "pip-audit reported known vulnerabilities in Python dependencies. "
                            "Consider upgrading affected packages or adding constraints."
                        ),
                        priority=OpportunityPriority.URGENT,
                        repo_path=str(repo_path),
                        file_path="requirements.txt",
                        evidence=audit.get("evidence"),
                    )
                )

        return ops

    def scan_for_quality_issues(self, project_id: str, repo_path: Path) -> list[Opportunity]:
        """Reviewer scan: basic testing/coverage hygiene heuristics."""

        ops: list[Opportunity] = []

        # Missing tests in Python repos.
        has_py = any((repo_path / p).exists() for p in ("pyproject.toml", "requirements.txt", "setup.py"))
        if has_py:
            tests_dir = repo_path / "tests"
            if not tests_dir.exists():
                ops.append(
                    Opportunity(
                        project_id=project_id,
                        agent_type="reviewer",
                        kind="missing_tests",
                        title="Add a minimal tests/ suite",
                        description=(
                            "Python project appears to have no tests/ directory. "
                            "Adding smoke tests (and wiring CI to run them) improves confidence."
                        ),
                        priority=OpportunityPriority.HIGH,
                        repo_path=str(repo_path),
                    )
                )

            cov_cfg = any((repo_path / p).exists() for p in (".coveragerc", "pytest.ini", "pyproject.toml"))
            if tests_dir.exists() and not cov_cfg:
                ops.append(
                    Opportunity(
                        project_id=project_id,
                        agent_type="reviewer",
                        kind="missing_coverage_config",
                        title="Add coverage configuration",
                        description=(
                            "Tests exist but no obvious coverage configuration was found (.coveragerc/pytest.ini). "
                            "Consider adding coverage.py settings and a CI coverage step."
                        ),
                        priority=OpportunityPriority.NORMAL,
                        repo_path=str(repo_path),
                    )
                )

        return ops

    def scan_for_doc_drift(self, project_id: str, repo_path: Path, now: datetime) -> list[Opportunity]:
        """Writer scan: stale docs and missing top-level documentation."""

        ops: list[Opportunity] = []

        readme = repo_path / "README.md"
        docs_dir = repo_path / "docs"

        if not readme.exists():
            ops.append(
                Opportunity(
                    project_id=project_id,
                    agent_type="writer",
                    kind="missing_readme",
                    title="Add a README.md",
                    description=(
                        "Repository has no README.md. Add a short overview, setup, and run/test instructions."
                    ),
                    priority=OpportunityPriority.HIGH,
                    repo_path=str(repo_path),
                )
            )

        # Doc drift heuristic: if latest code change is significantly newer than latest doc change.
        latest_code = self._latest_mtime(repo_path, exts=self.CODE_EXTS)
        latest_docs = self._latest_mtime(repo_path, exts=self.DOC_EXTS, prefer_dirs={"docs", "doc"})

        if latest_code and latest_docs:
            drift_days = (datetime.fromtimestamp(latest_code) - datetime.fromtimestamp(latest_docs)).days
            if drift_days >= self.settings.doc_drift_days:
                ops.append(
                    Opportunity(
                        project_id=project_id,
                        agent_type="writer",
                        kind="stale_docs",
                        title="Review docs for drift",
                        description=(
                            f"Latest code change is ~{drift_days} day(s) newer than latest docs. "
                            "Consider reviewing docs for accuracy and updating examples."
                        ),
                        priority=OpportunityPriority.NORMAL,
                        repo_path=str(repo_path),
                        evidence=f"docDriftDays={drift_days}",
                    )
                )

        # If docs directory exists but is empty/very small.
        if docs_dir.exists() and docs_dir.is_dir():
            doc_files = list(docs_dir.rglob("*.md"))
            if len(doc_files) == 0:
                ops.append(
                    Opportunity(
                        project_id=project_id,
                        agent_type="writer",
                        kind="empty_docs_dir",
                        title="Populate docs/ directory",
                        description="docs/ exists but contains no markdown files. Consider adding usage or API docs.",
                        priority=OpportunityPriority.LOW,
                        repo_path=str(repo_path),
                        file_path="docs/",
                    )
                )

        return ops

    def scan_for_tech_debt(self, project_id: str, repo_path: Path) -> list[Opportunity]:
        """Architect scan: large files as a proxy for complexity hotspots."""

        ops: list[Opportunity] = []
        large: list[tuple[int, Path]] = []

        for p in self._iter_repo_files(repo_path):
            if p.suffix.lower() not in self.CODE_EXTS:
                continue
            try:
                n = self._count_lines(p)
            except Exception:
                continue
            if n >= self.settings.large_file_line_threshold:
                large.append((n, p))

        large.sort(reverse=True, key=lambda t: t[0])
        for n, p in large[:5]:
            ops.append(
                Opportunity(
                    project_id=project_id,
                    agent_type="architect",
                    kind="complexity_hotspot",
                    title=f"Consider refactoring large file ({n} lines): {p.name}",
                    description=(
                        f"File {p.relative_to(repo_path)} is ~{n} lines. "
                        "Large modules can become maintenance hotspots; consider splitting or simplifying."
                    ),
                    priority=OpportunityPriority.LOW,
                    repo_path=str(repo_path),
                    file_path=str(p.relative_to(repo_path)),
                    evidence=f"lineCount={n}",
                )
            )

        return ops

    # ---------------------------------------------------------------------
    # Prioritization
    # ---------------------------------------------------------------------

    def _prioritize(self, ops: list[Opportunity]) -> list[Opportunity]:
        def base_score(op: Opportunity) -> float:
            # Higher score means more important.
            prio = int(op.priority)
            prio_score = {1: 100, 2: 70, 3: 40, 4: 20, 5: 5}.get(prio, 0)

            # Agent-type slight weighting (security/research & reviewer issues bubble up).
            agent_weight = {
                "researcher": 1.15,
                "reviewer": 1.10,
                "programmer": 1.0,
                "writer": 0.95,
                "architect": 0.9,
            }.get(op.agent_type, 1.0)

            return prio_score * agent_weight

        out: list[Opportunity] = []
        for op in ops:
            out.append(
                Opportunity(
                    **{**asdict(op), "score": float(base_score(op))}  # type: ignore[arg-type]
                )
            )

        out.sort(key=lambda o: (-o.score, o.project_id, o.agent_type, o.kind, o.title))
        return out

    # ---------------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------------

    def _save_state_after_scan(self, state: dict[str, Any], ops: list[Opportunity], now: datetime) -> None:
        state["lastScanAt"] = now.isoformat()

        # Per-day per-project counts.
        day = now.date().isoformat()
        counts = state.setdefault("counts", {}).setdefault(day, {})

        seen = state.setdefault("seen", {})
        for op in ops:
            op_id = op.opportunity_id or self._make_opportunity_id(op)
            seen[op_id] = now.isoformat()
            counts[op.project_id] = int(counts.get(op.project_id, 0)) + 1

        # Prune old counts beyond a week.
        try:
            cutoff = now.date() - timedelta(days=7)
            for d in list(state.get("counts", {}).keys()):
                try:
                    if date.fromisoformat(d) < cutoff:
                        state["counts"].pop(d, None)
                except Exception:
                    continue
        except Exception:
            pass

        # Prune seen older than 30 days.
        cutoff_dt = now - timedelta(days=30)
        for k, v in list(seen.items()):
            try:
                dt = datetime.fromisoformat(v)
                if dt < cutoff_dt:
                    seen.pop(k, None)
            except Exception:
                continue

        self._save_state(state)

    def _make_opportunity_id(self, op: Opportunity) -> str:
        raw = "::".join(
            [
                op.project_id,
                op.agent_type,
                op.kind,
                op.title,
                op.file_path or "",
                str(op.line_number or ""),
            ]
        ).lower()
        return sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _iter_repo_files(self, repo_path: Path) -> Iterable[Path]:
        n = 0
        for root, dirs, files in os.walk(repo_path):
            # In-place filter for ignored directories.
            dirs[:] = [d for d in dirs if d not in self.DEFAULT_IGNORE_DIRS and not d.startswith(".")]

            for name in files:
                if name.startswith("."):
                    continue
                p = Path(root) / name
                try:
                    if p.is_symlink():
                        continue
                    if p.stat().st_size > self.settings.max_file_bytes:
                        continue
                except Exception:
                    continue

                n += 1
                if n > self.settings.max_files_per_repo:
                    return
                yield p

    def _find_todos(self, path: Path) -> Iterable[tuple[int, str, str]]:
        try:
            text = path.read_text(errors="ignore")
        except Exception:
            return []
        out: list[tuple[int, str, str]] = []
        for i, line in enumerate(text.splitlines(), start=1):
            m = self.TODO_RE.search(line)
            if not m:
                continue
            marker = (m.group(1) or "TODO").strip()
            detail = (m.group(2) or "").strip()
            out.append((i, marker, detail))
            if len(out) >= 5:
                break
        return out

    def _count_lines(self, path: Path) -> int:
        # Avoid loading huge files into memory.
        with open(path, "rb") as f:
            return sum(1 for _ in f)

    def _latest_mtime(self, repo_path: Path, *, exts: set[str], prefer_dirs: set[str] | None = None) -> float | None:
        latest: float | None = None
        prefer_dirs = prefer_dirs or set()

        # If prefer_dirs provided, only walk those subtrees (when they exist)
        # for docs; otherwise walk full tree.
        roots: list[Path]
        if prefer_dirs:
            roots = [repo_path / d for d in prefer_dirs if (repo_path / d).exists()]
            if not roots:
                roots = [repo_path]
        else:
            roots = [repo_path]

        for root in roots:
            for p in self._iter_repo_files(root):
                if p.suffix.lower() not in exts:
                    continue
                try:
                    m = p.stat().st_mtime
                except Exception:
                    continue
                if latest is None or m > latest:
                    latest = m
        return latest

    def _run_npm_audit(self, repo_path: Path) -> dict[str, Any] | None:
        try:
            r = subprocess.run(
                ["npm", "audit", "--json"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=25,
                check=False,
            )
            blob = (r.stdout or "").strip()
            if not blob:
                return None
            data = json.loads(blob)

            # npm v6/v7 format differences. Normalize to a summary.
            summary: dict[str, Any] = {}
            sev = "low"
            count = 0

            if isinstance(data, dict):
                if "metadata" in data and isinstance(data["metadata"], dict):
                    v = data["metadata"].get("vulnerabilities")
                    if isinstance(v, dict):
                        summary = v
                        # Determine max severity present.
                        for s in ("critical", "high", "moderate", "low"):
                            if int(v.get(s, 0) or 0) > 0:
                                sev = s
                                break
                        count = int(v.get("total", 0) or 0)

                # npm v9+ may include vulnerabilities object directly.
                if not summary and "vulnerabilities" in data and isinstance(data["vulnerabilities"], dict):
                    vulns = data["vulnerabilities"]
                    # Count only actionable severities.
                    by_sev: dict[str, int] = {"low": 0, "moderate": 0, "high": 0, "critical": 0}
                    for _, item in vulns.items():
                        if not isinstance(item, dict):
                            continue
                        s = str(item.get("severity") or "").lower()
                        if s in by_sev:
                            by_sev[s] += 1
                    summary = by_sev
                    for s in ("critical", "high", "moderate", "low"):
                        if by_sev.get(s, 0) > 0:
                            sev = s
                            break
                    count = sum(by_sev.values())

            if count <= 0:
                return None
            return {"severity": sev, "count": count, "summary": summary}
        except Exception:
            return None

    def _run_pip_audit(self, repo_path: Path, req_path: Path) -> dict[str, Any] | None:
        try:
            r = subprocess.run(
                ["pip-audit", "-r", str(req_path), "-f", "json"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=35,
                check=False,
            )
            blob = (r.stdout or "").strip()
            if not blob:
                return None
            data = json.loads(blob)
            if not isinstance(data, list):
                return None
            count = len(data)
            evidence = "\n".join(
                [
                    f"- {item.get('name')} {item.get('version')}: {', '.join((v.get('id') for v in item.get('vulns', []) if isinstance(v, dict)))}"
                    for item in data[:10]
                    if isinstance(item, dict)
                ]
            )
            return {"count": count, "evidence": evidence}
        except Exception:
            return None
