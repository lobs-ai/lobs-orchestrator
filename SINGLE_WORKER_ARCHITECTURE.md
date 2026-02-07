# Single Worker Architecture - Implementation Summary

## Overview

The lobs-orchestrator implements a **strict single-worker architecture** where exactly ONE worker processes ALL tasks sequentially. Work is automatically queued when the worker is busy.

## Core Implementation

### 1. Single Worker Constraint

**Enforcement Points:**

- `ThreadPoolExecutor(max_workers=1)` in `WorkerManager.__init__` (worker.py:52)
- Pre-spawn check in `spawn_worker()` (worker.py:140-143)
- Runtime validation before adding to `active_workers` (worker.py:235-240)

**Agent Identity:**

- Always uses agent ID: `"worker"` (hardcoded in agents.py:51)
- Never creates per-project or per-task agents
- Single registration in `~/.openclaw/openclaw.json`
- Single workspace: `~/.openclaw/workspace-worker/`

### 2. Automatic Queueing

**How It Works:**

1. Scanner identifies eligible work via `provider.get_tasks()`
2. Engine loops through eligible items
3. WorkerManager checks if busy before spawning:
   ```python
   if self.active_workers or self.pending_workers:
       logger.info(f"[QUEUE] Worker busy. Queueing task {task_id}")
       return
   ```
4. Rejected tasks remain in provider's queue
5. Next engine iteration (poll interval) picks them up

**No Explicit Queue:**
- Tasks aren't moved to a separate queue data structure
- Provider's eligible work list IS the queue
- Simpler and restart-safe (filesystem is source of truth)

### 3. Worker Lifecycle States

```
IDLE → SYNCING → RUNNING → FINALIZING → IDLE
        ↓          ↓           ↓
     pending   active      pending
      workers   workers     workers
```

- **SYNCING**: `git pull --rebase` in project repo (lock acquired, status="syncing")
- **RUNNING**: OpenClaw agent executing task (lock has PID, status="running")
- **FINALIZING**: `git commit && git push` (lock held, status="finalizing")
- **IDLE**: No active/pending workers, ready for next task

### 4. Session Management

**Worker sessions are cleared between tasks** to ensure clean context.

**Implementation:**

After each task completion, the orchestrator deletes the worker's session files:

```python
# Delete session files from ~/.openclaw/agents/worker/sessions/
agent_sessions_dir.glob("*.jsonl")  # Delete all JSONL transcripts
sessions.json                        # Delete sessions store
```

**Why This Approach:**

✅ **Safe**: Only deletes worker agent's sessions, not system-wide
✅ **Clean**: Each task starts with fresh context
✅ **Isolated**: No risk of affecting user's main chat sessions
✅ **Simple**: Direct file deletion, no API calls that could have side effects

**Why NOT `sessions.reset`:**

❌ `sessions.reset` with `agentId: "worker"` would reset ALL sessions for that agent
❌ Could potentially interfere with gateway state
❌ Direct file deletion is more surgical and predictable

**Location:**

Session files are stored at:
```
~/.openclaw/agents/worker/sessions/
├── <session-id>.jsonl        # Transcript files (deleted)
└── sessions.json              # Session store (deleted)
```

These files are recreated automatically on the next worker spawn.

## Code Changes Made

### orchestrator/core/worker.py

1. **Enhanced docstring** (lines 25-38):
   - Clarified single-worker constraints
   - Documented queueing mechanism
   - Added lifecycle state explanations

2. **Improved spawn_worker** (lines 131-143):
   - Added detailed docstring explaining queueing
   - Better logging with `[QUEUE]` prefix
   - Shows current task when rejecting new work

3. **Added helper methods** (lines 380-415):
   - `is_worker_busy()`: Check if worker is active
   - `get_worker_status()`: Get detailed status (idle/syncing/running/finalizing)

4. **Runtime validation** (lines 235-240):
   - Asserts single-worker constraint before adding to active_workers
   - Raises RuntimeError if violated (defensive programming)

5. **Clarifying comments**:
   - Line 166: Explain agent_type is metadata, not agent selection
   - Lines 47-52: Document single-worker enforcement via ThreadPoolExecutor

### orchestrator/core/engine.py

1. **Queue depth logging** (lines 137-145):
   - Log `[QUEUE]` message when worker is busy
   - Show current task and queue size
   - Only logs if there's actual queued work

2. **Enhanced status method** (lines 223-234):
   - Added `worker_busy`, `worker_state`, `current_task` fields
   - Uses new `get_worker_status()` helper
   - Comments clarify active/pending should be 0 or 1

