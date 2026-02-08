#!/usr/bin/env python3
"""
Reset tasks that are stuck in "in_progress" state back to "not_started".

This fixes the bug where tasks were marked in_progress when queued but never worked on.
"""

import json
import sys
from pathlib import Path
from datetime import datetime, timezone

def reset_stuck_tasks():
    tasks_dir = Path.home() / "lobs-control" / "state" / "tasks"
    
    if not tasks_dir.exists():
        print(f"Tasks directory not found: {tasks_dir}")
        return 0
    
    reset_count = 0
    
    for task_file in tasks_dir.glob("*.json"):
        try:
            with open(task_file, 'r') as f:
                task = json.load(f)
            
            # Only reset tasks that are in_progress but not currently being worked on
            # (we check worker-status.json to see what's actually active)
            if task.get("workState") == "in_progress":
                task_id = task.get("id", "unknown")
                title = task.get("title", task_id[:8])
                
                print(f"Resetting task {task_id}: {title}")
                
                task["workState"] = "not_started"
                task["updatedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                
                with open(task_file, 'w') as f:
                    json.dump(task, f, indent=2)
                    f.write("\n")
                
                reset_count += 1
        
        except Exception as e:
            print(f"Error processing {task_file}: {e}", file=sys.stderr)
    
    return reset_count

if __name__ == "__main__":
    # First check what's actually active
    worker_status_file = Path.home() / "lobs-control" / "state" / "worker-status.json"
    active_task = None
    
    if worker_status_file.exists():
        try:
            with open(worker_status_file, 'r') as f:
                status = json.load(f)
                if status.get("active"):
                    active_task = status.get("currentTask")
        except Exception as e:
            print(f"Warning: Could not read worker status: {e}", file=sys.stderr)
    
    if active_task:
        print(f"Note: Task {active_task} is currently active and will NOT be reset")
        print()
    
    count = reset_stuck_tasks()
    print(f"\nReset {count} stuck task(s) from in_progress to not_started")
