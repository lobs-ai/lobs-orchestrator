# Settings Management

## Quick Start

### View current settings
```bash
python3 setup_config.py --show
```

### Reset all settings (for testing onboarding)
```bash
python3 setup_config.py --reset
```

This will delete:
- `.lobs_settings.json` (all orchestrator settings)
- `state/*.json` (orchestrator state files)
- `~/.openclaw/worker-state.json` (worker state)

You'll be prompted to confirm before deletion.

### Update settings
```bash
# General settings
python3 setup_config.py --base-dir /path/to/lobs
python3 setup_config.py --poll-interval 5
python3 setup_config.py --openclaw-executable /usr/local/bin/openclaw

# Ollama settings
python3 setup_config.py --ollama-model codellama
python3 setup_config.py --ollama-keep-alive 1h
python3 setup_config.py --ollama-url http://localhost:11434
```

### Chain multiple updates
```bash
python3 setup_config.py \
  --ollama-model llama3.1 \
  --ollama-keep-alive 30m \
  --poll-interval 5 \
  --show
```

## Available Settings

### General
| Setting | Flag | Default | Description |
|---------|------|---------|-------------|
| `provider` | `--provider` | `local` | Task provider backend |
| `base_dir` | `--base-dir` | `/Users/rafe/other/lobs` | Projects directory |
| `control_repo_path` | `--control-repo` | `{base_dir}/lobs-control` | Control repo path |
| `orchestrator_repo_path` | `--orchestrator-repo` | Current directory | Orchestrator repo path |
| `poll_interval` | `--poll-interval` | `10` | Polling interval (seconds) |
| `openclaw_executable` | `--openclaw-executable` | `openclaw` | OpenClaw command |
| `skip_prereqs` | N/A | `false` | Skip all prerequisite/tool checks |
| `skip_prereqs_list` | N/A | `[]` | List of specific tools to skip checking |

### Ollama
| Setting | Flag | Default | Description |
|---------|------|---------|-------------|
| `ollama_url` | `--ollama-url` | `http://localhost:11434` | Ollama API endpoint |
| `ollama_model` | `--ollama-model` | `llama3.1` | Model to use |
| `ollama_keep_alive` | `--ollama-keep-alive` | `5m` | Model memory retention |

### Prerequisite Checks
| Setting | Flag | Default | Description |
|---------|------|---------|-------------|
| `skip_prereqs` | N/A | `false` | Skip all tool detection checks (force assume available) |
| `skip_prereqs_list` | N/A | `[]` | Skip specific tools (e.g., `["node", "gh", "tsc"]`) |
| `strict_prereqs` | N/A | `false` | Fail startup if required tools are missing (strict mode) |

**Default Behavior (Graceful Degradation):**
By default, the orchestrator continues running even when optional tools are missing. It will:
- Log clear warnings about what's unavailable
- Show installation instructions
- Disable features that require missing tools
- Continue normal operation

**Strict Mode:**
Set `strict_prereqs: true` to fail startup if any required tools are missing. Use this in production environments where you want to ensure all dependencies are present.

**Skip Checks:**
Use `skip_prereqs` or `skip_prereqs_list` only if tool detection is failing incorrectly (e.g., tools are installed but not found). Edit `.lobs_settings.json` directly to configure.

## Settings File

Settings are stored in `.lobs_settings.json` in the project root:

```json
{
  "base_dir": "/Users/rafe/other/lobs",
  "poll_interval": 10,
  "openclaw_executable": "openclaw",
  "ollama_url": "http://localhost:11434",
  "ollama_model": "llama3.1",
  "ollama_keep_alive": "5m"
}
```

You can also edit this file directly, but using `setup_config.py` is recommended.

## Common Configurations

### For Active Development
```bash
python3 setup_config.py \
  --ollama-model llama3.1 \
  --ollama-keep-alive 1h \
  --poll-interval 5
```

### For Production/Always-On
```bash
python3 setup_config.py \
  --ollama-keep-alive -1 \
  --poll-interval 10
```

