import json
import subprocess
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


@patch("orchestrator.api.server.create_project_from_github")
def test_create_from_github_batch_sequential(mock_create, temp_control_repo, tmp_path):
    """Test that batch creation processes repos sequentially and returns summary."""
    from orchestrator.api.server import _read_json, _send_json
    from orchestrator.services.github_onboarding import create_project_from_github
    from unittest.mock import MagicMock
    import io
    
    # Mock successful project creation
    def create_side_effect(url, clone_path=None, sync_issues=False):
        # Extract repo name from URL
        repo_name = url.split("/")[-1]
        return {
            "ok": True,
            "project": {
                "id": repo_name,
                "name": repo_name,
                "repoPath": clone_path or f"/home/user/{repo_name}",
                "source": "github",
            }
        }
    
    mock_create.side_effect = create_side_effect
    
    # Prepare test data
    repos = [
        {"url": "https://github.com/foo/bar", "clonePath": str(tmp_path / "bar")},
        {"url": "https://github.com/foo/baz", "clonePath": str(tmp_path / "baz")},
        {"url": "https://github.com/foo/qux", "clonePath": str(tmp_path / "qux")},
    ]
    
    # Create a simple mock handler to test the batch logic
    class MockHandler:
        def __init__(self):
            self.headers = {"Content-Length": "0"}
            self.rfile = io.BytesIO()
            self.wfile = io.BytesIO()
            self.response_status = None
            self.response_headers = {}
        
        def send_response(self, status):
            self.response_status = status
        
        def send_header(self, key, value):
            self.response_headers[key] = value
        
        def end_headers(self):
            pass
    
    handler = MockHandler()
    request_body = json.dumps({"repos": repos}).encode("utf-8")
    handler.headers = {"Content-Length": str(len(request_body))}
    handler.rfile = io.BytesIO(request_body)
    
    # Import and call the batch handler logic directly
    from orchestrator.api.server import make_handler
    handler_class = make_handler()
    batch_fn = handler_class.routes[("POST", "/projects/create-from-github-batch")]
    
    # Call the batch handler
    batch_fn(handler)
    
    # Verify response
    assert handler.response_status == 200
    response_data = json.loads(handler.wfile.getvalue().decode("utf-8"))
    
    # Verify response structure
    assert response_data["ok"] is True
    assert "results" in response_data
    assert "summary" in response_data
    
    # Verify all repos were processed sequentially
    assert len(response_data["results"]) == 3
    assert response_data["summary"]["total"] == 3
    assert response_data["summary"]["succeeded"] == 3
    assert response_data["summary"]["failed"] == 0
    
    # Verify each result
    for result in response_data["results"]:
        assert result["ok"] is True
        assert "project" in result
    
    # Verify create_project_from_github was called 3 times (sequentially)
    assert mock_create.call_count == 3


@patch("orchestrator.api.server.create_project_from_github")
def test_create_from_github_batch_handles_failures(mock_create, temp_control_repo, tmp_path):
    """Test that batch creation continues after failures and reports them."""
    from unittest.mock import MagicMock
    import io
    
    # Mock git clone: first succeeds, second fails, third succeeds
    def create_side_effect(url, clone_path=None, sync_issues=False):
        repo_name = url.split("/")[-1]
        if "fail" in repo_name:
            raise ValueError(f"Failed to clone {repo_name}")
        return {
            "ok": True,
            "project": {
                "id": repo_name,
                "name": repo_name,
                "repoPath": clone_path or f"/home/user/{repo_name}",
                "source": "github",
            }
        }
    
    mock_create.side_effect = create_side_effect
    
    # Prepare test data
    repos = [
        {"url": "https://github.com/foo/bar", "clonePath": str(tmp_path / "bar")},
        {"url": "https://github.com/foo/fail", "clonePath": str(tmp_path / "fail")},
        {"url": "https://github.com/foo/qux", "clonePath": str(tmp_path / "qux")},
    ]
    
    # Create a simple mock handler
    class MockHandler:
        def __init__(self):
            self.headers = {"Content-Length": "0"}
            self.rfile = io.BytesIO()
            self.wfile = io.BytesIO()
            self.response_status = None
            self.response_headers = {}
        
        def send_response(self, status):
            self.response_status = status
        
        def send_header(self, key, value):
            self.response_headers[key] = value
        
        def end_headers(self):
            pass
    
    handler = MockHandler()
    request_body = json.dumps({"repos": repos}).encode("utf-8")
    handler.headers = {"Content-Length": str(len(request_body))}
    handler.rfile = io.BytesIO(request_body)
    
    # Import and call the batch handler
    from orchestrator.api.server import make_handler
    handler_class = make_handler()
    batch_fn = handler_class.routes[("POST", "/projects/create-from-github-batch")]
    
    # Call the batch handler
    batch_fn(handler)
    
    # Verify response
    assert handler.response_status == 200
    response_data = json.loads(handler.wfile.getvalue().decode("utf-8"))
    
    # Verify response structure
    assert response_data["ok"] is True
    assert "results" in response_data
    assert "summary" in response_data
    
    # Verify summary
    assert response_data["summary"]["total"] == 3
    assert response_data["summary"]["succeeded"] == 2
    assert response_data["summary"]["failed"] == 1
    
    # Verify results
    assert len(response_data["results"]) == 3
    assert response_data["results"][0]["ok"] is True
    assert response_data["results"][1]["ok"] is False
    assert "error" in response_data["results"][1]
    assert response_data["results"][2]["ok"] is True
