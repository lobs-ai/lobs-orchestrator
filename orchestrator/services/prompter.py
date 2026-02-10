"""
Prompt builder for worker agents.

Constructs structured prompts that include:
- Agent context (AGENTS.md + SOUL.md)
- Engineering rules (global + project-specific)
- Product context
- Task details
- Output contract

Note: The worker workspace also syncs agent template files that OpenClaw may auto-load.
This prompter explicitly embeds AGENTS.md + SOUL.md so prompts remain self-contained
(e.g., for non-OpenClaw runtimes or logs).
"""

import json
from pathlib import Path
from typing import Any


class Prompter:
    """Builds structured prompts for agents."""

    # ---------------------------------------------------------------------
    # Workspace resolution
    # ---------------------------------------------------------------------

    @staticmethod
    def _resolve_workspace_path(project_id: str) -> Path:
        """Resolve the workspace path for a project.

        Research projects may have no repoPath; for those we default to the control repo.
        """

        from orchestrator.config import BASE_DIR, CONTROL_REPO_PATH, PROJECTS_FILE

        # Default behavior: project lives at BASE_DIR/<project_id>
        default_path = (BASE_DIR / project_id).resolve()

        if not PROJECTS_FILE.exists():
            return default_path

        try:
            data = json.loads(PROJECTS_FILE.read_text())
            for project in data.get("projects", []):
                if project.get("id") != project_id:
                    continue

                repo_path = project.get("repoPath")
                if repo_path:
                    return Path(repo_path).resolve()

                # No repoPath configured. Research projects often have none.
                if project.get("type") == "research":
                    return CONTROL_REPO_PATH.resolve()

                return default_path
        except Exception:
            return default_path

    # ---------------------------------------------------------------------
    # Agent context
    # ---------------------------------------------------------------------

    @staticmethod
    def _normalize_agent_type(agent_type: str | None) -> str:
        """Normalize legacy/empty agent_type values to supported agent templates."""

        t = (agent_type or "").strip().lower()
        if not t:
            return "programmer"

        # Historically, agentType was metadata (e.g. 'worker'). Default those to programmer.
        legacy_to_programmer = {
            "worker",
            "task-runner",
            "worker-template",
        }
        if t in legacy_to_programmer:
            return "programmer"

        return t

    @staticmethod
    def _load_agent_system_prompt(agent_type: str) -> str:
        """Load AGENTS.md + SOUL.md for the agent type via AgentRegistry."""

        try:
            from orchestrator.core import registry

            cfg = registry.get_agent(agent_type)
            agents_md = (cfg.agents_md or "").strip()
            soul_md = (cfg.soul_md or "").strip()
            if not agents_md and not soul_md:
                return ""

            # Keep this reasonably bounded.
            agents_md = agents_md[:6000]
            soul_md = soul_md[:6000]

            return (
                "## Agent Context (Base System Prompt)\n\n"
                "### AGENTS.md\n"
                f"{agents_md}\n\n"
                "### SOUL.md\n"
                f"{soul_md}\n\n"
                "---\n\n"
            )
        except Exception:
            # If registry isn't available or agent not found, keep prompt usable.
            return ""

    # ---------------------------------------------------------------------
    # Project context helpers
    # ---------------------------------------------------------------------

    @staticmethod
    def _summarize_project_files(project_path: Path) -> str:
        """Return a compact file listing for code context.

        Intentionally shallow + filtered to avoid huge prompts.
        """

        ignore_names = {
            ".git",
            ".venv",
            "venv",
            "node_modules",
            "__pycache__",
            ".pytest_cache",
            "dist",
            "build",
        }

        lines: list[str] = []
        try:
            for p in sorted(project_path.iterdir()):
                if p.name in ignore_names:
                    continue
                if p.is_dir():
                    lines.append(f"- {p.name}/")
                else:
                    lines.append(f"- {p.name}")
        except Exception:
            return ""

        if not lines:
            return ""

        joined = "\n".join(lines[:80])
        if len(lines) > 80:
            joined += f"\n- ... ({len(lines) - 80} more)"

        return "## Project Files (top-level)\n\n" + joined + "\n\n---\n\n"

    @staticmethod
    def _build_agent_specific_guidance(
        agent_type: str,
        item: dict[str, Any],
        project_id: str,
        project_path: Path,
        kind: str,
    ) -> str:
        """Add agent-specific framing instructions."""

        t = agent_type

        if t == "programmer":
            code_ctx = Prompter._summarize_project_files(project_path)
            return (
                "## Agent Mode: Programmer\n\n"
                "Focus on implementation. Prefer small, correct changes. Add tests when appropriate.\n\n"
                + code_ctx
            )

        if t == "researcher":
            scope = item.get("scope") or item.get("constraints") or ""
            scope_block = f"**Scope/Constraints:** {scope}\n\n" if scope else ""

            # Prefer research_request.prompt, but allow generic tasks to act as research.
            question = item.get("prompt") if kind == "research_request" else (item.get("title") or "")
            notes = item.get("notes") or ""
            q = question or notes

            return (
                "## Agent Mode: Researcher\n\n"
                "Focus on answering the research question with sources and clear synthesis.\n\n"
                f"**Research Question:**\n{q}\n\n"
                + scope_block
                + f"Write findings to `state/research/{project_id}/docs/`.\n\n---\n\n"
            )

        if t == "reviewer":
            files = item.get("files") or item.get("paths") or item.get("changedFiles")
            files_block = ""
            if isinstance(files, list) and files:
                files_block = "**Files to Review:**\n" + "\n".join(f"- {f}" for f in files[:200]) + "\n\n"
            elif isinstance(files, str) and files.strip():
                files_block = f"**Files to Review:** {files.strip()}\n\n"
            else:
                files_block = "**Files to Review:** (not specified; infer from task and/or git diff)\n\n"

            criteria = item.get("criteria") or (
                "Correctness, readability, tests, security, and adherence to project conventions."
            )

            return (
                "## Agent Mode: Reviewer\n\n"
                + files_block
                + f"**Review Criteria:** {criteria}\n\n"
                + "Write review notes to a markdown file (task may specify location).\n\n---\n\n"
            )

        if t == "writer":
            brief = item.get("brief") or item.get("title") or "(no brief provided)"
            audience = item.get("audience") or "(unspecified)"
            style = item.get("style") or item.get("tone") or "(unspecified)"
            deliverable = item.get("deliverable") or item.get("output") or "(unspecified)"

            return (
                "## Agent Mode: Writer\n\n"
                f"**Brief:** {brief}\n\n"
                f"**Audience:** {audience}\n\n"
                f"**Style/Tone:** {style}\n\n"
                f"**Deliverable:** {deliverable}\n\n"
                "If no output path is given, choose an appropriate docs/ location and state it in `.work-summary`.\n\n"
                "---\n\n"
            )

        if t == "architect":
            constraints = item.get("constraints") or item.get("nonGoals") or ""
            constraints_block = f"**Constraints/Non-Goals:** {constraints}\n\n" if constraints else ""

            return (
                "## Agent Mode: Architect\n\n"
                + "Focus on system-level design, tradeoffs, and a concrete implementation plan broken into tasks.\n\n"
                + constraints_block
                + "Prefer incremental designs that fit existing architecture.\n\n"
                + "---\n\n"
            )

        # Unknown agent types: keep prompt usable.
        return (
            f"## Agent Mode: {agent_type}\n\n"
            "No specialized prompt template is defined for this agent type. Follow the task instructions below.\n\n"
            "---\n\n"
        )

    # ---------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------

    @staticmethod
    def build_task_prompt(
        item: dict[str, Any],
        project_id: str,
        rules: str = "",
        workspace_path: Path | None = None,
        agent_type: str | None = None,
    ) -> str:
        """Build a complete prompt for an agent.

        Args:
            item: Provider task payload.
            project_id: Project identifier.
            rules: Global engineering rules (text).
            workspace_path: Optional explicit workspace (repo) path.
            agent_type: Agent template type (programmer|researcher|reviewer|writer|architect|...).
                If not provided, falls back to item['agentType'].
        """

        from orchestrator.config import PROJECT_CONTEXT_FILES
        from orchestrator.utils.settings import get_setting

        project_path = (workspace_path or Prompter._resolve_workspace_path(project_id)).resolve()

        normalized_agent_type = Prompter._normalize_agent_type(agent_type or item.get("agentType"))

        # 0. Agent Context (AGENTS.md + SOUL.md)
        agent_context = Prompter._load_agent_system_prompt(normalized_agent_type)

        # 1. Product Context
        context_files = get_setting("project_context_files", PROJECT_CONTEXT_FILES)
        product_context = ""
        for filename in context_files:
            file_path = project_path / filename
            if file_path.exists():
                try:
                    content = file_path.read_text(encoding="utf-8")[:5000]  # Limit size
                    product_context += f"### {filename}\n{content}\n\n"
                except Exception:
                    pass

        # 2. Project-specific Engineering Rules
        project_rules = ""
        project_rules_path = project_path / "ENGINEERING_RULES.md"
        if project_rules_path.exists():
            try:
                project_rules = project_rules_path.read_text(encoding="utf-8")[:3000]
            except Exception:
                pass

        kind = item.get("kind", "task")
        item_id = item.get("id", "unknown")

        # Agent-specific framing (before task details)
        agent_guidance = Prompter._build_agent_specific_guidance(
            normalized_agent_type,
            item=item,
            project_id=project_id,
            project_path=project_path,
            kind=kind,
        )

        prompt = (
            agent_context
            + "# Work Assignment\n\n"
            + f"**Project:** {project_id}  \n"
            + f"**Workspace:** `{project_path}`  \n"
            + f"**Kind:** {kind}  \n"
            + f"**ID:** {item_id}\n\n"
            + "---\n\n"
            + f"{agent_guidance}"
            + "## Engineering Rules\n\n"
        )

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

        # Task details (kind-specific)
        if normalized_agent_type == "diagnostic" or item.get("agentType") == "diagnostic":
            prompt += (
                "**Diagnostic Mission (High Priority)**\n\n"
                f"{item.get('title')}\n\n"
                "**Error/Context:**\n"
                "```\n"
                f"{item.get('notes')}\n"
                "```\n\n"
                "**Goal:** Analyze the error, fix if possible, or provide root cause analysis.\n\n"
            )
        elif kind == "task":
            prompt += f"**{item.get('title')}**\n\n{item.get('notes', '(no additional notes)')}\n\n"
        elif kind == "research_request":
            prompt += (
                "**Research Request**\n\n"
                f"{item.get('prompt')}\n\n"
                f"Write findings to `state/research/{project_id}/docs/`. Be comprehensive but concise.\n\n"
            )
        elif kind == "inbox_response":
            # Format full conversation chain
            messages = item.get("messages", [])
            conversation_text = ""
            if messages:
                for msg in messages:
                    author = msg.get("author", "unknown")
                    text = msg.get("text", "")
                    timestamp = msg.get("timestamp", "")
                    ts_str = f" ({timestamp})" if timestamp else ""
                    conversation_text += f"**{author}**{ts_str}:\n{text}\n\n"
            else:
                # Fallback to lastMessage if no messages array
                conversation_text = f"**rafe**:\n{item.get('lastMessage', '')}\n"

            prompt += (
                "**Inbox Response**\n\n"
                f"Document: {item.get('docId')}\n\n"
                "## Conversation Thread\n\n"
                f"{conversation_text}"
                "---\n\n"
                "Process the response, create tasks if needed (via control-ops), mark as acknowledged.\n\n"
            )
        elif kind == "text_dump":
            prompt += (
                "**Text Dump Processing**\n\n"
                "Preview:\n"
                "```\n"
                f"{item.get('textPreview')}\n"
                "```\n\n"
                "Extract actionable items, create tasks/inbox items as needed.\n\n"
            )
        else:
            prompt += f"```json\n{json.dumps(item, indent=2)}\n```\n\n"

        prompt += (
            "---\n\n"
            "## When You're Done\n\n"
            "Just stop. The orchestrator handles git commits, pushes, and state updates automatically.\n\n"
            "**Optional:** Write a very short summary (1-2 lines) to `.work-summary`:\n\n"
            "```\n"
            "echo \"Add auth middleware\" > .work-summary\n"
            "```\n\n"
            "**If blocked:** Write blocker to `.work-summary` and exit 1:\n\n"
            "```\n"
            "echo \"BLOCKED: missing db schema\" > .work-summary\n"
            "exit 1\n"
            "```\n\n"
            "---\n\n"
            "Begin.\n"
        )

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
