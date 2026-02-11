import pytest
from pathlib import Path
from orchestrator.services.prompter import Prompter
from orchestrator.utils.settings import set_setting

def test_build_task_prompt_basic(temp_control_repo):
    # temp_control_repo is tmp_path / "lobs-control"
    base_dir = temp_control_repo.parent
    project_id = "test-project"
    project_dir = base_dir / project_id
    project_dir.mkdir()
    
    # Create a product context file
    (project_dir / "PRODUCT.md").write_text("Test Product Context")
    
    item = {
        "id": "t123",
        "kind": "task",
        "title": "Fix a bug",
        "notes": "Something is broken"
    }
    
    prompt = Prompter.build_task_prompt(item, project_id, rules="Global Rules")
    
    assert "### PRODUCT.md\nTest Product Context" in prompt
    assert "Global Rules" in prompt
    assert "Fix a bug" in prompt
    assert "Something is broken" in prompt
    assert f"**Project:** {project_id}" in prompt
    assert str(project_dir) in prompt

def test_build_task_prompt_research_request(temp_control_repo):
    base_dir = temp_control_repo.parent
    project_id = "test-project"

    # Research projects often have no repoPath. Ensure we default the workspace to lobs-control.
    projects_file = temp_control_repo / "state" / "projects.json"
    projects_file.write_text(
        '{"projects":[{"id":"test-project","type":"research","repoPath":null}]}'
    )

    item = {
        "id": "r456",
        "kind": "research_request",
        "prompt": "Investigate X",
    }

    prompt = Prompter.build_task_prompt(item, project_id)

    assert "**Research Request**" in prompt
    assert "Investigate X" in prompt
    assert str(temp_control_repo) in prompt
    assert "## Inputs / Outputs" in prompt
    assert "state/research" in prompt
