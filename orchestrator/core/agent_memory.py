"""orchestrator.core.agent_memory

Per-agent persistent memory and evolved traits. Manages markdown files in
lobs-control/memory/<type>/ that accumulate across runs:

- MEMORY.md: task outcomes, patterns learned, preferences
- EVOLVED_TRAITS.md: confidence areas, growth areas, behavioral adjustments

Memory is injected into agent prompts at execution time so agents have context
from their previous work.
"""

from __future__ import annotations

import logging
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_CONTEXT_CHARS = 3000  # Cap injected into prompts
PRUNE_AFTER_DAYS = 30  # Remove task outcomes older than this


class AgentMemoryManager:
    """Loads and saves per-agent memory files in lobs-control."""

    def __init__(self, control_repo_path: Path) -> None:
        self._control_repo = control_repo_path
        self._memory_dir = control_repo_path / "memory"
        logger.info("[AGENT_MEMORY] Initialized with memory dir: %s", self._memory_dir)

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------

    def _agent_dir(self, agent_type: str) -> Path:
        d = self._memory_dir / agent_type
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _memory_path(self, agent_type: str) -> Path:
        return self._agent_dir(agent_type) / "MEMORY.md"

    def _traits_path(self, agent_type: str) -> Path:
        return self._agent_dir(agent_type) / "EVOLVED_TRAITS.md"

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def load_memory(self, agent_type: str) -> str:
        """Load MEMORY.md for an agent type. Returns empty string if missing."""
        path = self._memory_path(agent_type)
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("[AGENT_MEMORY] Failed to read %s memory: %s", agent_type, e)
            return ""

    def load_evolved_traits(self, agent_type: str) -> str:
        """Load EVOLVED_TRAITS.md for an agent type."""
        path = self._traits_path(agent_type)
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("[AGENT_MEMORY] Failed to read %s traits: %s", agent_type, e)
            return ""

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def append_task_outcome(
        self,
        agent_type: str,
        project_id: str,
        task_title: str,
        success: bool,
        duration_seconds: float,
    ) -> None:
        """Append a task outcome entry to MEMORY.md under ## Task Outcomes."""
        path = self._memory_path(agent_type)
        content = self.load_memory(agent_type)

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        duration_min = int(duration_seconds / 60)
        result = "succeeded" if success else "failed"
        entry = f"- [{project_id}] {task_title} — {result}, {duration_min} min"

        date_header = f"### {today}"

        if date_header in content:
            # Append under existing date header
            idx = content.index(date_header) + len(date_header)
            # Find end of this date section (next ### or ## or end of file)
            rest = content[idx:]
            next_header = re.search(r"\n##", rest)
            if next_header:
                insert_at = idx + next_header.start()
                content = content[:insert_at] + "\n" + entry + content[insert_at:]
            else:
                content = content.rstrip() + "\n" + entry + "\n"
        elif "## Task Outcomes" in content:
            # Add new date header after ## Task Outcomes
            idx = content.index("## Task Outcomes") + len("## Task Outcomes")
            content = (
                content[:idx] + "\n\n" + date_header + "\n" + entry + content[idx:]
            )
        else:
            # No Task Outcomes section — append one
            content = (
                content.rstrip()
                + "\n\n## Task Outcomes\n\n"
                + date_header
                + "\n"
                + entry
                + "\n"
            )

        # Prune old entries
        content = self._prune_old_outcomes(content)

        try:
            path.write_text(content, encoding="utf-8")
            self._git_commit_memory(agent_type, f"memory: {agent_type} task outcome")
            logger.info(
                "[AGENT_MEMORY] Recorded %s outcome for %s: %s",
                result,
                agent_type,
                task_title[:60],
            )
        except Exception as e:
            logger.error("[AGENT_MEMORY] Failed to write memory for %s: %s", agent_type, e)

    def update_evolved_traits(self, agent_type: str, content: str) -> None:
        """Overwrite EVOLVED_TRAITS.md with new content."""
        path = self._traits_path(agent_type)
        try:
            path.write_text(content, encoding="utf-8")
            self._git_commit_memory(agent_type, f"memory: {agent_type} traits evolved")
            logger.info("[AGENT_MEMORY] Updated evolved traits for %s", agent_type)
        except Exception as e:
            logger.error(
                "[AGENT_MEMORY] Failed to write traits for %s: %s", agent_type, e
            )

    # ------------------------------------------------------------------
    # Personal files (agent-evolved SOUL.md, IDENTITY.md, etc.)
    # ------------------------------------------------------------------

    # Files that agents may evolve and that should be preserved across runs.
    PERSONAL_FILES = ("SOUL.md", "IDENTITY.md")

    def load_personal_file(self, agent_type: str, filename: str) -> str | None:
        """Load a personal file from memory/<type>/<filename>.

        Returns None if the file doesn't exist (meaning the base template should be used).
        """
        path = self._agent_dir(agent_type) / filename
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return None

    def save_personal_file(self, agent_type: str, filename: str, content: str) -> None:
        """Save a personal file to memory/<type>/<filename>."""
        path = self._agent_dir(agent_type) / filename
        try:
            path.write_text(content, encoding="utf-8")
            self._git_commit_memory(agent_type, f"memory: {agent_type} {filename} evolved")
            logger.info("[AGENT_MEMORY] Saved personal file %s/%s", agent_type, filename)
        except Exception as e:
            logger.error(
                "[AGENT_MEMORY] Failed to save personal file %s/%s: %s",
                agent_type, filename, e,
            )

    def recover_personal_files_from_workspace(
        self, agent_type: str, workspace_dir: Path
    ) -> None:
        """Read back personal files from the OpenClaw workspace after a run.

        If the agent modified SOUL.md or IDENTITY.md during its run, save the
        updated version to lobs-control so it persists across future runs.
        """
        for filename in self.PERSONAL_FILES:
            workspace_file = workspace_dir / filename
            if not workspace_file.exists():
                continue
            try:
                workspace_content = workspace_file.read_text(encoding="utf-8")
                existing = self.load_personal_file(agent_type, filename)
                if existing is None or workspace_content.strip() != existing.strip():
                    self.save_personal_file(agent_type, filename, workspace_content)
            except Exception as e:
                logger.warning(
                    "[AGENT_MEMORY] Failed to recover %s/%s: %s",
                    agent_type, filename, e,
                )

    # ------------------------------------------------------------------
    # Context building (for prompt injection)
    # ------------------------------------------------------------------

    def get_agent_context(self, agent_type: str) -> str:
        """Return combined memory + evolved traits for prompt injection.

        Capped at MAX_CONTEXT_CHARS to avoid bloating prompts.
        """
        memory = self.load_memory(agent_type).strip()
        traits = self.load_evolved_traits(agent_type).strip()

        if not memory and not traits:
            return ""

        parts: list[str] = ["## Agent Memory\n"]

        if memory:
            # Truncate memory to fit budget, keeping most recent content
            budget = MAX_CONTEXT_CHARS - len(traits) - 200
            if len(memory) > budget:
                memory = "...(truncated)\n" + memory[-budget:]
            parts.append(memory)

        if traits:
            parts.append("\n## Evolved Traits\n")
            parts.append(traits)

        combined = "\n".join(parts)
        if len(combined) > MAX_CONTEXT_CHARS:
            combined = combined[-MAX_CONTEXT_CHARS:]

        return combined

    # ------------------------------------------------------------------
    # Trait evolution
    # ------------------------------------------------------------------

    def maybe_evolve_traits(self, agent_type: str, completion_count: int) -> None:
        """Trigger trait evolution if enough tasks have been completed.

        Runs every 10 completions. Uses Ollama (local, cheap) if available,
        otherwise skips silently.
        """
        if completion_count == 0 or completion_count % 10 != 0:
            return

        import threading

        def _evolve() -> None:
            try:
                memory = self.load_memory(agent_type)
                current_traits = self.load_evolved_traits(agent_type)

                # Extract recent task outcomes (last 10)
                outcomes: list[str] = []
                in_outcomes = False
                for line in memory.split("\n"):
                    if line.startswith("## Task Outcomes"):
                        in_outcomes = True
                        continue
                    if in_outcomes and line.startswith("## ") and not line.startswith("### "):
                        break
                    if in_outcomes and line.startswith("- "):
                        outcomes.append(line)
                recent = outcomes[-10:] if len(outcomes) > 10 else outcomes

                if not recent:
                    return

                prompt = (
                    f"You are analyzing the work history of a '{agent_type}' AI agent. "
                    "Based on recent task outcomes, update the agent's evolved traits document.\n\n"
                    "## Recent Task Outcomes\n"
                    + "\n".join(recent)
                    + "\n\n## Current Evolved Traits\n"
                    + (current_traits or "(none yet)")
                    + "\n\n## Instructions\n"
                    "Write an updated EVOLVED_TRAITS.md with sections:\n"
                    "- Confidence Areas (skills/domains with consistent success)\n"
                    "- Growth Areas (recurring failures or struggles)\n"
                    "- Behavioral Adjustments (lessons learned)\n\n"
                    "Be concise. Output only the markdown content."
                )

                # Try Ollama first
                import subprocess as sp

                result = sp.run(
                    ["ollama", "run", "llama3.2:1b"],
                    input=prompt,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if result.returncode == 0 and result.stdout.strip():
                    self.update_evolved_traits(agent_type, result.stdout.strip() + "\n")
                    logger.info(
                        "[AGENT_MEMORY] Evolved traits for %s after %d completions",
                        agent_type,
                        completion_count,
                    )
                else:
                    logger.debug(
                        "[AGENT_MEMORY] Ollama not available for trait evolution, skipping"
                    )
            except FileNotFoundError:
                logger.debug("[AGENT_MEMORY] Ollama not installed, skipping trait evolution")
            except Exception as e:
                logger.debug("[AGENT_MEMORY] Trait evolution failed (non-critical): %s", e)

        # Run async to avoid blocking the main loop
        threading.Thread(target=_evolve, daemon=True).start()

    # ------------------------------------------------------------------
    # Pruning
    # ------------------------------------------------------------------

    @staticmethod
    def _prune_old_outcomes(content: str) -> str:
        """Remove task outcome entries older than PRUNE_AFTER_DAYS."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=PRUNE_AFTER_DAYS)
        ).strftime("%Y-%m-%d")

        lines = content.split("\n")
        result: list[str] = []
        skip_section = False

        for line in lines:
            # Check for date headers like ### 2026-01-15
            match = re.match(r"^### (\d{4}-\d{2}-\d{2})", line)
            if match:
                date_str = match.group(1)
                if date_str < cutoff:
                    skip_section = True
                    continue
                else:
                    skip_section = False

            if skip_section and (line.startswith("- ") or line.strip() == ""):
                continue

            if skip_section and line.startswith("#"):
                skip_section = False

            result.append(line)

        return "\n".join(result)

    # ------------------------------------------------------------------
    # Git
    # ------------------------------------------------------------------

    def _git_commit_memory(self, agent_type: str, message: str) -> None:
        """Stage and commit memory files for one agent type."""
        agent_dir = self._agent_dir(agent_type)
        relative = agent_dir.relative_to(self._control_repo)
        try:
            subprocess.run(
                ["git", "add", str(relative)],
                cwd=self._control_repo,
                capture_output=True,
                timeout=10,
            )
            result = subprocess.run(
                ["git", "diff", "--cached", "--quiet"],
                cwd=self._control_repo,
                capture_output=True,
                timeout=10,
            )
            if result.returncode == 0:
                return  # Nothing to commit

            subprocess.run(
                ["git", "commit", "-m", message],
                cwd=self._control_repo,
                capture_output=True,
                timeout=30,
            )
            logger.debug("[AGENT_MEMORY] Committed: %s", message)
        except Exception as e:
            logger.warning("[AGENT_MEMORY] Git commit failed: %s", e)
