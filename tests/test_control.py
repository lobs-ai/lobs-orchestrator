import json
from unittest.mock import patch, MagicMock
from orchestrator.services.control import ControlManager

def test_request_op(temp_control_repo):
    op = {"type": "test_op", "data": 123}
    ControlManager.request_op(op)
    
    ops_dir = temp_control_repo / "state" / "control-ops"
    op_files = list(ops_dir.glob("op_*.json"))
    assert len(op_files) == 1
    
    with open(op_files[0], "r") as f:
        saved_op = json.load(f)
    assert saved_op == op

@patch("subprocess.run")
def test_process_ops_no_files(mock_run, temp_control_repo):
    cm = ControlManager()
    # Ensure ops dir is empty
    ops_dir = temp_control_repo / "state" / "control-ops"
    for f in ops_dir.glob("*.json"):
        f.unlink()
        
    messages = cm.process_ops()
    assert messages == []
    # If no files, pull() should NOT be called based on the code logic
    mock_run.assert_not_called()

@patch("subprocess.run")
def test_process_ops_with_updates(mock_run, temp_control_repo):
    # Setup op file
    task_id = "test-task-1"
    op = {
        "type": "update_task",
        "task_id": task_id,
        "updates": {"workState": "completed"}
    }
    ControlManager.request_op(op)
    
    # Setup task file
    task_file = temp_control_repo / "state" / "tasks" / f"{task_id}.json"
    task_file.write_text(json.dumps({"id": task_id, "workState": "not_started"}))
    
    # Mock git pull (first call)
    # Mock git status (second call)
    # Mock git add (third call)
    # Mock git commit (fourth call)
    # Mock git push (fifth call)
    
    mock_pull = MagicMock(returncode=0)
    mock_status = MagicMock(returncode=0, stdout=b"M state/tasks/test-task-1.json")
    mock_add = MagicMock(returncode=0)
    mock_commit = MagicMock(returncode=0)
    mock_push = MagicMock(returncode=0)
    
    mock_run.side_effect = [mock_pull, mock_status, mock_add, mock_commit, mock_push]
    
    cm = ControlManager()
    messages = cm.process_ops()
    
    assert messages == []
    # Verify task was updated
    with open(task_file, "r") as f:
        updated_task = json.load(f)
    assert updated_task["workState"] == "completed"
    assert "updatedAt" in updated_task
    
    # Verify op file was removed
    ops_dir = temp_control_repo / "state" / "control-ops"
    assert len(list(ops_dir.glob("*.json"))) == 0
    
    # Verify git commands were called
    assert mock_run.call_count >= 2 # At least pull and status

def test_apply_op_update_worker_status(temp_control_repo):
    cm = ControlManager()
    status_file = temp_control_repo / "state" / "worker-status.json"
    
    op = {
        "type": "update_worker_status",
        "updates": {"worker-1": "idle"}
    }
    cm.apply_op(op)
    
    with open(status_file, "r") as f:
        status = json.load(f)
    assert status["worker-1"] == "idle"
