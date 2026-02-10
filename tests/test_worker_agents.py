from __future__ import annotations

from pathlib import Path

from orchestrator.core.agents import AgentManager


def _write_template_files(template_dir: Path) -> None:
    template_dir.mkdir(parents=True, exist_ok=True)
    (template_dir / "AGENTS.md").write_text("AGENTS", encoding="utf-8")
    (template_dir / "SOUL.md").write_text("SOUL", encoding="utf-8")
    (template_dir / "TOOLS.md").write_text("TOOLS", encoding="utf-8")
    (template_dir / "USER.md").write_text("USER", encoding="utf-8")
    (template_dir / "IDENTITY.md").write_text("IDENTITY", encoding="utf-8")
    (template_dir / "WORKER_RULES.md").write_text("RULES", encoding="utf-8")


def test_provision_worker_creates_workspace_and_copies_templates(tmp_path: Path):
    template_dir = tmp_path / "worker-template"
    openclaw_dir = tmp_path / ".openclaw"
    _write_template_files(template_dir)

    mgr = AgentManager(template_dir=template_dir, openclaw_dir=openclaw_dir)

    ok = mgr.provision_worker("any-project", register=False)
    assert ok is True

    agent_dir = openclaw_dir / "agents" / "worker"
    workspace_dir = openclaw_dir / "workspace-worker"

    assert agent_dir.exists() and agent_dir.is_dir()
    assert workspace_dir.exists() and workspace_dir.is_dir()

    # Template files should be copied into the workspace.
    assert (workspace_dir / "AGENTS.md").read_text(encoding="utf-8") == "AGENTS"
    assert (workspace_dir / "SOUL.md").read_text(encoding="utf-8") == "SOUL"
    assert (workspace_dir / "TOOLS.md").read_text(encoding="utf-8") == "TOOLS"
    assert (workspace_dir / "USER.md").read_text(encoding="utf-8") == "USER"
    assert (workspace_dir / "IDENTITY.md").read_text(encoding="utf-8") == "IDENTITY"
    assert (workspace_dir / "WORKER_RULES.md").read_text(encoding="utf-8") == "RULES"


def test_provision_aux_agent_creates_minimal_workspace(tmp_path: Path):
    template_dir = tmp_path / "worker-template"
    openclaw_dir = tmp_path / ".openclaw"

    # AgentManager requires template_dir to exist, but aux provisioning should not
    # depend on any particular files existing in it.
    template_dir.mkdir(parents=True)

    mgr = AgentManager(template_dir=template_dir, openclaw_dir=openclaw_dir)

    ok = mgr.provision_aux_agent(
        "suggester",
        name="Suggester",
        model="anthropic/claude-haiku-4-5",
        emoji=":)" ,
        register=False,
    )
    assert ok is True

    agent_dir = openclaw_dir / "agents" / "suggester"
    workspace_dir = openclaw_dir / "workspace-suggester"

    assert agent_dir.exists() and agent_dir.is_dir()
    assert workspace_dir.exists() and workspace_dir.is_dir()

    # Aux agent should only have IDENTITY.md by default (no worker rules / full context).
    identity = (workspace_dir / "IDENTITY.md").read_text(encoding="utf-8")
    assert "Suggester" in identity
    assert ":)" in identity
    assert not (workspace_dir / "WORKER_RULES.md").exists()
    assert not (workspace_dir / "AGENTS.md").exists()
    assert not (workspace_dir / "SOUL.md").exists()
    assert not (workspace_dir / "TOOLS.md").exists()
    assert not (workspace_dir / "USER.md").exists()
