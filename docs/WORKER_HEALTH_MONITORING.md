# Worker Health Monitoring

## Overview

The orchestrator includes automatic health monitoring for worker processes to detect and handle stuck workers. Workers can get stuck due to infinite loops, unresponsive services, memory leaks, or other issues.

## Features

### 1. Runtime Monitoring
Workers are monitored for excessive runtime:
- **Warning threshold**: 30 minutes (configurable)
- **Kill threshold**: 1 hour (configurable)

When a worker exceeds the warning threshold, a warning is logged. When it exceeds the kill threshold, the worker is automatically terminated.

### 2. Heartbeat Monitoring
Workers that stop responding are detected via heartbeat checks:
- **Heartbeat timeout**: 5 minutes (configurable)

The orchestrator updates worker heartbeats periodically when updating status. If a worker hasn't sent a heartbeat within the timeout window, it's considered stuck and terminated.

### 3. Graceful Termination
Stuck workers are terminated gracefully:
1. Send SIGTERM (graceful shutdown signal)
2. Wait 2 seconds
3. If still running, send SIGKILL (force termination)

### 4. Repository Cleanup
When a worker is killed, the orchestrator:
- Commits any uncommitted changes as WIP
- Pushes changes to prevent blocking future tasks
- Marks the task as failed with detailed reason

### 5. Dashboard Alerts
The dashboard API (`/workers/status`) includes health status for each worker:
- **healthy**: Worker is operating normally
- **warning**: Worker has been running longer than warning threshold
- **stuck**: Worker has exceeded kill threshold or heartbeat timeout

Stuck workers generate critical alerts in the dashboard.

## Configuration

Configure timeouts in `.lobs_settings.json`:

```json
{
  "worker_warning_timeout_sec": 1800,
  "worker_kill_timeout_sec": 3600,
  "worker_heartbeat_timeout_sec": 300
}
```

Or set via environment variables:
```bash
export LOBS_WORKER_WARNING_TIMEOUT_SEC=1800
export LOBS_WORKER_KILL_TIMEOUT_SEC=3600
export LOBS_WORKER_HEARTBEAT_TIMEOUT_SEC=300
```

## Monitoring

### Dashboard API

Query worker health via the dashboard API:

```bash
curl http://localhost:7171/workers/status
```

Response includes:
```json
{
  "activeWorkers": [
    {
      "taskId": "abc123...",
      "projectId": "my-project",
      "durationSeconds": 245,
      "heartbeatAgeSeconds": 12,
      "health": "healthy",
      "state": "running"
    }
  ],
  "alerts": [
    {
      "level": "warning",
      "message": "Worker for abc123ab has been running for 45min",
      "taskId": "abc123..."
    }
  ]
}
```

### Logs

Stuck worker events are logged at ERROR level:
```
ERROR Worker for task abc123ab (my-project) exceeded maximum runtime (65min > 60min). Killing worker.
```

## Failure Handling

When a worker is killed due to health monitoring:
1. Task is marked as `failed`
2. Failure reason indicates the specific issue:
   - `worker_stuck_exceeded_maximum_runtime_(60min)`
   - `worker_stuck_no_heartbeat_for_5min`
3. Auto-retry logic applies (with exponential backoff)
4. After max retries, task is marked as `blocked` and requires manual intervention

## Testing

Tests are located in `tests/test_worker_health.py`. Run with:

```bash
pytest tests/test_worker_health.py -v
```

## Implementation Details

### Worker State Tuple
Active workers are tracked with the following state:
```python
(process, project_id, log_file, start_time, agent_id, task_title, 
 agent_template, worker_id, session_label, last_heartbeat)
```

### Check Frequency
Worker health is checked on every engine poll cycle (default: 10 seconds).

### Heartbeat Updates
Heartbeats are updated whenever `_update_provider_status()` is called during normal status updates.

## Troubleshooting

### Workers Being Killed Prematurely

If workers are being killed before they complete:
1. Check if tasks genuinely need more time
2. Increase `worker_kill_timeout_sec` in settings
3. Review worker logs for actual progress

### Workers Not Being Killed

If stuck workers aren't being detected:
1. Verify configuration values are loaded correctly
2. Check orchestrator logs for health check messages
3. Ensure `check_workers()` is being called regularly

### False Heartbeat Timeouts

If workers are being killed for heartbeat timeouts but are actually progressing:
1. Increase `worker_heartbeat_timeout_sec`
2. Check if status updates are being blocked
3. Review system resource usage (CPU/memory pressure can delay updates)
