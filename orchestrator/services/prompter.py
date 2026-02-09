"""
Prompt builder for worker agents.

Constructs structured prompts that include:
- Engineering rules (global + project-specific)
- Product context
- Task details
- Output contract

Worker agent files (AGENTS.md, SOUL.md, etc.) are auto-loaded by OpenClaw.
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

        # 3. Worker Rules - now loaded from worker workspace (synced automatically)
        # No need to include in prompt - OpenClaw auto-loads workspace files

        kind = item.get("kind", "task")
        item_id = item.get("id", "unknown")

        # 4. Build the prompt
        # Note: WORKER_RULES.md, AGENTS.md, SOUL.md, etc. are auto-loaded by OpenClaw from worker workspace
        prompt = f"""# Work Assignment

**Project:** {project_id}  
**Workspace:** `{project_path}`  
**Kind:** {kind}  
**ID:** {item_id}

---

## Engineering Rules

"""
        
        # Add engineering rules (global + project)
        if rules:
            prompt += f"{rules}\n\n"
        if project_rules:
            prompt += f"### Project-Specific ({project_id})\n{project_rules}\n\n"
        
        if not rules and not project_rules:
            prompt += "(none)\n\n"
        
        prompt += "---\n\n"
        
        # Add product context
        if product_context:
            prompt += f"## Product Context\n\n{product_context}---\n\n"
        
        prompt += "## Your Task\n\n"
        
        # Add work-specific details
        agent_type = item.get("agentType")
        if agent_type == "diagnostic":
            prompt += f"""**Diagnostic Mission (High Priority)**

{item.get("title")}

**Error/Context:**
```
{item.get("notes")}
```

**Goal:** Analyze the error, fix if possible, or provide root cause analysis.

"""
        elif kind == "task":
            prompt += f"""**{item.get("title")}**

{item.get("notes", "(no additional notes)")}

"""
        elif kind == "research_request":
            prompt += f"""**Research Request**

{item.get("prompt")}

Write findings to `state/research/{project_id}/docs/`. Be comprehensive but concise.

"""
        elif kind == "inbox_response":
            prompt += f"""**Inbox Response**

Document: {item.get("docId")}

Last message:
```
{item.get("lastMessage")}
```

Process the response, create tasks if needed (via control-ops), mark as acknowledged.

"""
        elif kind == "text_dump":
            prompt += f"""**Text Dump Processing**

Preview:
```
{item.get("textPreview")}
```

Extract actionable items, create tasks/inbox items as needed.

"""
        else:
            prompt += f"""```json
{json.dumps(item, indent=2)}
```

"""

        prompt += """---

## When You're Done

Just stop. The orchestrator handles git commits, pushes, and state updates automatically.

**Optional:** Write a brief summary to `.work-summary` to provide context for the commit message:

```
echo "Added user authentication middleware" > .work-summary
```

**If blocked:** Write your blocker to `.work-summary` and exit with error:

```
echo "BLOCKED: Cannot proceed - missing database schema" > .work-summary
exit 1
```

---

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
