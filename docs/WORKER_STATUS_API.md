# Worker Status API

## Overview

The Worker Status API provides real-time visibility into the orchestrator's worker lifecycle, queue state, and system health. This endpoint is useful for monitoring, debugging, and building dashboards.

## Endpoint

```
GET http://localhost:7171/workers/status
```

Default port is 7171, configurable via `.lobs_settings.json`:

```json
{
  "dashboard_api_port": 7171,
  "dashboard_api_host": "127.0.0.1"
}
```

## Response Schema

### Success Response (200 OK)

```json
{
  "ok": true,
  "activeWorkers": [
    {
      "taskId": "ABC123...",
      "projectId": "my-project",
      "taskTitle": "Implement feature X",
      "agentId": "programmer",
      "agentTemplate": "programmer",
      "workerId": "programmer-1234567890-ABC12345",
      "durationSeconds": 245,
      "state": "running"
    }
  ],
  "queueDepth": 3,
  "queuedTasks": [
    {
      "taskId": "DEF456...",
      "projectId": "another-project",
      "title": "Fix bug Y",
      "kind": "task"
    }
  ],
  "recentCompletions": [
    {
      "taskId": "GHI789...",
      "projectId": "my-project",
      "title": "Completed task",
      "agentType": "programmer",
      "completedAt": "2024-01-15T10:30:00Z",
      "durationSeconds": 180
    }
  ],
  "recentFailures": [
    {
      "taskId": "JKL012...",
      "projectId": "my-project",
      "title": "Failed task",
      "agentType": "programmer",
      "completedAt": "2024-01-15T09:15:00Z"
    }
  ],
  "metrics": {
    "uptimeSeconds": 3600,
    "totalCompleted": 42,
    "totalFailed": 3,
    "averageDurationSeconds": 210,
    "activeCount": 1,
    "pendingCount": 0,
    "queueDepth": 3
  },
  "alerts": [
    {
      "level": "warning",
      "message": "Queue depth is high: 15 task(s) waiting"
    }
  ],
  "timestamp": "2024-01-15T10:45:30Z"
}
```

### Error Response (503 Service Unavailable)

Returned when the orchestrator is not fully initialized or not available:

```json
{
  "ok": false,
  "error": "Orchestrator not available"
}
```

## Field Descriptions

### activeWorkers

Array of currently running workers. Each worker includes:

- `taskId`: Unique task identifier
- `projectId`: Project the task belongs to
- `taskTitle`: Human-readable task description
- `agentId`: Agent identifier (e.g., "programmer", "researcher")
- `agentTemplate`: Agent template type
- `workerId`: Unique worker instance ID
- `durationSeconds`: How long the worker has been running
- `state`: Worker state ("running", "syncing/finalizing")

### queueDepth

Total number of eligible tasks waiting to be assigned to workers.

### queuedTasks

Details of the next 10 queued tasks (for visibility). Includes task ID, project, title, and kind.

### recentCompletions

Last 10 successfully completed tasks, including:

- Task details
- Completion timestamp
- Duration in seconds

### recentFailures

Last 5 failed tasks for debugging.

### metrics

System-level metrics:

- `uptimeSeconds`: Orchestrator uptime
- `totalCompleted`: Lifetime completed task count
- `totalFailed`: Lifetime failed task count
- `averageDurationSeconds`: Average task duration (last 50 tasks)
- `activeCount`: Number of currently running workers
- `pendingCount`: Number of workers in syncing/finalizing state
- `queueDepth`: Number of queued tasks

### alerts

Array of system alerts with severity levels:

- `level`: "info", "warning", or "error"
- `message`: Human-readable alert message

Common alerts:

- High queue depth (>10 tasks)
- Recent failures
- Long-running workers (>30 minutes)

## Usage Examples

### cURL

```bash
curl http://localhost:7171/workers/status | jq .
```

### Python

```python
import requests

response = requests.get("http://localhost:7171/workers/status")
data = response.json()

print(f"Active workers: {len(data['activeWorkers'])}")
print(f"Queue depth: {data['queueDepth']}")
print(f"Total completed: {data['metrics']['totalCompleted']}")
```

### JavaScript

```javascript
fetch('http://localhost:7171/workers/status')
  .then(res => res.json())
  .then(data => {
    console.log('Active workers:', data.activeWorkers.length);
    console.log('Queue depth:', data.queueDepth);
    console.log('Alerts:', data.alerts);
  });
```

## Use Cases

### Monitoring Dashboard

Poll this endpoint periodically (e.g., every 5-10 seconds) to build a live status dashboard showing:

- Current worker activity
- Queue depth trends
- Completion/failure rates
- System health alerts

### CI/CD Integration

Check worker status before deploying changes:

```bash
#!/bin/bash
status=$(curl -s http://localhost:7171/workers/status | jq -r '.queueDepth')
if [ "$status" -gt 0 ]; then
  echo "Warning: $status tasks queued"
fi
```

### Debugging

When investigating issues:

1. Check `activeWorkers` for stuck/long-running tasks
2. Review `recentFailures` for patterns
3. Monitor `alerts` for system problems
4. Check `queueDepth` for backlog issues

## Rate Limiting

No rate limiting currently enforced. For production use, consider:

- Polling every 5-10 seconds (not per second)
- Using efficient JSON parsing
- Caching results when appropriate

## Security

The API currently listens on `127.0.0.1` by default (localhost only). To expose externally:

1. Change `dashboard_api_host` in settings
2. Add authentication/authorization (not implemented yet)
3. Use a reverse proxy (nginx, Caddy) with TLS

## Future Enhancements

Planned features:

- WebSocket support for real-time updates
- Historical metrics (hourly/daily aggregates)
- Filtering by project/agent type
- Task log streaming
- Worker control actions (pause/resume/cancel)
