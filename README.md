# Lobs Orchestrator

Long-running Python service that manages scheduling, concurrency, and task execution for Lobs.

## Core Principles

- **Scripts own control.** LLMs do bounded work. Humans approve intent.
- **Deterministic & Restart-safe.** The filesystem is the recovery log.
- **Single Writer Pattern.** Exactly one component (`ControlManager`) writes to the control repo to avoid git races.
- **Multi-Worker Support.** Multiple workers can run concurrently (default: 3, configurable). Tasks are assigned as workers become available.

## Architecture

- **Engine:** Main loop that polls for state changes and triggers work.
- **ControlManager:** Manages git operations and applies state updates serially.
- **WorkerManager:** Manages multiple concurrent worker subprocesses.
  - Spawns via `openclaw agent --agent worker`
  - Supports concurrent execution (default max_workers=5)
  - Tasks are assigned to available workers
  - Session reset between tasks for clean state
- **Scanner:** Purely scripted fact detection (tasks, requests, failures).

## Queueing & Concurrency

**IMPORTANT:** This orchestrator supports multiple concurrent workers (default: 3, configurable).

### How Work Distribution Works

1. **Scanner** identifies all eligible work items
2. **Engine** loops through eligible items
3. **WorkerManager** spawns workers up to the configured limit:
   - If **slots available**: spawns worker for the task
   - If **at capacity**: task remains in provider queue
4. Next engine iteration picks up queued work as workers complete

### Multi-Worker Guarantees

- Configurable concurrent task execution (default: 3 workers, adjustable via `--max-concurrent-workers`)
- Each worker maintains independent session state
- Clean session state between tasks (session files cleared after each task)
- Tasks are processed in parallel when multiple eligible items exist

### Resource Control

Control concurrency to manage resource usage and API costs:

```bash
# Set to 1 for minimal resource usage
python3 setup_config.py --max-concurrent-workers 1

# Increase for higher throughput (if resources allow)
python3 setup_config.py --max-concurrent-workers 10
```

### Worker Lifecycle

```
Task Assigned → Syncing (git pull) → Running (OpenClaw agent) → Finalizing (git commit/push) → Idle
                     ↓                      ↓                           ↓
                Lock acquired          Lock active                Lock released
```

## Getting Started

### Prerequisites

- Python 3.11+
- `lobs-control` repository cloned in the same parent directory.

### Initial Setup

The orchestrator provides an interactive setup wizard to configure required components:

```bash
# Run the setup wizard (recommended for first-time setup)
python3 main.py --setup-wizard

# Check setup status (what's configured vs missing)
python3 main.py --setup-status

# Configure individual steps without the full wizard
python3 main.py --configure base_dir
python3 main.py --configure control_repo
python3 main.py --configure orchestrator_repo
python3 main.py --configure openclaw_executable
```

**Skipping Setup Steps:**

The setup wizard allows you to skip any step by entering `skip` or `later` when prompted. The orchestrator will work with limited functionality without repository configuration - it just won't have any projects to work on until repos are added later.

The setup wizard tracks completion state persistently in `.lobs_setup_state.json`, so you can:
- Skip steps during initial setup and complete them later
- Re-run individual configuration steps without redoing everything
- Check what's configured vs missing at any time
- Run the orchestrator with partial configuration (e.g., without repos)

### Running the Orchestrator

```bash
python3 main.py
```

## Configuration

Configuration is managed in `orchestrator/config.py` and `.lobs_settings.json`. Key settings:

- `POLL_INTERVAL`: How often the orchestrator scans for work (default: 10s). This is also the queue processing interval.
- `MAX_WORKERS`: Maximum concurrent workers across all projects (default: 3). Controls resource usage and API costs.
- `BASE_DIR`: Parent directory containing the project repositories.
- `openclaw_executable`: Path to openclaw binary (default: "openclaw")

To adjust worker concurrency:
```bash
python3 setup_config.py --max-concurrent-workers 5
```

### Worker Configuration

The single worker agent is configured at:
- **Agent Config**: `~/.openclaw/openclaw.json` (agent registration)
- **Workspace**: `~/.openclaw/workspace-worker/` (agent context files)
- **Templates**: `worker-template/` in this repo (copied to workspace on provision)

