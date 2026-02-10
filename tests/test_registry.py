from pathlib import Path

import pytest

from orchestrator.core.registry import AgentRegistry


def test_registry_loads_known_agent():
    repo_root = Path(__file__).resolve().parents[1]
    agents_root = repo_root / "agents"
    registry = AgentRegistry(agents_root=agents_root)

    cfg = registry.get_agent("programmer")

    assert cfg.type == "programmer"

    # Ensure we loaded the actual on-disk content for each required file.
    assert cfg.agents_md == (agents_root / "programmer" / "AGENTS.md").read_text(encoding="utf-8")
    assert cfg.soul_md == (agents_root / "programmer" / "SOUL.md").read_text(encoding="utf-8")
    assert cfg.tools_md == (agents_root / "programmer" / "TOOLS.md").read_text(encoding="utf-8")
    assert cfg.user_md == (agents_root / "programmer" / "USER.md").read_text(encoding="utf-8")
    assert cfg.identity_md == (agents_root / "programmer" / "IDENTITY.md").read_text(encoding="utf-8")

    assert isinstance(cfg.model, str) and cfg.model.strip()
    assert isinstance(cfg.capabilities, list)
    assert "code" in [c.lower() for c in cfg.capabilities]
    assert isinstance(cfg.proactive, list)
    assert "scan_todos" in cfg.proactive


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


def test_registry_parses_proactive_configs():
    """Verify that proactive configs are parsed from IDENTITY.md."""
    repo_root = Path(__file__).resolve().parents[1]
    agents_root = repo_root / "agents"
    registry = AgentRegistry(agents_root=agents_root)

    # Test programmer
    programmer = registry.get_agent("programmer")
    assert isinstance(programmer.proactive, list)
    assert "scan_todos" in programmer.proactive
    assert "fix_warnings" in programmer.proactive
    assert "small_improvements" in programmer.proactive

    # Test researcher
    researcher = registry.get_agent("researcher")
    assert isinstance(researcher.proactive, list)
    assert "monitor_deps" in researcher.proactive
    assert "check_security" in researcher.proactive

    # Test reviewer
    reviewer = registry.get_agent("reviewer")
    assert isinstance(reviewer.proactive, list)
    assert "quality_sweeps" in reviewer.proactive
    assert "coverage_checks" in reviewer.proactive

    # Test writer
    writer = registry.get_agent("writer")
    assert isinstance(writer.proactive, list)
    assert "detect_drift" in writer.proactive
    assert "update_stale" in writer.proactive

    # Test architect
    architect = registry.get_agent("architect")
    assert isinstance(architect.proactive, list)
    assert "scan_complexity" in architect.proactive
    assert "identify_patterns" in architect.proactive


def test_registry_handles_missing_proactive_field(tmp_path: Path):
    """Verify that proactive field is optional and defaults to empty list."""
    agents_root = tmp_path / "agents"
    (agents_root / "testbot").mkdir(parents=True)

    # Create all required files, but IDENTITY has no Proactive field
    (agents_root / "testbot" / "AGENTS.md").write_text("# AGENTS\n", encoding="utf-8")
    (agents_root / "testbot" / "SOUL.md").write_text("# SOUL\n", encoding="utf-8")
    (agents_root / "testbot" / "TOOLS.md").write_text("# TOOLS\n", encoding="utf-8")
    (agents_root / "testbot" / "USER.md").write_text("# USER\n", encoding="utf-8")
    (agents_root / "testbot" / "IDENTITY.md").write_text(
        "# IDENTITY\n\n- **Model:** Standard\n- **Capabilities:** test\n",
        encoding="utf-8",
    )

    registry = AgentRegistry(agents_root=agents_root)
    cfg = registry.get_agent("testbot")

    assert isinstance(cfg.proactive, list)
    assert len(cfg.proactive) == 0  # Empty list when field is missing
