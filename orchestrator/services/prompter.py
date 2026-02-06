"""
Prompt builder for worker agents.

Constructs structured prompts that include:
- Agent rules (from AGENTS.md)
- Engineering rules (global + project-specific)
- Product context
- Task details
- Output contract
"""

import json
from pathlib import Path
from typing import Any


class Prompter:
    """
    Builds structured prompts for workers.
    Gathers product context, work details, and engineering rules.
    """

    @staticmethod
    def build_task_prompt(item: dict[str, Any], project_id: str, rules: str = "") -> str:
        """
        Build a complete prompt for a task worker.
        
        Args:
            item: The work item (task, research request, etc.)
            project_id: The project ID
            rules: Global engineering rules
            
        Returns:
            A formatted prompt string
        """
        from orchestrator.config import BASE_DIR, PROJECT_CONTEXT_FILES, CONTROL_REPO_PATH, ORCHESTRATOR_REPO_PATH
        from orchestrator.utils.settings import get_setting
        
        project_path = (BASE_DIR / project_id).resolve()

        # 1. Product Context
        context_files = get_setting("project_context_files", PROJECT_CONTEXT_FILES)
        product_context = ""
        for filename in context_files:
            file_path = project_path / filename
            if file_path.exists():
                try:
                    content = file_path.read_text()[:5000]  # Limit size
                    product_context += f"### {filename}\n{content}\n\n"
                except Exception:
                    pass

        # 2. Project-specific Engineering Rules
        project_rules = ""
        project_rules_path = project_path / "ENGINEERING_RULES.md"
        if project_rules_path.exists():
            try:
                project_rules = project_rules_path.read_text()[:3000]
            except Exception:
                pass

        # 3. Agent Rules from orchestrator
        agent_rules = ""
        agents_rules_path = (ORCHESTRATOR_REPO_PATH / "AGENTS.md").resolve()
        if agents_rules_path.exists():
            try:
                agent_rules = agents_rules_path.read_text()
            except Exception:
                pass

        kind = item.get("kind", "task")
        item_id = item.get("id", "unknown")

        # 4. Build the prompt
        prompt = f"""You are a task-scoped software engineer. Execute the assigned work precisely.

## Rules
{agent_rules}

## Global Engineering Rules
{rules}

## Project Engineering Rules ({project_id})
{project_rules}

## Product Context ({project_id})
{product_context}

## Work Assignment
- **Kind:** {kind}
- **ID:** {item_id}
- **Project:** {project_id}
- **Workspace:** {project_path}
- **Control Repo:** {CONTROL_REPO_PATH.resolve()}

"""
        
        # Add work-specific details
        agent_type = item.get("agentType")
        if agent_type == "diagnostic":
            prompt += f"""## DIAGNOSTIC TASK
This is a high-priority diagnostic mission.

**Title:** {item.get("title")}
**Context/Error:**
{item.get("notes")}

### Your Goal
1. Analyze the workspace and the reported error
2. Attempt to fix the issue if it is a configuration or code error
3. If fixed, verify it works
4. If not fixable, provide a detailed root cause analysis

"""
        elif kind == "task":
            prompt += f"""## TASK
**Title:** {item.get("title")}

**Notes:**
{item.get("notes", "(no notes)")}

"""
        elif kind == "research_request":
            prompt += f"""## RESEARCH REQUEST
**Prompt:**
{item.get("prompt")}

### Instructions
1. Research the topic thoroughly
2. Write findings to `state/research/{project_id}/docs/`
3. Be comprehensive but concise

"""
        elif kind == "inbox_response":
            prompt += f"""## INBOX RESPONSE
**Document:** {item.get("docId")}
**Last Message:**
{item.get("lastMessage")}

### Instructions
1. Process the inbox response
2. Create tasks if needed using control-ops
3. Mark as acknowledged when done

"""
        elif kind == "text_dump":
            prompt += f"""## TEXT DUMP PROCESSING
**Project:** {item.get("projectId")}
**Preview:**
{item.get("textPreview")}

### Instructions
1. Process the text dump
2. Extract actionable items
3. Create tasks or inbox items as needed

"""
        else:
            prompt += f"""## WORK ITEM
```json
{json.dumps(item, indent=2)}
```

"""

        # Output contract
        prompt += f"""## Output Contract
1. Work in the `{project_id}` repository at `{project_path}`
2. Use `git pull --rebase` before making changes
3. Produce clean, idiomatic code with relevant tests
4. Commit your changes with clear messages
5. Do NOT push - the orchestrator handles that

### Control Operations
To request state changes, create a JSON file in `{CONTROL_REPO_PATH / "state" / "control-ops"}`:

**Update task:**
```json
{{"type": "update_task", "task_id": "{item_id}", "updates": {{"workState": "completed"}}}}
```

**Add inbox suggestion:**
```json
{{"type": "add_inbox_item", "item": {{"title": "...", "body": "...", "type": "suggestion", "projectId": "{project_id}"}}}}
```

## Constraints
- Focus strictly on this work item
- Do not work on unrelated files
- Do not invent new tasks
- Stop and report if blocked

Begin.
"""
        return prompt

    @staticmethod
    def build_diagnostic_prompt(error_log: str, task_id: str, project_id: str) -> str:
        """Build a prompt for diagnostic analysis."""
        return f"""You are a diagnostic agent. Analyze this failure and propose a fix.

## Context
- **Task ID:** {task_id}
- **Project:** {project_id}

## Error Log
```
{error_log}
```

## Instructions
1. Analyze the error carefully
2. Identify the root cause
3. Propose a specific fix
4. If you can fix it directly, do so
5. Write your findings to a diagnostic report

Be thorough but concise.
"""

    @staticmethod
    def build_research_prompt(prompt: str, project_id: str) -> str:
        """Build a prompt for research tasks."""
        return f"""You are a research agent. Investigate the following topic.

## Project
{project_id}

## Research Prompt
{prompt}

## Instructions
1. Research thoroughly using available tools
2. Synthesize findings into a clear document
3. Include sources where applicable
4. Write to the project's research docs folder

Be comprehensive but focused.
"""