To change the worker's model, edit `~/.openclaw/agents/worker/config.json`:
```json
{
  "model": "anthropic/claude-opus-4-5"
}
```

### Graceful Degradation

**By default, the orchestrator continues running even if optional tools are missing.** Missing tools don't block startup; they just disable specific features:

- No `node`/`npm` → JavaScript/TypeScript checks disabled
- No `mypy` → Python type checking disabled  
- No `gh` → GitHub features unavailable
- No `tsc` → TypeScript compilation checks disabled

You'll see helpful warnings like:
```
⚠️  Node.js not found - JavaScript/TypeScript features disabled. Install: https://nodejs.org
```

**Strict Mode:** To fail startup if tools are missing (production environments):
```json
{
  "strict_prereqs": true
}
```

**Override Detection:** If tool detection fails incorrectly (tools are installed but not found):
```json
{
  "skip_prereqs": true
}
```
or skip specific tools:
```json
{
  "skip_prereqs_list": ["node", "gh"]
}
```

See [SETTINGS.md](SETTINGS.md) for complete documentation.

## Monitoring & Dashboard API

The orchestrator exposes a REST API for monitoring worker status and system health.

### Worker Status Endpoint

**Endpoint:** `GET http://localhost:7171/workers/status`

Returns real-time information about:
- Active workers (task ID, project, agent, duration)
- Queue depth and queued tasks
- Recent completions and failures
- System metrics (uptime, completion counts, average duration)
- Alerts (high queue depth, long-running workers, etc.)

**Example:**

```bash
curl http://localhost:7171/workers/status | jq .
```

**Response:**

```json
{
  "ok": true,
  "activeWorkers": [
    {
      "taskId": "ABC123...",
      "projectId": "my-project",
      "taskTitle": "Implement feature X",
      "agentId": "programmer",
      "durationSeconds": 245,
      "state": "running"
    }
  ],
  "queueDepth": 3,
  "metrics": {
    "totalCompleted": 42,
    "totalFailed": 3,
    "activeCount": 1,
    "averageDurationSeconds": 210
  },
  "alerts": []
}
```

**Configuration:**

Set custom port/host in `.lobs_settings.json`:

```json
{
  "dashboard_api_port": 7171,
  "dashboard_api_host": "127.0.0.1"
}
```

For detailed API documentation, see [docs/WORKER_STATUS_API.md](docs/WORKER_STATUS_API.md).

## Development

### Running Tests

Install development dependencies:

```bash
pip install -r requirements-dev.txt
```

Run tests:

```bash
# Run all tests (basic)
pytest

# Run specific test file
pytest tests/test_config.py

# Run with extra verbose output
pytest -vv

# Run tests matching a pattern
pytest -k "test_config"
```

Run tests with coverage:

```bash
# Run tests with coverage (requires pytest-cov from requirements-dev.txt)
pytest --cov=orchestrator --cov-report=term-missing

# Generate HTML coverage report
pytest --cov=orchestrator --cov-report=html
# Then open htmlcov/index.html in a browser

# Generate multiple report formats
pytest --cov=orchestrator --cov-report=term-missing --cov-report=html --cov-report=xml
```

### Coverage Configuration

Coverage is configured in `.coveragerc` and `pytest.ini`:

- **Minimum coverage**: Tests measure coverage of the `orchestrator` package
- **Branch coverage**: Enabled for more detailed analysis
- **Reports**: Terminal summary, HTML report (`htmlcov/`), and XML for CI
- **Exclusions**: Test files, virtual environments, and common pragma patterns

### Continuous Integration

GitHub Actions workflow (`.github/workflows/test.yml`) runs tests automatically on:
- Push to `main`/`master` branches
- Pull requests
- Tests run on Python 3.11 and 3.12
- Coverage reports uploaded to Codecov (if configured)

## Control Operations

To request a state change (e.g., update a task), other components or workers should create a JSON file in `lobs-control/state/control-ops/`.

Example `update_task` operation:

```json
{
  "type": "update_task",
  "task_id": "UUID",
  "updates": {
    "workState": "completed"
  }
}
```

The orchestrator will pick this up, apply it, and push the changes.
