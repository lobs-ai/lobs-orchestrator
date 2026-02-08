# Ollama Model Loading & Memory Management

## How Ollama Model Loading Works

**Good news:** You don't need to keep models "running" manually! Ollama handles this automatically.

### Automatic Model Loading

1. **First Request:**
   - Ollama loads the model into memory (~2-5 seconds for llama3.1)
   - Request processes normally
   - Response is returned

2. **Subsequent Requests:**
   - Model is already in memory
   - Requests are **instant** (no loading delay)
   - As fast as OpenAI/Anthropic API calls

3. **After Inactivity:**
   - Ollama keeps model in memory based on `keep_alive` setting
   - Default: 5 minutes
   - After timeout, model is unloaded to free memory
   - Next request will reload it (small delay again)

### Memory Management with `keep_alive`

Control how long Ollama keeps models loaded:

```bash
# Keep model loaded for 10 minutes after last use
./bin/settings set ollama_keep_alive 10m

# Keep model loaded for 1 hour
./bin/settings set ollama_keep_alive 1h

# Keep model loaded forever (until Ollama restarts)
./bin/settings set ollama_keep_alive -1

# Unload immediately after each request (default Ollama behavior)
./bin/settings set ollama_keep_alive 0
```

### Recommended Settings

**For active development (frequent diagnostics):**
```bash
./bin/settings set ollama_keep_alive 1h
```
- Model stays loaded for 1 hour
- No loading delays during active work
- Automatically unloads when you're done

**For occasional use:**
```bash
./bin/settings set ollama_keep_alive 5m  # (default)
```
- Model unloads after 5 minutes of inactivity
- Saves memory when not in use
- Small delay (~2-5s) when you come back

**For always-on orchestrator:**
```bash
./bin/settings set ollama_keep_alive -1
```
- Model stays loaded permanently
- Zero loading delays
- Uses ~4-8GB RAM continuously (depends on model)

## Model Size & Memory Usage

| Model | Size | RAM Usage | Speed | Best For |
|-------|------|-----------|-------|----------|
| llama3.2 | 2GB | ~3GB | Fast | Quick diagnostics |
| llama3.1 | 4.7GB | ~6GB | Medium | **Recommended** - balanced |
| codellama | 3.8GB | ~5GB | Medium | Code-focused tasks |
| llama3.1:70b | 40GB | ~48GB | Slow | High-quality analysis |

**Recommendation:** Use `llama3.1` (default) - good balance of quality and speed.

## Checking Model Status

### See what's currently loaded

```bash
curl http://localhost:11434/api/ps
```

Output:
```json
{
  "models": [
    {
      "name": "llama3.1:latest",
      "size": 4661224384,
      "size_vram": 4661224384,
      "expires_at": "2026-02-08T15:30:00Z"
    }
  ]
}
```

### Pre-load a model (optional)

If you want to avoid the first-request delay:

```bash
# Load model into memory immediately
curl http://localhost:11434/api/generate -d '{
  "model": "llama3.1",
  "prompt": "",
  "keep_alive": "1h"
}'
```

Or use the Ollama CLI:
```bash
ollama run llama3.1 ""  # Loads model, exits immediately
```

## Settings Management

### View current settings

```bash
./bin/settings list
```

Output:
```
📋 Current Settings

======================================================================

ollama_url
  Value: http://localhost:11434
  Info:  Ollama API endpoint

ollama_model
  Value: llama3.1
  Info:  Ollama model to use (e.g., llama3.1, codellama, mistral)

ollama_keep_alive
  Value: 5m
  Info:  How long Ollama keeps model in memory (e.g., 5m, 1h, -1 for forever)

======================================================================
```

### Update settings

```bash
# Change model
./bin/settings set ollama_model codellama

# Change keep_alive
./bin/settings set ollama_keep_alive 30m

# Change Ollama URL (if running on different host)
./bin/settings set ollama_url http://192.168.1.100:11434
```

### Initialize settings file

```bash
./bin/settings init
```

Creates `.lobs_settings.json` with defaults.

## Performance Tips

### 1. Use SSD Storage
Models load ~5x faster from SSD vs HDD.

### 2. Increase keep_alive for Active Work
```bash
./bin/settings set ollama_keep_alive 1h
```

### 3. Pre-load Model on System Startup
Add to your shell rc file (~/.bashrc, ~/.zshrc):

```bash
# Pre-load Ollama model on startup (runs in background)
if command -v ollama &> /dev/null; then
    curl -s http://localhost:11434/api/generate -d '{
        "model": "llama3.1",
        "prompt": "",
        "keep_alive": "2h"
    }' > /dev/null 2>&1 &
fi
```

### 4. Use Smaller Model for Faster Loading
```bash
./bin/settings set ollama_model llama3.2  # Smaller, loads faster
```

## Troubleshooting

### "Model takes too long to load"
- Check disk speed (SSD recommended)
- Use smaller model: `llama3.2` instead of `llama3.1`
- Pre-load on startup (see above)

### "Model keeps unloading"
- Increase keep_alive: `./bin/settings set ollama_keep_alive 1h`
- Or use `-1` to keep loaded forever

### "Using too much memory"
- Lower keep_alive: `./bin/settings set ollama_keep_alive 5m`
- Use smaller model: `./bin/settings set ollama_model llama3.2`
- Or let it unload: `./bin/settings set ollama_keep_alive 0`

### Check if model is loaded
```bash
curl http://localhost:11434/api/ps | jq '.models[].name'
```

If empty, model is not loaded (will load on next request).

## Summary

✅ **You don't need to keep models running manually**
- Ollama loads models automatically on first request
- Keeps them in memory based on `keep_alive` setting
- Unloads after timeout to save memory

✅ **Recommended settings for development:**
```bash
./bin/settings set ollama_model llama3.1
./bin/settings set ollama_keep_alive 1h
```

✅ **First request:** ~2-5 second delay (model loading)

✅ **Subsequent requests:** Instant (model already loaded)

✅ **Update settings anytime:**
```bash
./bin/settings set ollama_keep_alive 30m
```

Changes take effect on next task.
