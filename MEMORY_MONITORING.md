# Memory Monitoring Implementation

This document describes the memory monitoring and OOM prevention system added to lobs-orchestrator.

## Problem Statement

On systems with limited RAM (especially 4GB macOS VMs), the orchestrator was being killed by the OS via SIGKILL when memory pressure became too high. SIGKILL is uncatchable, so the orchestrator couldn't gracefully handle the situation.

## Solution

A comprehensive memory monitoring system that:

1. **Tracks memory usage** - Logs RSS, VMS, and system available memory
2. **Prevents dangerous spawns** - Blocks worker spawning when memory is low
3. **Watchdog protection** - Background thread monitors memory and blocks spawns during pressure
4. **Startup warnings** - Detects low-memory systems and recommends settings
5. **High-water marks** - Tracks peak memory usage for leak detection

## Components

### New Files

- `orchestrator/utils/memory_monitor.py` - Core memory monitoring module
- `tests/test_memory_monitor.py` - Comprehensive test suite
- `com.lobs.orchestrator.plist` - macOS launchd service configuration
- `MACOS_SERVICE.md` - macOS service installation guide
- `MEMORY_MONITORING.md` - This documentation

### Modified Files

- `main.py` - Initialize memory monitoring at startup
- `orchestrator/core/engine.py` - Log memory in main loop
- `orchestrator/core/worker.py` - Check memory before spawning workers
- `requirements.txt` - Added `psutil==6.1.1` dependency

## Installation

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

This will install `psutil` which is required for memory monitoring.

### 2. Configure Settings

For low-memory systems (4GB or less), add to `.lobs_settings.json`:

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

### 3. Test the Installation

```bash
# Verify psutil is installed
python3 -c "import psutil; print(f'psutil {psutil.version_info}')"

# Run syntax checks
python3 -m py_compile orchestrator/utils/memory_monitor.py

# Run tests (requires pytest and pytest-dev dependencies)
pip install -r requirements-dev.txt
pytest tests/test_memory_monitor.py -v
```

## Configuration Options

All memory settings are optional and have sensible defaults:

| Setting | Default | Description |
|---------|---------|-------------|
| `memory_min_free_mb_for_spawn` | 500 | Minimum free memory (MB) required to spawn workers |
| `memory_critical_free_mb` | 200 | Memory threshold (MB) that triggers warnings |
| `memory_watchdog_enabled` | true | Enable background memory watchdog thread |
| `memory_watchdog_interval` | 30 | Watchdog check interval (seconds) |

## How It Works

### Startup

1. `main.py` calls `init_memory_monitoring()` early in startup
2. Logs total system RAM and available memory
3. Warns if system has <8GB total RAM or <1GB available
4. Starts watchdog thread (if enabled)

### Main Loop

1. Every iteration of `engine.run_once()` logs memory snapshot (periodic)
2. Logs include RSS, VMS, available memory, and high-water marks
3. Critical warnings if available memory drops below threshold

### Worker Spawning

1. Before spawning, `worker.spawn_worker()` checks memory
2. If available memory < `min_free_mb_for_spawn`, spawn is blocked
3. If watchdog detected memory pressure, spawn is blocked
4. Logs memory before and after successful spawn
5. Task remains eligible and retries on next poll

### Watchdog Thread

1. Runs in background, checks memory every `watchdog_interval` seconds
2. Sets `_memory_pressure` flag when available memory < `critical_free_mb`
3. Clears flag when memory recovers
4. Blocks all worker spawns while pressure flag is set

### Shutdown

1. Signal handlers (SIGTERM/SIGINT) stop the watchdog thread
2. Graceful cleanup before exit

## Monitoring

### Check Logs

```bash
# Watch for memory events
tail -f logs/orchestrator.log | grep MEMORY

# Watch for memory warnings
tail -f logs/orchestrator.log | grep -E "MEMORY|WARN"
```

### Log Patterns

