# Quick Start for Low-Memory Systems

If you're running lobs-orchestrator on a system with 4GB RAM or less, follow these steps to prevent OOM kills.

## 1. Install Dependencies

```bash
cd /Users/lobs/lobs-orchestrator
pip install -r requirements.txt
```

This installs `psutil` which is required for memory monitoring.

## 2. Configure for Low Memory

Edit `.lobs_settings.json` and add/update these settings:

```json
{
  "max_concurrent_workers": 1,
  "memory_min_free_mb_for_spawn": 500,
  "memory_critical_free_mb": 200,
  "memory_watchdog_enabled": true,
  "memory_watchdog_interval": 30,
  "ollama_advisor_enabled": false,
  "poll_interval": 30
}
```

**What these do:**
- `max_concurrent_workers: 1` - Run only 1 worker at a time (saves memory)
- `memory_min_free_mb_for_spawn: 500` - Need 500MB free to spawn workers
- `memory_critical_free_mb: 200` - Warn when below 200MB free
- `memory_watchdog_enabled: true` - Enable background memory monitoring
- `memory_watchdog_interval: 30` - Check memory every 30 seconds
- `ollama_advisor_enabled: false` - Disable AI advisor (saves memory)
- `poll_interval: 30` - Check for work every 30s instead of 15s

## 3. Test Before Running as Service

```bash
python3 main.py
```

Watch the startup logs. You should see:

```
[MEMORY] System Memory Report
--------------------------------------------------------------
Total RAM: 4096.0MB
Available: 1500.0MB (63% used)
Orchestrator Process: RSS 120.0MB, VMS 250.0MB
Platform: Darwin 25.3.0
--------------------------------------------------------------
⚠️  Low memory system detected (4096MB total). Recommend reducing max_concurrent_workers to 1.
```

If you see `VERY LOW MEMORY system! (<4GB)`, your RAM is critically low.

Press Ctrl+C to stop after verifying it starts without errors.

## 4. Set Up as macOS Service (Optional)

Follow the full guide in `MACOS_SERVICE.md`:

```bash
# Create logs directory
mkdir -p logs

# Update paths in plist (Python path, working directory, log paths)
nano com.lobs.orchestrator.plist

# Install service
cp com.lobs.orchestrator.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.lobs.orchestrator.plist
launchctl start com.lobs.orchestrator

# Check it's running
launchctl list | grep com.lobs.orchestrator

# Watch logs
tail -f logs/orchestrator.log | grep MEMORY
```

## 5. Monitor Memory Usage

### Check logs for memory events:

```bash
tail -f logs/orchestrator.log | grep MEMORY
```

### Look for these patterns:

✅ **Normal operation:**
```
[MEMORY] System Memory Report
[ENGINE] RSS: 150.0MB, VMS: 280.0MB, Available: 1200.0MB / 4096.0MB
[PRE-SPAWN] Before spawning worker for task abc12345...
[POST-SPAWN] After spawning worker for task abc12345...
```

⚠️ **Memory pressure:**
```
[MEMORY] Worker spawn blocked: Insufficient memory: 450.0MB available, need 500MB
[MEMORY-WATCHDOG] ⚠️  MEMORY PRESSURE DETECTED! Available: 180.0MB < 200MB
```

🚨 **Critical:**
```
⚠️  CRITICAL: System memory below 200MB! OOM kill risk is HIGH.
```

### Monitor the process:

```bash
# Watch orchestrator memory in real-time
top -pid $(pgrep -f "python3.*main.py")

# Check system memory
vm_stat
```

## Troubleshooting

### Still getting killed?

1. **Increase spawn threshold:** Set `memory_min_free_mb_for_spawn` to 750 or 1000
2. **Check other processes:** Close unnecessary apps, browsers, etc.
3. **Reduce poll interval:** Set `poll_interval` to 60 (check work every minute)
4. **Check logs:** Look for memory pressure before the kill

### Workers never spawn?

1. **Check threshold:** Maybe `memory_min_free_mb_for_spawn` is too high
2. **Free up memory:** Close other applications
3. **Check logs:** Look for `Worker spawn blocked` messages
4. **Restart:** `launchctl stop com.lobs.orchestrator && launchctl start com.lobs.orchestrator`

### Need more aggressive settings?

For 2-3GB systems:

```json
{
  "max_concurrent_workers": 1,
  "memory_min_free_mb_for_spawn": 750,
  "memory_critical_free_mb": 300,
  "memory_watchdog_enabled": true,
  "memory_watchdog_interval": 20,
  "ollama_advisor_enabled": false,
  "ollama_enabled": false,
  "poll_interval": 60,
  "auto_review_enabled": false
}
```

## What Changed?

The orchestrator now:

✅ Logs system memory at startup  
✅ Warns if your system has low RAM  
✅ Checks available memory before spawning workers  
✅ Blocks spawns when memory is low  
✅ Runs a watchdog thread that monitors memory every 30s  
✅ Logs memory periodically during operation  
✅ Tracks peak memory usage (high-water marks)

## Getting Help

If you're still experiencing issues:

1. **Check logs:** `tail -100 logs/orchestrator.log`
2. **System memory:** `vm_stat` (look for "Pages free" and "Pages speculative")
3. **Other processes:** `top -o MEM` (see what else is using RAM)
4. **Documentation:** See `MEMORY_MONITORING.md` for full details

## More Info

- Full documentation: `MEMORY_MONITORING.md`
- macOS service guide: `MACOS_SERVICE.md`
- All settings: `docs/SETTINGS.md`
