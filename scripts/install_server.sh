#!/usr/bin/env bash
# Install and (re)load the claude-hooks FastAPI server as a launchd agent.
# Safe to run multiple times — unloads first to avoid stale registration.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$REPO_DIR/launchd/com.claude-hooks-pipeline.plist.template"
LABEL="com.claude-hooks-pipeline"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"

UV_PATH="$(which uv 2>/dev/null || echo "$HOME/.local/bin/uv")"
if [ ! -x "$UV_PATH" ]; then
  echo "Error: uv not found. Install it: curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 1
fi
UV_DIR="$(dirname "$UV_PATH")"

echo "Using uv: $UV_PATH"
echo "Repo:     $REPO_DIR"

echo "Unloading existing agent (if any)..."
launchctl unload "$PLIST_DST" 2>/dev/null || true

echo "Generating plist from template..."
sed \
  -e "s|__HOME__|$HOME|g" \
  -e "s|__UV__|$UV_PATH|g" \
  -e "s|__UV_DIR__|$UV_DIR|g" \
  -e "s|__REPO__|$REPO_DIR|g" \
  "$TEMPLATE" > "$PLIST_DST"

echo "Loading agent..."
launchctl load "$PLIST_DST"

echo "Done. Server should be running on port 8766."
echo "Logs: /tmp/claude-hooks-pipeline.log"
echo "Health: curl http://127.0.0.1:8766/health"
