import json
from unittest.mock import patch, MagicMock
from orchestrator.services.scanner import Scanner

def test_check_pending_request(temp_control_repo):
    scanner = Scanner()
    request_file = temp_control_repo / "state" / "worker-request.json"
    
    assert scanner.check_pending_request() is False
    
    request_file.write_text("{}")
    assert scanner.check_pending_request() is True

def test_get_projects_empty(temp_control_repo):
    scanner = Scanner()
    assert scanner.get_projects() == []

def test_get_projects_valid(temp_control_repo):
    scanner = Scanner()
    projects_file = temp_control_repo / "state" / "projects.json"
    projects_data = {"projects": [{"id": "p1", "name": "Project 1"}]}
    projects_file.write_text(json.dumps(projects_data))
    
    projects = scanner.get_projects()
    assert len(projects) == 1
    assert projects[0]["id"] == "p1"

def test_get_eligible_tasks_mock_script(temp_control_repo):
    scanner = Scanner()
    script_path = temp_control_repo / "bin" / "open-work"
    script_path.touch()
    # Make it executable for the subprocess.run check (though we'll mock it)
    script_path.chmod(0o755)
    
    mock_output = json.dumps([
        {"id": "t1", "kind": "task", "workState": "not_started", "status": "active"},
        {"id": "t2", "kind": "task", "workState": "in_progress", "status": "active"},
        {"id": "r1", "kind": "research_request"}
    ])
    
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout=mock_output, check_returncode=lambda: None)
        
        eligible = scanner.get_eligible_tasks()
        
        assert len(eligible) == 2
        assert eligible[0]["id"] == "t1"
        assert eligible[1]["id"] == "r1"
