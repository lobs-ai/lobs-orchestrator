# Memory Monitoring Installation Checklist

Use this checklist to verify the memory monitoring implementation is correctly installed and configured.

## Pre-Installation Verification

- [ ] Backup existing `.lobs_settings.json`
- [ ] Backup existing orchestrator state
- [ ] Stop orchestrator if running: `launchctl stop com.lobs.orchestrator` (if using service)
- [ ] Note current RAM: `sysctl hw.memsize` (macOS) or `free -h` (Linux)

## Installation Steps

### 1. Install Dependencies

- [ ] Run: `pip install -r requirements.txt`
- [ ] Verify psutil: `python3 -c "import psutil; print(psutil.version_info)"`
- [ ] Expected output: `(6, 1, 1)` or similar

### 2. Verify File Integrity

- [ ] Check new file exists: `ls -l orchestrator/utils/memory_monitor.py`
- [ ] Check test file exists: `ls -l tests/test_memory_monitor.py`
- [ ] Check plist exists: `ls -l com.lobs.orchestrator.plist`
- [ ] Check docs exist: `ls -l MEMORY_MONITORING.md MACOS_SERVICE.md QUICK_START_LOW_MEMORY.md`

### 3. Syntax Validation

Run these commands to verify syntax:

- [ ] `python3 -m py_compile orchestrator/utils/memory_monitor.py`
- [ ] `python3 -m py_compile main.py`
- [ ] `python3 -m py_compile orchestrator/core/engine.py`
- [ ] `python3 -m py_compile orchestrator/core/worker.py`
- [ ] `python3 -m py_compile tests/test_memory_monitor.py`

All should complete without errors.

### 4. Integration Verification

Verify the integrations are present:

- [ ] `grep "memory_monitor" main.py` - Should show 4+ matches
- [ ] `grep "memory_monitor" orchestrator/core/engine.py` - Should show 3 matches
- [ ] `grep "memory_monitor" orchestrator/core/worker.py` - Should show 5+ matches

### 5. Configure Settings

For 4GB systems, edit `.lobs_settings.json`:

- [ ] Set `"max_concurrent_workers": 1`
- [ ] Set `"memory_min_free_mb_for_spawn": 500`
- [ ] Set `"memory_critical_free_mb": 200`
- [ ] Set `"memory_watchdog_enabled": true`
- [ ] Set `"memory_watchdog_interval": 30`
- [ ] Set `"ollama_advisor_enabled": false`
- [ ] Set `"poll_interval": 30`
- [ ] Save and validate JSON: `python3 -m json.tool .lobs_settings.json > /dev/null`

## Testing

### 1. Manual Start Test

- [ ] Run: `python3 main.py`
- [ ] Look for: `[MEMORY] System Memory Report`
- [ ] Look for: `Total RAM:` and `Available:` lines
- [ ] Look for warnings if system has low RAM
- [ ] Let it run for 1-2 minutes
- [ ] Look for: `[ENGINE] RSS:` periodic logs
- [ ] Press Ctrl+C to stop
- [ ] Should exit cleanly

Expected startup output:
```
[MEMORY] System Memory Report
--------------------------------------------------------------
Total RAM: 4096.0MB
Available: 1500.0MB (63% used)
Orchestrator Process: RSS 120.0MB, VMS 250.0MB
Platform: Darwin 25.3.0
--------------------------------------------------------------
[MEMORY] Starting memory watchdog (check every 30s, critical threshold: 200MB)
```

### 2. Unit Tests (Optional)

If pytest is available:

- [ ] Run: `pip install -r requirements-dev.txt` (if exists)
- [ ] Run: `pytest tests/test_memory_monitor.py -v`
- [ ] Verify: All tests pass or skip gracefully

### 3. Memory Protection Test

Test that worker spawning is blocked when memory is low:

- [ ] Temporarily edit `.lobs_settings.json`: `"memory_min_free_mb_for_spawn": 99999`
- [ ] Run: `python3 main.py`
- [ ] Create a test task (if you have dashboard access)
- [ ] Look for: `[MEMORY] Worker spawn blocked: Insufficient memory`
- [ ] Stop orchestrator
- [ ] Restore: `"memory_min_free_mb_for_spawn": 500`

### 4. Watchdog Test

Test that watchdog detects memory pressure:

- [ ] Edit `.lobs_settings.json`: `"memory_critical_free_mb": 99999`
- [ ] Run: `python3 main.py`
- [ ] Wait 30-60 seconds
- [ ] Look for: `[MEMORY-WATCHDOG] ⚠️  MEMORY PRESSURE DETECTED`
- [ ] Stop orchestrator
- [ ] Restore: `"memory_critical_free_mb": 200`

## macOS Service Installation (Optional)

### 1. Prepare Service Files

- [ ] Edit `com.lobs.orchestrator.plist`
- [ ] Update `<string>/usr/local/bin/python3</string>` to your Python path
- [ ] Update `<string>/Users/lobs/lobs-orchestrator/main.py</string>` to your path
- [ ] Update `<string>/Users/lobs/lobs-orchestrator</string>` to your working directory
- [ ] Update log paths in StandardOutPath and StandardErrorPath
- [ ] Create logs directory: `mkdir -p logs`

### 2. Install Service

- [ ] Copy plist: `cp com.lobs.orchestrator.plist ~/Library/LaunchAgents/`
- [ ] Load service: `launchctl load ~/Library/LaunchAgents/com.lobs.orchestrator.plist`
- [ ] Start service: `launchctl start com.lobs.orchestrator`
- [ ] Check status: `launchctl list | grep com.lobs.orchestrator`
- [ ] Should show PID and status code 0

