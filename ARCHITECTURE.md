# Lobs Orchestrator - Architecture

## Table of Contents

1. [System Overview](#system-overview)
2. [Core Principles](#core-principles)
3. [Architecture Components](#architecture-components)
4. [Data Flow](#data-flow)
5. [Single Worker Model](#single-worker-model)
6. [State Management](#state-management)
7. [Session Management](#session-management)
8. [Queueing & Concurrency](#queueing--concurrency)
9. [Error Handling](#error-handling)
10. [File Structure](#file-structure)
11. [Configuration](#configuration)
12. [Deployment](#deployment)

---

## System Overview

The Lobs Orchestrator is a long-running Python service that manages task scheduling, execution, and coordination for the Lobs system. It acts as the central nervous system, coordinating between:

- **Control Repository** (lobs-control): Source of truth for tasks, projects, and state
- **Project Repositories**: Individual codebases where work is performed
- **OpenClaw Agent**: LLM-powered worker that executes tasks

```
┌─────────────────────────────────────────────────────────────┐
│                    Lobs Orchestrator                         │
│                                                               │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐                │
│  │  Engine  │───│  Worker  │───│  Agent   │                │
│  │  (Loop)  │   │ Manager  │   │ Manager  │                │
│  └─────┬────┘   └────┬─────┘   └────┬─────┘                │
│        │             │              │                        │
│  ┌─────▼────┐   ┌───▼──────┐  ┌───▼──────┐                │
│  │ Provider │   │ Control  │  │ Scanner  │                │
│  │(Scanner) │   │ Manager  │  │          │                │
│  └──────────┘   └──────────┘  └──────────┘                │
└───────────┬─────────────────────────┬───────────────────────┘
            │                         │
    ┌───────▼────────┐       ┌────────▼────────┐
    │  lobs-control  │       │ OpenClaw Agent  │
    │  (filesystem)  │       │    (worker)     │
    └────────────────┘       └─────────────────┘
            │                         │
    ┌───────▼────────┐       ┌────────▼────────┐
    │ Project Repos  │       │ Project Files   │
    │  (git repos)   │       │  (workspace)    │
    └────────────────┘       └─────────────────┘
```

---

## Core Principles

### 1. Scripts Own Control

**Scripts are authoritative. LLMs are executors. Humans approve intent.**

- State lives in the filesystem (lobs-control repository)
- Scripts (not LLMs) detect facts and manage transitions
- LLMs receive bounded, well-defined tasks
- Humans approve work through the control repository

### 2. Per-Project Worker Limiting

**Only one worker can be active per project at a time.**

- Enforces project-level locking to prevent concurrent modifications
- Different projects can run workers in parallel
- Tasks for busy projects are automatically queued by the engine
- Project locks are acquired when worker starts and released on completion/failure
- Locks are re-acquired on orchestrator restart when re-adopting workers

### 3. Single Writer Pattern

**Exactly ONE component writes to the control repository.**

- ControlManager is the sole writer to lobs-control
- Prevents git race conditions and conflicts
- All state updates go through control-ops queue
- Atomic, serialized state transitions

### 4. Restart Safety

**The system can be stopped and restarted at any time.**

- Filesystem is the recovery log
- No critical in-memory state
- Idempotent operations
- Work-in-progress is tracked via locks

---

## Architecture Components

### Engine (`orchestrator/core/engine.py`)

**The main orchestration loop.**

**Responsibilities:**
- Poll for state changes every N seconds (default: 10s)
- Coordinate all subsystems
- Process control operations
- Manage worker lifecycle
- Trigger periodic reconciliation
- Handle heartbeats and monitoring

**Main Loop:**
```python
while True:
    1. Pulse system heartbeat
    2. Sync with provider (process control-ops)
    3. Check active workers
    4. Process heartbeat ticks
    5. Monitor system health
    6. Scan for eligible work
    7. Periodic reconciliation
    8. Assign work to worker (or queue)
    9. Sleep (adaptive backoff when idle)
```

**Key Properties:**
- Adaptive polling: increases sleep when idle, resets on activity
- Single-threaded main loop (simplicity, restart safety)
- No critical state in memory

---

### WorkerManager (`orchestrator/core/worker.py`)

**Manages concurrent worker subprocesses with per-project locking.**

**Responsibilities:**
- Track multiple workers across different projects
- Spawn workers via `openclaw agent --agent <type>`
- Track worker lifecycle (syncing → running → finalizing)
- Enforce per-project worker limits (one worker per project)
- Handle worker success/failure
- Clean up sessions between tasks

**Worker Lifecycle:**

```
IDLE
  ├─> CHECK PROJECT LOCK
  │     ├─> Project locked → Return False (queue task)
  │     └─> Project available → Continue
  │
  ├─> SYNCING (git pull --rebase)
  │     ├─> Task in pending_workers
  │     └─> No lock yet (async spawning)
  │
  ├─> RUNNING (openclaw agent)
  │     ├─> Project lock acquired
  │     ├─> Task in active_workers
  │     └─> Subprocess executing
  │
  ├─> FINALIZING (git commit/push)
  │     ├─> Lock still held
  │     └─> Task in pending_workers
  │
  └─> IDLE
        ├─> Project lock released
        ├─> Session cleaned
        └─> Ready for next task
```

**Per-Project Locking:**

1. **project_locks dict**: Maps `project_id → task_id` for active workers
2. **Pre-spawn check**: Returns False if project already has an active worker
3. **Lock acquisition**: When worker subprocess starts (in RUNNING state)
4. **Lock release**: When worker completes (success/failure) or is cleaned up
5. **Restart safety**: Locks are re-acquired when re-adopting workers on restart

**Queueing Logic:**
```python
def spawn_worker(self, task, project_id, ...):
    # Check if project already has a worker
    if project_id in self.project_locks:
        logger.info(f"[PROJECT-LOCK] Project {project_id} busy. Queueing task.")
        return False  # Engine will retry on next poll
    
    # Mark as pending and spawn async
    self.pending_workers.add(task_id)
    self.executor.submit(self._async_spawn_openclaw_flow, ...)
    return True
```

**Benefits:**
- Different projects can run workers in parallel
- Prevents concurrent modifications to the same project repository
- No git conflicts or race conditions within a project
- Automatic queueing when project is busy
- Maximum parallelism across project boundaries

---

### AgentManager (`orchestrator/core/agents.py`)

**Manages the worker agent provisioning.**

**Responsibilities:**
- Ensure worker agent exists and is registered
- Copy template files to worker workspace
- Register agent in `~/.openclaw/openclaw.json`
- Handle gateway restarts after registration
- Provide worker sync/cleanup utilities

**Worker Agent:**
- **ID**: `worker` (hardcoded, never changes)
- **Directory**: `~/.openclaw/agents/worker/`
- **Workspace**: `~/.openclaw/workspace-worker/`
- **Templates**: `worker-template/` (AGENTS.md, SOUL.md, TOOLS.md, etc.)
- **Model**: Claude Sonnet 4.5 (configurable in agent config)

**One-Time Setup:**
1. Check if worker exists and is registered
2. If not, create directories and copy templates
3. Register in openclaw.json
4. Restart gateway (required for registration to take effect)

---

### Scanner (`orchestrator/services/scanner.py`)

**Purely scripted fact detection.**

**Responsibilities:**
- Scan lobs-control for eligible work
- Call `bin/open-work` script to identify tasks
- Detect pending worker requests
- Load projects list
- No state modification (read-only)

**Eligible Work:**
- Tasks with `workState: not_started` and `status: active`
- Research requests, inbox responses, diagnostics
- Filtered to exclude completed/archived items

---

### ControlManager (`orchestrator/services/control.py`)

**The ONLY writer to the lobs-control repository.**

**Responsibilities:**
- Process control-ops queue
- Apply state updates atomically
- Commit and push changes to lobs-control
- Maintain single-writer guarantee

**Control Operations:**

Workers and components request changes by creating JSON files in `lobs-control/state/control-ops/`:

```json
{
  "type": "update_task",
  "task_id": "UUID",
  "updates": {
    "workState": "completed"
  }
}
```

ControlManager processes these serially:
1. Read op file
2. Apply changes to state files
3. Commit to git
4. Push to remote
5. Delete op file

---

### Provider (`orchestrator/providers/`)

**Abstract interface for task/state backends.**

**Responsibilities:**
- Decouple orchestrator from specific state storage
- Provide uniform interface for tasks, projects, state
- Currently: LocalTaskProvider (filesystem-based)
- Future: Could support API, database, etc.

**Key Methods:**
- `get_tasks()`: Fetch eligible work
- `get_projects()`: Fetch project list
- `update_task()`: Request task state change
- `sync()`: Process control operations
- `get_engineering_rules()`: Fetch global rules

---

### Reconciler (`orchestrator/core/reconciler.py`)

**Periodic self-healing and consistency checks.**

**Responsibilities:**
- Detect stale/stuck tasks
- Identify abandoned work
- Clean up orphaned locks
- Runs every 5 minutes (configurable)

---

### Monitor (`orchestrator/core/monitor.py`)

**System health monitoring.**

**Responsibilities:**
- Track system metrics
- Detect anomalies
- Alert on failures
- Periodic health checks

---

### Prompter (`orchestrator/services/prompter.py`)

**Builds prompts for worker agents.**

**Responsibilities:**
- Load engineering rules (global + project-specific)
- Include product context (PRODUCT.md, ARCHITECTURE.md, etc.)
- Inject AGENTS.md rules
- Format work assignment
- Provide output contract

**Prompt Structure:**
```
# TASK ASSIGNMENT

## Orchestrator Rules (AGENTS.md)
## Global Engineering Rules
## Project Engineering Rules
## Product Context

## Work Assignment
- Kind: task/research/diagnostic
- ID: <uuid>
- Project: <project-id>
- Workspace: <path>

## TASK / RESEARCH / DIAGNOSTIC
<work-specific details>

## Output Contract
- Work in project repo
- git pull --rebase before changes
- Commit changes with clear messages
- Do NOT push (orchestrator handles that)

## Constraints
- Focus on this work item only
- Do not invent new tasks
- Stop and report if blocked
```

---

## Data Flow

### Task Assignment Flow

```
1. Scanner scans lobs-control
   └─> Calls bin/open-work script
       └─> Returns eligible tasks JSON

2. Engine receives eligible tasks
   └─> Loops through each task
       └─> Checks if project is locked
           └─> WorkerManager.spawn_worker()

3. WorkerManager checks constraint
   ├─> If busy: log [QUEUE], return (task stays queued)
   └─> If idle: spawn async flow

4. Async Spawn Flow (in ThreadPoolExecutor)
   ├─> Acquire lock (status=syncing)
   ├─> git pull --rebase in project
   ├─> Build prompt (Prompter)
   ├─> Spawn: openclaw agent --agent worker -m "prompt"
   ├─> Update lock (status=running, pid=X)
   └─> Add to active_workers

5. Engine marks task as in_progress
   └─> Provider.update_task()
       └─> Creates control-op
           └─> ControlManager processes
               └─> Updates state, commits, pushes

6. Worker executes task
   └─> Reads AGENTS.md, SOUL.md (auto-loaded)
   └─> Works in project directory
   └─> Creates control-ops for state changes
   └─> Exits (code 0 = success, non-0 = failure)

7. WorkerManager detects completion
   ├─> If success: finalize flow
   │   ├─> Lock status=finalizing
   │   ├─> git add . && git commit
   │   ├─> git push
   │   ├─> Update task to completed
   │   ├─> Clean session files
   │   └─> Release lock
   │
   └─> If failure: handle failure
       ├─> Log error
       ├─> Update task to failed
       ├─> Escalation (create diagnostic task)
       ├─> Clean session
       └─> Release lock

8. Next engine iteration picks up queued work
```

---

## Single Worker Model

### Constraint Enforcement

**Three layers of protection:**

1. **ThreadPoolExecutor(max_workers=1)**
   - Only one async spawn flow can run at a time
   - Prevents race conditions in spawn logic

2. **Pre-spawn Check**
   ```python
   if self.active_workers or self.pending_workers:
       logger.info("[QUEUE] Worker busy")
       return
   ```
   - Rejects new work if worker is active/pending
   - Tasks remain in provider queue

3. **Runtime Validation**
   ```python
   if len(self.active_workers) > 0:
       raise RuntimeError("Single-worker constraint violated")
   ```
   - Defensive check before adding to active_workers
   - Catches bugs that slip through earlier checks

### Queueing Mechanism

**No explicit queue data structure.**

The provider's eligible work list IS the queue:

1. Scanner returns all eligible tasks
2. Engine attempts to assign each one
3. WorkerManager accepts first, rejects rest
4. Rejected tasks stay in provider queue
5. Next engine loop (10s later) tries again
6. First idle worker picks up next task

**Benefits:**
- Simpler code (no queue management)
- Restart-safe (filesystem is source of truth)
- FIFO order (scanner returns sorted list)
- No stale queue state

### Logging

**Queue visibility:**
```
[QUEUE] Worker busy with abc123. Queueing task def456 (Fix login bug)
[QUEUE] Worker busy (current: abc123, state: running). 3 task(s) queued.
```

**Worker lifecycle:**
```
[WORKER] Using shared worker agent: worker (type: task)
[WORKER] Cleared 2 session file(s) for agent worker
```

---

## State Management

### State Storage

**All state lives in the lobs-control repository:**

```
lobs-control/
├── state/
│   ├── tasks.json              # All tasks
│   ├── tasks/                  # Individual task files
│   ├── projects.json           # Project registry
│   ├── worker-status.json      # Current worker status
│   ├── control-ops/            # Pending state changes
│   ├── worker-results/         # Task execution logs
│   ├── inbox/                  # Inbox items
│   └── alerts/                 # System alerts
├── bin/
│   └── open-work               # Script to identify eligible work
└── ENGINEERING_RULES.md        # Global rules
```

### State Transitions

**Tasks:**
```
not_started → in_progress → completed
                 ↓
              failed → diagnostic_pending
```

**Worker Status:**
```
{
  "activeWorkers": [
    {
      "workerId": "programmer-1707595200-task123",
      "taskId": "task-id",
      "projectId": "project-id",
      "agentType": "programmer",
      "startedAt": "ISO-8601",
      "lastHeartbeat": "ISO-8601",
      "status": "running",
      "taskTitle": "Task description"
    }
  ],
  "totalActiveWorkers": 1,
  "lastUpdated": "ISO-8601"
}
```

### Control Operations

**Standard operations:**

- `update_task`: Change task state
- `update_project`: Change project properties
- `update_worker_status`: Update worker status
- `add_inbox_item`: Create inbox suggestion
- `update_inbox_item`: Change inbox item
- `update_alert`: Modify alert

**Processing:**
1. Components create JSON files in `control-ops/`
2. ControlManager reads ops on each sync
3. Applies changes to state files
4. Commits and pushes as a batch
5. Deletes processed op files

---

## Session Management

### The Problem

Worker agents accumulate context across tasks. Without cleanup:
- Context window fills up
- Previous task context leaks into next task
- Worker may reference unrelated previous work

### The Solution

**Delete session files directly after each task.**

```python
def _cleanup_worker_session(self, agent_id: str):
    agent_sessions_dir = Path.home() / ".openclaw" / "agents" / agent_id / "sessions"

    # Delete all session transcripts
    for session_file in agent_sessions_dir.glob("*.jsonl"):
        session_file.unlink()

    # Delete session store
    (agent_sessions_dir / "sessions.json").unlink()
```

### Why NOT `sessions.reset`

❌ **Don't use:** `openclaw gateway call sessions.reset --params '{"agentId":"worker"}'`

**Reasons:**
- Resets ALL sessions for the agent (too broad)
- Could affect other sessions using the same agent
- API call could have unintended side effects
- Less predictable than direct file deletion

### Why Direct File Deletion

✅ **Surgical**: Only deletes worker's session files
✅ **Safe**: No risk to main chat sessions
✅ **Simple**: Plain file operations
✅ **Predictable**: Files recreated automatically on next spawn

### Files Deleted

```
~/.openclaw/agents/worker/sessions/
├── <session-id>.jsonl        # Conversation transcript
├── <session-id>-2.jsonl      # Additional transcripts
└── sessions.json              # Session metadata store
```

All files are recreated automatically when the worker spawns for the next task.

---

## Queueing & Concurrency

### Concurrency Model

**Single-threaded main loop + async worker spawn.**

```
Main Thread (Engine Loop)
  ├─> Sync provider
  ├─> Check workers
  ├─> Scan for work
  └─> Assign work
        └─> WorkerManager.spawn_worker()
              └─> ThreadPoolExecutor (max_workers=1)
                    └─> Async spawn flow
                          ├─> git pull
                          ├─> Spawn openclaw agent (subprocess)
                          └─> Track in active_workers
```

**Why Single-Threaded:**
- Simpler reasoning about state
- No locks needed (except project locks)
- Easier to debug
- Restart-safe

**Why Async Spawn:**
- git pull can be slow
- Agent provisioning might restart gateway
- Don't block main loop during setup
- Worker execution is async anyway (subprocess)

### Project Locks

**Prevent concurrent modifications to project repositories.**

```python
locks/
├── project-a.lock    # {"pid": 12345, "status": "running", "acquired_at": 1234567890}
└── project-b.lock
```

**Lock States:**
- `syncing`: git pull in progress
- `running`: worker executing task
- `finalizing`: git commit/push in progress

**Lock Lifecycle:**
1. Acquired before git pull (status=syncing)
2. Updated after spawn (status=running, pid=X)
3. Updated before finalize (status=finalizing)
4. Released after completion
5. Timeout: 10 minutes (cleaned up if stale)

---

## Error Handling

### Worker Failures

**When worker exits with non-zero code:**

1. Log error with tail of output
2. Mark task as `failed`
3. Create diagnostic task (escalation)
4. Clean up session
5. Release lock

**Escalation:**
```python
{
  "kind": "diagnostic",
  "agentType": "diagnostic",
  "title": "Investigate failure: <task-title>",
  "notes": "<error log tail>",
  "priority": "high"
}
```

### Stuck Workers

**Reconciler detects:**
- Tasks in `in_progress` for > N hours
- Stale locks with dead PIDs
- Abandoned work

**Actions:**
- Release locks
- Mark tasks as `failed`
- Create alerts

### Git Conflicts

**During git pull:**
- Worker spawn fails
- Task stays queued
- Reconciler will retry
- Alert created if repeated failures

**During git push:**
- Finalization fails
- Task marked as failed
- Error logged
- Manual intervention required

---

## File Structure

```
lobs-orchestrator/
├── orchestrator/
│   ├── core/
│   │   ├── engine.py           # Main orchestration loop
│   │   ├── worker.py           # Worker lifecycle management
│   │   ├── agents.py           # Agent provisioning
│   │   ├── reconciler.py       # Self-healing
│   │   ├── monitor.py          # Health monitoring
│   │   ├── heartbeat.py        # Heartbeat/notifications
│   │   └── escalation.py       # Error escalation
│   ├── services/
│   │   ├── scanner.py          # Task scanning
│   │   ├── control.py          # State writer (single writer)
│   │   ├── prompter.py         # Prompt builder
│   │   ├── messages.py         # Message processor
│   │   └── chat.py             # Chat interface
│   ├── providers/
│   │   ├── base.py             # Abstract provider interface
│   │   └── local.py            # Filesystem provider
│   ├── models/
│   │   ├── tasks.py            # Task models
│   │   └── agents.py           # Agent models
│   ├── utils/
│   │   ├── cli.py              # CLI utilities
│   │   ├── settings.py         # Settings management
│   │   └── results.py          # Result handling
│   └── config.py               # Configuration constants
├── worker-template/
│   ├── AGENTS.md               # Worker agent rules
│   ├── SOUL.md                 # Worker personality
│   ├── TOOLS.md                # Tool usage notes
│   ├── USER.md                 # About the user
│   └── IDENTITY.md             # Worker identity
├── locks/                      # Project lock files
├── bin/
│   └── manage-workers          # Worker management CLI
├── tests/                      # Test suite
├── main.py                     # Entry point
├── .lobs_settings.json         # Local settings
├── README.md                   # Quick start guide
├── AGENTS.md                   # Agent rules (for workers)
├── WORKER_AGENTS.md            # Worker implementation details
├── ARCHITECTURE.md             # This file
└── SINGLE_WORKER_ARCHITECTURE.md  # Single-worker deep dive
```

---

## Configuration

### Settings File (`.lobs_settings.json`)

```json
{
  "base_dir": "/path/to/lobs",
  "control_repo_path": "/path/to/lobs-control",
  "orchestrator_repo_path": "/path/to/lobs-orchestrator",
  "poll_interval": 10,
  "openclaw_executable": "openclaw"
}
```

### Python Config (`orchestrator/config.py`)

```python
BASE_DIR = Path(get_setting("base_dir"))
CONTROL_REPO_PATH = Path(get_setting("control_repo_path"))
POLL_INTERVAL = get_setting("poll_interval", 10)
```

### OpenClaw Config (`~/.openclaw/openclaw.json`)

```json
{
  "agents": {
    "list": [
      {
        "id": "worker",
        "name": "Lobs Worker",
        "workspace": "~/.openclaw/workspace-worker",
        "model": "anthropic/claude-sonnet-4-5",
        "identity": {
          "name": "Lobs Worker",
          "emoji": "🔧"
        }
      }
    ]
  },
  "session": {
    "reset": {
      "mode": "daily",
      "atHour": 4
    }
  }
}
```

**Session Reset Policy (Optional):**

Natural session expiry can be configured as a backup to file deletion:
- `daily`: Reset at specific hour (4 AM default)
- `idle`: Reset after N minutes of inactivity
- Both: Whichever expires first

---

## Deployment

### Prerequisites

- Python 3.11+
- OpenClaw installed and configured
- lobs-control repository cloned
- Project repositories cloned in same parent directory

### Installation

```bash
# Clone orchestrator
git clone <repo> lobs-orchestrator
cd lobs-orchestrator

# Create virtual environment
python3 -m venv env
source env/bin/activate

# Install dependencies
pip install -r requirements.txt

# Setup configuration
./setup_config.py
```

### Running

**Development:**
```bash
python3 main.py
```

**Production (systemd):**
```bash
# Copy service file
sudo cp lobs-orchestrator.service /etc/systemd/system/

# Enable and start
sudo systemctl enable lobs-orchestrator
sudo systemctl start lobs-orchestrator

# View logs
sudo journalctl -u lobs-orchestrator -f
```

### Monitoring

**Status:**
```bash
# Check service
systemctl status lobs-orchestrator

# Check active workers
ps aux | grep "openclaw agent"

# Check locks
ls -la locks/

# Check worker logs
tail -f ../lobs-control/state/worker-results/*.log
```

**Health Checks:**
- Heartbeat in worker-status.json (should update every 30s)
- Active worker count (should be 0 or 1)
- Lock files (should be cleaned up after tasks)
- Control-ops queue (should process regularly)

### Troubleshooting

**Worker won't spawn:**
```bash
# Check worker exists
bin/manage-workers list

# Provision worker
bin/manage-workers provision

# Check gateway
openclaw gateway status
```

**Tasks stuck in queue:**
```bash
# Check locks
ls -la locks/

# Remove stale locks
rm locks/*.lock

# Check worker process
ps aux | grep openclaw
```

**Session issues:**
```bash
# List worker sessions
openclaw gateway call sessions.list --params '{"agentId":"worker"}'

# Clear sessions manually
rm ~/.openclaw/agents/worker/sessions/*.jsonl
rm ~/.openclaw/agents/worker/sessions/sessions.json
```

---

## Design Decisions

### Why Single Worker?

**Alternatives considered:**
1. ❌ Multiple workers per project
2. ❌ Worker pool (N workers)
3. ✅ Single shared worker

**Reasons:**
- Simpler state management (no concurrent modifications)
- Easier to debug (one thing happening at a time)
- Fewer git conflicts (only one writer per project at a time)
- Predictable execution order (FIFO)
- Lower resource usage (one LLM session at a time)
- Restart-safe (filesystem tracks what's queued)

**Trade-offs:**
- ❌ Lower throughput (one task at a time)
- ✅ Simpler implementation
- ✅ More predictable behavior
- ✅ Easier to reason about

For Lobs' use case (personal productivity system), simplicity and predictability outweigh throughput.

### Why Filesystem State?

**Alternatives considered:**
1. ❌ Database (PostgreSQL, SQLite)
2. ❌ API backend
3. ✅ Git repository (lobs-control)

**Reasons:**
- Human-readable state (cat tasks.json)
- Git history is the audit log
- Restart-safe (no migration needed)
- Easy to inspect and debug
- Works offline (no API dependency)
- Scriptable (standard file operations)

### Why Control-Ops Queue?

**Alternatives considered:**
1. ❌ Direct state writes from workers
2. ❌ API calls to orchestrator
3. ✅ Control-ops JSON files

**Reasons:**
- Single writer pattern (avoids git conflicts)
- Atomic operations (git commit is transactional)
- Asynchronous (workers don't block on state updates)
- Restart-safe (pending ops survive restarts)
- Auditable (git log shows all state changes)

### Why Delete Session Files?

**Alternatives considered:**
1. ❌ Keep sessions (accumulate context)
2. ❌ Use `sessions.reset` API
3. ✅ Delete session files directly

**Reasons:**
- Clean context for each task (no leakage)
- No risk of affecting other sessions
- Simple and predictable (file operations)
- Fast (no API calls)
- Safe (worker sessions recreated automatically)

---

## Future Enhancements

### Potential Improvements

1. **Multi-Worker Support (Optional)**
   - Per-project workers for parallel execution
   - Requires lock coordination
   - Useful for teams with many projects

2. **Remote Workers**
   - Workers on different machines
   - Distributed task execution
   - API-based coordination

3. **Priority Queues**
   - High-priority tasks jump queue
   - User-requested work prioritized
   - Diagnostic tasks run first

4. **Telemetry & Metrics**
   - Task completion rates
   - Worker utilization
   - Error rates by project
   - Dashboard visualization

5. **Smart Scheduling**
   - Time-based task execution
   - Dependency-aware scheduling
   - Resource-aware allocation

6. **Agent Specialization**
   - Research agents
   - Code review agents
   - Documentation agents
   - Testing agents

---

## Conclusion

The Lobs Orchestrator implements a simple, predictable architecture centered around:

1. **Single worker** - one task at a time, automatic queueing
2. **Filesystem state** - git repository as source of truth
3. **Single writer** - ControlManager prevents conflicts
4. **Clean sessions** - direct file deletion between tasks
5. **Restart safety** - filesystem tracks all state

This design prioritizes **simplicity**, **predictability**, and **debuggability** over raw throughput, making it ideal for personal productivity systems where understanding what's happening is more important than speed.

---

**Document Version:** 1.0
**Last Updated:** 2026-02-07
**Author:** Lobs Orchestrator Team
