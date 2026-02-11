# AGENTS.md

## Purpose

Operational quick reference for orchestrator maintainers.

- Keep this file small and actionable.
- Put deep detail in `docs/`.
- Worker behavior rules belong in `WORKER_RULES.md` (synced into worker workspace).

---

## Core Operating Rules

**Scripts control. Agents execute. Humans approve.**

- Agents are task-scoped executors.
- One unit of work per invocation.
- No proactive action outside explicit tasks unless explicitly configured by orchestrator features.
- Stop and report when blocked or unclear.

---

## Runtime Model (Must-Know)

This orchestrator is **multi-agent + multi-worker**.

- Agent templates live in `agents/<type>/` (programmer, reviewer, researcher, writer, architect).
- Work is routed to an agent type via explicit task `agent` or router rules.
- Workers run concurrently up to configured capacity (`max_concurrent_workers`).
- One worker per project at a time is enforced with project locks.

Implementation anchors:
- `orchestrator/core/worker.py`
- `orchestrator/core/engine.py`
- `orchestrator/core/router.py`
- `orchestrator/core/registry.py`

---

## Session Hygiene (Must-Know)

After each task, worker session files are deleted from:

- `~/.openclaw/agents/worker/sessions/*.jsonl`
- `~/.openclaw/agents/worker/sessions/sessions.json`

Use direct file cleanup (`_cleanup_worker_session`) to avoid broad resets.

---

## Worker Template Sync

Before each task spawn, template files are synced:

- Source: `worker-template/`
- Destination: `~/.openclaw/workspace-worker/`
- Trigger: worker spawn flow (`_async_spawn_openclaw_flow`)

If updating worker instructions:
1. Edit root files (usually `WORKER_RULES.md` and relevant template docs).
2. Run `scripts/sync-worker-template.sh`.
3. Next spawn picks up changes automatically.

---

## Git Rule (Mandatory)

Before making changes:

```bash
git pull --rebase
```

---

## Minimal Doc Loading Guide

Read only what the current change needs:

- `docs/ARCHITECTURE.md`
When changing scheduling, concurrency, routing, lifecycles, or control flow.

- `WORKER_RULES.md`
When changing worker prompt rules, output style, scope limits, or `.work-summary` behavior.

- `docs/agents/WORKER_AGENTS.md`
When changing provisioning, template sync flow, worker registration, or agent-template loading.

- `docs/agents/WORKER_RUNTIME.md`
When changing queueing/capacity behavior, locks, runtimes, lifecycle states, or session cleanup.

- `docs/agents/AWARENESS.md`
When changing awareness context, metrics, pattern detection, prompt injection, or state persistence.

---

## Code Pointers

- Worker runtime/lifecycle: `orchestrator/core/worker.py`
- Engine assignment loop: `orchestrator/core/engine.py`
- Agent provisioning/registration: `orchestrator/core/agents.py`
- Agent registry/templates: `orchestrator/core/registry.py`, `agents/`
- Prompt construction: `orchestrator/services/prompter.py`
- Worker template source: `worker-template/`
- Worker management CLI: `bin/manage-workers`

---

## Maintenance Note

If detail grows here, move it into `docs/` and keep `AGENTS.md` as the short entrypoint.
