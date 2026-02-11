# Auto-Retry System

The orchestrator automatically retries tasks when failures are determined to be transient or fixable without human intervention.

## Overview

When a task fails, the retry system (Level 0 of escalation) checks if the failure matches known transient patterns. If so, it automatically resets the task to `not_started` with guidance appended to the task notes.

## Retry-Eligible Patterns

### Network Timeout
- **Patterns**: timeout, connection reset, connection refused, ETIMEDOUT, etc.
- **Max Retries**: 3
- **Guidance**: "Network timeout detected. Retrying - the service may have been temporarily unavailable."

### Rate Limit
- **Patterns**: rate limit, too many requests, 429, quota exceeded, throttle
- **Max Retries**: 3
- **Guidance**: "Rate limit hit. Retrying after backoff - the API should be available again."

### Transient API Error
- **Patterns**: 502, 503, 504, internal server error, temporary failure
- **Max Retries**: 3
- **Guidance**: "Transient API error detected. Retrying - the service should recover."

### Lock Conflict
- **Patterns**: resource locked, lock timeout, could not acquire lock
- **Max Retries**: 3
- **Guidance**: "Resource lock conflict. Retrying - the lock should be released."

### Missing Context
- **Patterns**: no such file, file not found, module not found, import failed
- **Max Retries**: 2 (fewer for context issues)
- **Guidance**: "Missing dependency or file. Check if dependencies were installed correctly..."

## Retry Loop Prevention

The system prevents infinite retry loops in two ways:

1. **Max Retry Count**: Tasks are not retried more than 3 times (or pattern-specific limit)
2. **Same Root Cause Detection**: If the same pattern fails twice in a row, no further retries are attempted

## Task Metadata

Tasks track retry state in the following fields:

- `retryCount`: Number of retry attempts (integer)
- `lastRetryReason`: Pattern name of the last retry (e.g., "network_timeout")
- `retryHistory`: Array of retry entries with:
  - `attempt`: Retry attempt number
  - `reason`: Pattern name that triggered the retry
  - `failedAt`: ISO timestamp of the failure
  - `errorPreview`: First 500 chars of the error log

## Retry Flow

```
Task Fails
    ↓
Check if retry-eligible (Level 0)
    ├─> Yes: Reset to not_started, append guidance to notes
    └─> No: Create alert, proceed with Level 1 escalation
```

## Implementation

The retry system is implemented in:
- **`orchestrator/core/retry.py`**: Core retry logic and pattern matching
- **`orchestrator/core/escalation.py`**: Integration with escalation (Level 0)
- **`orchestrator/models/tasks.py`**: Task model with retry fields

## Example Task After Retry

```json
{
  "id": "task-123",
  "workState": "not_started",
  "retryCount": 1,
  "lastRetryReason": "network_timeout",
  "retryHistory": [
    {
      "attempt": 1,
      "reason": "network_timeout",
      "failedAt": "2024-02-11T03:30:00Z",
      "errorPreview": "Error: Connection timeout after 30 seconds"
    }
  ],
  "notes": "Original task notes\n\n---\n**Retry #1** (network_timeout):\nNetwork timeout detected. Retrying - the service may have been temporarily unavailable.\n\nPrevious error:\n```\nError: Connection timeout after 30 seconds\n```"
}
```

## Testing

Tests are located in `tests/test_retry.py` and cover:
- Pattern matching
- Retry eligibility checks
- Max retry enforcement
- Retry loop prevention
- Retry preparation and metadata updates

Run tests with:
```bash
pytest tests/test_retry.py -v
```

## Configuration

Retry patterns and max counts are defined in `orchestrator/core/retry.py`. To customize:

1. Edit `RETRY_PATTERNS` to add/modify patterns
2. Adjust `MAX_RETRY_COUNT` for global max
3. Set `max_retries` on individual patterns for pattern-specific limits
