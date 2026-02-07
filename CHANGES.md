# Orchestrator Changes: Single Worker Architecture

## Summary

Updated the orchestrator to use a single worker at a time with the `--agent worker` flag instead of multiple per-project workers.

## Key Changes

### 1. Worker Concurrency (worker.py)
- **Before**: `ThreadPoolExecutor(max_workers=10)` - up to 10 concurrent workers
- **After**: `ThreadPoolExecutor(max_workers=1)` - single worker at a time
- Added global lock check in `spawn_worker()` to prevent spawning if any worker is already running

### 2. Agent ID (worker.py)
- **Before**: Each project had its own agent: `worker-<project-id>` (e.g., `worker-flock-master`)
- **After**: Single shared agent: `worker`
- Updated spawn command to always use `--agent worker`

### 3. Agent Manager (agents.py)
- Added `_get_agent_id()` helper method that always returns `"worker"`
- Updated all methods to use the single shared worker agent:
  - `worker_exists()`
  - `provision_worker()`
  - `sync_worker_templates()`
  - `cleanup_worker()`
  - `is_agent_registered()`
  - `register_agent()`
  - `unregister_agent()`
  - `list_workers()`
- Updated agent registration to use name "Lobs Worker" instead of per-project names

### 4. Documentation (README.md)
- Updated Core Principles: "Single Worker" instead of "Domain Locks"
- Updated Architecture: Worker manager now spawns single subprocess with `--agent worker`

## Worker Directory Structure

### Before
```
~/.openclaw/agents/worker-flock-master/
~/.openclaw/agents/worker-prairie/
~/.openclaw/workspace-worker-flock-master/
~/.openclaw/workspace-worker-prairie/
```

### After
```
~/.openclaw/agents/worker/
~/.openclaw/workspace-worker/
```

## Behavior Changes

1. **Concurrency**: Only 1 worker can run at a time (globally across all projects)
2. **Agent Session**: All workers share the same "worker" agent session
3. **Session Cleanup**: Worker session is reset after each task completion (using gateway API)
4. **Domain Locks**: Still maintained per-project, but now redundant due to global single worker limit

## Migration Notes

If you have existing per-project workers, they will be ignored. The orchestrator will create and use the single shared "worker" agent on first run.

To clean up old per-project workers manually:
```bash
rm -rf ~/.openclaw/agents/worker-*
rm -rf ~/.openclaw/workspace-worker-*
```

Then update `~/.openclaw/openclaw.json` to remove any old worker-* agent entries.