### For Low-Memory Systems
```bash
python3 setup_config.py \
  --ollama-model llama3.2 \
  --ollama-keep-alive 2m
```

### Using Remote Ollama
```bash
python3 setup_config.py \
  --ollama-url http://192.168.1.100:11434 \
  --ollama-model llama3.1
```

## Examples

### Initial Setup
```bash
# 1. Show current settings
python3 setup_config.py --show

# 2. Configure Ollama
python3 setup_config.py --ollama-model llama3.1

# 3. Verify
python3 setup_config.py --show
```

### Graceful Degradation (Default)

**By default, missing tools don't block orchestrator startup or operation.** The system will:
- Continue running even if optional tools (mypy, tsc, gh, etc.) are missing
- Log helpful warnings with installation instructions
- Automatically disable features that require unavailable tools

**Example messages you might see:**
```
⚠️  Node.js not found - JavaScript/TypeScript features disabled. Install: https://nodejs.org
⚠️  mypy not found - Python type checking disabled. Run: pip install mypy
⚠️  gh not authenticated - GitHub features unavailable. Run: gh auth login
```

The orchestrator continues working; you just won't get certain proactive opportunities or checks.

### Enable Strict Mode

To fail startup if required tools are missing (useful for production):

```bash
# Edit .lobs_settings.json and add:
{
  "strict_prereqs": true,
  ...
}
```

With strict mode enabled, the orchestrator will exit with an error if any required tool is not found.

### Override Tool Detection

**Only needed if tool detection is failing incorrectly** (tools are installed but not found):

**Skip all checks:**
```bash
# Edit .lobs_settings.json and add:
{
  "skip_prereqs": true,
  ...
}
```

**Skip specific tools:**
```bash
# Edit .lobs_settings.json and add:
{
  "skip_prereqs_list": ["node", "gh", "tsc"],
  ...
}
```

The orchestrator will assume these tools are available and not try to find them.

### Change Ollama Model
```bash
# Switch to CodeLlama for code-focused tasks
python3 setup_config.py --ollama-model codellama --show
```

### Optimize for Memory
```bash
# Use smaller model and shorter keep-alive
python3 setup_config.py \
  --ollama-model llama3.2 \
  --ollama-keep-alive 2m \
  --show
```

### Keep Model Loaded Permanently
```bash
# Never unload model (uses ~6GB RAM continuously)
python3 setup_config.py --ollama-keep-alive -1 --show
```

## Ollama Keep-Alive Values

| Value | Behavior | Use Case |
|-------|----------|----------|
| `0` | Unload immediately | Save maximum memory |
| `5m` | Keep 5 minutes (default) | Occasional diagnostics |
| `30m` | Keep 30 minutes | Active development |
| `1h` | Keep 1 hour | Long work sessions |
| `-1` | Keep forever | Production/always-on |

## Programmatic Access

From Python code:

```python
from orchestrator.utils.settings import get_setting, set_setting

# Get setting
model = get_setting("ollama_model", "llama3.1")

# Update setting
set_setting("ollama_keep_alive", "30m")
```

## Troubleshooting

### Settings not taking effect
- Restart the orchestrator after changing settings
- Check `.lobs_settings.json` was updated correctly

### Can't find setup_config.py
```bash
# Run from orchestrator root directory
cd /path/to/lobs-orchestrator
python3 setup_config.py --show
```

### Want to reset to defaults
```bash
# Use the reset command (recommended)
python3 setup_config.py --reset

# Then recreate with defaults
python3 setup_config.py --show
```

## See Also

- [OLLAMA_INTEGRATION.md](OLLAMA_INTEGRATION.md) - Ollama runtime documentation
- [OLLAMA_MODEL_LOADING.md](OLLAMA_MODEL_LOADING.md) - Model loading details
- [OLLAMA_QUICKSTART.md](OLLAMA_QUICKSTART.md) - Quick start guide
