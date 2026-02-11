# Worker Agents

Current worker-agent model.

## Summary

The orchestrator uses one OpenClaw agent identity (`worker`) and swaps workspace context per task using agent templates.

- Runtime agent ID: `worker`
- Agent templates: `agents/<type>/`
- Template types: `programmer`, `reviewer`, `researcher`, `writer`, `architect`

## How Template Selection Works

1. Task provides explicit `agent` OR router infers one from task text.
2. `WorkerManager` normalizes the template type.
3. `AgentRegistry` loads `agents/<type>/` files.
4. Template files are written into `~/.openclaw/workspace-worker/` before spawn.
5. OpenClaw runs `--agent worker` with template-specific prompt context.

Fallback order:
- requested type
- `programmer`
- legacy `worker-template/` sync (last resort)

## Required Template Files

Each `agents/<type>/` directory must include:

- `AGENTS.md`
- `SOUL.md`
- `TOOLS.md`
- `IDENTITY.md`
- `USER.md`

`IDENTITY.md` is parsed for:
- model
- capabilities
- proactive tags

## Provisioning Model

Provisioning is handled by `AgentManager` for OpenClaw registration/workspace bootstrap.

- agent dir: `~/.openclaw/agents/worker/`
- workspace: `~/.openclaw/workspace-worker/`
- config registration: `~/.openclaw/openclaw.json`

Legacy template sync still exists for compatibility (`worker-template/`).

## Why This Design

- Keeps one stable OpenClaw agent identity.
- Supports many behavioral roles without provisioning separate runtime agents.
- Simplifies operational management while preserving role-specific prompts.

## Code References

- `orchestrator/core/registry.py`
- `orchestrator/core/worker.py`
- `orchestrator/core/agents.py`
- `orchestrator/core/router.py`
- `agents/`
