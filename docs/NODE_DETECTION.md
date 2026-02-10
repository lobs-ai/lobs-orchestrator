# Node.js Detection

## Overview

The orchestrator includes robust Node.js detection that works even when the service's `PATH` environment variable doesn't include node's installation directory.

## How It Works

The `orchestrator/utils/executables.py` module provides a `which()` function that:

1. **First tries standard PATH lookup** using Python's `shutil.which()`
2. **Falls back to common installation locations** if not found in PATH:
   - `/usr/bin` (standard Linux package manager installs)
   - `/usr/local/bin` (manual installs)
   - `/opt/homebrew/bin` (macOS Homebrew on Apple Silicon)
   - `~/.local/bin` (user-local installs)
   - `~/.npm-global/bin` (npm global installs)
   - `~/.nvm/versions/node/*` (nvm-managed Node.js versions)

3. **Special handling for nvm** - searches all installed node versions and uses the most recent

## Usage

### Checking for executables

```python
from orchestrator.utils.executables import which

# Find node
node_path = which('node')
if node_path:
    print(f"Node found at: {node_path}")

# Find npm
npm_path = which('npm')
if npm_path:
    print(f"npm found at: {npm_path}")
```

### Checking node availability

```python
from orchestrator.utils.executables import check_node_available, get_node_version

if check_node_available():
    version = get_node_version()
    print(f"Node.js is available: {version}")
else:
    print("Node.js not found")
```

## Service Configuration

The systemd service file includes a `PATH` environment variable:

```ini
Environment="PATH=/home/rafe/.local/bin:/home/rafe/.npm-global/bin:/home/rafe/bin:/usr/local/bin:/usr/bin:/bin:/snap/bin"
```

However, even if node is installed in a location not covered by this PATH, the robust detection will still find it in common locations.

## Startup Diagnostics

When the orchestrator starts, it logs node detection status:

```
Node.js detected: v22.22.0 at /usr/bin/node
```

Or if not found:

```
Node.js not detected - some features may not work
Check if node is in PATH or installed in standard locations
```

You can also run `python setup_config.py --show` to see the current node detection status.

## Troubleshooting

### Node not detected

If node is installed but not detected:

1. Check that node is executable: `chmod +x /path/to/node`
2. Verify node location is in one of the searched paths
3. Check systemd service logs: `journalctl -u lobs-orchestrator -n 50`
4. Add custom paths using the `extra_paths` parameter:

```python
from orchestrator.utils.executables import which

node_path = which('node', extra_paths=['/custom/path/to/node/bin'])
```

### Multiple node versions

If you have multiple node versions (e.g., via nvm):

- The detection will prefer the version in PATH
- If not in PATH, it will use the most recent nvm version
- To use a specific version, ensure it's in the service's PATH or use nvm's `default` alias
