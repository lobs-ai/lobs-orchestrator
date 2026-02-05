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
        from .config import BASE_DIR, PROJECT_CONTEXT_FILES, CONTROL_REPO_PATH, ORCHESTRATOR_REPO_PATH
        project_path = BASE_DIR / project_id

        # 1. Product Context
        product_context = ""
        for filename in PROJECT_CONTEXT_FILES:
            file_path = project_path / filename
            if file_path.exists():
                product_context += f"### {filename}\n{file_path.read_text()}\n\n"

        # 2. Agent Rules
        # Add AGENTS.md rules from this repo
        agents_rules_path = ORCHESTRATOR_REPO_PATH / "AGENTS.md"
        agent_rules = (
            agents_rules_path.read_text()
            if agents_rules_path.exists()
            else ""
        )

        # 3. Prompt Construction
        prompt = f"""SYSTEM:
{agent_rules}

Engineering Rules:
{rules}

Product Context ({project_id}):
{product_context}

Output Contract:
1. Implement the task in the {project_id} repository.
2. Produce clean, idiomatic code and relevant tests.
3. Write a summary of your work (notes, findings, etc.) to:
   {CONTROL_REPO_PATH}/state/worker-results/{task["id"]}.md
4. Do NOT attempt to update tasks.json or other control state yourself.

CONTEXT:
Task ID: {task["id"]}
Project: {project_id}
Control Repo: {CONTROL_REPO_PATH}

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
