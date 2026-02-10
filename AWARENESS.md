# Awareness Monitor - Agent Situational Awareness

## Overview

The Awareness Monitor provides agents with situational awareness of the system state. This enables agents to make informed decisions based on what's happening across the system, not just their isolated task.

## What Agents Can See

When agents receive a task, they automatically get a **System Awareness Context** section in their prompt that includes:

### 1. Currently Active Work
- What other agents are working on
- How long tasks have been running
- Which projects are active

### 2. Recent Completions
- Tasks completed in the last hour
- Work distribution across projects
- Recent activity summary

### 3. Recent Failures
- Failed tasks and their details
- Patterns in failures (same project, same agent type)
- When failures occurred

### 4. System Metrics
- Queue depth (how much work is pending)
- System uptime
- Task completion/failure rates
- Average task duration
- Proactive work statistics

### 5. Observed Patterns
- High failure rates
- Queue backlogs building up
- Project or agent-specific issues
- System idle periods

### 6. Recommendations
- Suggestions based on current system state
- Context-appropriate actions
- Priority guidance

## Architecture

### Components

**`orchestrator/core/awareness.py`**
- `AwarenessMonitor`: Main monitoring class
- `WorkItem`: Represents a tracked work item
- `SystemMetrics`: System health metrics
- `AwarenessSnapshot`: Point-in-time system state

### Integration Points

1. **Engine** (`orchestrator/core/engine.py`)
   - Instantiates `AwarenessMonitor`
   - Passes it to `WorkerManager`

2. **WorkerManager** (`orchestrator/core/worker.py`)
   - Tracks work started/completed/failed events
   - Retrieves awareness context for prompts

3. **Prompter** (`orchestrator/services/prompter.py`)
   - Injects awareness context into agent prompts
   - Placed after engineering rules, before task details

### State Persistence

The awareness monitor persists state to:
- **File**: `~/.openclaw/state/awareness-state.json`
- **Contents**: Total counts, recent history, last update timestamp
- **Restart-safe**: State survives orchestrator restarts

## How It Works

### Work Lifecycle Tracking

```
1. Task assigned → WorkerManager.spawn_worker()
2. Worker starts → awareness.track_work_started()
3. Work completes → awareness.track_work_completed()
   OR
   Work fails → awareness.track_work_failed()
```

### Context Generation

When an agent is spawned:

```python
# In WorkerManager._async_spawn_openclaw_flow
awareness_context = awareness.format_context_for_agent(agent_type)

prompt = Prompter.build_task_prompt(
    task,
    project_id,
    rules=rules,
    workspace_path=workspace,
    agent_type=agent_type,
    awareness_context=awareness_context,  # ← Injected here
)
```

### Pattern Detection

The monitor automatically detects:
- **High failure rates** (>30% in last hour)
- **Queue buildup** (>10 tasks pending)
- **Project concentration** (multiple failures in same project)
- **Agent issues** (multiple failures with same agent type)
- **System idle** (no activity)

## Benefits

### For Agents

1. **Context-aware decisions**: Agents can see system load and adjust priorities
2. **Proactive work**: When idle, agents know it's a good time for improvements
3. **Failure awareness**: Agents can avoid problematic patterns or help debug
4. **Collaboration visibility**: See what other agents are working on

### For System Health

1. **Self-awareness**: The system knows its own state
2. **Pattern detection**: Automatically spots issues
3. **Load visibility**: Clear view of queue depth and capacity
4. **Audit trail**: Full history of work progression

## Example Awareness Context

```markdown
# System Awareness Context

*As of 2025-02-10 09:15 UTC*

## Currently Active

- **[my-project]** Refactor authentication module (programmer, 15m ago)

## Recent Completions (last 1h)

- api-service: 2 task(s)
- my-project: 1 task(s)

## Recent Failures (last 1h)

- ⚠️ [api-service] Fix database migration (programmer, 23m ago)

## System Metrics

- **Queue Depth:** 3 task(s)
- **Uptime:** 2h 15m
- **Completed:** 45 total, 8 last hour
- **Failed:** 2 total, 1 last hour
- **Avg Duration:** 12m
- **Proactive Work:** 2 today, 15 total

## Observed Patterns

- ⚠️ High failure rate in last hour (12%)

## Recommendations

- Consider reviewing recent failures for common root causes
```

## Configuration

Currently, awareness monitoring is always enabled when the orchestrator runs. Future configuration options could include:

- Awareness context verbosity level
- History window size
- Pattern detection thresholds
- Per-agent context filtering

## Testing

Tests are located in `tests/test_awareness.py` and cover:
- Work lifecycle tracking
- Failure detection
- Pattern recognition
- Context formatting
- State persistence
- Recommendation generation

Run tests:
```bash
.venv/bin/python -m pytest tests/test_awareness.py -v
```

## Future Enhancements

Potential improvements:

1. **Agent-specific filtering**: Tailor context based on agent capabilities
2. **Trend analysis**: Detect performance trends over time
3. **Cross-project insights**: Identify patterns across multiple projects
4. **Predictive alerts**: Warn about potential issues before they occur
5. **Context compression**: Summarize large histories for efficiency
6. **Awareness API**: Allow agents to query awareness state programmatically

## Related Components

- **Monitor** (`orchestrator/core/monitor.py`): LLM-powered proactive suggestions
- **Observer** (`orchestrator/core/observer.py`): Deterministic opportunity scanning
- **HeartbeatManager** (`orchestrator/core/heartbeat.py`): Morning briefs and status updates
- **Prompter** (`orchestrator/services/prompter.py`): Prompt construction

---

*Built as part of the multi-agent orchestrator system (ARCHITECTURE-V2.md)*
