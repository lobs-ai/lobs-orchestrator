"""
Agent provisioning manager.

Automatically creates and manages the single shared worker agent.

Also manages openclaw.json registration (requires gateway restart).
"""

import json
import logging
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class AgentManager:
    """
    Manages worker agent provisioning.

    Creates a single shared worker agent:
    - ~/.openclaw/agents/worker/
    - ~/.openclaw/workspace-worker/

    Copies template files from orchestrator's worker-template/ directory.
    """

    def __init__(self, template_dir: Path, openclaw_dir: Optional[Path] = None):
        """
        Initialize agent manager.

        Args:
            template_dir: Path to worker-template directory (in orchestrator repo)
            openclaw_dir: Path to .openclaw directory (defaults to ~/.openclaw)
        """
        self.template_dir = template_dir.resolve()
        self.openclaw_dir = (openclaw_dir or Path.home() / ".openclaw").resolve()
        self.agents_dir = self.openclaw_dir / "agents"
        self.config_file = self.openclaw_dir / "openclaw.json"

        if not self.template_dir.exists():
            raise ValueError(f"Worker template directory not found: {self.template_dir}")

        # Ensure base directories exist
        self.agents_dir.mkdir(parents=True, exist_ok=True)

    def _get_agent_id(self, project_id: str) -> str:
        """Get agent ID from project ID. Use single 'worker' agent."""
        # Always use single shared worker agent
        return "worker"
    
    def worker_exists(self, project_id: str) -> bool:
        """Check if worker agent exists."""
        agent_id = self._get_agent_id(project_id)
        agent_dir = self.agents_dir / agent_id
        workspace_dir = self.openclaw_dir / f"workspace-{agent_id}"
        return agent_dir.exists() and workspace_dir.exists()
    
    def provision_worker(self, project_id: str, force: bool = False, register: bool = True) -> bool:
        """
        Provision a worker agent.

        Creates:
        - ~/.openclaw/agents/worker/
        - ~/.openclaw/workspace-worker/
        - Registers in openclaw.json (if register=True)

        Copies template files to workspace.

        Args:
            project_id: Project identifier (used for compatibility, always creates shared worker)
            force: If True, recreate even if worker exists
            register: If True, register agent in openclaw.json (requires restart)

        Returns:
            True if provisioned (or already exists), False on error
        """
        agent_id = self._get_agent_id(project_id)
        agent_dir = self.agents_dir / agent_id
        workspace_dir = self.openclaw_dir / f"workspace-{agent_id}"
        
        # Check if already exists
        if not force and self.worker_exists(project_id) and (not register or self.is_agent_registered(project_id)):
            logger.debug(f"Worker agent {agent_id} already provisioned")
            return True
        
        try:
            # Create agent directory (minimal, no files needed here)
            agent_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"Created agent directory: {agent_dir}")
            
            # Create workspace directory
            workspace_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"Created workspace directory: {workspace_dir}")
            
            # Copy template files to workspace
            template_files = [
                "AGENTS.md",
                "SOUL.md",
                "TOOLS.md",
                "USER.md",
                "IDENTITY.md",
                "WORKER_RULES.md",
            ]
            
            for filename in template_files:
                src = self.template_dir / filename
                dst = workspace_dir / filename
                
                if src.exists():
                    shutil.copy2(src, dst)
                    logger.debug(f"Copied {filename} to {workspace_dir}")
                else:
                    logger.warning(f"Template file not found: {src}")
            
            logger.info(f"✅ Provisioned worker files: {agent_id}")
            
            # Register in openclaw.json
            if register and not self.register_agent(project_id):
                logger.error(f"Failed to register agent {agent_id} in openclaw.json")
                return False
            
            return True
            
        except Exception as e:
            logger.error(f"Failed to provision worker {agent_id}: {e}")
            return False
    
    def aux_agent_exists(self, agent_id: str) -> bool:
        """Check if a non-worker agent (e.g. suggester) exists."""
        agent_dir = self.agents_dir / agent_id
        workspace_dir = self.openclaw_dir / f"workspace-{agent_id}"
        return agent_dir.exists() and workspace_dir.exists()

    def provision_aux_agent(
        self,
        agent_id: str,
        *,
        name: str,
        model: str,
        emoji: str = "💡",
        force: bool = False,
        register: bool = True,
    ) -> bool:
        """Provision a lightweight auxiliary agent.

        This is used for cheap/fast background analysis (e.g. proactive suggestions).
        """
        agent_dir = self.agents_dir / agent_id
        workspace_dir = self.openclaw_dir / f"workspace-{agent_id}"

        if not force and self.aux_agent_exists(agent_id) and (not register or self.is_agent_registered_by_id(agent_id)):
            return True

        try:
            agent_dir.mkdir(parents=True, exist_ok=True)
            workspace_dir.mkdir(parents=True, exist_ok=True)

            # Minimal workspace context (avoid copying WORKER_RULES.md).
            identity_path = workspace_dir / "IDENTITY.md"
            if not identity_path.exists() or force:
                identity_path.write_text(
                    f"# IDENTITY\n\n- **Name:** {name}\n- **Emoji:** {emoji}\n\n"
                )

            if register:
                if not self.register_agent_by_id(
                    agent_id=agent_id,
                    name=name,
                    workspace=str(workspace_dir),
                    model=model,
                    identity_name=name,
                    identity_emoji=emoji,
                ):
                    return False

            logger.info(f"✅ Provisioned aux agent: {agent_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to provision aux agent {agent_id}: {e}")
            return False

    def sync_worker_templates(self, project_id: str) -> bool:
        """
        Sync template files to the shared worker.

        Useful for updating worker when templates change.

        Args:
            project_id: Project identifier (used for compatibility)

        Returns:
            True if synced successfully
        """
        if not self.worker_exists(project_id):
            logger.warning(f"Worker does not exist, cannot sync")
            return False

        agent_id = self._get_agent_id(project_id)
        workspace_dir = self.openclaw_dir / f"workspace-{agent_id}"
        
        try:
            template_files = [
                "AGENTS.md",
                "SOUL.md",
                "TOOLS.md",
                "USER.md",
                "IDENTITY.md",
                "WORKER_RULES.md",
            ]
            
            for filename in template_files:
                src = self.template_dir / filename
                dst = workspace_dir / filename
                
                if src.exists():
                    shutil.copy2(src, dst)
                    logger.debug(f"Synced {filename} to {workspace_dir}")
            
            logger.info(f"✅ Synced templates for worker: {agent_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to sync templates for {agent_id}: {e}")
            return False
    
    def cleanup_worker(self, project_id: str, unregister: bool = True) -> bool:
        """
        Remove worker agent and workspace.

        Args:
            project_id: Project identifier (used for compatibility)
            unregister: If True, unregister from openclaw.json (requires restart)

        Returns:
            True if removed successfully
        """
        agent_id = self._get_agent_id(project_id)
        agent_dir = self.agents_dir / agent_id
        workspace_dir = self.openclaw_dir / f"workspace-{agent_id}"
        
        try:
            if agent_dir.exists():
                shutil.rmtree(agent_dir)
                logger.info(f"Removed agent directory: {agent_dir}")
            
            if workspace_dir.exists():
                shutil.rmtree(workspace_dir)
                logger.info(f"Removed workspace directory: {workspace_dir}")
            
            # Unregister from openclaw.json
            if unregister and not self.unregister_agent(project_id):
                logger.error(f"Failed to unregister agent {agent_id} from openclaw.json")
                return False
            
            logger.info(f"✅ Cleaned up worker: {agent_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to cleanup worker {agent_id}: {e}")
            return False
    
    def list_workers(self) -> list[str]:
        """
        List all provisioned worker agents.

        Returns:
            List with "worker" if the shared worker exists
        """
        if self.worker_exists("worker"):
            return ["worker"]
        return []
    
    def _read_config(self) -> dict:
        """Read openclaw.json config."""
        if not self.config_file.exists():
            raise FileNotFoundError(f"OpenClaw config not found: {self.config_file}")
        
        with open(self.config_file, "r") as f:
            return json.load(f)
    
    def _write_config(self, config: dict):
        """Write openclaw.json config."""
        with open(self.config_file, "w") as f:
            json.dump(config, f, indent=2)
        logger.info(f"Updated OpenClaw config: {self.config_file}")
    
    def is_agent_registered(self, project_id: str) -> bool:
        """Check if worker agent is registered in openclaw.json."""
        return self.is_agent_registered_by_id(self._get_agent_id(project_id))

    def is_agent_registered_by_id(self, agent_id: str) -> bool:
        """Check if an agent is registered in openclaw.json."""
        try:
            config = self._read_config()
            agents_list = config.get("agents", {}).get("list", [])
            return any(agent.get("id") == agent_id for agent in agents_list)
        except Exception as e:
            logger.error(f"Failed to check agent registration: {e}")
            return False

    def register_agent(self, project_id: str, model: Optional[str] = None) -> bool:
        """Register the shared worker agent in openclaw.json."""
        return self.register_agent_by_id(
            agent_id=self._get_agent_id(project_id),
            name="Lobs Worker",
            workspace=str(self.openclaw_dir / f"workspace-{self._get_agent_id(project_id)}"),
            model=model or "anthropic/claude-sonnet-4-5",
            identity_name="Lobs Worker",
            identity_emoji="🔧",
        )

    def register_agent_by_id(
        self,
        agent_id: str,
        name: str,
        workspace: str,
        model: str,
        identity_name: Optional[str] = None,
        identity_emoji: Optional[str] = None,
    ) -> bool:
        """Register an agent in openclaw.json."""
        try:
            config = self._read_config()

            # Ensure agents.list exists
            if "agents" not in config:
                config["agents"] = {}
            if "list" not in config["agents"]:
                config["agents"]["list"] = []

            agents_list = config["agents"]["list"]

            # Check if already registered
            if any(agent.get("id") == agent_id for agent in agents_list):
                logger.debug(f"Agent {agent_id} already registered")
                return True

            new_agent: dict = {
                "id": agent_id,
                "name": name,
                "workspace": workspace,
                "model": model,
            }

            if identity_name or identity_emoji:
                new_agent["identity"] = {
                    "name": identity_name or name,
                    "emoji": identity_emoji or "",
                }

            agents_list.append(new_agent)
            self._write_config(config)

            logger.info(f"✅ Registered agent in openclaw.json: {agent_id}")
            logger.warning("⚠️  Gateway restart required for agent to be available")
            return True

        except Exception as e:
            logger.error(f"Failed to register agent {agent_id}: {e}")
            return False
    
    def unregister_agent(self, project_id: str) -> bool:
        """
        Unregister worker agent from openclaw.json.

        Args:
            project_id: Project identifier (used for compatibility)

        Returns:
            True if unregistered successfully
        """
        agent_id = self._get_agent_id(project_id)

        try:
            config = self._read_config()
            agents_list = config.get("agents", {}).get("list", [])

            # Remove agent
            original_len = len(agents_list)
            agents_list[:] = [a for a in agents_list if a.get("id") != agent_id]

            if len(agents_list) == original_len:
                logger.debug(f"Agent {agent_id} not found in config")
                return True

            self._write_config(config)

            logger.info(f"✅ Unregistered agent from openclaw.json: {agent_id}")
            logger.warning("⚠️  Gateway restart required for change to take effect")
            return True

        except Exception as e:
            logger.error(f"Failed to unregister agent {agent_id}: {e}")
            return False
    
    def restart_gateway(self) -> bool:
        """
        Restart OpenClaw gateway to load new agent registrations.
        
        Returns:
            True if restart initiated successfully
        """
        import subprocess
        from orchestrator.utils.settings import get_setting
        
        executable = get_setting("openclaw_executable", "openclaw")
        
        try:
            logger.info("Restarting OpenClaw gateway...")
            subprocess.run(
                [executable, "gateway", "restart"],
                check=True,
                capture_output=True,
                timeout=30
            )
            logger.info("✅ Gateway restart initiated")
            return True
        except Exception as e:
            logger.error(f"Failed to restart gateway: {e}")
            return False
