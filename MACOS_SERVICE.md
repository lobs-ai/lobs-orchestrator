# macOS Service Installation

This guide explains how to run lobs-orchestrator as a macOS launch agent using launchd.

## Prerequisites

1. Python 3.11+ installed
2. All Python dependencies installed (`pip install -r requirements.txt`)
3. OpenClaw installed and configured

## Configuration

### 1. Update the plist file

Edit `com.lobs.orchestrator.plist` and update these paths:

- `ProgramArguments`: Path to your Python interpreter
- `WorkingDirectory`: Path to lobs-orchestrator directory
- `StandardOutPath`: Path for stdout logs
- `StandardErrorPath`: Path for stderr logs

Example:
```xml
<key>ProgramArguments</key>
<array>
    <string>/opt/homebrew/bin/python3</string>  <!-- Update this -->
    <string>/Users/lobs/lobs-orchestrator/main.py</string>  <!-- Update this -->
</array>
```

### 2. Create logs directory

```bash
mkdir -p /Users/lobs/lobs-orchestrator/logs
```

### 3. Test manually first

Before installing as a service, test that it runs:

```bash
cd /Users/lobs/lobs-orchestrator
python3 main.py
```

Press Ctrl+C to stop. If it works, proceed to installation.

## Installation

### Install the launch agent

```bash
# Copy plist to LaunchAgents directory
cp com.lobs.orchestrator.plist ~/Library/LaunchAgents/

# Load the service
launchctl load ~/Library/LaunchAgents/com.lobs.orchestrator.plist

# Start the service
launchctl start com.lobs.orchestrator
```

### Verify it's running

```bash
# Check service status
launchctl list | grep com.lobs.orchestrator

# Check logs
tail -f /Users/lobs/lobs-orchestrator/logs/orchestrator.log
tail -f /Users/lobs/lobs-orchestrator/logs/orchestrator.error.log
```

## Management Commands

### Stop the service
```bash
launchctl stop com.lobs.orchestrator
```

### Restart the service
```bash
launchctl stop com.lobs.orchestrator
launchctl start com.lobs.orchestrator
```

### Unload (disable) the service
```bash
launchctl unload ~/Library/LaunchAgents/com.lobs.orchestrator.plist
```

### Reload configuration after editing plist
```bash
launchctl unload ~/Library/LaunchAgents/com.lobs.orchestrator.plist
cp com.lobs.orchestrator.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.lobs.orchestrator.plist
```

## Memory Configuration for Low-RAM Systems

For systems with 4GB RAM or less, add these settings to `.lobs_settings.json`:

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

These settings:
- Limit to 1 concurrent worker
- Require 500MB free memory before spawning workers
- Warn when free memory drops below 200MB
- Enable memory watchdog (checks every 30s)
- Disable AI advisor (saves memory)
- Increase poll interval to reduce CPU/memory churn

## Troubleshooting

### Service won't start

1. Check logs in `/Users/lobs/lobs-orchestrator/logs/`
2. Verify Python path: `which python3`
3. Test manually: `python3 /Users/lobs/lobs-orchestrator/main.py`
4. Check launchd errors: `launchctl error com.lobs.orchestrator`

### Service keeps crashing (OOM kills)

1. Check system memory: `vm_stat`
2. Reduce `max_concurrent_workers` to 1
3. Increase `memory_min_free_mb_for_spawn` to 750 or 1000
4. Disable `ollama_advisor_enabled`
5. Check logs for "[MEMORY]" entries showing pressure

### Service isn't auto-starting at login

The plist is configured with `RunAtLoad` which should start it when you log in.
If it's not starting:

```bash
# Check if it's loaded
launchctl list | grep com.lobs.orchestrator

# If not loaded, load it
launchctl load ~/Library/LaunchAgents/com.lobs.orchestrator.plist
```

## Differences from Linux systemd

- macOS uses `launchd` instead of `systemd`
- Configuration is in XML plist format, not INI
- Service files go in `~/Library/LaunchAgents/` (user) or `/Library/LaunchDaemons/` (system)
- Use `launchctl` instead of `systemctl`
- Logs go to files specified in plist, not journalctl

## Resource Limits

The plist includes soft/hard memory limits:
- Soft limit: 2GB RSS
- Hard limit: 3GB RSS

If the process exceeds these, macOS will terminate it. Adjust based on your system:

For 4GB systems:
```xml
<key>SoftResourceLimits</key>
<dict>
    <key>ResidentSetSize</key>
    <integer>1073741824</integer>  <!-- 1GB -->
</dict>
```

## Monitoring

Watch memory usage in real-time:

```bash
# Monitor orchestrator process
top -pid $(pgrep -f "python3.*main.py")

# Watch memory logs
tail -f /Users/lobs/lobs-orchestrator/logs/orchestrator.log | grep MEMORY
```

## Additional Resources

- [launchd.info](https://www.launchd.info/) - launchd reference
- [Apple launchd docs](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html)
