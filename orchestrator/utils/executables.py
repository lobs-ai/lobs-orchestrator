"""Robust executable detection with fallback to common installation paths.

Handles cases where executables (especially node-based tools) may not be in 
the service's PATH but are installed in standard locations.
"""

import shutil
from pathlib import Path
from typing import Optional


# Common installation paths for node/npm and related tools
NODE_SEARCH_PATHS = [
    "/usr/bin",
    "/usr/local/bin",
    "/opt/homebrew/bin",  # macOS Homebrew on Apple Silicon
    "/home/linuxbrew/.linuxbrew/bin",  # Linux Homebrew
    Path.home() / ".local/bin",
    Path.home() / ".npm-global/bin",
    Path.home() / "bin",
    Path.home() / ".nvm/current/bin",
    Path.home() / ".nvm/versions/node",  # Will search subdirs
]


def _search_nvm_versions(executable: str) -> Optional[Path]:
    """Search for executable in all nvm node version directories."""
    nvm_versions = Path.home() / ".nvm/versions/node"
    if not nvm_versions.exists():
        return None
    
    # Look for most recent version (reverse sort)
    for version_dir in sorted(nvm_versions.iterdir(), reverse=True):
        if version_dir.is_dir():
            bin_dir = version_dir / "bin"
            exe_path = bin_dir / executable
            if exe_path.exists() and exe_path.is_file():
                return exe_path
    
    return None


def which(executable: str, extra_paths: Optional[list] = None) -> Optional[str]:
    """Find executable in PATH or common installation locations.
    
    Args:
        executable: Name of executable to find (e.g., 'node', 'npm', 'tsc')
        extra_paths: Additional paths to search
    
    Returns:
        Absolute path to executable if found, None otherwise
    """
    # First try standard shutil.which (respects PATH)
    found = shutil.which(executable)
    if found:
        return found
    
    # Build search paths
    search_paths = list(NODE_SEARCH_PATHS)
    if extra_paths:
        search_paths.extend(extra_paths)
    
    # Search common locations
    for search_path in search_paths:
        if isinstance(search_path, str):
            search_path = Path(search_path)
        
        # Handle ~ expansion
        search_path = search_path.expanduser()
        
        if not search_path.exists():
            continue
        
        exe_path = search_path / executable
        if exe_path.exists() and exe_path.is_file():
            # Check if executable bit is set
            try:
                if exe_path.stat().st_mode & 0o111:  # Any execute bit
                    return str(exe_path)
            except Exception:
                continue
    
    # Special handling for nvm versions
    if executable in ('node', 'npm', 'npx'):
        nvm_path = _search_nvm_versions(executable)
        if nvm_path:
            return str(nvm_path)
    
    return None


def get_node_version() -> Optional[str]:
    """Get installed node version, if available.
    
    Returns:
        Version string (e.g., 'v22.22.0') or None if node not found
    """
    import subprocess
    
    node_path = which('node')
    if not node_path:
        return None
    
    try:
        result = subprocess.run(
            [node_path, '--version'],
            capture_output=True,
            text=True,
            timeout=5,
            check=False
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    
    return None


def check_node_available() -> bool:
    """Check if node is available and can be executed.
    
    Returns:
        True if node is found and working, False otherwise
    """
    return get_node_version() is not None
