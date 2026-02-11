# Ollama Integration

The orchestrator supports using **Ollama** as an alternative runtime for lightweight diagnostic and error-fixing tasks.

## Why Ollama?

- **Faster for diagnostics**: Simpler execution without full agent workspace overhead
- **Lower-level work**: Error analysis, log inspection, quick fixes
- **Local execution**: Runs locally without cloud API calls
- **Stateless**: No session management needed

## When Ollama is Used

The orchestrator automatically uses Ollama for tasks with:
- `agentType: "diagnostic"` - Error fixing and diagnostics

All other tasks (regular work items with `kind: "task"`) use OpenClaw.

You can also explicitly set the runtime:
```json
{
  "id": "task-123",
  "runtime": "ollama",  // Force Ollama
  "agentType": "diagnostic",
  "title": "Fix authentication error",
  "notes": "User reported 401 errors..."
}
```

## Setup

### 1. Install Ollama

```bash
# macOS/Linux
curl -fsSL https://ollama.com/install.sh | sh

# Or download from https://ollama.com
```

### 2. Pull a Model

```bash
# Recommended for code tasks
ollama pull llama3.1

# Or try codellama for coding-focused work
ollama pull codellama

# Or use a smaller model
ollama pull llama3.2
```

### 3. Configure the Orchestrator

Add to `.lobs_settings.json`:

```json
{
  "ollama_url": "http://localhost:11434",
  "ollama_model": "llama3.1"
}
```

**Defaults:**
- URL: `http://localhost:11434` (Ollama's default)
- Model: `llama3.1`

### 4. Verify Setup

Check if Ollama is running:

```bash
curl http://localhost:11434/api/tags
```

You should see a list of available models.

## How It Works

### Task Flow (Ollama)

1. **Sync repo**: `git pull --rebase` in project directory
2. **Build prompt**: Same prompt builder as OpenClaw
3. **Execute**: Direct API call to Ollama (streaming)
4. **Finalize**: Commit and push changes (same as OpenClaw)
5. **Complete**: Update task status

### Differences from OpenClaw

| Feature | OpenClaw | Ollama |
|---------|----------|--------|
| Agent workspace | ✅ Yes | ❌ No |
| Session management | ✅ Yes | ❌ No |
| Tool use | ✅ Full suite | ❌ Limited |
| Execution | Subprocess | Direct API |
| Best for | Full tasks | Diagnostics |

## Example Use Cases

### ✅ Good for Ollama
- Analyzing error logs
- Diagnosing test failures
- Quick configuration fixes
- Code review/analysis
- Documentation review

### ❌ Better with OpenClaw
- Feature implementation
- Complex refactoring
- Multi-file changes
- Tasks requiring tools (git, npm, docker)
- Long-running work

## Troubleshooting

### Ollama Not Available

If Ollama is not running, the orchestrator will automatically fall back to OpenClaw with a warning:

```
WARNING: Ollama not available, falling back to OpenClaw for diagnostic task
```

### Check Ollama Status

```bash
# See running models
curl http://localhost:11434/api/ps

# List available models
curl http://localhost:11434/api/tags

# Test generation
curl http://localhost:11434/api/generate -d '{
  "model": "llama3.1",
  "prompt": "Why is the sky blue?",
  "stream": false
}'
```

### Change Model

Update settings:
```json
{
  "ollama_model": "codellama"  // Or any installed model
}
```

## Logs

Ollama execution logs are written to the same location as OpenClaw logs:

```
~/lobs/lobs-control/state/worker-results/{task_id}.log
```

Format:
```
=== Ollama Task Execution ===
Task ID: task-123
Model: llama3.1
Project: my-project
Agent Type: diagnostic

=== Prompt ===
[Full prompt...]

=== Response ===
[Streamed response...]

=== Completed ===
```

## Configuration Reference

### Settings (.lobs_settings.json)

```json
{
  "ollama_url": "http://localhost:11434",     // Ollama API endpoint
  "ollama_model": "llama3.1",                 // Default model
  "openclaw_executable": "openclaw",          // OpenClaw command
  "base_dir": "/Users/rafe/other/lobs",      // Projects directory
  "poll_interval": 10                         // Orchestrator poll interval
}
```

### Task Schema

```json
{
  "id": "task-xyz",
  "kind": "task",                    // task, research_request, inbox_response
  "runtime": "ollama",               // Optional: "ollama" or "openclaw"
  "agentType": "diagnostic",         // diagnostic → auto-uses Ollama
  "projectId": "my-project",
  "title": "Fix login bug",
  "notes": "Details about the error..."
}
```

## Architecture Notes

- **Multi-worker**: Multiple workers can run concurrently up to configured capacity
- **Queueing**: Tasks queue automatically when capacity is full or a project lock is active
- **Same tracking**: Ollama workers tracked the same as OpenClaw workers
- **No sessions**: Ollama is stateless - no session cleanup needed
- **Same locks**: Uses same domain locking mechanism
