# Ollama Integration - Quick Reference

## Setup (One-Time)

```bash
# 1. Install Ollama
curl -fsSL https://ollama.com/install.sh | sh

# 2. Pull a model
ollama pull llama3.1

# 3. Install Python dependencies (if needed)
source env/bin/activate
pip install -r requirements.txt

# 4. Configure (optional - defaults work)
python3 setup_config.py --ollama-model llama3.1 --show
```

Done! Ollama will automatically be used for diagnostic tasks.

## Settings Management

### View Settings
```bash
python3 setup_config.py --show
```

### Update Ollama Settings
```bash
# Change model
python3 setup_config.py --ollama-model codellama

# Keep model loaded longer (1 hour)
python3 setup_config.py --ollama-keep-alive 1h

# Change Ollama URL (if remote)
python3 setup_config.py --ollama-url http://192.168.1.100:11434

# View results
python3 setup_config.py --show
```

## How It Works

**Automatic Runtime Selection:**
- Tasks with `agentType: "diagnostic"` → **Ollama** ✅
- All other tasks (`kind: "task"`) → **OpenClaw** ✅
- If Ollama not available → Falls back to OpenClaw ✅

**No Model Management Needed:**
- First request: Model loads automatically (~2-5s)
- Subsequent requests: Instant (model already loaded)
- After timeout: Model unloads to save memory
- Completely automatic - you don't need to do anything!

## Recommended Settings

**For active development:**
```bash
python3 setup_config.py \
  --ollama-model llama3.1 \
  --ollama-keep-alive 1h
```

**For production (always-on):**
```bash
python3 setup_config.py --ollama-keep-alive -1
```

**For low memory:**
```bash
python3 setup_config.py \
  --ollama-model llama3.2 \
  --ollama-keep-alive 5m
```

## Model Loading Explained

**You asked:** "Does Ollama require me to have the model already running?"

**Answer:** No! Ollama handles this automatically:

1. **First diagnostic task:**
   - Ollama loads model into memory (~2-5 seconds)
   - Task runs
   - Model stays loaded based on `keep_alive` setting

2. **Next diagnostic task (within keep_alive period):**
   - Model already in memory
   - Runs instantly (no loading delay)

3. **After keep_alive timeout:**
   - Model unloads to free memory
   - Next task will reload it (small delay again)

**Control with `keep_alive`:**
- `5m` (default): Model unloads after 5 min of inactivity
- `1h`: Model stays loaded for 1 hour
- `-1`: Model stays loaded forever (until Ollama restarts)

**Memory usage:** ~6GB for llama3.1 while loaded

## Check Model Status

```bash
# See what's loaded
curl http://localhost:11434/api/ps

# List available models
ollama list

# Pre-load model (optional - avoids first-request delay)
ollama run llama3.1 ""
```

## Files Created

```
orchestrator/core/ollama_client.py   # Ollama API client
requirements.txt                     # Added 'requests'
docs/SETTINGS.md                     # Settings documentation
docs/OLLAMA_INTEGRATION.md           # Full Ollama docs
docs/OLLAMA_QUICKSTART.md            # Quick start guide
docs/OLLAMA_MODEL_LOADING.md         # Model loading explained
docs/README_OLLAMA.md                # This file
```

## Example Usage

### Create a Diagnostic Task

In your task JSON:
```json
{
  "id": "diag-001",
  "kind": "task",
  "agentType": "diagnostic",  // ← Triggers Ollama
  "projectId": "my-project",
  "title": "Fix authentication error",
  "notes": "Users getting 401 on /api/login endpoint"
}
```

The orchestrator will:
1. Detect `agentType: "diagnostic"`
2. Check if Ollama is running
3. Execute with Ollama runtime
4. Fall back to OpenClaw if Ollama unavailable

### Logs

Check execution:
```bash
cat ~/lobs/lobs-control/state/worker-results/diag-001.log
```

Format:
```
=== Ollama Task Execution ===
Task ID: diag-001
Model: llama3.1
Project: my-project

=== Prompt ===
[Full task prompt...]

=== Response ===
[Streamed Ollama response...]

=== Completed ===
```

## Troubleshooting

**"Ollama not available, falling back to OpenClaw"**
- Ollama isn't running
- Fix: `ollama serve` (or install from https://ollama.com)

**Model loading takes too long**
- Use smaller model: `python3 setup_config.py --ollama-model llama3.2`
- Or increase keep_alive: `python3 setup_config.py --ollama-keep-alive 2h`

**Want to test Ollama manually**
```bash
curl http://localhost:11434/api/generate -d '{
  "model": "llama3.1",
  "prompt": "Why is the sky blue?",
  "stream": false
}'
```

## Summary

✅ **Settings managed via:** `python3 setup_config.py --ollama-model <model>`

✅ **No manual model loading:** Ollama handles it automatically

✅ **Keep model loaded:** Set `--ollama-keep-alive 1h` (or `-1` for forever)

✅ **View settings:** `python3 setup_config.py --show`

✅ **Automatic fallback:** Uses OpenClaw if Ollama unavailable

✅ **Multi-worker aware:** Ollama/OpenClaw tasks run under normal worker capacity and project-lock rules

## Next Steps

1. Install Ollama: `curl -fsSL https://ollama.com/install.sh | sh`
2. Pull model: `ollama pull llama3.1`
3. Configure: `python3 setup_config.py --ollama-keep-alive 1h`
4. Create diagnostic tasks with `agentType: "diagnostic"`
5. Monitor logs in `~/lobs/lobs-control/state/worker-results/`

That's it! 🎉
