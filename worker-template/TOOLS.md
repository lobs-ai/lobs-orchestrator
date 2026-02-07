# TOOLS.md - Worker Tools

## Available Tools

You have standard OpenClaw tools:

- File operations (read, write, edit)
- Shell commands (exec)
- Web search and fetch
- Git operations

## Constraints

- No messaging tools (you don't message users)
- No cron/reminders (you're task-scoped)

## Control Operations

To update lobs-control state, create JSON files in `state/control-ops/`:

**Update task:**

```json
{"type": "update_task", "task_id": "...", "updates": {"workState": "completed"}}
```

**Add inbox item:**

```json
{"type": "add_inbox_item", "item": {"title": "...", "body": "...", "type": "suggestion"}}
```

---

Keep it simple. Use what you need, nothing more.
