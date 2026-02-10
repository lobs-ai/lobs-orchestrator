"""orchestrator.core.registry

Agent Registry

Loads agent definitions from the repository's `agents/<type>/` directories.

Each agent directory is expected to contain:
- AGENTS.md
- SOUL.md
- TOOLS.md
- IDENTITY.md
- USER.md

`IDENTITY.md` is parsed for:
- Model
- Capabilities

Configs are cached after first load.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


_REQUIRED_FILES = (
    "AGENTS.md",
    "SOUL.md",
    "TOOLS.md",
    "IDENTITY.md",
    "USER.md",
)


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """Fully materialized agent definition loaded from disk."""

    type: str
    agents_md: str
    soul_md: str
    tools_md: str
    identity_md: str
    user_md: str
    model: str
    capabilities: List[str]
    proactive: List[str]


class AgentRegistry:
    """Loads and caches agent definitions from `agents/` directory."""

    def __init__(self, agents_root: Optional[Path] = None):
        # Default to repo_root/agents (repo_root is two parents above orchestrator/).
        if agents_root is None:
            repo_root = Path(__file__).resolve().parents[2]
            agents_root = repo_root / "agents"
        self.agents_root = agents_root
        self._cache: Dict[str, AgentConfig] = {}

    def get_agent(self, type: str) -> AgentConfig:
        """Return the AgentConfig for `type` (cached after first load)."""
        if not type or not type.strip():
            raise ValueError("Agent type must be a non-empty string")

        key = type.strip().lower()
        if key in self._cache:
            return self._cache[key]

        config = self._load_agent(key)
        self._cache[key] = config
        return config

    def available_types(self) -> List[str]:
        """List available agent types (directory names) under agents_root."""
        if not self.agents_root.exists():
            return []

        types: List[str] = []
        for p in sorted(self.agents_root.iterdir()):
            if p.is_dir() and not p.name.startswith("."):
                types.append(p.name)
        return types

    def _load_agent(self, key: str) -> AgentConfig:
        agent_dir = self.agents_root / key
        if not agent_dir.exists() or not agent_dir.is_dir():
            raise FileNotFoundError(f"Agent type not found: {key} (expected dir {agent_dir})")

        missing = [name for name in _REQUIRED_FILES if not (agent_dir / name).exists()]
        if missing:
            raise FileNotFoundError(
                f"Agent '{key}' is missing required files: {', '.join(missing)}"
            )

        agents_md = (agent_dir / "AGENTS.md").read_text(encoding="utf-8")
        soul_md = (agent_dir / "SOUL.md").read_text(encoding="utf-8")
        tools_md = (agent_dir / "TOOLS.md").read_text(encoding="utf-8")
        identity_md = (agent_dir / "IDENTITY.md").read_text(encoding="utf-8")
        user_md = (agent_dir / "USER.md").read_text(encoding="utf-8")

        model, capabilities, proactive = _parse_identity(identity_md)

        return AgentConfig(
            type=key,
            agents_md=agents_md,
            soul_md=soul_md,
            tools_md=tools_md,
            identity_md=identity_md,
            user_md=user_md,
            model=model,
            capabilities=capabilities,
            proactive=proactive,
        )


_IDENTITY_FIELD_RE = re.compile(r"^\s*-\s*\*\*(?P<key>[^*]+)\*\*\s*:\s*(?P<value>.+?)\s*$")


def _parse_identity(identity_md: str) -> tuple[str, List[str], List[str]]:
    """Parse model + capabilities + proactive from IDENTITY.md.

    Expected bullet format (case-insensitive):
    - **Model:** <value>
    - **Capabilities:** a, b, c
    - **Proactive:** x, y, z

    Returns:
        (model, capabilities, proactive)

    Raises:
        ValueError if required fields cannot be parsed.
    """

    model: Optional[str] = None
    capabilities: Optional[List[str]] = None
    proactive: List[str] = []  # Optional field, defaults to empty

    for line in identity_md.splitlines():
        m = _IDENTITY_FIELD_RE.match(line)
        if not m:
            continue

        k = m.group("key").strip().lower()
        v = m.group("value").strip()

        if k == "model":
            model = v
        elif k == "capabilities":
            # Split on commas. Keep raw tokens (no special normalization beyond strip).
            capabilities = [c.strip() for c in v.split(",") if c.strip()]
        elif k == "proactive":
            # Split on commas. Keep raw tokens (no special normalization beyond strip).
            proactive = [c.strip() for c in v.split(",") if c.strip()]

    if model is None:
        raise ValueError("IDENTITY.md missing required field: Model")
    if capabilities is None:
        raise ValueError("IDENTITY.md missing required field: Capabilities")

    return model, capabilities, proactive


# Convenience singleton for call-sites that don't want to manage an instance.
_default_registry = AgentRegistry()


def get_agent(type: str) -> AgentConfig:
    """Module-level convenience wrapper around the default registry."""

    return _default_registry.get_agent(type)
