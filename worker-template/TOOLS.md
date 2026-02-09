# TOOLS.md - Worker Tools

## Available Tools

You have standard OpenClaw tools:

- File operations (read, write, edit)
- Shell commands (exec)
- Web search and fetch

## What You Don't Need

- **Git commands** — orchestrator handles git pull/commit/push
- **State updates** — orchestrator marks tasks complete/failed
- **Messaging** — you don't message users
- **Cron/reminders** — you're task-scoped

## Work Summary

Optionally write a brief description of your changes to `.work-summary`:

```bash
echo "Added user authentication with JWT tokens" > .work-summary
```

This gets used as the commit message. If you don't write one, the orchestrator generates a message from the diff.

## If Blocked

```bash
echo "BLOCKED: Need API credentials for external service" > .work-summary
exit 1
```

---

Keep it simple. Do the work, exit.
