from pathlib import Path

from orchestrator.core.registry import AgentConfig
from orchestrator.core.worker import _normalize_agent_template_type, _write_agent_template_files


def test_normalize_agent_template_type_legacy_values_map_to_programmer():
    assert _normalize_agent_template_type("") == "programmer"
    assert _normalize_agent_template_type("worker") == "programmer"
    assert _normalize_agent_template_type("task-runner") == "programmer"
    assert _normalize_agent_template_type("worker-template") == "programmer"


def test_normalize_agent_template_type_preserves_known_types():
    assert _normalize_agent_template_type("Programmer") == "programmer"
    assert _normalize_agent_template_type("researcher") == "researcher"


def test_write_agent_template_files(tmp_path: Path):
    cfg = AgentConfig(
        type="programmer",
        agents_md="A",
        soul_md="S",
        tools_md="T",
        identity_md="I",
        user_md="U",
        model="m",
        capabilities=["code"],
    )

    _write_agent_template_files(tmp_path, cfg)

    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == "A"
    assert (tmp_path / "SOUL.md").read_text(encoding="utf-8") == "S"
    assert (tmp_path / "TOOLS.md").read_text(encoding="utf-8") == "T"
    assert (tmp_path / "IDENTITY.md").read_text(encoding="utf-8") == "I"
    assert (tmp_path / "USER.md").read_text(encoding="utf-8") == "U"
