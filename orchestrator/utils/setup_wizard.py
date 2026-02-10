"""Interactive setup wizard and state tracking for Lobs Orchestrator."""

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from orchestrator.utils.settings import get_setting, set_setting

logger = logging.getLogger(__name__)

SETUP_STATE_FILE = Path(".lobs_setup_state.json")


class SetupState:
    """Track which setup steps have been completed."""

    def __init__(self):
        self.state = self._load_state()

    def _load_state(self) -> dict[str, Any]:
        if not SETUP_STATE_FILE.exists():
            return {
                "version": 1,
                "completed_steps": [],
                "skipped_steps": [],
            }
        try:
            with open(SETUP_STATE_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load setup state: {e}")
            return {
                "version": 1,
                "completed_steps": [],
                "skipped_steps": [],
            }

    def _save_state(self) -> None:
        with open(SETUP_STATE_FILE, "w") as f:
            json.dump(self.state, f, indent=2)

    def is_completed(self, step: str) -> bool:
        return step in self.state.get("completed_steps", [])

    def is_skipped(self, step: str) -> bool:
        return step in self.state.get("skipped_steps", [])

    def mark_completed(self, step: str) -> None:
        completed = self.state.get("completed_steps", [])
        if step not in completed:
            completed.append(step)
            self.state["completed_steps"] = completed
        # Remove from skipped if it was there
        skipped = self.state.get("skipped_steps", [])
        if step in skipped:
            skipped.remove(step)
            self.state["skipped_steps"] = skipped
        self._save_state()

    def mark_skipped(self, step: str) -> None:
        skipped = self.state.get("skipped_steps", [])
        if step not in skipped:
            skipped.append(step)
            self.state["skipped_steps"] = skipped
        self._save_state()

    def reset(self) -> None:
        self.state = {
            "version": 1,
            "completed_steps": [],
            "skipped_steps": [],
        }
        self._save_state()


def validate_base_dir(path: str) -> tuple[bool, str]:
    """Validate base directory exists and is accessible."""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        return False, f"Directory does not exist: {p}"
    if not p.is_dir():
        return False, f"Path is not a directory: {p}"
    if not os.access(p, os.R_OK | os.W_OK):
        return False, f"Directory is not readable/writable: {p}"
    return True, str(p)


def validate_git_repo(path: str) -> tuple[bool, str]:
    """Validate path is a git repository."""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        return False, f"Directory does not exist: {p}"
    if not p.is_dir():
        return False, f"Path is not a directory: {p}"
    git_dir = p / ".git"
    if not git_dir.exists():
        return False, f"Not a git repository (no .git directory): {p}"
    return True, str(p)


def validate_openclaw_executable(path: str) -> tuple[bool, str]:
    """Validate OpenClaw executable exists and is executable."""
    if path == "openclaw":
        # Check if it's in PATH
        try:
            result = subprocess.run(
                ["which", "openclaw"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return True, result.stdout.strip()
        except Exception:
            pass
        return False, "openclaw not found in PATH"
    
    p = Path(path).expanduser().resolve()
    if not p.exists():
        return False, f"File does not exist: {p}"
    if not os.access(p, os.X_OK):
        return False, f"File is not executable: {p}"
    return True, str(p)


def check_step_status(step: str) -> tuple[str, str]:
    """
    Check the status of a setup step.
    
    Returns:
        (status, message) where status is one of: "complete", "incomplete", "skipped", "invalid"
    """
    state = SetupState()
    
    if state.is_completed(step):
        return "complete", "✅"
    if state.is_skipped(step):
        return "skipped", "⏭️  (skipped)"
    
    # Check if the setting exists and is valid
    if step == "base_dir":
        value = get_setting("base_dir")
        if not value:
            return "incomplete", "❌ Not configured"
        valid, _ = validate_base_dir(value)
        if valid:
            # Auto-mark as complete if valid
            state.mark_completed(step)
            return "complete", "✅"
        return "invalid", f"⚠️  Invalid: {value}"
    
    elif step == "control_repo":
        value = get_setting("control_repo_path")
        if not value:
            return "incomplete", "❌ Not configured"
        valid, _ = validate_git_repo(value)
        if valid:
            state.mark_completed(step)
            return "complete", "✅"
        return "invalid", f"⚠️  Invalid: {value}"
    
    elif step == "orchestrator_repo":
        value = get_setting("orchestrator_repo_path")
        if not value:
            return "incomplete", "❌ Not configured"
        valid, _ = validate_git_repo(value)
        if valid:
            state.mark_completed(step)
            return "complete", "✅"
        return "invalid", f"⚠️  Invalid: {value}"
    
    elif step == "openclaw_executable":
        value = get_setting("openclaw_executable", "openclaw")
        valid, _ = validate_openclaw_executable(value)
        if valid:
            state.mark_completed(step)
            return "complete", "✅"
        return "invalid", f"⚠️  Invalid: {value}"
    
    elif step == "worker_provisioned":
        # Check if worker has been provisioned
        worker_state = Path.home() / ".openclaw" / "worker-state.json"
        if worker_state.exists():
            state.mark_completed(step)
            return "complete", "✅"
        return "incomplete", "❌ Not provisioned"
    
    return "incomplete", "❌ Not configured"


def show_setup_status() -> None:
    """Display current setup status."""
    print("\n" + "=" * 60)
    print("Lobs Orchestrator Setup Status")
    print("=" * 60 + "\n")
    
    steps = [
        ("base_dir", "Base Directory", get_setting("base_dir", "Not set")),
        ("control_repo", "Control Repository", get_setting("control_repo_path", "Not set")),
        ("orchestrator_repo", "Orchestrator Repository", get_setting("orchestrator_repo_path", "Not set")),
        ("openclaw_executable", "OpenClaw Executable", get_setting("openclaw_executable", "openclaw")),
        ("worker_provisioned", "Worker Provisioned", "Check worker-state.json"),
    ]
    
    all_complete = True
    
    for step_id, step_name, current_value in steps:
        status, message = check_step_status(step_id)
        if status != "complete":
            all_complete = False
        
        print(f"{step_name:25} {message}")
        if status in ("complete", "invalid"):
            print(f"  → {current_value}")
        print()
    
    print("=" * 60)
    if all_complete:
        print("✅ All setup steps are complete!\n")
        print("You can now run the orchestrator with: python3 main.py\n")
    else:
        # Check if any steps are skipped vs incomplete/invalid
        has_skipped = any(check_step_status(step_id)[0] == "skipped" for step_id, _, _ in steps)
        has_incomplete = any(check_step_status(step_id)[0] in ("incomplete", "invalid") for step_id, _, _ in steps)
        
        if has_skipped and not has_incomplete:
            print("ℹ️  Some setup steps were skipped.\n")
            print("You can run the orchestrator with: python3 main.py")
            print("(It will work with limited functionality until repos are configured)\n")
        else:
            print("⚠️  Some setup steps are incomplete or invalid.\n")
        
        print("To complete or reconfigure steps:")
        print("  python3 main.py --setup-wizard      (run full wizard)")
        print("  python3 main.py --configure base_dir")
        print("  python3 main.py --configure control_repo")
        print("  python3 main.py --configure orchestrator_repo")
        print("  python3 main.py --configure openclaw_executable")
        print()


def run_interactive_step(step: str) -> bool:
    """
    Run an interactive setup step.
    
    Returns:
        True if step was completed, False if skipped
    """
    state = SetupState()
    
    print(f"\n{'=' * 60}")
    
    if step == "base_dir":
        print("Step 1: Base Directory")
        print("-" * 60)
        print("This is the parent directory where project repositories are located.")
        print("Example: /home/user/projects")
        print()
        print("ℹ️  Note: You can skip this step and configure it later.")
        print()
        
        current = get_setting("base_dir")
        if current:
            print(f"Current value: {current}")
            use_current = input("Use this value? [Y/n]: ").strip().lower()
            if use_current in ("", "y", "yes"):
                valid, msg = validate_base_dir(current)
                if valid:
                    state.mark_completed(step)
                    print(f"✅ Using: {msg}")
                    return True
                else:
                    print(f"⚠️  Invalid: {msg}")
        
        while True:
            path = input("Enter base directory path (or 'skip'/'later' to skip): ").strip()
            if path.lower() in ("skip", "later"):
                state.mark_skipped(step)
                print("⏭️  Skipped base directory setup")
                print("   You can configure this later with: python3 main.py --configure base_dir")
                return False
            
            valid, msg = validate_base_dir(path)
            if valid:
                set_setting("base_dir", msg)
                state.mark_completed(step)
                print(f"✅ Base directory set to: {msg}")
                return True
            else:
                print(f"❌ {msg}")
                print("Please try again.")
    
    elif step == "control_repo":
        print("Step 2: Control Repository")
        print("-" * 60)
        print("Path to the lobs-control repository (must be a git repository).")
        print()
        print("ℹ️  Note: You can skip this step and add repos later.")
        print("   The orchestrator will run with limited functionality until repos are configured.")
        print()
        
        current = get_setting("control_repo_path")
        if current:
            print(f"Current value: {current}")
            use_current = input("Use this value? [Y/n]: ").strip().lower()
            if use_current in ("", "y", "yes"):
                valid, msg = validate_git_repo(current)
                if valid:
                    state.mark_completed(step)
                    print(f"✅ Using: {msg}")
                    return True
                else:
                    print(f"⚠️  Invalid: {msg}")
        
        while True:
            path = input("Enter control repo path (or 'skip'/'later' to skip): ").strip()
            if path.lower() in ("skip", "later"):
                state.mark_skipped(step)
                print("⏭️  Skipped control repo setup")
                print("   You can configure this later with: python3 main.py --configure control_repo")
                return False
            
            valid, msg = validate_git_repo(path)
            if valid:
                set_setting("control_repo_path", msg)
                state.mark_completed(step)
                print(f"✅ Control repo set to: {msg}")
                return True
            else:
                print(f"❌ {msg}")
                print("Please try again.")
    
    elif step == "orchestrator_repo":
        print("Step 3: Orchestrator Repository")
        print("-" * 60)
        print("Path to this repository (lobs-orchestrator).")
        print()
        print("ℹ️  Note: You can skip this step and configure it later if needed.")
        print()
        
        # Default to current directory
        default_path = str(Path.cwd().resolve())
        current = get_setting("orchestrator_repo_path", default_path)
        
        print(f"Detected path: {current}")
        use_current = input("Use this value? [Y/n]: ").strip().lower()
        if use_current in ("", "y", "yes"):
            valid, msg = validate_git_repo(current)
            if valid:
                set_setting("orchestrator_repo_path", msg)
                state.mark_completed(step)
                print(f"✅ Using: {msg}")
                return True
            else:
                print(f"⚠️  Invalid: {msg}")
        
        while True:
            path = input("Enter orchestrator repo path (or 'skip'/'later' to skip): ").strip()
            if path.lower() in ("skip", "later"):
                state.mark_skipped(step)
                print("⏭️  Skipped orchestrator repo setup")
                print("   You can configure this later with: python3 main.py --configure orchestrator_repo")
                return False
            
            valid, msg = validate_git_repo(path)
            if valid:
                set_setting("orchestrator_repo_path", msg)
                state.mark_completed(step)
                print(f"✅ Orchestrator repo set to: {msg}")
                return True
            else:
                print(f"❌ {msg}")
                print("Please try again.")
    
    elif step == "openclaw_executable":
        print("Step 4: OpenClaw Executable")
        print("-" * 60)
        print("Path to the openclaw executable (or 'openclaw' if in PATH).")
        print()
        print("ℹ️  Note: You can skip this step and configure it later.")
        print()
        
        current = get_setting("openclaw_executable", "openclaw")
        print(f"Current value: {current}")
        
        valid, msg = validate_openclaw_executable(current)
        if valid:
            use_current = input("Use this value? [Y/n]: ").strip().lower()
            if use_current in ("", "y", "yes"):
                state.mark_completed(step)
                print(f"✅ Using: {msg}")
                return True
        else:
            print(f"⚠️  Not found: {msg}")
        
        while True:
            path = input("Enter openclaw executable path (or 'skip'/'later' to skip): ").strip()
            if path.lower() in ("skip", "later"):
                state.mark_skipped(step)
                print("⏭️  Skipped openclaw executable setup")
                print("   You can configure this later with: python3 main.py --configure openclaw_executable")
                return False
            
            valid, msg = validate_openclaw_executable(path)
            if valid:
                set_setting("openclaw_executable", path)
                state.mark_completed(step)
                print(f"✅ OpenClaw executable set to: {msg}")
                return True
            else:
                print(f"❌ {msg}")
                print("Please try again.")
    
    return False


def run_setup_wizard() -> None:
    """Run the interactive setup wizard."""
    print("\n" + "=" * 60)
    print("Lobs Orchestrator Setup Wizard")
    print("=" * 60)
    print("\nWelcome! This wizard will guide you through setting up the orchestrator.")
    print("You can skip any step and complete it later.")
    print()
    print("ℹ️  The orchestrator will work with limited functionality if you skip")
    print("   repository setup - it just won't have any projects to work on until")
    print("   repos are configured.\n")
    
    input("Press Enter to begin...")
    
    steps = [
        "base_dir",
        "control_repo",
        "orchestrator_repo",
        "openclaw_executable",
    ]
    
    for step in steps:
        # Skip if already complete
        status, _ = check_step_status(step)
        if status == "complete":
            print(f"\n✅ {step} is already configured. Skipping.")
            continue
        
        run_interactive_step(step)
    
    print("\n" + "=" * 60)
    print("Setup Wizard Complete")
    print("=" * 60 + "\n")
    
    show_setup_status()


def configure_step(step: str) -> None:
    """Configure a specific setup step."""
    valid_steps = [
        "base_dir",
        "control_repo",
        "orchestrator_repo",
        "openclaw_executable",
    ]
    
    if step not in valid_steps:
        print(f"❌ Invalid step: {step}")
        print(f"Valid steps: {', '.join(valid_steps)}")
        sys.exit(1)
    
    run_interactive_step(step)
