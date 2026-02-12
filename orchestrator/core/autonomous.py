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
COOLDOWN_SECONDS = 4 * 60 * 60  # 4 hours

# Agent types eligible for autonomous work.
# Programmer is excluded — it needs specific tasks (code changes without
# clear direction are risky).
AUTONOMOUS_AGENTS = {
    "researcher": {
        "project": "self-improvement",
    },
    # Reviewer is NOT autonomous — it runs as a batched review of accumulated
    # changes rather than per-task or idle self-directed work.
    "architect": {
        "project": "lobs-dashboard",
    },
    "writer": {
        "project": "lobs-dashboard",
    },
}

# The universal autonomous prompt. Each agent's AGENTS.md, SOUL.md, and MEMORY.md
# define who they are and what they know. We just tell them: you're on the clock,
# figure out what's most valuable and do it.
AUTONOMOUS_PROMPT = """\
You're on the clock. No one has assigned you a specific task — it's up to you to \
decide what's most valuable to work on right now.

## How to decide

1. **Read your MEMORY.md** — what have you done before? What did you learn? \
Don't repeat yourself. Build on past work.
2. **Look at the projects** — read the codebase, recent git history, existing docs. \
What needs attention? What's broken, missing, or could be better?
3. **Think about what only YOU can do** — you're a {agent_type}. What's the highest-value \
contribution someone in your role could make right now?

## What you can do freely

These are always safe to do without asking:
- **Fix bugs** — broken behavior, crashes, incorrect logic
- **Resolve TODOs** — clean up technical debt in the codebase
- **Improve code quality** — refactor, remove dead code, improve performance
- **Add/improve tests** — better coverage, fix flaky tests
- **Research** — investigate topics, write reports, explore ideas in depth

These don't change the product. They improve what's already there or produce knowledge.

## What needs approval

If your idea would **change what the product does or looks like**, don't build it. \
Instead, create an inbox proposal:

- New features or UI changes
- New endpoints or APIs
- Architecture changes
- Anything a user would notice as different

Write the proposal as an inbox item with:
- **Title** — what you want to build
- **Why** — what problem it solves
- **Scope** — what you'd change
- **Project** — which project it's for

Then move on to other work. If it gets approved, it'll come back as a task.

## The test

Ask yourself: "Am I making a product decision, or improving existing work?" \
If it's a product decision, propose it. If it's improvement or research, do it.

## Standards

- **Only work on things that matter.** If you can't articulate why this is valuable, don't do it.
- **Go deep, not wide.** One excellent piece of work beats five shallow ones.
- **Don't invent busywork.** If nothing genuinely needs doing, write a brief note in your memory about what you looked at and stop. Not every launch needs a big deliverable.
- **Be opinionated.** Have a point of view. Make recommendations. Argue for them.
- **Produce a real deliverable.** Write it to the appropriate directory:
  - Research/proposals → `state/research/proposals/`
  - Design docs → `state/designs/`
  - Audit reports → `state/reports/`
  - Documentation → the project's docs directory
- **Update your MEMORY.md** with what you learned, what you think, and what \
you'd want to work on next time.
"""


class AutonomousLauncher:
    """Tracks and triggers autonomous agent launches."""

    def __init__(self, cooldown_seconds: float = COOLDOWN_SECONDS):
        self.cooldown_seconds = cooldown_seconds
        # agent_type -> last launch timestamp
        # Initialize all agents as "just launched" so we don't flood on startup
        now = time.time()
        self._last_launch: dict[str, float] = {
            agent_type: now for agent_type in AUTONOMOUS_AGENTS
        }

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
