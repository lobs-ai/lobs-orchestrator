# Lobs Orchestrator

Long-running Python service that manages scheduling, concurrency, and task execution for Lobs.

## Core Principles

- **Scripts own control.** LLMs do bounded work. Humans approve intent.
- **Deterministic & Restart-safe.** The filesystem is the recovery log.
- **Single Writer Pattern.** Exactly one component (`ControlManager`) writes to the control repo to avoid git races.
- **Single Worker.** Exactly ONE worker runs at a time globally. All work is automatically queued and processed sequentially.

## Architecture

- **Engine:** Main loop that polls for state changes and triggers work.
- **ControlManager:** Manages git operations and applies state updates serially.
- **WorkerManager:** Manages the single shared worker subprocess.
  - Spawns via `openclaw agent --agent worker`
  - Enforces single-worker constraint (max_workers=1)
  - Automatic queueing: tasks wait in provider queue if worker is busy
  - Session reset between tasks for clean state
- **Scanner:** Purely scripted fact detection (tasks, requests, failures).

## Queueing & Concurrency

**IMPORTANT:** This orchestrator runs EXACTLY ONE worker at a time.

### How Queueing Works

1. **Scanner** identifies all eligible work items
2. **Engine** loops through eligible items
3. **WorkerManager** checks if worker is busy:
   - If **idle**: spawns worker for the task
   - If **busy**: task remains in provider queue (automatic queueing)
4. Next engine iteration picks up queued work when worker completes

### Single Worker Guarantees

- No concurrent task execution
- No race conditions between tasks
- Predictable sequential processing
- Clean session state between tasks (session files cleared after each task)

### Worker Lifecycle

```
Task Assigned → Syncing (git pull) → Running (OpenClaw agent) → Finalizing (git commit/push) → Idle
                     ↓                      ↓                           ↓
                Lock acquired          Lock active                Lock released
```

## Getting Started

### Prerequisites

- Python 3.11+
- `lobs-control` repository cloned in the same parent directory.

### Running the Orchestrator

```bash
python3 main.py
```

## Configuration

Configuration is managed in `orchestrator/config.py` and `.lobs_settings.json`. Key settings:

- `POLL_INTERVAL`: How often the orchestrator scans for work (default: 10s). This is also the queue processing interval.
- `BASE_DIR`: Parent directory containing the project repositories.
- `openclaw_executable`: Path to openclaw binary (default: "openclaw")

### Worker Configuration

The single worker agent is configured at:
- **Agent Config**: `~/.openclaw/openclaw.json` (agent registration)
- **Workspace**: `~/.openclaw/workspace-worker/` (agent context files)
- **Templates**: `worker-template/` in this repo (copied to workspace on provision)

To change the worker's model, edit `~/.openclaw/agents/worker/config.json`:
```json
{
  "model": "anthropic/claude-opus-4-5"
}
```

### Prerequisite Checks

The orchestrator checks for required tools (node, npm, gh, tsc, etc.) before running certain operations. If tool detection is failing incorrectly, you can bypass these checks:

**Skip all checks:**
```json
{
  "skip_prereqs": true
}
```

**Skip specific tools:**
```json
{
  "skip_prereqs_list": ["node", "gh", "tsc"]
}
```

When skipping, the orchestrator logs warnings but allows work to proceed. See [SETTINGS.md](SETTINGS.md) for details.

## Control Operations

To request a state change (e.g., update a task), other components or workers should create a JSON file in `lobs-control/state/control-ops/`.

Example `update_task` operation:

```json
{
  "type": "update_task",
  "task_id": "UUID",
  "updates": {
    "workState": "completed"
  }
}
```

The orchestrator will pick this up, apply it, and push the changes.
