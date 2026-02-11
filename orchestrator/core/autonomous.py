"""orchestrator.core.autonomous

Autonomous agent launches. When agents have no queued tasks, the orchestrator
launches them to do their standing work. Each agent type has an inherent job —
the researcher researches, the reviewer reviews, etc.

Agents figure out what to do based on their AGENTS.md, project context, and
their own memory. The orchestrator just launches them.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# Minimum seconds between autonomous launches for the same agent type.
# Prevents rapid re-launching if an agent finishes quickly.
COOLDOWN_SECONDS = 30 * 60  # 30 minutes

# Agent types eligible for autonomous work.
# Programmer is excluded — it needs specific tasks (code changes without
# clear direction are risky).
AUTONOMOUS_AGENTS = {
    "researcher": {
        "prompt": (
            "You have no specific task assigned. Do your job as a researcher:\n\n"
            "1. Read your MEMORY.md to see what you've already researched\n"
            "2. Check the projects you have access to — what could benefit from research?\n"
            "3. Look for new tools, patterns, best practices, or community developments\n"
            "4. Focus on things that would be actionable and useful\n"
            "5. Write your findings to a research document\n"
            "6. Create handoffs to writer if findings deserve a polished write-up\n"
            "7. Update your MEMORY.md with what you learned\n\n"
            "Pick the most valuable thing to research right now. Be thorough but focused — "
            "one good finding is better than five shallow ones."
        ),
        "project": "self-improvement",
    },
    "reviewer": {
        "prompt": (
            "You have no specific task assigned. Do your job as a reviewer:\n\n"
            "1. Read your MEMORY.md to see what you've already reviewed\n"
            "2. Check recent changes in the project repos (git log)\n"
            "3. Look for code quality issues, missed edge cases, or patterns that could cause bugs\n"
            "4. Review any recent work by other agents (check .work-summary files)\n"
            "5. Write review notes or create handoffs for issues found\n"
            "6. Update your MEMORY.md with patterns and common issues\n\n"
            "Focus on the most impactful review you can do right now."
        ),
        "project": "lobs-dashboard",
    },
    "architect": {
        "prompt": (
            "You have no specific task assigned. Do your job as an architect:\n\n"
            "1. Read your MEMORY.md to see your current understanding of the systems\n"
            "2. Review the architecture of the projects — is anything drifting from the design?\n"
            "3. Look for opportunities to simplify, improve performance, or reduce complexity\n"
            "4. Check if any components need better documentation or design docs\n"
            "5. Write design notes or create handoffs for improvements\n"
            "6. Update your MEMORY.md with architectural insights\n\n"
            "Focus on the highest-leverage architectural insight you can provide."
        ),
        "project": "lobs-dashboard",
    },
    "writer": {
        "prompt": (
            "You have no specific task assigned. Do your job as a writer:\n\n"
            "1. Read your MEMORY.md to see what documentation you've worked on\n"
            "2. Check for stale or missing documentation in the projects\n"
            "3. Look for README files that need updating, missing setup guides, etc.\n"
            "4. Check if any recent research or design docs need polishing\n"
            "5. Write or improve documentation\n"
            "6. Update your MEMORY.md with documentation conventions and patterns\n\n"
            "Focus on the most impactful documentation improvement right now."
        ),
        "project": "lobs-dashboard",
    },
}


class AutonomousLauncher:
    """Tracks and triggers autonomous agent launches."""

    def __init__(self, cooldown_seconds: float = COOLDOWN_SECONDS):
        self.cooldown_seconds = cooldown_seconds
        # agent_type -> last launch timestamp
        self._last_launch: dict[str, float] = {}

    def get_idle_agents(
        self,
        active_agent_types: set[str],
        queued_agent_types: set[str],
    ) -> list[tuple[str, dict[str, Any]]]:
        """Return agent types that are idle, have no queued work, and are past cooldown.
        
        Returns list of (agent_type, config) tuples.
        """
        now = time.time()
        idle = []

        for agent_type, config in AUTONOMOUS_AGENTS.items():
            # Skip if agent is currently running
            if agent_type in active_agent_types:
                continue

            # Skip if there are queued tasks for this agent type
            if agent_type in queued_agent_types:
                continue

            # Skip if within cooldown
            last = self._last_launch.get(agent_type, 0)
            if now - last < self.cooldown_seconds:
                remaining = self.cooldown_seconds - (now - last)
                logger.debug(
                    f"[AUTONOMOUS] {agent_type} in cooldown ({remaining:.0f}s remaining)"
                )
                continue

            idle.append((agent_type, config))

        return idle

    def mark_launched(self, agent_type: str) -> None:
        """Record that an agent was autonomously launched."""
        self._last_launch[agent_type] = time.time()
        logger.info(f"[AUTONOMOUS] Launched {agent_type} for autonomous work")

    def get_status(self) -> dict[str, Any]:
        """Return status of autonomous launcher."""
        now = time.time()
        status = {}
        for agent_type in AUTONOMOUS_AGENTS:
            last = self._last_launch.get(agent_type, 0)
            cooldown_remaining = max(0, self.cooldown_seconds - (now - last))
            status[agent_type] = {
                "last_launch": last,
                "cooldown_remaining": cooldown_remaining,
                "ready": cooldown_remaining == 0,
            }
        return status
