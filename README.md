# Lobs Orchestrator

Long-running Python service that manages scheduling, concurrency, and task execution for Lobs.

## Core Principles

- **Scripts own control.** LLMs do bounded work. Humans approve intent.
- **Deterministic & Restart-safe.** The filesystem is the recovery log.
- **Single Writer Pattern.** Exactly one component (`ControlManager`) writes to the control repo to avoid git races.
- **Domain Locks.** At most one worker per project domain at a time.

## Architecture

- **Engine:** Main loop that polls for state changes and triggers work.
- **ControlManager:** Manages git operations and applies state updates serially.
- **WorkerManager:** Spawns worker subprocesses and manages domain locks.
- **Scanner:** Purely scripted fact detection (tasks, requests, failures).

## Getting Started

### Prerequisites

- Python 3.11+
- `lobs-control` repository cloned in the same parent directory.

### Running the Orchestrator

```bash
python3 main.py
```

## Configuration

Configuration is managed in `orchestrator/config.py`. Key settings:

- `POLL_INTERVAL`: How often the orchestrator scans for work (default: 10s).
- `BASE_DIR`: Parent directory containing the project repositories.

## Control Operations

To request a state change (e.g., update a task), other components or workers should create a JSON file in `lobs-control/state/ops/`.

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
