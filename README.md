# Lobs Orchestrator

Long-running Python service that manages scheduling, concurrency, and task execution for Lobs.

## Core Principles

- **Scripts own control.** LLMs do bounded work. Humans approve intent.
- **Deterministic & Restart-safe.** The filesystem is the recovery log.
- **Single Writer Pattern.** Exactly one component (`ControlManager`) writes to the control repo to avoid git races.
- **Multi-Worker Support.** Multiple workers can run concurrently (default: 5). Tasks are assigned as workers become available.

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

**IMPORTANT:** This orchestrator supports multiple concurrent workers (default: 5).

### How Work Distribution Works

1. **Scanner** identifies all eligible work items
2. **Engine** loops through eligible items
3. **WorkerManager** spawns workers up to the configured limit:
   - If **slots available**: spawns worker for the task
   - If **at capacity**: task remains in provider queue
4. Next engine iteration picks up queued work as workers complete

### Multi-Worker Guarantees

- Configurable concurrent task execution (default: 5 workers)
- Each worker maintains independent session state
- Clean session state between tasks (session files cleared after each task)
- Tasks are processed in parallel when multiple eligible items exist

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

### Running the Orchestrator

```bash
python3 main.py
```

## Configuration

Configuration is managed in `orchestrator/config.py` and `.lobs_settings.json`. Key settings:

- `POLL_INTERVAL`: How often the orchestrator scans for work (default: 10s). This is also the queue processing interval.
- `BASE_DIR`: Parent directory containing the project repositories.
- `openclaw_executable`: Path to openclaw binary (default: "openclaw")

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
