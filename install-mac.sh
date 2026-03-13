#!/usr/bin/env bash
# install-mac.sh — Install the Rivian EVCC Bridge as a macOS LaunchAgent
# so it starts automatically at login and restarts if it crashes.
#
# Usage: ./install-mac.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BRIDGE_DIR="$SCRIPT_DIR/rivian_bridge"
PLIST_NAME="com.rivian.evcc.bridge"
PLIST_DEST="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"
LOG_DIR="$HOME/Library/Logs/rivian-evcc-bridge"

# ── Check config.json exists ──────────────────────────────────────────────────
if [ ! -f "$BRIDGE_DIR/config.json" ]; then
  echo "ERROR: rivian_bridge/config.json not found."
  echo "Copy config-example.json and fill in your credentials:"
  echo "  cp config-example.json rivian_bridge/config.json"
  exit 1
fi

# ── Check rivian-session.json exists ─────────────────────────────────────────
if [ ! -f "$BRIDGE_DIR/rivian-session.json" ]; then
  echo "ERROR: rivian_bridge/rivian-session.json not found."
  echo "If you already have one from the original project, copy it here:"
  echo "  cp /path/to/rivian-session.json rivian_bridge/rivian-session.json"
  echo ""
  echo "Or run the OTP auth script to generate one:"
  echo "  python RivianSessionInitOTP.py"
  exit 1
fi

# ── Install Python deps ───────────────────────────────────────────────────────
PYTHON=$(which python3)
echo "Using Python: $PYTHON"
"$PYTHON" -m pip install --quiet -r "$BRIDGE_DIR/requirements.txt"

# ── Create log directory ──────────────────────────────────────────────────────
mkdir -p "$LOG_DIR"

# ── Write LaunchAgent plist ───────────────────────────────────────────────────
cat > "$PLIST_DEST" <<PLIST
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
    <string>-u</string>
    <string>$BRIDGE_DIR/server.py</string>
  </array>

  <!-- server.py reads config.json and rivian-session.json from its WorkingDirectory -->
  <key>WorkingDirectory</key>
  <string>$BRIDGE_DIR</string>

  <key>StandardOutPath</key>
  <string>$LOG_DIR/bridge.log</string>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR/bridge.error.log</string>

  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
</dict>
</plist>
PLIST

echo "LaunchAgent plist written to: $PLIST_DEST"

# ── Load (or reload) the agent ────────────────────────────────────────────────
launchctl unload "$PLIST_DEST" 2>/dev/null || true
launchctl load "$PLIST_DEST"

echo ""
echo "✓ Rivian EVCC Bridge installed and started."
echo ""
echo "Check logs at: $LOG_DIR/"
echo "Test with:     curl http://localhost:7070/health"
echo ""
echo "To stop:   launchctl unload $PLIST_DEST"
echo "To start:  launchctl load   $PLIST_DEST"
echo "To remove: launchctl unload $PLIST_DEST && rm $PLIST_DEST"
