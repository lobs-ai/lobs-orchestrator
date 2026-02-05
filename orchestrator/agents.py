from dataclasses import dataclass
from typing import Dict


@dataclass
class AgentConfig:
    name: str
    model: str
    description: str


AGENTS: Dict[str, AgentConfig] = {
    "task-runner": AgentConfig(
        name="task-runner",
        model="claude-3-5-sonnet",
        description="Implement tasks, fix bugs, and write code.",
    ),
    "diagnostic": AgentConfig(
        name="diagnostic",
        model="claude-3-5-sonnet",
        description="Analyze failures and provide root-cause analysis.",
    ),
    "design-review": AgentConfig(
        name="design-review",
        model="claude-3-opus",
        description="Provide UX and architecture suggestions.",
    ),
    "overview-review": AgentConfig(
        name="overview-review",
        model="claude-3-opus",
        description="Periodic insight pass and high-level system review.",
    ),
    "light-check": AgentConfig(
        name="light-check",
        model="claude-3-haiku",
        description="Cheap classification and routing.",
    ),
}