### 3. Verify Service

- [ ] Check logs: `tail -50 logs/orchestrator.log`
- [ ] Look for memory startup logs
- [ ] Check errors: `tail -50 logs/orchestrator.error.log`
- [ ] Should be empty or minimal
- [ ] Watch logs: `tail -f logs/orchestrator.log | grep MEMORY`
- [ ] Should see periodic memory logs

### 4. Test Service Restart

- [ ] Stop: `launchctl stop com.lobs.orchestrator`
- [ ] Wait 10 seconds
- [ ] Check restarted: `launchctl list | grep com.lobs.orchestrator`
- [ ] Service should auto-restart (KeepAlive is configured)

## Monitoring

### Real-Time Monitoring

Set up monitoring commands:

- [ ] Memory logs: `tail -f logs/orchestrator.log | grep MEMORY`
- [ ] Process memory: `top -pid $(pgrep -f "python3.*main.py")`
- [ ] System memory: `vm_stat 1`

### What to Watch

During normal operation, you should see:

- [ ] `[ENGINE] RSS:` logs every ~10 poll cycles
- [ ] `[PRE-SPAWN]` and `[POST-SPAWN]` logs when workers spawn
- [ ] RSS should be 100-300MB typically
- [ ] Available memory should stay above your `memory_min_free_mb_for_spawn` threshold

Warning signs:

- [ ] `[MEMORY] Worker spawn blocked` - Memory too low
- [ ] `[MEMORY-WATCHDOG] MEMORY PRESSURE DETECTED` - Critical memory
- [ ] RSS steadily increasing - Possible memory leak
- [ ] Available < 500MB on 4GB system - Risk of OOM

## Troubleshooting

### Orchestrator Won't Start

- [ ] Check Python path: `which python3`
- [ ] Check imports: `python3 -c "import psutil"`
- [ ] Check syntax: `python3 -m py_compile main.py`
- [ ] Check logs: `cat logs/orchestrator.error.log`

### Workers Not Spawning

- [ ] Check memory threshold: grep "memory_min_free_mb_for_spawn" .lobs_settings.json
- [ ] Check available memory: `vm_stat | grep "Pages free"`
- [ ] Check logs: `grep "Worker spawn blocked" logs/orchestrator.log`
- [ ] Lower threshold if needed

### High Memory Usage

- [ ] Check worker count: grep "max_concurrent_workers" .lobs_settings.json
- [ ] Reduce to 1 if on 4GB system
- [ ] Disable advisors: `"ollama_advisor_enabled": false`
- [ ] Check for leaks: Monitor RSS high-water marks in logs

### Service Not Auto-Starting

- [ ] Check plist loaded: `launchctl list | grep com.lobs`
- [ ] Check plist syntax: `plutil -lint ~/Library/LaunchAgents/com.lobs.orchestrator.plist`
- [ ] Check RunAtLoad: grep "RunAtLoad" com.lobs.orchestrator.plist
- [ ] Reload: `launchctl unload ~/Library/LaunchAgents/com.lobs.orchestrator.plist && launchctl load ~/Library/LaunchAgents/com.lobs.orchestrator.plist`

## Post-Installation

### Documentation Review

- [ ] Read: `MEMORY_MONITORING.md` - Full implementation details
- [ ] Read: `MACOS_SERVICE.md` - Service management
- [ ] Read: `QUICK_START_LOW_MEMORY.md` - Quick reference

### Ongoing Monitoring

Set up regular checks:

- [ ] Daily: Check orchestrator is running
- [ ] Daily: Review memory logs for warnings
- [ ] Weekly: Check RSS high-water marks for leaks
- [ ] Monthly: Review and adjust thresholds based on experience

### Performance Baseline

Record baseline metrics for future comparison:

- [ ] Idle RSS: _____ MB
- [ ] RSS with 1 worker: _____ MB
- [ ] Typical available memory: _____ MB
- [ ] System total RAM: _____ MB

## Rollback Plan

If you need to rollback:

### 1. Stop Service
```bash
launchctl stop com.lobs.orchestrator
launchctl unload ~/Library/LaunchAgents/com.lobs.orchestrator.plist
```

### 2. Restore Files
```bash
# Restore from git
git checkout main.py orchestrator/core/engine.py orchestrator/core/worker.py requirements.txt
# Remove new files
rm orchestrator/utils/memory_monitor.py tests/test_memory_monitor.py
rm MEMORY_MONITORING.md MACOS_SERVICE.md QUICK_START_LOW_MEMORY.md
```

### 3. Restore Settings
```bash
# Restore your backed-up .lobs_settings.json
cp .lobs_settings.json.backup .lobs_settings.json
```

### 4. Restart
```bash
python3 main.py
# or reload service
```

## Completion

- [ ] All installation steps completed
- [ ] All tests passed
- [ ] Service running (if applicable)
- [ ] Monitoring in place
- [ ] Documentation reviewed
- [ ] Baseline metrics recorded

## Sign-Off

Installation completed by: _________________  
Date: _________________  
System RAM: _________________  
Notes: _________________________________________________________________

---

For questions or issues, refer to:
- `MEMORY_MONITORING.md` - Full documentation
- `QUICK_START_LOW_MEMORY.md` - Quick reference
- `MACOS_SERVICE.md` - Service management
