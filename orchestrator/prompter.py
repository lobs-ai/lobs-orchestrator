import os
import json
from pathlib import Path
from typing import List, Dict, Any
from .config import BASE_DIR, PROJECT_CONTEXT_FILES, CONTROL_REPO_PATH


class Prompter:
    """
    Builds structured prompts for workers.
    Gathers product context, task details, and engineering rules.
    """

    @staticmethod
    def build_task_prompt(task: Dict[str, Any], project_id: str) -> str:
        project_path = BASE_DIR / project_id

        # 1. Product Context
        product_context = ""
        for filename in PROJECT_CONTEXT_FILES:
            file_path = project_path / filename
            if file_path.exists():
                product_context += f"### {filename}\n{file_path.read_text()}\n\n"

        # 2. Engineering Rules (Global)
        rules_path = CONTROL_REPO_PATH / "ENGINEERING_RULES.md"
        rules = (
            rules_path.read_text()
            if rules_path.exists()
            else "Standard engineering practices."
        )

        # 3. Prompt Construction
        prompt = f"""SYSTEM:
Engineering Rules:
{rules}

Product Context ({project_id}):
{product_context}

Output Contract:
Produce changes as git diffs or specific artifacts.
Report completion by creating a control-op in state/control-ops/.
Write detailed notes to state/worker-results/{task["id"]}.md.

CONTEXT:
Task ID: {task["id"]}
Project: {project_id}
Status: {task.get("status")}

TASK:
Title: {task.get("title")}
Notes: {task.get("notes")}

CONSTRAINTS:
- Do not run git commands directly.
- Use fresh sessions only.
- Focus strictly on the defined task.
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
