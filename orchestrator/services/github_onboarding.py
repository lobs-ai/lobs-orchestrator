import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Tuple

from orchestrator.config import BASE_DIR, CONTROL_REPO_PATH
from orchestrator.services.control import ControlManager

logger = logging.getLogger(__name__)


_GITHUB_URL_RE = re.compile(
    r"^(?:https?://)?github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)

_OWNER_REPO_RE = re.compile(r"^(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)$")


@dataclass(frozen=True)
class GitHubRepoRef:
    owner: str
    repo: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


def parse_github_ref(url_or_owner_repo: str) -> GitHubRepoRef:
    """Parse a GitHub repo reference.

    Accepts:
      - https://github.com/owner/repo
      - https://github.com/owner/repo.git
      - owner/repo
    """
    s = (url_or_owner_repo or "").strip()
    if not s:
        raise ValueError("Missing GitHub URL/repo")

    m = _GITHUB_URL_RE.match(s)
    if m:
        owner = m.group("owner")
        repo = m.group("repo")
        return GitHubRepoRef(owner=owner, repo=repo)

    m2 = _OWNER_REPO_RE.match(s)
    if m2:
        return GitHubRepoRef(owner=m2.group("owner"), repo=m2.group("repo"))

    raise ValueError("Invalid GitHub repo reference. Use https://github.com/owner/repo or owner/repo")


def normalize_clone_url(ref: GitHubRepoRef, original: str) -> str:
    """Return a clone URL.

    If the original input was an SSH URL or non-github HTTPS URL, we keep it.
    Otherwise default to https://github.com/owner/repo.git.
    """
    s = (original or "").strip()

    # If user pasted an SSH remote, keep it (supports private repos via SSH keys)
    if s.startswith("git@") or s.startswith("ssh://"):
        return s

    # If they gave a full http(s) github url, normalize it.
    if "github.com" in s:
        return f"https://github.com/{ref.full_name}.git"

    # owner/repo
    return f"https://github.com/{ref.full_name}.git"


def default_clone_path(repo_name: str) -> Path:
    return (Path.home() / repo_name).expanduser().resolve()


def _slugify(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9._-]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "project"


def _pick_project_id(existing: list[dict[str, Any]], desired: str) -> str:
    desired = _slugify(desired)
    existing_ids = {p.get("id") for p in existing}
    if desired not in existing_ids:
        return desired
    i = 2
    while f"{desired}-{i}" in existing_ids:
        i += 1
    return f"{desired}-{i}"


def _ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _is_empty_dir(path: Path) -> bool:
    if not path.exists():
        return True
    if not path.is_dir():
        return False
    return not any(path.iterdir())


def create_project_from_github(
    *,
    url: str,
    clone_path: str | None = None,
    sync_issues: bool = False,
) -> dict[str, Any]:
    """Clone a GitHub repo and create a project entry via control-ops.

    Returns a dict suitable for an API response.
    """
    ref = parse_github_ref(url)

    target_path = Path(clone_path).expanduser().resolve() if clone_path else default_clone_path(ref.repo)

    if target_path.exists() and not _is_empty_dir(target_path):
        raise ValueError(f"Clone path already exists and is not empty: {target_path}")

    _ensure_parent_dir(target_path)

    clone_url = normalize_clone_url(ref, url)

    # Clone repository
    logger.info(f"Cloning {clone_url} -> {target_path}")
    subprocess.run(
        ["git", "clone", clone_url, str(target_path)],
        cwd=str(BASE_DIR),
        check=True,
        capture_output=True,
        text=True,
        timeout=600,
    )

    # Determine a project id that won't collide
    existing_projects: list[dict[str, Any]] = []
    projects_file = CONTROL_REPO_PATH / "state" / "projects.json"
    if projects_file.exists():
        try:
            existing_projects = json.loads(projects_file.read_text()).get("projects", [])
        except Exception:
            existing_projects = []

    project_id = _pick_project_id(existing_projects, ref.repo)

    project: dict[str, Any] = {
        "id": project_id,
        "name": ref.repo,
        "repoPath": str(target_path),
        "source": "github",
        "githubRepo": ref.full_name,
        # Only enable GitHub issue tracking if the user opted in
        "tracking": "github" if sync_issues else "local",
        "archived": False,
        "createdAt": int(time.time() * 1000),
    }

    ControlManager.request_op({
        "type": "add_project",
        "project": project,
        "summary": f"add project {project_id}",
    })

    # Optionally sync issues
    if sync_issues:
        script_path = CONTROL_REPO_PATH / "bin" / "gh-sync"
        if script_path.exists():
            logger.info(f"Syncing GitHub issues for {project_id} via gh-sync")
            subprocess.run(
                [str(script_path), project_id],
                cwd=str(CONTROL_REPO_PATH),
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
        else:
            logger.warning(f"gh-sync script not found at {script_path}; skipping initial sync")

    return {
        "ok": True,
        "project": project,
    }
