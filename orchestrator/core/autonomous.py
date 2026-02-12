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
        "title": "Research: discover high-value opportunities",
        "prompt": (
            "Your mission: Find something genuinely valuable that we're not doing yet.\n\n"
            "## Instructions\n\n"
            "1. Read your MEMORY.md — don't repeat past research\n"
            "2. Use the browser to research ONE of these areas (pick the most promising):\n"
            "   - **New tools/integrations** that could save significant time or unlock capabilities\n"
            "   - **How other teams/people solve problems** we're currently struggling with\n"
            "   - **Emerging patterns** in AI agents, automation, developer tooling\n"
            "   - **OpenClaw ecosystem** — new skills on ClaWHub, community patterns, docs updates\n"
            "3. Go deep on ONE finding — don't skim five things\n"
            "4. Produce a concrete deliverable:\n\n"
            "## Output: Write a proposal document to `state/research/proposals/`\n\n"
            "The document MUST include:\n"
            "- **What you found** — the specific tool/pattern/approach with sources\n"
            "- **Why it matters for us** — concrete benefits, not vague promises\n"
            "- **Implementation plan** — what would it take? Small tweak or big project?\n"
            "- **Effort estimate** — hours/days, complexity (low/medium/high)\n"
            "- **Recommendation** — should we do this? Why or why not?\n\n"
            "Create a handoff to writer to polish the proposal for dashboard review.\n"
            "Update your MEMORY.md with what you learned.\n\n"
            "Quality over quantity. One excellent proposal beats ten shallow ones."
        ),
        "project": "self-improvement",
    },
    "reviewer": {
        "title": "Review: audit recent agent work and code quality",
        "prompt": (
            "Your mission: Audit recent work and surface real issues worth fixing.\n\n"
            "## Instructions\n\n"
            "1. Read your MEMORY.md — build on past reviews, don't repeat them\n"
            "2. Check `git log --oneline -20` in the project repos to see recent changes\n"
            "3. Review the most significant recent changes:\n"
            "   - Are there bugs or edge cases missed?\n"
            "   - Are there patterns that will cause problems at scale?\n"
            "   - Did other agents (programmer, architect) make questionable decisions?\n"
            "   - Is test coverage adequate for critical paths?\n"
            "4. Check agent work quality: read recent worker logs in state/worker-results/\n\n"
            "## Output: Write a review report to `state/reports/`\n\n"
            "The report MUST include:\n"
            "- **Critical issues** — bugs or problems that need immediate attention\n"
            "- **Quality concerns** — patterns that should change\n"
            "- **What's working well** — good patterns to reinforce\n"
            "- **Specific recommendations** — actionable items, not vague advice\n\n"
            "For each critical issue, create a handoff to programmer with clear fix instructions.\n"
            "Update your MEMORY.md with review patterns and recurring issues."
        ),
        "project": "lobs-dashboard",
    },
    "architect": {
        "title": "Architecture: evaluate and propose system improvements",
        "prompt": (
            "Your mission: Find the highest-leverage architectural improvement we could make.\n\n"
            "## Instructions\n\n"
            "1. Read your MEMORY.md — what's your current understanding of the systems?\n"
            "2. Review the codebase architecture:\n"
            "   - Read ARCHITECTURE.md, AI.md, README.md in the project\n"
            "   - Check the actual code structure — does it match the documented architecture?\n"
            "   - Look for: unnecessary complexity, missing abstractions, performance bottlenecks\n"
            "   - Look for: things that will break as the system grows\n"
            "3. Think about the BIG picture:\n"
            "   - What's the most impactful change we could make to the system design?\n"
            "   - What architectural debt will hurt us most in 3 months?\n"
            "   - Are there simpler approaches to what we're doing?\n\n"
            "## Output: Write a design proposal to `state/designs/`\n\n"
            "The proposal MUST include:\n"
            "- **Current state** — how things work now (brief)\n"
            "- **Problem** — what's wrong or limiting\n"
            "- **Proposed change** — specific, concrete design with diagrams/pseudocode if helpful\n"
            "- **Tradeoffs** — what we gain, what we lose, risks\n"
            "- **Migration path** — how to get from here to there incrementally\n"
            "- **Effort estimate** — scope and complexity\n\n"
            "Create a handoff to writer to polish the proposal for review.\n"
            "Update your MEMORY.md with architectural insights."
        ),
        "project": "lobs-dashboard",
    },
    "writer": {
        "title": "Documentation: improve or create high-impact docs",
        "prompt": (
            "Your mission: Find and fix the most impactful documentation gap.\n\n"
            "## Instructions\n\n"
            "1. Read your MEMORY.md — what docs have you already worked on?\n"
            "2. Survey the current documentation state:\n"
            "   - Check README.md — is it accurate and useful for a new person?\n"
            "   - Check ARCHITECTURE.md — does it reflect the actual system?\n"
            "   - Look in state/research/ and state/designs/ for unpolished docs\n"
            "   - Check if any recent changes made docs stale\n"
            "3. Pick ONE documentation task — the highest impact gap\n\n"
            "## Output\n\n"
            "Write or significantly improve ONE document. It should be:\n"
            "- **Accurate** — verified against actual code/behavior\n"
            "- **Scannable** — headers, bullets, code examples\n"
            "- **Actionable** — a reader should know what to do after reading it\n"
            "- **Complete** — covers the topic end to end, not a stub\n\n"
            "If you find unpolished research or design proposals, polish those first — \n"
            "they're higher priority than writing new docs from scratch.\n"
            "Update your MEMORY.md with documentation conventions and project knowledge."
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
