import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from orchestrator.services.control import ControlManager


def test_parse_github_ref_accepts_url_and_owner_repo():
    from orchestrator.services.github_onboarding import parse_github_ref

    ref = parse_github_ref("https://github.com/foo/bar")
    assert ref.owner == "foo"
    assert ref.repo == "bar"

    ref2 = parse_github_ref("foo/bar")
    assert ref2.full_name == "foo/bar"


@patch("orchestrator.services.github_onboarding.subprocess.run")
def test_create_project_from_github_writes_control_op(mock_run, temp_control_repo, tmp_path):
    from orchestrator.services.github_onboarding import create_project_from_github

    # Mock successful git clone
    mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

    clone_path = tmp_path / "bar"
    result = create_project_from_github(url="https://github.com/foo/bar", clone_path=str(clone_path), sync_issues=False)

    assert result["ok"] is True
    project = result["project"]
    assert project["name"] == "bar"
    assert project["githubRepo"] == "foo/bar"
    assert project["tracking"] == "local"
    assert project["repoPath"] == str(clone_path.resolve())

    # Control op should be created
    ops_dir = temp_control_repo / "state" / "control-ops"
    op_files = list(ops_dir.glob("op_*.json"))
    assert len(op_files) == 1

    op = json.loads(op_files[0].read_text())
    assert op["type"] == "add_project"
    assert op["project"]["id"] == project["id"]


def test_apply_op_add_project_updates_projects_file(temp_control_repo):
    cm = ControlManager()

    op = {
        "type": "add_project",
        "project": {"id": "p1", "name": "P1", "repoPath": "/tmp/p1"},
    }

    cm.apply_op(op)

    projects_file = temp_control_repo / "state" / "projects.json"
    data = json.loads(projects_file.read_text())
    assert data["projects"][0]["id"] == "p1"
