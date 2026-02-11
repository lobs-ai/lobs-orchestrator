"""orchestrator.services.agent_meta

Meta-brain for agent self-awareness. Uses a two-tier model strategy:

- **Primary**: Ollama (local, free, fast) for frequent meta-work
- **Fallback**: Claude Haiku (cloud, cheap) when Ollama is unavailable
- **Guardrail**: Claude Haiku periodically audits Ollama's memory/trait outputs

Functions:
- summarize_thinking: Summarize recent log tail into a coherent thought
- describe_activity: Generate a natural activity description
- reflect_on_task: Reflect on what went well/poorly after completion
- synthesize_memory: Update Patterns Learned + Preferences
- evolve_traits: Rewrite EVOLVED_TRAITS.md from recent outcomes
- audit_memory: Haiku reviews Ollama's memory outputs for quality (guardrail)

All calls are non-blocking (submitted to a thread pool) and gracefully degrade
to dumb fallbacks when neither model is available.
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orchestrator.core.agent_memory import AgentMemoryManager
    from orchestrator.core.agent_tracker import AgentTracker
    from orchestrator.core.llm_backend import LLMBackend

logger = logging.getLogger(__name__)

# Defaults (overridable via settings)
DEFAULT_THINKING_INTERVAL = 30  # seconds between thinking summarizations per agent
DEFAULT_ACTIVITY_INTERVAL = 60  # seconds between activity descriptions per agent
AVAILABILITY_CACHE_TTL = 60  # seconds to cache Ollama availability check
HAIKU_AUDIT_EVERY = 20  # audit Ollama memory output every N completions



# Seed guidance per agent type — tells the AI what to look for beyond deterministic scanning.
ADVISOR_SEED_GUIDANCE: dict[str, str] = {
    "programmer": (
        "Look for: code needing a different approach after recent failures, "
        "integration risks between projects, missing error handling patterns, "
        "small refactors that would ease upcoming work."
    ),
    "researcher": (
        "Look for: dependencies nearing end-of-life, technology alternatives worth evaluating, "
        "knowledge gaps revealed by recent failures, security patterns worth investigating."
    ),
    "reviewer": (
        "Look for: dense recent changes that need review, recurring bug patterns, "
        "high-churn code areas with merge risk, quality regressions in recent work."
    ),
    "writer": (
        "Look for: recently completed features without documentation, architecture decisions "
        "that should be recorded, onboarding friction points, outdated README sections."
    ),
    "architect": (
        "Look for: recurring failure patterns suggesting design problems, growing coupling "
        "between components, performance concerns from recent activity, shared library opportunities."
    ),
}

DEFAULT_ADVISOR_INTERVAL = 900  # 15 minutes between AI advisor calls per agent type


class AgentMetaBrain:
    """Multi-backend meta-cognitive service with ordered fallback."""

    def __init__(
        self,
        backends: list[LLMBackend],
        agent_tracker: AgentTracker,
        agent_memory: AgentMemoryManager,
        thinking_interval: int = DEFAULT_THINKING_INTERVAL,
        activity_interval: int = DEFAULT_ACTIVITY_INTERVAL,
        advisor_interval: int = DEFAULT_ADVISOR_INTERVAL,
        audit_backend: LLMBackend | None = None,
    ) -> None:
        self._backends = backends
        self._tracker = agent_tracker
        self._memory = agent_memory
        self._thinking_interval = thinking_interval
        self._activity_interval = activity_interval
        self._advisor_interval = advisor_interval
        self._audit_backend = audit_backend

        # Thread pool — at most 2 concurrent model calls to avoid flooding
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="meta-brain")

        # Rate limiting per agent type
        self._last_thinking: dict[str, float] = {}
        self._last_activity: dict[str, float] = {}

        # AI advisor rate limiting per agent type
        self._last_advisor_scan: dict[str, float] = {}

        # Track completions for Haiku audit trigger
        self._completion_counts: dict[str, int] = {}

        logger.info(
            "[AGENT_META] Initialized (backends=%s, audit=%s, thinking=%ds, activity=%ds, advisor=%ds)",
            [b.name for b in backends],
            audit_backend.name if audit_backend else "none",
            thinking_interval,
            activity_interval,
            advisor_interval,
        )

    # ------------------------------------------------------------------
    # Backend availability
    # ------------------------------------------------------------------

    def _any_backend_available(self) -> bool:
        """Check if at least one backend is available."""
        return any(b.is_available() for b in self._backends)

    # ------------------------------------------------------------------
    # Unified generate helper
    # ------------------------------------------------------------------

    def _generate(self, prompt: str, temperature: float = 0.3) -> str | None:
        """Generate text by trying each backend in order until one succeeds."""
        for backend in self._backends:
            if not backend.is_available():
                continue
            try:
                result = backend.generate(prompt, temperature=temperature)
                if result:
                    return result
            except Exception as e:
                logger.debug("[AGENT_META] %s failed: %s", backend.name, e)
        return None

    def _read_log_tail(self, log_path: Path, lines: int = 40) -> str:
        """Read the last N lines of a log file."""
        if not log_path or not log_path.exists():
            return ""
        try:
            content = log_path.read_text(encoding="utf-8", errors="replace")
            all_lines = content.strip().splitlines()
            tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
            return "\n".join(tail)
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # 1. Thinking Summarization
    # ------------------------------------------------------------------

    def summarize_thinking(self, agent_type: str, log_path: Path) -> None:
        """Summarize what the agent is currently thinking from its log output.

        Rate-limited to `thinking_interval` seconds per agent. Falls back to
        log-tail extraction if no model is available.
        """
        now = time.time()
        if (now - self._last_thinking.get(agent_type, 0)) < self._thinking_interval:
            return
        self._last_thinking[agent_type] = now

        if not self._any_backend_available():
            # Fallback: use existing dumb extraction
            from orchestrator.core.agent_tracker import AgentTracker as _AT
            snippet = _AT.extract_thinking_from_log(log_path)
            if snippet:
                self._tracker.update_thinking(agent_type, snippet)
            return

        self._executor.submit(self._do_summarize_thinking, agent_type, log_path)

    def _do_summarize_thinking(self, agent_type: str, log_path: Path) -> None:
        """Worker thread: generate thinking summary."""
        log_tail = self._read_log_tail(log_path, lines=40)
        if not log_tail:
            return

        prompt = (
            f"You are the inner voice of a {agent_type} AI agent that is actively working on a task. "
            "Based on the recent log output below, write ONE concise sentence (max 100 chars) describing "
            "what you're currently thinking about or working on. Be specific and technical. "
            "Do not include any preamble.\n\n"
            f"Log output:\n```\n{log_tail[-3000:]}\n```"
        )

        result = self._generate(prompt)
        if result:
            line = result.split("\n")[0].strip().strip('"').strip("'")
            if line:
                self._tracker.update_thinking(agent_type, line[:500])
                logger.debug("[AGENT_META] Thinking for %s: %s", agent_type, line[:80])

    # ------------------------------------------------------------------
    # 2. Activity Description
    # ------------------------------------------------------------------

    def describe_activity(
        self,
        agent_type: str,
        task_title: str,
        project_id: str,
        task_notes: str = "",
    ) -> None:
        """Generate a natural activity description for what the agent is doing.

        Rate-limited to `activity_interval` seconds per agent.
        """
        now = time.time()
        if (now - self._last_activity.get(agent_type, 0)) < self._activity_interval:
            return
        self._last_activity[agent_type] = now

        if not self._any_backend_available():
            return  # activity is already set from mark_working()

        self._executor.submit(
            self._do_describe_activity, agent_type, task_title, project_id, task_notes
        )

    def _do_describe_activity(
        self, agent_type: str, task_title: str, project_id: str, task_notes: str
    ) -> None:
        """Worker thread: generate activity description."""
        notes_snippet = (task_notes or "")[:500]
        prompt = (
            f"Write a brief natural description (max 15 words) of what a {agent_type} "
            f"agent is doing right now. No preamble, just the description.\n\n"
            f"Task: {task_title}\nProject: {project_id}\n"
        )
        if notes_snippet:
            prompt += f"Notes: {notes_snippet}\n"

        result = self._generate(prompt)
        if result:
            line = result.split("\n")[0].strip().strip('"').strip("'")
            if line:
                status = self._tracker.get_status(agent_type)
                if status and status.status != "idle":
                    status.activity = line[:200]
                    self._tracker._dirty.add(agent_type)
                    logger.debug("[AGENT_META] Activity for %s: %s", agent_type, line[:80])

    # ------------------------------------------------------------------
    # 3. Post-Task Reflection
    # ------------------------------------------------------------------

    def reflect_on_task(
        self,
        agent_type: str,
        task_title: str,
        project_id: str,
        success: bool,
        duration_seconds: float,
        log_path: Path,
    ) -> None:
        """Generate a reflection after task completion. Non-blocking."""
        if not self._any_backend_available():
            return

        self._executor.submit(
            self._do_reflect_on_task,
            agent_type, task_title, project_id, success, duration_seconds, log_path,
        )

    def _do_reflect_on_task(
        self,
        agent_type: str,
        task_title: str,
        project_id: str,
        success: bool,
        duration_seconds: float,
        log_path: Path,
    ) -> None:
        """Worker thread: generate post-task reflection."""
        log_tail = self._read_log_tail(log_path, lines=30)
        duration_min = int(duration_seconds / 60)
        outcome = "succeeded" if success else "failed"

        prompt = (
            f"You are a {agent_type} AI agent reflecting on a task you just completed.\n\n"
            f"Task: {task_title}\n"
            f"Project: {project_id}\n"
            f"Outcome: {outcome} in {duration_min} minutes\n\n"
        )
        if log_tail:
            prompt += f"Log tail:\n```\n{log_tail[-2000:]}\n```\n\n"
        prompt += (
            "Write 2-3 concise sentences reflecting on this task. What went well? "
            "What was tricky? What would you do differently next time? "
            "No preamble — just the reflection."
        )

        result = self._generate(prompt, temperature=0.5)
        if result:
            self._memory.append_reflection(agent_type, result)
            logger.info("[AGENT_META] Reflection for %s: %s", agent_type, result[:100])

    # ------------------------------------------------------------------
    # 4. Memory Synthesis
    # ------------------------------------------------------------------

    def synthesize_memory(self, agent_type: str) -> None:
        """Update Patterns Learned and Preferences from task outcomes. Non-blocking."""
        if not self._any_backend_available():
            return

        self._executor.submit(self._do_synthesize_memory, agent_type)

    def _do_synthesize_memory(self, agent_type: str) -> None:
        """Worker thread: synthesize memory sections."""
        memory = self._memory.load_memory(agent_type)
        if not memory.strip():
            return

        prompt = (
            f"You are a {agent_type} AI agent reviewing your memory file. Based on your "
            "task outcomes and reflections, update the Patterns Learned and Preferences sections.\n\n"
            "Current memory:\n"
            f"```\n{memory[-4000:]}\n```\n\n"
            "Rules:\n"
            "- Keep existing valid entries\n"
            "- Add new observations from recent outcomes\n"
            "- Remove stale or contradicted entries\n"
            "- Be concise — bullet points, not paragraphs\n\n"
            "Output ONLY these two sections as markdown (starting with ## Patterns Learned and ## Preferences). "
            "No other text."
        )

        result = self._generate(prompt, temperature=0.4)
        if result and ("Patterns" in result or "Preferences" in result):
            patterns = ""
            preferences = ""

            sections = result.split("## ")
            for section in sections:
                lower = section.lower()
                if lower.startswith("patterns"):
                    patterns = section[section.index("\n") + 1:].strip() if "\n" in section else ""
                elif lower.startswith("preferences"):
                    preferences = section[section.index("\n") + 1:].strip() if "\n" in section else ""

            if patterns or preferences:
                self._memory.replace_memory_sections(agent_type, patterns, preferences)
                logger.info("[AGENT_META] Synthesized memory for %s", agent_type)

    # ------------------------------------------------------------------
    # 5. Trait Evolution
    # ------------------------------------------------------------------

    def evolve_traits(self, agent_type: str, completion_count: int) -> None:
        """Trigger trait evolution every 10 completions. Non-blocking.

        Also triggers a Haiku guardrail audit every HAIKU_AUDIT_EVERY completions.
        """
        if completion_count == 0 or completion_count % 10 != 0:
            return

        if not self._any_backend_available():
            return

        self._executor.submit(self._do_evolve_traits, agent_type, completion_count)

        # Guardrail audit every HAIKU_AUDIT_EVERY completions
        if completion_count % HAIKU_AUDIT_EVERY == 0 and self._audit_backend and self._audit_backend.is_available():
            self._executor.submit(self._do_audit_memory, agent_type)

    def _do_evolve_traits(self, agent_type: str, completion_count: int) -> None:
        """Worker thread: evolve traits."""
        memory = self._memory.load_memory(agent_type)
        current_traits = self._memory.load_evolved_traits(agent_type)

        # Extract recent task outcomes
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
        recent = outcomes[-10:]

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

        result = self._generate(prompt, temperature=0.5)
        if result:
            self._memory.update_evolved_traits(agent_type, result.strip() + "\n")
            logger.info(
                "[AGENT_META] Evolved traits for %s after %d completions",
                agent_type, completion_count,
            )

    # ------------------------------------------------------------------
    # 6. Haiku Guardrail Audit
    # ------------------------------------------------------------------

    def _do_audit_memory(self, agent_type: str) -> None:
        """Worker thread: Haiku reviews Ollama's memory/traits for quality.

        This is the guardrail — a smarter model periodically checks the cheaper
        model's outputs for hallucinations, contradictions, or low-quality content.
        """
        memory = self._memory.load_memory(agent_type)
        traits = self._memory.load_evolved_traits(agent_type)

        if not memory.strip() and not traits.strip():
            return

        prompt = (
            f"You are auditing the memory and evolved traits of a '{agent_type}' AI agent. "
            "These were generated by a small local model (Ollama). Review them for quality.\n\n"
            "## Current Memory\n"
            f"```\n{memory[-3000:]}\n```\n\n"
            "## Current Evolved Traits\n"
            f"```\n{traits[-2000:]}\n```\n\n"
            "## Audit Instructions\n"
            "Check for:\n"
            "1. Hallucinated entries (claims not supported by task outcomes)\n"
            "2. Contradictions between entries\n"
            "3. Stale or irrelevant entries\n"
            "4. Missing patterns that the outcomes clearly show\n\n"
            "If issues exist, output corrected versions of ONLY the affected sections "
            "(## Patterns Learned, ## Preferences, or EVOLVED_TRAITS). "
            "If everything looks good, respond with just: LGTM\n\n"
            "Be concise."
        )

        result = self._audit_backend.generate(prompt, temperature=0.2, max_tokens=1500) if self._audit_backend else None
        if not result:
            return

        if result.strip().upper().startswith("LGTM"):
            logger.info("[AGENT_META] Haiku audit for %s: LGTM", agent_type)
            return

        # Haiku found issues — apply corrections
        logger.info("[AGENT_META] Haiku audit for %s found issues, applying corrections", agent_type)

        # Check if it contains corrected memory sections
        if "## Patterns Learned" in result or "## Preferences" in result:
            patterns = ""
            preferences = ""
            sections = result.split("## ")
            for section in sections:
                lower = section.lower()
                if lower.startswith("patterns"):
                    patterns = section[section.index("\n") + 1:].strip() if "\n" in section else ""
                elif lower.startswith("preferences"):
                    preferences = section[section.index("\n") + 1:].strip() if "\n" in section else ""
            if patterns or preferences:
                self._memory.replace_memory_sections(agent_type, patterns, preferences)

        # Check if it contains corrected traits
        if "Confidence Areas" in result or "Growth Areas" in result or "Behavioral" in result:
            # Extract just the traits portion
            traits_start = None
            for marker in ("# Evolved Traits", "## Confidence Areas", "Confidence Areas"):
                if marker in result:
                    traits_start = result.index(marker)
                    break
            if traits_start is not None:
                self._memory.update_evolved_traits(agent_type, result[traits_start:].strip() + "\n")

    # ------------------------------------------------------------------
    # 7. AI-Powered Proactive Work Suggestions
    # ------------------------------------------------------------------

    def suggest_proactive_work(
        self,
        agent_type: str,
        awareness_context: str,
        agent_capabilities: list[str],
        agent_proactive: list[str],
        project_summaries: list[dict],
        existing_kinds: set[str],
    ) -> list[dict]:
        """Generate AI-powered proactive work suggestions.

        Synchronous — called from the main loop when results are needed
        immediately. Uses the same two-tier Ollama+Haiku infrastructure.

        Rate-limited per agent_type to ``_advisor_interval`` seconds.

        Returns a list of dicts with keys:
            project_id, agent_type, kind, title, description, priority
        """
        # Rate limiting per agent type
        now = time.time()
        last = self._last_advisor_scan.get(agent_type, 0)
        if (now - last) < self._advisor_interval:
            return []
        self._last_advisor_scan[agent_type] = now

        if not self._any_backend_available():
            return []

        # Build the prompt from 4 context blocks (each capped)
        prompt = self._build_advisor_prompt(
            agent_type,
            awareness_context,
            agent_capabilities,
            agent_proactive,
            project_summaries,
            existing_kinds,
        )

        result = self._generate(prompt, temperature=0.5)
        if not result:
            logger.debug("[PROACTIVE-AI] No response from model for %s", agent_type)
            return []

        # Extract valid project IDs for validation
        valid_projects = {s["id"] for s in project_summaries if "id" in s}

        suggestions = self._parse_advisor_suggestions(result, agent_type, valid_projects)
        logger.info("[PROACTIVE-AI] %s suggested %d item(s)", agent_type, len(suggestions))
        return suggestions

    def _build_advisor_prompt(
        self,
        agent_type: str,
        awareness_context: str,
        agent_capabilities: list[str],
        agent_proactive: list[str],
        project_summaries: list[dict],
        existing_kinds: set[str],
    ) -> str:
        """Build the prompt for the AI advisor from capped context blocks."""
        # Block 1: Awareness snapshot (~800 chars)
        awareness_block = (awareness_context or "No current activity.")[:800]

        # Block 2: Agent memory (~600 chars)
        memory = self._memory.get_agent_context(agent_type)
        memory_block = (memory or "No memory yet.")[:600]

        # Block 3: Project summaries (~600 chars)
        proj_lines: list[str] = []
        for ps in project_summaries:
            langs: list[str] = []
            if ps.get("has_python"):
                langs.append("Python")
            if ps.get("has_node"):
                langs.append("Node")
            if ps.get("has_swift"):
                langs.append("Swift")
            lang_str = ", ".join(langs) if langs else "unknown"
            proj_lines.append(f"- {ps['id']} ({lang_str})")
        projects_block = "\n".join(proj_lines)[:600] if proj_lines else "No projects."

        # Block 4: Capabilities + seed guidance (~400 chars)
        caps_str = ", ".join(agent_capabilities[:8]) if agent_capabilities else "general"
        proactive_str = ", ".join(agent_proactive[:5]) if agent_proactive else "general proactive work"
        seed = ADVISOR_SEED_GUIDANCE.get(agent_type, "Look for useful work this agent could do.")
        caps_block = f"Capabilities: {caps_str}\nProactive focus: {proactive_str}\n{seed}"[:400]

        # Existing kinds to avoid
        existing_str = ", ".join(sorted(existing_kinds)[:10]) if existing_kinds else "none"

        prompt = (
            f"You are an AI advisor for a '{agent_type}' agent in an automated software system. "
            "Based on the context below, suggest 1-3 concrete, actionable tasks this agent could work on.\n\n"
            "RULES:\n"
            "- Each suggestion must be specific and actionable, not vague\n"
            "- Suggest work the DETERMINISTIC scanner would miss (it already finds: TODOs, unpinned deps, "
            "missing tests, doc drift, large files)\n"
            f"- Avoid overlapping with these already-found opportunity kinds: {existing_str}\n"
            "- Use ai_ prefix for the KIND field\n"
            "- Priority must be one of: URGENT, HIGH, NORMAL, LOW\n"
            "- PROJECT must be one of the listed project IDs\n\n"
            f"## System State\n{awareness_block}\n\n"
            f"## Agent Memory\n{memory_block}\n\n"
            f"## Projects\n{projects_block}\n\n"
            f"## Agent Profile\n{caps_block}\n\n"
            "OUTPUT FORMAT — one suggestion per line, pipe-delimited:\n"
            "KIND: ai_<name> | PRIORITY: <level> | PROJECT: <id> | TITLE: <short title> | DESCRIPTION: <detail>\n\n"
            "Output ONLY the suggestion lines, nothing else. If nothing useful to suggest, output: NONE"
        )
        return prompt

    def _parse_advisor_suggestions(
        self, raw: str, agent_type: str, valid_projects: set[str]
    ) -> list[dict]:
        """Parse pipe-delimited AI advisor output into suggestion dicts.

        Skips lines that are unparseable, have invalid projects, or
        are missing required fields. Returns at most 3 suggestions.
        """
        if not raw or raw.strip().upper() == "NONE":
            return []

        priority_map = {"URGENT": 1, "HIGH": 2, "NORMAL": 3, "LOW": 4}
        suggestions: list[dict] = []

        for line in raw.strip().splitlines():
            line = line.strip()
            if not line or line.upper() == "NONE":
                continue

            # Parse pipe-delimited fields
            fields: dict[str, str] = {}
            for segment in line.split("|"):
                segment = segment.strip()
                if ":" not in segment:
                    continue
                key, _, value = segment.partition(":")
                fields[key.strip().upper()] = value.strip()

            # Validate required fields
            kind = fields.get("KIND", "").strip().lower()
            priority_str = fields.get("PRIORITY", "NORMAL").strip().upper()
            project = fields.get("PROJECT", "").strip()
            title = fields.get("TITLE", "").strip()
            description = fields.get("DESCRIPTION", "").strip()

            if not kind or not title or not project:
                continue

            # Ensure ai_ prefix
            if not kind.startswith("ai_"):
                kind = f"ai_{kind}"

            # Validate project
            if project not in valid_projects:
                continue

            # Map priority
            priority_val = priority_map.get(priority_str, 3)

            suggestions.append({
                "project_id": project,
                "agent_type": agent_type,
                "kind": kind,
                "title": title[:200],
                "description": description[:500] if description else title,
                "priority": priority_val,
            })

            if len(suggestions) >= 3:
                break

        return suggestions

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Shut down the thread pool gracefully."""
        self._executor.shutdown(wait=False)
        logger.info("[AGENT_META] Shut down")
