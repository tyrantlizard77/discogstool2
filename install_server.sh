#!/usr/bin/env bash
# install_server.sh — Install dt_server as a macOS login agent.
# Generates ~/Library/LaunchAgents/com.discogstool.server.plist with the
# correct paths for this machine, then loads it immediately.
#
# Usage:
#   ./install_server.sh           # install and start
#   ./install_server.sh --unload  # stop and remove

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python3"
PLIST_NAME="com.discogstool.server"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"
LOG_DIR="$HOME/Library/Logs/discogstool"

if [[ "${1:-}" == "--unload" ]]; then
    launchctl unload "$PLIST_DST" 2>/dev/null && echo "Stopped." || echo "Was not loaded."
    rm -f "$PLIST_DST" && echo "Removed $PLIST_DST."
    exit 0
fi

if [[ ! -x "$PYTHON" ]]; then
    echo "Error: venv not found at $PYTHON"
    echo "Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi

mkdir -p "$LOG_DIR" "$(dirname "$PLIST_DST")"

# Unload any existing agent before rewriting
launchctl unload "$PLIST_DST" 2>/dev/null || true

cat > "$PLIST_DST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$PLIST_NAME</string>

  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$SCRIPT_DIR/dt_server</string>
  </array>

  <key>WorkingDirectory</key>
  <string>$SCRIPT_DIR</string>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>

  <key>StandardOutPath</key>
  <string>$LOG_DIR/server.log</string>

  <key>StandardErrorPath</key>
  <string>$LOG_DIR/server.log</string>
</dict>
</plist>
EOF

launchctl load "$PLIST_DST"
echo "dt_server installed and started."
echo "Logs: $LOG_DIR/server.log"