- `[MEMORY] System Memory Report` - Startup memory info
- `[ENGINE] RSS: X MB` - Periodic memory snapshot in main loop
- `[PRE-SPAWN] RSS: X MB` - Before spawning worker
- `[POST-SPAWN] RSS: X MB` - After spawning worker
- `[MEMORY] Worker spawn blocked` - Memory prevented spawn
- `[MEMORY-WATCHDOG] MEMORY PRESSURE DETECTED` - Watchdog triggered
- `⚠️  Low memory system detected` - Startup warning for <8GB systems
- `⚠️  VERY LOW MEMORY system` - Startup warning for <4GB systems
- `⚠️  CRITICAL: System memory below X MB` - Dangerous memory level

### System Monitoring

```bash
# Monitor orchestrator process
top -pid $(pgrep -f "python3.*main.py")

# Check system memory
vm_stat

# Monitor in real-time
watch -n 5 'ps aux | grep main.py | grep -v grep'
```

## Troubleshooting

### Orchestrator still getting killed

1. **Lower spawn threshold**: Increase `memory_min_free_mb_for_spawn` to 750 or 1000
2. **Reduce workers**: Set `max_concurrent_workers` to 1
3. **Disable features**: Turn off `ollama_advisor_enabled`
4. **Check logs**: Look for `[MEMORY]` entries before the kill
5. **System pressure**: Check other processes consuming RAM

### Workers never spawn

1. **Check threshold**: Verify `memory_min_free_mb_for_spawn` isn't too high
2. **Check watchdog**: Look for `MEMORY PRESSURE DETECTED` in logs
3. **System memory**: Ensure system has enough free memory
4. **Restart**: Try restarting orchestrator to clear pressure flag

### Watchdog not starting

1. **Check setting**: Verify `memory_watchdog_enabled` is true
2. **Check logs**: Look for `[MEMORY] Starting memory watchdog`
3. **Thread error**: Check for exceptions in watchdog thread

### High memory usage

1. **Check workers**: See how many concurrent workers are running
2. **Check models**: LLM models consume significant memory
3. **Check advisors**: AI advisor adds memory overhead
4. **Check leaks**: Monitor high-water marks over time

## Performance Impact

Memory monitoring has minimal performance impact:

- Snapshots use `psutil` which is highly optimized (C extension)
- Main loop logs only every 10th iteration by default
- Watchdog checks every 30 seconds (configurable)
- No locks or blocking operations in hot paths

Typical overhead:
- Memory snapshot: <1ms
- Watchdog check: <1ms every 30s
- Total: <0.1% CPU usage

## Testing

### Unit Tests

```bash
# Run all memory monitor tests
pytest tests/test_memory_monitor.py -v

# Run specific test
pytest tests/test_memory_monitor.py::TestMemoryMonitor::test_get_snapshot -v

# Run with coverage
pytest tests/test_memory_monitor.py --cov=orchestrator.utils.memory_monitor
```

### Integration Testing

```bash
# Run orchestrator with verbose logging
python3 main.py

# Watch memory logs
tail -f logs/orchestrator.log | grep MEMORY

# Simulate memory pressure (in another terminal)
# Run memory-intensive tasks to trigger watchdog
```

### Stress Testing

To test OOM protection:

1. Set very high `memory_min_free_mb_for_spawn` (e.g., 2000)
2. Try to spawn workers - should be blocked
3. Lower threshold back to normal
4. Workers should spawn again

## Future Enhancements

Potential improvements:

1. **Memory limits per worker**: Track individual worker RSS
2. **Proactive cleanup**: Force garbage collection when memory is tight
3. **Dynamic thresholds**: Adjust based on system total RAM
4. **Memory leak detection**: Alert on steadily increasing high-water marks
5. **Metrics export**: Expose memory metrics for Prometheus/Grafana
6. **Swap monitoring**: Track swap usage as early warning

## References

- [psutil documentation](https://psutil.readthedocs.io/)
- [macOS memory pressure](https://support.apple.com/en-us/HT201740)
- [Understanding SIGKILL](https://www.man7.org/linux/man-pages/man7/signal.7.html)

## Related Files

- Main implementation: `orchestrator/utils/memory_monitor.py`
- Tests: `tests/test_memory_monitor.py`
- macOS service: `MACOS_SERVICE.md`
- Settings: `.lobs_settings.json`
