# Orchestrator V2 Configuration Guide

## Overview

This guide covers configuration options for Orchestrator V2 multi-agent features, including automatic code review, failure pattern detection, and agent collaboration.

## Auto-Review Configuration

Enable automatic code review after programmer task completion:

```json
{
  "auto_review": {
    "enabled": true,
    "review_all_tasks": false
  }
}
```

**Options:**
- `enabled` (boolean): Enable/disable automatic code review. Default: `false`
- `review_all_tasks` (boolean): If true, reviews all programmer tasks. If false, only reviews tasks that are part of an initiative. Default: `false`

**How it works:**
1. When a programmer task completes successfully
2. If the task is part of an initiative (or `review_all_tasks` is true)
3. A review task is automatically created and assigned to the reviewer agent
4. The review task is linked to the original task via collaboration metadata
5. If the reviewer finds issues, they can create follow-up tasks via handoffs

## Proactive Work Configuration

Configure proactive work discovery and suggestions:

```json
{
  "proactive": {
    "enabled": true,
    "min_priority": "NORMAL",
    "max_daily_tasks": 5,
    "quiet_hours_start": "22:00",
    "quiet_hours_end": "08:00"
  }
}
```

**Options:**
- `enabled` (boolean): Enable proactive work discovery. Default: `true`
- `min_priority` (string): Minimum priority for proactive tasks. Values: `URGENT`, `HIGH`, `NORMAL`, `LOW`, `BACKGROUND`. Default: `NORMAL`
- `max_daily_tasks` (number): Maximum proactive tasks to create per day. Default: `5`
- `quiet_hours_start/end` (string): Time window (HH:MM format) to skip proactive work. Optional.

## Failure Pattern Detection

The Monitor automatically detects failure patterns and creates diagnostic tasks. No configuration required.

**Pattern Detection Rules:**
- **Project Failures**: 3+ failures in same project within 24h → Researcher investigates
- **Agent Failures**: 4+ failures with same agent type within 24h → Researcher investigates  
- **Error Patterns**: 3+ failures with same error keyword within 24h → Researcher investigates

**Diagnostic Tasks:**
- Created automatically in `lobs-control` project
- Assigned to researcher agent
- Tagged with `diagnostic` and `auto-generated`
- Include severity level (`high` or `medium`)

## Agent Collaboration

Agents collaborate via handoff files. No configuration required.

**Creating a Handoff:**

Agents create `.handoffs/{unique-id}.json` files in the project directory:

```json
{
  "to": "reviewer",
  "initiative": "user-auth-system",
  "title": "Review authentication middleware",
  "context": "Please review the JWT implementation in auth/middleware.py",
  "acceptance": "Code review complete with feedback",
  "files": ["auth/middleware.py", "tests/test_auth.py"]
}
```

**Valid Handoff Patterns:**
- Architect → Programmer (design to implementation)
- Architect → Researcher (strategic research needed)
- Reviewer → Programmer (issues found, fixes needed)
- Reviewer → Architect (systemic issues, redesign needed)
- Researcher → Architect (important findings for strategy)

**Handoff Processing:**
1. Agent completes task and exits
2. Orchestrator finds `.handoffs/*.json` files
3. CollaborationManager validates and creates derived tasks
4. New tasks are routed to destination agent
5. Handoff files are cleaned up

## Workflow Tracking

The WorkflowEngine automatically tracks initiatives and work chains. No configuration required.

**Initiative Metadata:**

Tasks in the same initiative are linked:

```json
{
  "id": "TASK-123",
  "title": "Implement JWT middleware",
  "initiative": "user-auth-system",
  "agent": "programmer",
  "parentTaskId": "TASK-100"
}
```

**Initiative Progress:**

Check initiative status via the orchestrator API or logs:

```bash
# View active initiatives
tail -f logs/orchestrator.log | grep WORKFLOW
```

**Dependency Blocking:**

Tasks with `parentTaskId` or explicit `dependsOn` are blocked until dependencies complete:

```json
{
  "id": "TASK-456",
  "title": "Document auth API",
  "initiative": "user-auth-system",
  "dependsOn": ["TASK-123", "TASK-124"]
}
```

## Error Recovery Configuration

Error recovery is automatic with sensible defaults. No configuration required.

**Retry Logic:**
- Attempt 1: Retry after 5 minutes
- Attempt 2: Retry after 15 minutes
- Attempt 3: Retry after 1 hour
- After 3 failures: Task marked as `blocked`, alert created

**Git Conflict Recovery:**
- Automatic rebase on push rejection
- Up to 3 push retry attempts
- Conflict detection and abort if unresolvable
- Exponential backoff between attempts

## Example: Full V2 Configuration

```json
{
  "auto_review": {
    "enabled": true,
    "review_all_tasks": false
  },
  "proactive": {
    "enabled": true,
    "min_priority": "NORMAL",
    "max_daily_tasks": 5,
    "quiet_hours_start": "22:00",
    "quiet_hours_end": "08:00"
  },
  "max_workers": 3,
  "poll_interval": 10
}
```

Add this to `.lobs_settings.json` in the orchestrator directory.

## Monitoring V2 Features

**Check failure patterns:**
```bash
cat state/failure-history.json | jq '.failures[-10:]'
```

**Check diagnostic tasks:**
```bash
cat state/diagnostic-tasks.json | jq '.tasks'
```

**Check initiative progress:**
```bash
cat state/workflow-state.json | jq '.initiatives'
```

**Check collaboration state:**
```bash
cat state/collaboration-state.json | jq '.initiatives'
```

## Best Practices

1. **Start with auto-review disabled** - Enable once you trust the reviewer agent's prompts
2. **Set conservative proactive limits** - Start with `max_daily_tasks: 3` to avoid overwhelming the system
3. **Monitor failure patterns** - Check `failure-history.json` regularly for recurring issues
4. **Use quiet hours** - Prevent proactive work during maintenance windows or off-hours
5. **Review diagnostic tasks** - Diagnostic tasks surface real issues; don't dismiss them lightly

## Troubleshooting

**Auto-review not triggering:**
- Check `auto_review.enabled` is `true` in config
- Verify task has an `initiative` field (or set `review_all_tasks: true`)
- Check logs for `[AUTO-REVIEW]` messages

**Handoffs not processing:**
- Verify `.handoffs/` directory is in project root
- Check handoff JSON is valid (use `jq . < file.json`)
- Verify `to` field matches a known agent type
- Check logs for `[HANDOFF]` messages

**Failure patterns not detected:**
- Monitor runs every 10 minutes by default
- Need 3+ failures within 24h for detection
- Check `state/failure-history.json` exists and has entries

**Workflow not tracking:**
- Verify tasks have `initiative` field set
- Check `state/workflow-state.json` for computed state
- Tasks update every 1 minute by default
