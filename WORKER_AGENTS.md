# Worker Agent Architecture

## Overview

The orchestrator uses **ONE shared worker** for ALL work types (tasks, research, inbox responses, diagnostics).

### Key Design Principles

- **Single Worker:** Only one worker agent exists: `worker`
- **No Project-Specific Agents:** All projects use the same shared worker
- **Automatic Queueing:** Tasks wait in provider queue when worker is busy
- **Sequential Processing:** Work items execute one at a time, never concurrently
- **Session Isolation:** Worker session resets between tasks for clean state

### How Queueing Works

1. **Provider** (Scanner) identifies eligible work items
2. **Engine** attempts to assign work to WorkerManager
3. **WorkerManager** enforces single-worker constraint:
   - If worker idle → spawn for current task
   - If worker busy → task remains in queue (provider holds it)
4. **Next poll** picks up queued work when worker completes

**No separate queue data structure** - the provider's eligible work list IS the queue.

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

### 3. Queueing (Automatic)

**Single Worker Enforcement:**
- WorkerManager checks `active_workers` and `pending_workers` before spawning
- If ANY worker is active/pending → spawn request rejected, task stays queued
- Logs: `[QUEUE] Worker busy with <task>. Queueing task <new_task>`

**Queue Processing:**
- Provider's `get_tasks()` returns eligible work (the queue)
- Engine iterates through items every poll interval (default: 10s)
- When worker completes → lock released → next iteration spawns queued work
- FIFO order (first eligible task gets assigned when worker becomes free)

**No Explicit Queue Structure:**
- Tasks aren't moved to a separate queue
- They remain in provider's eligible work list
- This is simpler and restart-safe (filesystem is the queue)

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
