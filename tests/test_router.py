from pathlib import Path

import pytest

from orchestrator.core.registry import AgentRegistry
from orchestrator.core.router import Router


@pytest.fixture()
def registry() -> AgentRegistry:
    repo_root = Path(__file__).resolve().parents[1]
    return AgentRegistry(agents_root=repo_root / "agents")


def test_router_uses_explicit_agent_field(registry: AgentRegistry):
    router = Router(registry=registry)

    task = {"title": "anything", "notes": "", "agent": "writer"}
    assert router.route(task) == "writer"


def test_router_programmer_match_fix_bug(registry: AgentRegistry):
    router = Router(registry=registry)

    task = {"title": "Fix bug in router", "notes": ""}
    assert router.route(task) == "programmer"


def test_router_researcher_match(registry: AgentRegistry):
    router = Router(registry=registry)

    task = {"title": "Investigate why X happens", "notes": ""}
    assert router.route(task) == "researcher"


def test_router_reviewer_match(registry: AgentRegistry):
    router = Router(registry=registry)

    task = {"title": "Audit auth flow", "notes": ""}
    assert router.route(task) == "reviewer"


def test_router_writer_match(registry: AgentRegistry):
    router = Router(registry=registry)

    task = {"title": "Write documentation for Y", "notes": ""}
    assert router.route(task) == "writer"


def test_router_architect_match(registry: AgentRegistry):
    router = Router(registry=registry)

    task = {"title": "Restructure modules", "notes": ""}
    assert router.route(task) == "architect"


def test_router_defaults_to_programmer(registry: AgentRegistry):
    router = Router(registry=registry)

    task = {"title": "Chore: clean up", "notes": "No keywords"}
    assert router.route(task) == "programmer"


def test_router_rejects_unknown_explicit_agent(registry: AgentRegistry):
    router = Router(registry=registry)

    task = {"title": "Anything", "notes": "", "agent": "does-not-exist"}
    with pytest.raises(FileNotFoundError):
        router.route(task)
