# Lobs Orchestrator Architecture

Canonical architecture for current runtime behavior.

## Scope

This doc describes the active system design. Historical V1/V2 narrative docs are archived under `docs/archive/`.

## Invariants

- Scripts own control state; agents execute bounded tasks.
- `ControlManager` is the only control-repo writer.
- Filesystem state is source of truth for restart safety.
- Concurrency is bounded globally and locked per project.

## Runtime Model

- **Multi-agent templates:** `agents/<type>/` (`programmer`, `reviewer`, `researcher`, `writer`, `architect`).
- **Shared execution identity:** OpenClaw runs with `--agent worker`; workspace context is rewritten per task template.
- **Multi-worker execution:** concurrent workers up to `MAX_WORKERS` (`max_concurrent_workers` setting).
- **Per-project lock:** max one active worker per `project_id`.

## Core Components

- `orchestrator/core/engine.py`
Main loop: sync control ops, process workers, scan tasks, route and assign, run monitoring/reconciliation.

- `orchestrator/core/worker.py`
Worker lifecycle manager: capacity checks, project locking, repo sync, prompt/run, finalize, cleanup, retries.

- `orchestrator/core/router.py`
Rule-based agent selection when task has no explicit `agent`.

- `orchestrator/core/registry.py`
Loads agent template files and model metadata from `agents/<type>/`.

- `orchestrator/services/scanner.py`
Scripted discovery of eligible work (read-only).

- `orchestrator/services/control.py`
Serialized control operations and state writes.

- `orchestrator/core/collaboration.py`
Processes `.handoffs/*.json` into routed follow-up tasks.

- `orchestrator/core/awareness.py`
Tracks recent activity/failures/metrics; injects awareness context into prompts.

## Task Flow

1. Scanner/provider returns eligible work.
2. Engine routes task to an agent type (`task.agent` override or router keywords).
3. WorkerManager checks:
   - global capacity (`active_workers < max_workers`)
   - project lock availability (`project_id` not locked)
4. Worker enters `pending` (syncing), then runs runtime flow.
5. On completion/failure:
   - finalize changes and update provider
   - process handoffs and optional review/diagnostic hooks
   - release project lock and clear worker session files

## Concurrency and Queueing

Queueing is implicit:
- If capacity is full or project is locked, spawn is rejected.
- Task remains eligible in provider data and is retried next poll.

Guarantees:
- Max concurrent workers bounded by config.
- No concurrent writes in same project.
- Restart-safe recovery from persisted worker state.

## Runtime Selection

Worker runtime is selected per task:
- `agentType: diagnostic` or explicit `runtime: ollama` -> Ollama path
- default -> OpenClaw path
- if Ollama unavailable -> OpenClaw fallback

## Session and State Hygiene

- Worker state persisted in `~/.openclaw/worker-state.json` for recovery.
- Per-task worker session transcripts for agent `worker` are deleted after task completion.
- Awareness/workflow/failure state persisted in `state/*.json`.

## Configuration That Matters Most

- `max_concurrent_workers`: global worker capacity
- `poll_interval`: scheduling frequency
- `auto_review.*`: post-programmer review behavior
- `proactive.*`: proactive opportunity scanning limits
- `ollama_*`: local model runtime settings

See `docs/SETTINGS.md` for full settings.

## Code Landmarks

- `orchestrator/core/engine.py`
- `orchestrator/core/worker.py`
- `orchestrator/core/router.py`
- `orchestrator/core/registry.py`
- `orchestrator/services/prompter.py`
- `orchestrator/core/collaboration.py`
- `orchestrator/core/awareness.py`

## Related Docs

- `docs/agents/WORKER_RUNTIME.md`
- `docs/agents/WORKER_AGENTS.md`
- `docs/agents/AWARENESS.md`
- `WORKER_RULES.md`
