from pathlib import Path

import pytest

from orchestrator.core.registry import AgentRegistry


def test_registry_loads_known_agent():
    repo_root = Path(__file__).resolve().parents[1]
    registry = AgentRegistry(agents_root=repo_root / "agents")

    cfg = registry.get_agent("programmer")

    assert cfg.type == "programmer"
    assert "# IDENTITY" in cfg.identity_md or "# IDENTITY.md" in cfg.identity_md
    assert isinstance(cfg.model, str) and cfg.model.strip()
    assert isinstance(cfg.capabilities, list)
    assert "code" in [c.lower() for c in cfg.capabilities]


def test_registry_caches_after_first_load():
    repo_root = Path(__file__).resolve().parents[1]
    registry = AgentRegistry(agents_root=repo_root / "agents")

    cfg1 = registry.get_agent("programmer")
    cfg2 = registry.get_agent("Programmer")

    assert cfg1 is cfg2


def test_registry_validates_required_files(tmp_path: Path):
    agents_root = tmp_path / "agents"
    (agents_root / "broken").mkdir(parents=True)

    # Only create some required files
    (agents_root / "broken" / "AGENTS.md").write_text("# AGENTS\n", encoding="utf-8")
    (agents_root / "broken" / "IDENTITY.md").write_text(
        "# IDENTITY\n\n- **Model:** X\n- **Capabilities:** a\n",
        encoding="utf-8",
    )

    registry = AgentRegistry(agents_root=agents_root)

    with pytest.raises(FileNotFoundError):
        registry.get_agent("broken")
