"""orchestrator.services.opportunities

Opportunity detectors for each agent type.

This module provides a structured API for detecting proactive work opportunities
across different agent specializations (programmer, researcher, reviewer, writer, architect).

Each detector class focuses on a specific agent type and provides methods to scan
projects for relevant opportunities.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any

from orchestrator.core.observer import Opportunity, OpportunityPriority
from orchestrator.utils.executables import which

logger = logging.getLogger(__name__)


class ProgrammerOpportunities:
    """Detects code improvement opportunities for programmer agents."""

    TODO_RE = re.compile(r"\b(TODO|FIXME|HACK)\b[:]?(.*)$", re.IGNORECASE)
    CODE_EXTS = {
        ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".kt",
        ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".php", ".rb", ".swift",
    }
    IGNORE_DIRS = {
        ".git", "node_modules", "dist", "build", ".venv", "venv",
        "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    }

    def scan_todos(self, project: dict[str, Any]) -> list[Opportunity]:
        """Find TODO/FIXME/HACK comments in code."""
        project_id = str(project.get("id", ""))
        repo_path = Path(project.get("repoPath", ""))
        
        if not repo_path.exists():
            return []

        opportunities: list[Opportunity] = []
        
        for path in self._iter_code_files(repo_path):
            for line_no, marker, detail in self._find_todos(path):
                title = f"Resolve {marker.upper()} in {path.name}:{line_no}"
                desc = (detail or "").strip()
                if desc:
                    desc = f"Found {marker.upper()} comment: {desc}"
                else:
                    desc = f"Found {marker.upper()} comment; consider addressing or converting to a tracked task."
                
                opportunities.append(
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
                
                if len(opportunities) >= 20:
                    return opportunities
        
        return opportunities

    def scan_warnings(self, project: dict[str, Any]) -> list[Opportunity]:
        """Run build tools and parse warnings."""
        project_id = str(project.get("id", ""))
        repo_path = Path(project.get("repoPath", ""))
        
        if not repo_path.exists():
            return []

        opportunities: list[Opportunity] = []
        
        # Python: mypy type checking
        if self._has_python(repo_path):
            warnings = self._run_mypy(repo_path)
            if warnings:
                opportunities.append(
                    Opportunity(
                        project_id=project_id,
                        agent_type="programmer",
                        kind="type_warnings",
                        title=f"Fix mypy type warnings ({warnings['count']})",
                        description=(
                            f"mypy found {warnings['count']} type issues. "
                            "Fixing type warnings improves code safety and maintainability."
                        ),
                        priority=OpportunityPriority.NORMAL,
                        repo_path=str(repo_path),
                        evidence=warnings.get("sample", ""),
                    )
                )
        
        # TypeScript: tsc compiler warnings
        if self._has_typescript(repo_path):
            warnings = self._run_tsc(repo_path)
            if warnings:
                opportunities.append(
                    Opportunity(
                        project_id=project_id,
                        agent_type="programmer",
                        kind="typescript_warnings",
                        title=f"Fix TypeScript compiler warnings ({warnings['count']})",
                        description=(
                            f"TypeScript compiler found {warnings['count']} issues. "
                            "Addressing these improves type safety."
                        ),
                        priority=OpportunityPriority.NORMAL,
                        repo_path=str(repo_path),
                        evidence=warnings.get("sample", ""),
                    )
                )
        
        return opportunities

    def scan_simple_fixes(self, project: dict[str, Any]) -> list[Opportunity]:
        """Detect obvious improvements (e.g., missing .gitignore, hardcoded secrets patterns)."""
        project_id = str(project.get("id", ""))
        repo_path = Path(project.get("repoPath", ""))
        
        if not repo_path.exists():
            return []

        opportunities: list[Opportunity] = []
        
        # Missing .gitignore
        gitignore = repo_path / ".gitignore"
        if not gitignore.exists():
            opportunities.append(
                Opportunity(
                    project_id=project_id,
                    agent_type="programmer",
                    kind="missing_gitignore",
                    title="Add .gitignore file",
                    description="Project lacks a .gitignore. Add one to prevent committing build artifacts and credentials.",
                    priority=OpportunityPriority.HIGH,
                    repo_path=str(repo_path),
                )
            )
        
        # Check for hardcoded secrets patterns
        secrets = self._scan_for_secrets(repo_path)
        if secrets:
            opportunities.append(
                Opportunity(
                    project_id=project_id,
                    agent_type="programmer",
                    kind="hardcoded_secrets",
                    title=f"Review potential hardcoded secrets ({len(secrets)} found)",
                    description=(
                        "Found patterns that may be hardcoded secrets (API keys, tokens). "
                        "Review and move to environment variables or secret management."
                    ),
                    priority=OpportunityPriority.URGENT,
                    repo_path=str(repo_path),
                    evidence="\n".join(secrets[:5]),
                )
            )
        
        # Inconsistent formatting (no formatter config)
        if self._has_python(repo_path):
            if not any((repo_path / f).exists() for f in [".ruff.toml", "pyproject.toml", ".black"]):
                opportunities.append(
                    Opportunity(
                        project_id=project_id,
                        agent_type="programmer",
                        kind="missing_formatter_config",
                        title="Add Python code formatter configuration",
                        description="No formatter config found (ruff/black). Adding one ensures consistent code style.",
                        priority=OpportunityPriority.LOW,
                        repo_path=str(repo_path),
                    )
                )
        
        return opportunities

    # Helper methods
    
    def _iter_code_files(self, repo_path: Path, max_files: int = 500):
        """Iterate over code files in the repository."""
        count = 0
        for root, dirs, files in repo_path.walk():
            dirs[:] = [d for d in dirs if d not in self.IGNORE_DIRS and not d.startswith(".")]
            
            for name in files:
                if name.startswith("."):
                    continue
                path = root / name
                if path.suffix.lower() in self.CODE_EXTS:
                    count += 1
                    if count > max_files:
                        return
                    yield path

    def _find_todos(self, path: Path) -> list[tuple[int, str, str]]:
        """Extract TODO/FIXME/HACK comments from a file."""
        try:
            text = path.read_text(errors="ignore")
        except Exception:
            return []
        
        results: list[tuple[int, str, str]] = []
        for i, line in enumerate(text.splitlines(), start=1):
            m = self.TODO_RE.search(line)
            if m:
                marker = (m.group(1) or "TODO").strip()
                detail = (m.group(2) or "").strip()
                results.append((i, marker, detail))
                if len(results) >= 5:
                    break
        return results

    def _has_python(self, repo_path: Path) -> bool:
        """Check if project has Python code."""
        return any((repo_path / f).exists() for f in ["pyproject.toml", "requirements.txt", "setup.py"])

    def _has_typescript(self, repo_path: Path) -> bool:
        """Check if project has TypeScript code."""
        return (repo_path / "tsconfig.json").exists()

    def _run_mypy(self, repo_path: Path) -> dict[str, Any] | None:
        """Run mypy and return warning summary."""
        mypy_path = which("mypy")
        if not mypy_path:
            logger.debug(f"Skipping mypy check for {repo_path.name} - mypy not available")
            return None
        
        try:
            result = subprocess.run(
                ["mypy", ".", "--no-error-summary"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            
            output = result.stdout + result.stderr
            lines = [l for l in output.splitlines() if ": error:" in l or ": warning:" in l]
            
            if not lines:
                return None
            
            return {
                "count": len(lines),
                "sample": "\n".join(lines[:5]),
            }
        except Exception:
            return None

    def _run_tsc(self, repo_path: Path) -> dict[str, Any] | None:
        """Run tsc and return warning summary."""
        tsc_path = which("tsc")
        if not tsc_path:
            logger.debug(f"Skipping TypeScript check for {repo_path.name} - tsc not available")
            return None
        
        try:
            result = subprocess.run(
                ["tsc", "--noEmit"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            
            output = result.stdout + result.stderr
            lines = [l for l in output.splitlines() if " error TS" in l or " warning TS" in l]
            
            if not lines:
                return None
            
            return {
                "count": len(lines),
                "sample": "\n".join(lines[:5]),
            }
        except Exception:
            return None

    def _scan_for_secrets(self, repo_path: Path) -> list[str]:
        """Scan for potential hardcoded secrets."""
        patterns = [
            (r'api[_-]?key\s*=\s*["\']([a-zA-Z0-9_\-]{20,})["\']', "API key"),
            (r'secret[_-]?key\s*=\s*["\']([a-zA-Z0-9_\-]{20,})["\']', "Secret key"),
            (r'token\s*=\s*["\']([a-zA-Z0-9_\-]{20,})["\']', "Token"),
            (r'password\s*=\s*["\'][^"\']{8,}["\']', "Password"),
        ]
        
        findings: list[str] = []
        for path in self._iter_code_files(repo_path, max_files=200):
            try:
                content = path.read_text(errors="ignore")
                for pattern, kind in patterns:
                    matches = re.finditer(pattern, content, re.IGNORECASE)
                    for m in matches:
                        rel_path = path.relative_to(repo_path)
                        findings.append(f"{rel_path}: {kind} pattern detected")
                        if len(findings) >= 10:
                            return findings
            except Exception:
                continue
        
        return findings


class ResearcherOpportunities:
    """Detects research and dependency opportunities."""

    def scan_outdated_deps(self, project: dict[str, Any]) -> list[Opportunity]:
        """Check for outdated package versions."""
        project_id = str(project.get("id", ""))
        repo_path = Path(project.get("repoPath", ""))
        
        if not repo_path.exists():
            return []

        opportunities: list[Opportunity] = []
        
        # Python: unpinned dependencies
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
                    opportunities.append(
                        Opportunity(
                            project_id=project_id,
                            agent_type="researcher",
                            kind="unpinned_dependencies",
                            title="Pin versions in requirements.txt",
                            description=(
                                f"Found {len(unpinned)} unpinned dependency entries. "
                                "Pinning versions improves reproducibility and security review."
                            ),
                            priority=OpportunityPriority.NORMAL,
                            repo_path=str(repo_path),
                            file_path="requirements.txt",
                            evidence="\n".join(unpinned[:20]),
                        )
                    )
            except Exception:
                pass
        
        # NPM: outdated packages
        if (repo_path / "package.json").exists() and which("npm"):
            outdated = self._check_npm_outdated(repo_path)
            if outdated:
                opportunities.append(
                    Opportunity(
                        project_id=project_id,
                        agent_type="researcher",
                        kind="outdated_npm_packages",
                        title=f"Update outdated npm packages ({outdated['count']})",
                        description=(
                            f"Found {outdated['count']} outdated npm packages. "
                            "Review and update to get bug fixes and security patches."
                        ),
                        priority=OpportunityPriority.NORMAL,
                        repo_path=str(repo_path),
                        file_path="package.json",
                        evidence=outdated.get("sample", ""),
                    )
                )
        
        return opportunities

    def scan_security_advisories(self, project: dict[str, Any]) -> list[Opportunity]:
        """Check for known security vulnerabilities."""
        project_id = str(project.get("id", ""))
        repo_path = Path(project.get("repoPath", ""))
        
        if not repo_path.exists():
            return []

        opportunities: list[Opportunity] = []
        
        # NPM audit
        if (repo_path / "package-lock.json").exists() and which("npm"):
            vulns = self._run_npm_audit(repo_path)
            if vulns:
                sev = vulns.get("severity", "low")
                count = vulns.get("count", 0)
                prio = OpportunityPriority.URGENT if sev in ("critical", "high") else OpportunityPriority.HIGH
                opportunities.append(
                    Opportunity(
                        project_id=project_id,
                        agent_type="researcher",
                        kind="npm_vulnerabilities",
                        title=f"Fix npm security vulnerabilities ({sev}: {count})",
                        description=(
                            "npm audit found security vulnerabilities. "
                            "Update packages or apply recommended fixes."
                        ),
                        priority=prio,
                        repo_path=str(repo_path),
                        file_path="package-lock.json",
                        evidence=json.dumps(vulns.get("summary"), indent=2) if vulns.get("summary") else None,
                    )
                )
        
        # pip-audit
        req = repo_path / "requirements.txt"
        if req.exists() and which("pip-audit"):
            audit = self._run_pip_audit(repo_path, req)
            if audit and audit.get("count", 0) > 0:
                opportunities.append(
                    Opportunity(
                        project_id=project_id,
                        agent_type="researcher",
                        kind="pip_vulnerabilities",
                        title=f"Fix Python security vulnerabilities ({audit['count']})",
                        description=(
                            "pip-audit found known vulnerabilities. "
                            "Upgrade affected packages or add constraints."
                        ),
                        priority=OpportunityPriority.URGENT,
                        repo_path=str(repo_path),
                        file_path="requirements.txt",
                        evidence=audit.get("evidence"),
                    )
                )
        
        return opportunities

    def scan_deprecated_apis(self, project: dict[str, Any]) -> list[Opportunity]:
        """Find usage of deprecated APIs and patterns."""
        project_id = str(project.get("id", ""))
        repo_path = Path(project.get("repoPath", ""))
        
        if not repo_path.exists():
            return []

        opportunities: list[Opportunity] = []
        
        # Python: deprecated imports/patterns
        deprecated_patterns = [
            (r'from\s+distutils\s+import', "distutils is deprecated, use setuptools"),
            (r'from\s+imp\s+import', "imp module is deprecated, use importlib"),
            (r'assertEquals\(', "assertEquals is deprecated, use assertEqual"),
        ]
        
        findings = self._scan_patterns(repo_path, deprecated_patterns, [".py"])
        if findings:
            opportunities.append(
                Opportunity(
                    project_id=project_id,
                    agent_type="researcher",
                    kind="deprecated_api_usage",
                    title=f"Replace deprecated API usage ({len(findings)} found)",
                    description=(
                        "Found usage of deprecated APIs. "
                        "Migrate to recommended alternatives before support is removed."
                    ),
                    priority=OpportunityPriority.HIGH,
                    repo_path=str(repo_path),
                    evidence="\n".join(findings[:10]),
                )
            )
        
        return opportunities

    # Helper methods
    
    def _check_npm_outdated(self, repo_path: Path) -> dict[str, Any] | None:
        """Check for outdated npm packages."""
        try:
            result = subprocess.run(
                ["npm", "outdated", "--json"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=25,
                check=False,
            )
            
            if not result.stdout.strip():
                return None
            
            data = json.loads(result.stdout)
            if not data:
                return None
            
            count = len(data)
            sample = "\n".join([f"- {k}: {v.get('current')} → {v.get('latest')}" for k, v in list(data.items())[:10]])
            
            return {"count": count, "sample": sample}
        except Exception:
            return None

    def _run_npm_audit(self, repo_path: Path) -> dict[str, Any] | None:
        """Run npm audit."""
        try:
            result = subprocess.run(
                ["npm", "audit", "--json"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=25,
                check=False,
            )
            
            if not result.stdout.strip():
                return None
            
            data = json.loads(result.stdout)
            if not isinstance(data, dict):
                return None
            
            summary = {}
            sev = "low"
            count = 0
            
            if "metadata" in data and isinstance(data["metadata"], dict):
                v = data["metadata"].get("vulnerabilities")
                if isinstance(v, dict):
                    summary = v
                    for s in ("critical", "high", "moderate", "low"):
                        if int(v.get(s, 0) or 0) > 0:
                            sev = s
                            break
                    count = int(v.get("total", 0) or 0)
            
            if count <= 0:
                return None
            
            return {"severity": sev, "count": count, "summary": summary}
        except Exception:
            return None

    def _run_pip_audit(self, repo_path: Path, req_path: Path) -> dict[str, Any] | None:
        """Run pip-audit."""
        try:
            result = subprocess.run(
                ["pip-audit", "-r", str(req_path), "-f", "json"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=35,
                check=False,
            )
            
            if not result.stdout.strip():
                return None
            
            data = json.loads(result.stdout)
            if not isinstance(data, list):
                return None
            
            count = len(data)
            if count == 0:
                return None
            
            evidence = "\n".join([
                f"- {item.get('name')} {item.get('version')}: "
                f"{', '.join((v.get('id') for v in item.get('vulns', []) if isinstance(v, dict)))}"
                for item in data[:10]
                if isinstance(item, dict)
            ])
            
            return {"count": count, "evidence": evidence}
        except Exception:
            return None

    def _scan_patterns(self, repo_path: Path, patterns: list[tuple[str, str]], exts: list[str]) -> list[str]:
        """Scan code files for regex patterns."""
        findings: list[str] = []
        
        for root, dirs, files in repo_path.walk():
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in {"node_modules", "dist", "build"}]
            
            for name in files:
                path = root / name
                if path.suffix not in exts:
                    continue
                
                try:
                    content = path.read_text(errors="ignore")
                    for pattern, reason in patterns:
                        if re.search(pattern, content):
                            rel_path = path.relative_to(repo_path)
                            findings.append(f"{rel_path}: {reason}")
                            if len(findings) >= 20:
                                return findings
                except Exception:
                    continue
        
        return findings


class ReviewerOpportunities:
    """Detects code quality and testing opportunities."""

    def scan_code_smells(self, project: dict[str, Any]) -> list[Opportunity]:
        """Run linters and detect code smells."""
        project_id = str(project.get("id", ""))
        repo_path = Path(project.get("repoPath", ""))
        
        if not repo_path.exists():
            return []

        opportunities: list[Opportunity] = []
        
        # Python: ruff linting
        if self._has_python_files(repo_path) and which("ruff"):
            issues = self._run_ruff(repo_path)
            if issues:
                opportunities.append(
                    Opportunity(
                        project_id=project_id,
                        agent_type="reviewer",
                        kind="lint_issues",
                        title=f"Fix ruff linting issues ({issues['count']})",
                        description=(
                            f"ruff found {issues['count']} code quality issues. "
                            "Address these to improve code maintainability."
                        ),
                        priority=OpportunityPriority.NORMAL,
                        repo_path=str(repo_path),
                        evidence=issues.get("sample", ""),
                    )
                )
        
        # TypeScript/JavaScript: eslint
        if (repo_path / ".eslintrc.js").exists() or (repo_path / ".eslintrc.json").exists():
            if which("eslint"):
                issues = self._run_eslint(repo_path)
                if issues:
                    opportunities.append(
                        Opportunity(
                            project_id=project_id,
                            agent_type="reviewer",
                            kind="eslint_issues",
                            title=f"Fix ESLint issues ({issues['count']})",
                            description=(
                                f"ESLint found {issues['count']} issues. "
                                "Review and fix for better code quality."
                            ),
                            priority=OpportunityPriority.NORMAL,
                            repo_path=str(repo_path),
                            evidence=issues.get("sample", ""),
                        )
                    )
        
        return opportunities

    def scan_coverage_gaps(self, project: dict[str, Any]) -> list[Opportunity]:
        """Find untested code."""
        project_id = str(project.get("id", ""))
        repo_path = Path(project.get("repoPath", ""))
        
        if not repo_path.exists():
            return []

        opportunities: list[Opportunity] = []
        
        # Python: missing tests directory
        has_py = self._has_python_files(repo_path)
        if has_py:
            tests_dir = repo_path / "tests"
            if not tests_dir.exists():
                opportunities.append(
                    Opportunity(
                        project_id=project_id,
                        agent_type="reviewer",
                        kind="missing_tests",
                        title="Add tests/ directory and initial test suite",
                        description=(
                            "Python project has no tests/ directory. "
                            "Adding tests improves confidence and prevents regressions."
                        ),
                        priority=OpportunityPriority.HIGH,
                        repo_path=str(repo_path),
                    )
                )
            
            # Missing coverage configuration
            cov_cfg = any((repo_path / p).exists() for p in [".coveragerc", "pytest.ini", "pyproject.toml"])
            if tests_dir.exists() and not cov_cfg:
                opportunities.append(
                    Opportunity(
                        project_id=project_id,
                        agent_type="reviewer",
                        kind="missing_coverage_config",
                        title="Add coverage configuration",
                        description=(
                            "Tests exist but no coverage config. "
                            "Add .coveragerc or pytest.ini with coverage settings."
                        ),
                        priority=OpportunityPriority.NORMAL,
                        repo_path=str(repo_path),
                    )
                )
        
        return opportunities

    def scan_quality_trends(self, project: dict[str, Any]) -> list[Opportunity]:
        """Compare quality metrics to baseline."""
        project_id = str(project.get("id", ""))
        repo_path = Path(project.get("repoPath", ""))
        
        if not repo_path.exists():
            return []

        opportunities: list[Opportunity] = []
        
        # This is a placeholder for trend analysis
        # Would require storing historical metrics and comparing
        # For now, we can check for absence of CI configuration
        
        ci_files = [".github/workflows", ".gitlab-ci.yml", ".circleci/config.yml", "Jenkinsfile"]
        has_ci = any((repo_path / f).exists() for f in ci_files)
        
        if not has_ci:
            opportunities.append(
                Opportunity(
                    project_id=project_id,
                    agent_type="reviewer",
                    kind="missing_ci",
                    title="Add CI/CD configuration",
                    description=(
                        "No CI configuration found. "
                        "Set up GitHub Actions, GitLab CI, or similar to automate testing and quality checks."
                    ),
                    priority=OpportunityPriority.HIGH,
                    repo_path=str(repo_path),
                )
            )
        
        return opportunities

    # Helper methods
    
    def _has_python_files(self, repo_path: Path) -> bool:
        """Check if project has Python files."""
        for root, dirs, files in repo_path.walk():
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
            for name in files:
                if name.endswith(".py"):
                    return True
        return False

    def _run_ruff(self, repo_path: Path) -> dict[str, Any] | None:
        """Run ruff linter."""
        try:
            result = subprocess.run(
                ["ruff", "check", ".", "--output-format", "text"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            
            lines = [l for l in result.stdout.splitlines() if l.strip() and not l.startswith("Found")]
            if not lines:
                return None
            
            return {
                "count": len(lines),
                "sample": "\n".join(lines[:10]),
            }
        except Exception:
            return None

    def _run_eslint(self, repo_path: Path) -> dict[str, Any] | None:
        """Run ESLint."""
        try:
            result = subprocess.run(
                ["eslint", ".", "--format", "compact"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            
            lines = [l for l in result.stdout.splitlines() if ": " in l]
            if not lines:
                return None
            
            return {
                "count": len(lines),
                "sample": "\n".join(lines[:10]),
            }
        except Exception:
            return None


class WriterOpportunities:
    """Detects documentation opportunities."""

    DOC_EXTS = {".md", ".rst", ".txt"}

    def scan_stale_docs(self, project: dict[str, Any]) -> list[Opportunity]:
        """Find docs with timestamps older than recent code changes."""
        from datetime import datetime, timedelta
        
        project_id = str(project.get("id", ""))
        repo_path = Path(project.get("repoPath", ""))
        
        if not repo_path.exists():
            return []

        opportunities: list[Opportunity] = []
        
        # Compare latest code vs docs modification times
        latest_code = self._latest_mtime(repo_path, exts={".py", ".ts", ".js", ".go", ".rs"})
        latest_docs = self._latest_mtime(repo_path, exts=self.DOC_EXTS, prefer_dirs={"docs", "doc"})
        
        if latest_code and latest_docs:
            code_dt = datetime.fromtimestamp(latest_code)
            docs_dt = datetime.fromtimestamp(latest_docs)
            drift_days = (code_dt - docs_dt).days
            
            if drift_days >= 14:  # 2 weeks
                opportunities.append(
                    Opportunity(
                        project_id=project_id,
                        agent_type="writer",
                        kind="stale_docs",
                        title="Review documentation for drift",
                        description=(
                            f"Latest code change is ~{drift_days} days newer than docs. "
                            "Review docs for accuracy and update examples."
                        ),
                        priority=OpportunityPriority.NORMAL,
                        repo_path=str(repo_path),
                        evidence=f"docDriftDays={drift_days}",
                    )
                )
        
        return opportunities

    def scan_missing_docs(self, project: dict[str, Any]) -> list[Opportunity]:
        """Find undocumented public APIs and missing documentation."""
        project_id = str(project.get("id", ""))
        repo_path = Path(project.get("repoPath", ""))
        
        if not repo_path.exists():
            return []

        opportunities: list[Opportunity] = []
        
        # Missing README
        readme = repo_path / "README.md"
        if not readme.exists():
            opportunities.append(
                Opportunity(
                    project_id=project_id,
                    agent_type="writer",
                    kind="missing_readme",
                    title="Add README.md",
                    description=(
                        "Repository lacks a README. "
                        "Add overview, setup instructions, and usage examples."
                    ),
                    priority=OpportunityPriority.HIGH,
                    repo_path=str(repo_path),
                )
            )
        
        # Empty docs directory
        docs_dir = repo_path / "docs"
        if docs_dir.exists() and docs_dir.is_dir():
            doc_files = list(docs_dir.rglob("*.md"))
            if len(doc_files) == 0:
                opportunities.append(
                    Opportunity(
                        project_id=project_id,
                        agent_type="writer",
                        kind="empty_docs_dir",
                        title="Populate docs/ directory",
                        description="docs/ exists but is empty. Add usage guides or API documentation.",
                        priority=OpportunityPriority.LOW,
                        repo_path=str(repo_path),
                        file_path="docs/",
                    )
                )
        
        # Missing CONTRIBUTING guide for projects with issues/PRs
        if (repo_path / ".git").exists():
            contributing = repo_path / "CONTRIBUTING.md"
            if not contributing.exists():
                opportunities.append(
                    Opportunity(
                        project_id=project_id,
                        agent_type="writer",
                        kind="missing_contributing",
                        title="Add CONTRIBUTING.md",
                        description=(
                            "Add contributor guidelines to help others contribute effectively."
                        ),
                        priority=OpportunityPriority.LOW,
                        repo_path=str(repo_path),
                    )
                )
        
        return opportunities

    def scan_doc_code_drift(self, project: dict[str, Any]) -> list[Opportunity]:
        """Detect when code has changed but related docs haven't."""
        project_id = str(project.get("id", ""))
        repo_path = Path(project.get("repoPath", ""))
        
        if not repo_path.exists():
            return []

        opportunities: list[Opportunity] = []
        
        # Check for API changes in Python (function signatures)
        # This is a simple heuristic: look for files with recent changes
        # that have corresponding docs that are older
        
        try:
            # Get recently modified code files (last 30 days)
            result = subprocess.run(
                ["git", "log", "--since=30 days ago", "--name-only", "--pretty=format:", "--", "*.py", "*.ts", "*.js"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            
            changed_files = set(l.strip() for l in result.stdout.splitlines() if l.strip())
            
            # For each changed file, check if there's a corresponding doc
            # and if the doc is older
            for file_path in list(changed_files)[:10]:  # Limit check
                code_file = repo_path / file_path
                if not code_file.exists():
                    continue
                
                # Look for docs mentioning this file
                stem = code_file.stem
                doc_candidates = list((repo_path / "docs").rglob(f"*{stem}*.md")) if (repo_path / "docs").exists() else []
                
                if doc_candidates:
                    code_mtime = code_file.stat().st_mtime
                    for doc in doc_candidates:
                        doc_mtime = doc.stat().st_mtime
                        if code_mtime > doc_mtime:
                            opportunities.append(
                                Opportunity(
                                    project_id=project_id,
                                    agent_type="writer",
                                    kind="doc_code_drift",
                                    title=f"Update docs for {file_path}",
                                    description=(
                                        f"Code file {file_path} was modified but related doc {doc.name} wasn't. "
                                        "Review and update documentation."
                                    ),
                                    priority=OpportunityPriority.NORMAL,
                                    repo_path=str(repo_path),
                                    file_path=str(doc.relative_to(repo_path)),
                                )
                            )
                            break  # One opportunity per code file
        except Exception:
            pass
        
        return opportunities

    # Helper methods
    
    def _latest_mtime(self, repo_path: Path, *, exts: set[str], prefer_dirs: set[str] | None = None) -> float | None:
        """Get latest modification time for files with given extensions."""
        latest: float | None = None
        
        roots: list[Path]
        if prefer_dirs:
            roots = [repo_path / d for d in prefer_dirs if (repo_path / d).exists()]
            if not roots:
                roots = [repo_path]
        else:
            roots = [repo_path]
        
        for root in roots:
            for file_root, dirs, files in root.walk():
                dirs[:] = [d for d in dirs if not d.startswith(".") and d not in {"node_modules", "dist"}]
                
                for name in files:
                    path = file_root / name
                    if path.suffix.lower() not in exts:
                        continue
                    try:
                        mtime = path.stat().st_mtime
                        if latest is None or mtime > latest:
                            latest = mtime
                    except Exception:
                        continue
        
        return latest


class ArchitectOpportunities:
    """Detects architectural and technical debt opportunities."""

    CODE_EXTS = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".kt"}

    def scan_complexity_hotspots(self, project: dict[str, Any]) -> list[Opportunity]:
        """Find files with high cyclomatic complexity or line count."""
        project_id = str(project.get("id", ""))
        repo_path = Path(project.get("repoPath", ""))
        
        if not repo_path.exists():
            return []

        opportunities: list[Opportunity] = []
        
        # Find large files (simple heuristic)
        large_files: list[tuple[int, Path]] = []
        
        for root, dirs, files in repo_path.walk():
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in {"node_modules", "dist", "build"}]
            
            for name in files:
                path = root / name
                if path.suffix.lower() not in self.CODE_EXTS:
                    continue
                
                try:
                    line_count = self._count_lines(path)
                    if line_count >= 500:  # Threshold for "large file"
                        large_files.append((line_count, path))
                except Exception:
                    continue
        
        large_files.sort(reverse=True, key=lambda t: t[0])
        
        for line_count, path in large_files[:5]:
            opportunities.append(
                Opportunity(
                    project_id=project_id,
                    agent_type="architect",
                    kind="complexity_hotspot",
                    title=f"Consider refactoring large file: {path.name} ({line_count} lines)",
                    description=(
                        f"File {path.relative_to(repo_path)} has {line_count} lines. "
                        "Large files can be maintenance hotspots; consider splitting or simplifying."
                    ),
                    priority=OpportunityPriority.LOW,
                    repo_path=str(repo_path),
                    file_path=str(path.relative_to(repo_path)),
                    evidence=f"lineCount={line_count}",
                )
            )
        
        return opportunities

    def scan_coupling(self, project: dict[str, Any]) -> list[Opportunity]:
        """Identify high coupling between modules."""
        project_id = str(project.get("id", ""))
        repo_path = Path(project.get("repoPath", ""))
        
        if not repo_path.exists():
            return []

        opportunities: list[Opportunity] = []
        
        # Simple heuristic: count imports in Python files
        # High import counts can indicate tight coupling
        
        import_counts: dict[Path, int] = {}
        
        for root, dirs, files in repo_path.walk():
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in {"node_modules", "__pycache__"}]
            
            for name in files:
                if not name.endswith(".py"):
                    continue
                
                path = root / name
                try:
                    content = path.read_text(errors="ignore")
                    imports = len([l for l in content.splitlines() if l.strip().startswith(("import ", "from "))])
                    if imports >= 20:  # Threshold
                        import_counts[path] = imports
                except Exception:
                    continue
        
        sorted_files = sorted(import_counts.items(), key=lambda x: x[1], reverse=True)
        
        for path, count in sorted_files[:3]:
            opportunities.append(
                Opportunity(
                    project_id=project_id,
                    agent_type="architect",
                    kind="high_coupling",
                    title=f"Review high coupling in {path.name} ({count} imports)",
                    description=(
                        f"File {path.relative_to(repo_path)} has {count} import statements. "
                        "Consider reducing dependencies or splitting into smaller modules."
                    ),
                    priority=OpportunityPriority.LOW,
                    repo_path=str(repo_path),
                    file_path=str(path.relative_to(repo_path)),
                    evidence=f"importCount={count}",
                )
            )
        
        return opportunities

    def scan_repeated_patterns(self, project: dict[str, Any]) -> list[Opportunity]:
        """Find code duplication (DRY violations)."""
        project_id = str(project.get("id", ""))
        repo_path = Path(project.get("repoPath", ""))
        
        if not repo_path.exists():
            return []

        opportunities: list[Opportunity] = []
        
        # Simple heuristic: look for files with very similar names
        # or patterns indicating copy-paste (e.g., file1.py, file2.py)
        
        similar_patterns: list[tuple[str, list[str]]] = []
        file_groups: dict[str, list[str]] = {}
        
        for root, dirs, files in repo_path.walk():
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in {"node_modules", "dist"}]
            
            for name in files:
                if not any(name.endswith(ext) for ext in self.CODE_EXTS):
                    continue
                
                # Group by base name pattern
                base = re.sub(r'\d+', 'N', name)  # Replace numbers with N
                file_groups.setdefault(base, []).append(name)
        
        for pattern, files in file_groups.items():
            if len(files) >= 3:  # At least 3 similar files
                similar_patterns.append((pattern, files))
        
        for pattern, files in similar_patterns[:3]:
            opportunities.append(
                Opportunity(
                    project_id=project_id,
                    agent_type="architect",
                    kind="potential_duplication",
                    title=f"Review potential code duplication: {pattern}",
                    description=(
                        f"Found {len(files)} files with similar naming pattern: {', '.join(files[:5])}. "
                        "This may indicate copy-paste code that could be refactored."
                    ),
                    priority=OpportunityPriority.LOW,
                    repo_path=str(repo_path),
                    evidence=f"files={', '.join(files)}",
                )
            )
        
        return opportunities

    # Helper methods
    
    def _count_lines(self, path: Path) -> int:
        """Count lines in a file."""
        with open(path, "rb") as f:
            return sum(1 for _ in f)
