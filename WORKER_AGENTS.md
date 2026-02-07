# Worker Agent Architecture

## Overview

The orchestrator now uses **project-specific worker agents** for complete session isolation and automatic agent file loading.

Each project gets its own dedicated worker agent:
- `worker-flock` for flock-master project
- `worker-prairie` for prairie-learn-builder project
- etc.

## Architecture

### Agent Structure

```
~/.openclaw/
  agents/
    worker-flock/              # Agent directory (minimal)
    worker-prairie/
  workspace-worker-flock/      # Agent workspace with files
    AGENTS.md
    SOUL.md
    TOOLS.md
    USER.md
    IDENTITY.md
  workspace-worker-prairie/
    AGENTS.md
    ...
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

When a worker is provisioned, these templates are copied to the worker's workspace.

## How It Works

### 1. Provisioning (Automatic with One-Time Restart)

When a task is spawned for a project, the orchestrator:
1. Checks if `worker-<project-id>` exists and is registered
2. If not, creates:
   - `~/.openclaw/agents/worker-<project-id>/`
   - `~/.openclaw/workspace-worker-<project-id>/`
   - Copies template files to workspace
   - **Registers agent in openclaw.json**
   - **Requires gateway restart** (one-time, handled automatically)

**openclaw.json is automatically updated** to register the new agent.

**Gateway restart required** but happens automatically during first provision.

### 2. Spawning Workers

```python
# WorkerManager spawns with project-specific agent
cmd = [
    "openclaw", "agent",
    "--agent", "worker-flock",  # Project-specific agent
    "-m", prompt,               # Task prompt
]
```

Each worker:
- Runs in its own agent session (isolated by agent ID)
- Auto-loads AGENTS.md, SOUL.md, etc. from workspace
- Can have custom model configured per agent

### 3. Session Management

- **Isolation:** Each agent has its own session namespace
- **Parallel:** `worker-flock` and `worker-prairie` can run simultaneously
- **Cleanup:** After task completion, session is reset (not deleted) via `sessions.reset`
- **Reuse:** Same worker agent handles all tasks for its project

## Benefits

✅ **True isolation** - Workers cannot interfere with each other  
✅ **No shared state** - Each agent has its own session  
✅ **Auto-loaded files** - Agent files automatically included (no prompt embedding)  
✅ **Parallel execution** - Multiple workers on different projects run concurrently  
✅ **Per-project config** - Can set different models per worker agent  
✅ **No restarts** - Workers provision on-demand  
✅ **Simple management** - Templates in one place, copied to workers

## Management

### CLI Tool: `bin/manage-workers`

```bash
# List all provisioned workers
bin/manage-workers list

# Provision worker for a project
bin/manage-workers provision flock

# Sync updated templates to a worker
bin/manage-workers sync flock

# Sync templates to all workers
bin/manage-workers sync-all

# Remove a worker
bin/manage-workers cleanup flock
```

### Updating Worker Templates

When you update `worker-template/*.md`:

```bash
# Sync changes to all existing workers
bin/manage-workers sync-all
```

New workers automatically get the latest templates.

### Custom Per-Project Workers

To customize a specific worker (e.g., different instructions for flock):

1. Edit `~/.openclaw/workspace-worker-flock/AGENTS.md`
2. Changes apply only to that worker
3. Template syncs won't overwrite (or will, so be careful)

**Recommendation:** Keep workers uniform via templates. If you need custom behavior, add it to the task prompt instead.

## Model Configuration

### Option 1: Per-Agent Config (Future)

Add to `~/.openclaw/agents/worker-flock/config.json`:
```json
{
  "model": "anthropic/claude-opus-4-5"
}
```

### Option 2: Gateway Default

All workers use the gateway's default model unless overridden.

### Option 3: Task-Level Override (Future)

Add `model` field to tasks in lobs-control, orchestrator passes it somehow.

## Migration

**Automatic.** Next time orchestrator spawns a worker:
- It provisions the worker agent (if doesn't exist)
- Spawns with `--agent worker-<project-id>`
- No manual steps needed

**Old shared worker sessions** will naturally stop being used.

## Troubleshooting

### Worker won't spawn

Check provisioning:
```bash
bin/manage-workers list
ls -la ~/.openclaw/agents/worker-*
ls -la ~/.openclaw/workspace-worker-*/
```

### Templates not loaded

Verify files exist:
```bash
ls -la ~/.openclaw/workspace-worker-<project-id>/
```

Re-provision if needed:
```bash
bin/manage-workers provision <project-id>
```

### Workers interfering with each other

Should not happen. Verify they use different agent IDs:
```bash
ps aux | grep "openclaw agent"
```

Each should have unique `--agent worker-<project-id>`.

### Session not resetting between tasks

Check logs for reset errors. Reset manually:
```bash
openclaw gateway call sessions.reset --params '{"agentId":"worker-<project-id>"}'
```

## Code References

- **AgentManager:** `orchestrator/core/agents.py`
- **WorkerManager:** `orchestrator/core/worker.py`
- **Prompter:** `orchestrator/services/prompter.py`
- **Templates:** `worker-template/`
- **CLI:** `bin/manage-workers`

## Future Enhancements

- [ ] Per-agent model configuration
- [ ] Worker agent metrics/monitoring
- [ ] Template versioning
- [ ] Agent-specific logging
- [ ] Worker health checks
