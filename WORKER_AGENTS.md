# Worker Agent Architecture

## Overview

The orchestrator uses **one shared worker** for all work types (tasks, research, inbox responses, diagnostics).
There are no project-specific workers.

Only one worker runs at a time globally. New work is **queued** by leaving it in the provider until the
current worker finishes.

## Architecture

### Agent Structure

```
~/.openclaw/
  agents/
    worker/                  # Agent directory (minimal)
  workspace-worker/          # Agent workspace with files
    AGENTS.md
    SOUL.md
    TOOLS.md
    USER.md
    IDENTITY.md
```

### Template System

Worker template files live in the orchestrator repo:

```
~/lobs-orchestrator/
  worker-template/
    AGENTS.md       # Core worker instructions
    SOUL.md         # Worker personality/vibe
    TOOLS.md        # Tool usage notes
    USER.md         # About Rafe
    IDENTITY.md     # Worker identity
```

When the shared worker is provisioned, these templates are copied to the worker's workspace.

## How It Works

### 1. Provisioning (Automatic with One-Time Restart)

When any task is spawned, the orchestrator:
1. Checks if `worker` exists and is registered
2. If not, creates:
   - `~/.openclaw/agents/worker/`
   - `~/.openclaw/workspace-worker/`
   - Copies template files to workspace
   - **Registers agent in openclaw.json**
   - **Requires gateway restart** (one-time, handled automatically)

### 2. Spawning Workers

```python
# WorkerManager spawns with the shared agent
cmd = [
    "openclaw", "agent",
    "--agent", "worker",
    "-m", prompt,
]
```

### 3. Queueing

Only one worker can run at a time:
- If a worker is active, new work is **not spawned**
- The work remains pending in the provider
- The next loop iteration will pick it up when the worker is free

## Management

### CLI Tool: `bin/manage-workers`

```bash
# List provisioned workers
bin/manage-workers list

# Provision shared worker
bin/manage-workers provision

# Sync updated templates to the worker
bin/manage-workers sync

# Remove the shared worker
bin/manage-workers cleanup
```

### Updating Worker Templates

When you update `worker-template/*.md`:

```bash
# Sync changes to the shared worker
bin/manage-workers sync
```

## Model Configuration

### Gateway Default

The shared worker uses the gateway's default model unless overridden.

### Per-Agent Config (Future)

Add to `~/.openclaw/agents/worker/config.json`:
```json
{
  "model": "anthropic/claude-opus-4-5"
}
```

## Troubleshooting

### Worker won't spawn

Check provisioning:
```bash
bin/manage-workers list
ls -la ~/.openclaw/agents/worker/
ls -la ~/.openclaw/workspace-worker/
```

Re-provision if needed:
```bash
bin/manage-workers provision
```

### Templates not loaded

Verify files exist:
```bash
ls -la ~/.openclaw/workspace-worker/
```

Re-sync if needed:
```bash
bin/manage-workers sync
```

### Session not resetting between tasks

Check logs for reset errors. Reset manually:
```bash
openclaw gateway call sessions.reset --params '{"agentId":"worker"}'
```

## Code References

- **AgentManager:** `orchestrator/core/agents.py`
- **WorkerManager:** `orchestrator/core/worker.py`
- **Prompter:** `orchestrator/services/prompter.py`
- **Templates:** `worker-template/`
- **CLI:** `bin/manage-workers`
