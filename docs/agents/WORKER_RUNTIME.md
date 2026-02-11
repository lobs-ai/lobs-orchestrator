# Worker Runtime

Current worker lifecycle, concurrency, and queueing behavior.

## Concurrency Model

- Global capacity: `max_concurrent_workers` (`MAX_WORKERS`)
- Per-project capacity: one active worker per project (`project_locks`)
- Queueing: implicit via retry on next poll when spawn is rejected

A task is queued when:
- global capacity reached, or
- its project already has an active worker

## Lifecycle

Typical states:

- `syncing` (pending): repo sync and preflight
- `running` (active): runtime execution (OpenClaw or Ollama)
- `finalizing` (pending): commit/push and follow-up operations
- cleanup: lock release, session cleanup, state update

## Runtime Selection

- Ollama path for diagnostic tasks or explicit `runtime: ollama`
- OpenClaw path for standard tasks
- Ollama fallback to OpenClaw when unavailable

## Restart Safety

- Worker state persisted in `~/.openclaw/worker-state.json`
- On startup, running workers are re-adopted when possible
- stale/dead worker state is cleaned automatically

## Session Cleanup

After each task, worker session files are deleted from:

- `~/.openclaw/agents/worker/sessions/*.jsonl`
- `~/.openclaw/agents/worker/sessions/sessions.json`

This keeps task context isolated between runs.

## Operational Verification

- check active worker status API (`/workers/status`)
- inspect logs for `[CAPACITY]` and `[PROJECT-LOCK]` messages
- confirm lock behavior by submitting two tasks for same project

## Code References

- `orchestrator/core/worker.py`
- `orchestrator/core/engine.py`
- `orchestrator/config.py`
