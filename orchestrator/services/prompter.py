import json
from pathlib import Path
from typing import Any


class Prompter:
    """
    Builds structured prompts for workers.
    Gathers product context, task details, and engineering rules.
    """

    @staticmethod
    def build_task_prompt(task: dict[str, Any], project_id: str, rules: str = "") -> str:
        from orchestrator.config import BASE_DIR, PROJECT_CONTEXT_FILES, CONTROL_REPO_PATH, ORCHESTRATOR_REPO_PATH
        from orchestrator.utils.settings import get_setting
        
        project_path = (BASE_DIR / project_id).resolve()

        # 1. Product Context
        context_files = get_setting("project_context_files", PROJECT_CONTEXT_FILES)
        product_context = ""
        for filename in context_files:
            file_path = project_path / filename
            if file_path.exists():
                product_context += f"### {filename}\n{file_path.read_text()}\n\n"

        # 2. Project-specific Engineering Rules
        project_rules_path = project_path / "ENGINEERING_RULES.md"
        project_rules = project_rules_path.read_text() if project_rules_path.exists() else ""

        # 3. Global Agent Rules
        agents_rules_path = (ORCHESTRATOR_REPO_PATH / "AGENTS.md").resolve()
        agent_rules = (
            agents_rules_path.read_text()
            if agents_rules_path.exists()
            else ""
        )

        # 4. Prompt Construction
        prompt = f"""SYSTEM:
{agent_rules}

Global Engineering Rules:
{rules}

Project Engineering Rules ({project_id}):
{project_rules}

Product Context ({project_id}):
{product_context}

Output Contract:
1. Implement the task in the {project_id} repository.
2. Produce clean, idiomatic code and relevant tests.
3. Write a summary of your work (notes, findings, etc.) to:
   {(CONTROL_REPO_PATH / "state" / "worker-results" / f"{task['id']}.md").resolve()}
4. Proactive Suggestions: If you have ideas for further improvements or need clarification, you can add a suggestion to my inbox by creating a file in:
   {(CONTROL_REPO_PATH / "state" / "control-ops").resolve()}
   Format: {{"type": "add_inbox_item", "item": {{"title": "...", "body": "...", "type": "suggestion", "projectId": "{project_id}"}}}}
5. System Control: You can request system actions by creating a control op file.
   Format: {{"type": "message", "action": "register_project", "payload": {{"id": "new-repo", "name": "...", "path": "..."}}}}
   Supported actions: register_project, update_instructions, broadcast.
6. Do NOT attempt to update tasks.json or other control state yourself.

CONTEXT:
Task ID: {task["id"]}
Project: {project_id}
Control Repo: {CONTROL_REPO_PATH.resolve()}
Workspace: {project_path}

TASK:
Title: {task.get("title")}
Notes: {task.get("notes")}

CONSTRAINTS:
- You are operating on the real filesystem in {project_path}.
- Use 'git pull --rebase' before starting.
- Focus strictly on this task.
"""
        return prompt

    @staticmethod
    def build_diagnostic_prompt(error_log: str, task_id: str) -> str:
        return f"""SYSTEM:
You are a diagnostic agent. Your goal is to analyze the failure of task {task_id}.

CONTEXT:
Error Log:
{error_log}

TASK:
Analyze the error and propose a fix or explanation.
Output your findings to state/worker-results/{task_id}_diagnostic.md.
"""
