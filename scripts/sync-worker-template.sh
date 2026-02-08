#!/bin/bash
# Sync key orchestrator files to worker-template/
# Run this after updating WORKER_RULES.md or other worker-facing files

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
TEMPLATE_DIR="$REPO_ROOT/worker-template"

echo "Syncing files to worker-template/..."

# Files to sync from orchestrator root to worker-template
cp "$REPO_ROOT/WORKER_RULES.md" "$TEMPLATE_DIR/WORKER_RULES.md"

echo "✅ Synced WORKER_RULES.md to worker-template/"
echo ""
echo "Files in worker-template/ will be synced to ~/.openclaw/workspace-worker/"
echo "automatically before each task spawn."