3. **Clarifying comments**:
   - Line 171: Explain agent_type is metadata, not agent selector
   - Line 177: Note that spawn_worker will queue if busy

### Documentation Updates

1. **README.md**:
   - Added "Queueing & Concurrency" section explaining single-worker model
   - Worker lifecycle diagram
   - Configuration section for worker setup
   - Clarified Core Principles

2. **AGENTS.md**:
   - Expanded "Worker Architecture" section
   - Added "Core Constraints" subsection
   - Detailed queueing behavior
   - Implementation details and guarantees

3. **WORKER_AGENTS.md**:
   - Rewrote "Overview" to emphasize single worker
   - Added "Key Design Principles"
   - Expanded "How Queueing Works" section
   - Clarified "No Explicit Queue Structure"

## Verification

### How to Verify Single Worker

1. **Check worker count**:
   ```bash
   # Should show 0 or 1 active worker
   ps aux | grep "openclaw agent --agent worker"
   ```

2. **Monitor logs**:
   ```bash
   # Look for [QUEUE] messages
   python3 main.py | grep -E "\[QUEUE\]|\[WORKER\]"
   ```

3. **Check agent registration**:
   ```bash
   # Should show exactly one "worker" agent
   cat ~/.openclaw/openclaw.json | jq '.agents.list[] | select(.id == "worker")'
   ```

4. **Verify workspace**:
   ```bash
   # Should exist with template files
   ls -la ~/.openclaw/workspace-worker/
   ```

### Expected Behavior

When multiple tasks are ready:
1. First task spawns worker immediately
2. Subsequent tasks log: `[QUEUE] Worker busy with <task>. Queueing task <new_task>`
3. After first task completes, second task spawns on next engine iteration
4. Process repeats until queue is empty

### Guarantees

✅ **Never more than 1 worker process running**
✅ **Tasks execute sequentially in FIFO order**
✅ **No concurrent file modifications**
✅ **Clean session state between tasks**
✅ **Automatic queueing without manual intervention**
✅ **Restart-safe (filesystem is source of truth)**

## Configuration

### Worker Settings

- **Agent**: Registered in `~/.openclaw/openclaw.json`
- **Workspace**: `~/.openclaw/workspace-worker/`
- **Templates**: `worker-template/` (orchestrator repo)
- **Model**: Configurable in `~/.openclaw/agents/worker/config.json`

### Session Management Configuration

**Recommended**: Configure session expiry to prevent indefinite context growth.

Edit `~/.openclaw/openclaw.json`:

```json
{
  "session": {
    "reset": {
      "mode": "daily",
      "atHour": 4
    }
  }
}
```

**Options:**
- `daily`: Reset at specific hour (4 AM by default)
- `idle`: Reset after N minutes of inactivity
- Combined: `{"mode": "daily", "atHour": 4, "idleMinutes": 120}` (whichever expires first)

**Per-Agent Configuration** (optional):

To set different reset policies for the worker agent:

```json
{
  "agents": {
    "list": [
      {
        "id": "worker",
        "name": "Lobs Worker",
        "workspace": "~/.openclaw/workspace-worker",
        "model": "anthropic/claude-sonnet-4-5",
        "session": {
          "reset": {
            "mode": "idle",
            "idleMinutes": 240
          }
        }
      }
    ]
  }
}
```

### System Settings (`.lobs_settings.json`)

```json
{
  "poll_interval": 10,
  "openclaw_executable": "openclaw"
}
```

`poll_interval` = queue processing frequency (how often to check for queued work)

## Troubleshooting

### Multiple workers running

**Symptom**: More than one `openclaw agent` process
**Cause**: Bug violating single-worker constraint
**Action**: Check logs for "CRITICAL: Single-worker constraint violated"

### Tasks not processing

**Symptom**: Tasks stay in queue indefinitely
**Diagnosis**:
```bash
# Check if worker is stuck
ps aux | grep openclaw
cat locks/*.lock
```

### Worker won't spawn

**Check**:
1. Is worker registered? `bin/manage-workers list`
2. Is gateway running? `openclaw gateway status`
3. Check provisioning: `bin/manage-workers provision`

## Summary

The orchestrator now has:
- ✅ Strict single-worker enforcement
- ✅ Automatic queueing built into spawn logic
- ✅ Clear logging with `[QUEUE]` and `[WORKER]` prefixes
- ✅ Runtime validation to catch violations
- ✅ Comprehensive documentation
- ✅ Helper methods for status monitoring

**Result**: A production-ready single-worker orchestrator with transparent queueing and robust constraint enforcement.
