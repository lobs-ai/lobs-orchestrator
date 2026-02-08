# AGENTS.md

## Purpose

This file documents the **worker architecture and implementation details** for developers/operators.

**For worker prompts**, see `WORKER_RULES.md` (sent to workers in task prompts).
**This file** contains implementation details that workers don't need to know.

---

## Design Principles

**Scripts control. Agents execute. Humans approve.**

- Agents are task-scoped executors (not planners, schedulers, or product managers)
- Single unit of work per agent invocation
- No proactive action outside explicit tasks
- Stop and report when blocked or unclear

---

## Worker Architecture

This orchestrator uses a **single shared worker** model with **automatic queueing**.

### Core Constraints

- **ONE worker** runs at a time globally (not per-project, not per-task)
- **All work is queued** automatically when worker is busy
- **Sequential processing** - tasks execute one after another
- **No concurrency** - zero concurrent task execution

### Implementation Details

- Worker spawned with `openclaw agent --agent worker`
- All projects share the same `worker` agent
- Worker session files cleared between tasks for fresh context
- ThreadPoolExecutor limited to `max_workers=1` enforces constraint
- Queueing happens in the provider - tasks wait for next engine iteration

### Worker Agent Details

- **Agent ID**: `worker` (hardcoded, never changes)
- **Agent Directory**: `~/.openclaw/agents/worker/`
- **Workspace**: `~/.openclaw/workspace-worker/`
- **Registration**: Single "Lobs Worker" agent in openclaw.json
- **Model**: Claude Sonnet 4.5 (configurable per-agent)

### Queueing Behavior

When a task is assigned:
1. WorkerManager checks if worker is busy (`active_workers` or `pending_workers`)
2. If busy: logs "[QUEUE]" message, task remains in provider queue
3. If idle: spawns worker, acquires lock, begins work
4. Next engine iteration (poll interval) picks up queued tasks

This ensures:
- **No concurrent work conflicts** - only one task modifies files at a time
- **Clean session state** - each task starts with fresh agent context
- **Simplified management** - one agent to provision/configure
- **Predictable execution** - deterministic FIFO order

### Session Cleanup Between Tasks

**CRITICAL: Proper session cleanup is essential to prevent context leakage.**

After each task completes, the orchestrator clears the worker's session files:

```python
# Location: ~/.openclaw/agents/worker/sessions/
# Deleted files:
- *.jsonl           # Conversation transcripts
- sessions.json     # Session metadata store
```

**Why NOT `sessions.reset`:**
- ❌ `sessions.reset` with `agentId: "worker"` resets ALL sessions for that agent
- ❌ Could interfere with main chat sessions or other system sessions
- ❌ API calls can have unintended side effects

**Why Direct File Deletion:**
- ✅ Only affects worker agent's sessions (surgical, isolated)
- ✅ No risk of affecting user's main chat session
- ✅ Simple file operations - predictable and safe
- ✅ Files are recreated automatically on next worker spawn
- ✅ Ensures clean context for each task

**Implementation:** See `orchestrator/core/worker.py::_cleanup_worker_session()`

---

## Git Rules (Mandatory)

Before making changes:

```bash
git pull --rebase
```

