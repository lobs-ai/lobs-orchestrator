# Ollama Quick Start

## Install & Run Ollama

```bash
# 1. Install Ollama
curl -fsSL https://ollama.com/install.sh | sh

# 2. Pull a model
ollama pull llama3.1

# 3. Verify it's running
curl http://localhost:11434/api/tags
```

## Configure Orchestrator

Optional - add to `.lobs_settings.json` (defaults work if omitted):

```json
{
  "ollama_url": "http://localhost:11434",
  "ollama_model": "llama3.1"
}
```

## Usage

The orchestrator **automatically** uses Ollama for diagnostic tasks:

```json
{
  "id": "diag-001",
  "agentType": "diagnostic",  // ← This triggers Ollama
  "projectId": "my-project",
  "title": "Fix authentication bug",
  "notes": "Users getting 401 errors on /api/login"
}
```

**That's it!** The orchestrator will:
1. Detect `agentType: "diagnostic"`
2. Check if Ollama is available
3. Use Ollama for execution
4. Fall back to OpenClaw if Ollama isn't running

## Runtime Selection

| Task Type | Runtime |
|-----------|---------|
| `kind: "task"` | OpenClaw |
| `agentType: "diagnostic"` | **Ollama** |
| Explicit `runtime: "ollama"` | **Ollama** |
| Explicit `runtime: "openclaw"` | OpenClaw |

## Logs

Check execution logs:
```bash
cat ~/lobs/lobs-control/state/worker-results/{task-id}.log
```

Ollama logs include:
- Model used
- Full prompt
- Streamed response
- Completion status

## Verify Setup

```bash
# Check if Ollama is running
source env/bin/activate
python -c "
from orchestrator.core.ollama_client import OllamaClient
client = OllamaClient()
print('Ollama available:', client.is_available())
print('Models:', client.list_models())
"
```

Expected output:
```
Ollama available: True
Models: ['llama3.1', ...]
```

## Troubleshooting

**"Ollama not available, falling back to OpenClaw"**
- Ollama isn't running
- Start it: `ollama serve` (or it auto-starts on macOS/Linux)
- Verify: `curl http://localhost:11434/api/tags`

**Want to use different model?**
```json
{
  "ollama_model": "codellama"  // Or any installed model
}
```

**Want to force OpenClaw for diagnostics?**
```json
{
  "id": "diag-001",
  "agentType": "diagnostic",
  "runtime": "openclaw"  // ← Force OpenClaw
}
```

## See Also

- [OLLAMA_INTEGRATION.md](OLLAMA_INTEGRATION.md) - Full documentation
- [SINGLE_WORKER_ARCHITECTURE.md](SINGLE_WORKER_ARCHITECTURE.md) - Worker architecture
