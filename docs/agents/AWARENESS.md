# Awareness Monitor

Agent situational awareness for current orchestrator runtime.

## What It Provides

Awareness context is injected into worker prompts with:

- active work summary
- recent completions/failures
- queue depth and throughput metrics
- detected patterns (backlog, failure concentration, etc.)
- lightweight recommendations

## Integration

- `Engine` initializes `AwarenessMonitor` and passes it to `WorkerManager`.
- `WorkerManager` tracks start/success/failure events.
- `Prompter` injects formatted awareness context into task prompts.

## Persistence

- file: `~/.openclaw/state/awareness-state.json`
- stores counts, recent history, and update timestamps
- survives orchestrator restarts

## Pattern Detection (Current)

- elevated recent failure rates
- backlog growth / queue pressure
- repeated failures by project or agent type
- idle periods

## Why It Exists

- reduces isolated-agent blind spots
- improves prioritization in busy periods
- helps agents avoid repeating recently failed work patterns

## Tests

- `tests/test_awareness.py`

## Code References

- `orchestrator/core/awareness.py`
- `orchestrator/core/engine.py`
- `orchestrator/core/worker.py`
- `orchestrator/services/prompter.py`
