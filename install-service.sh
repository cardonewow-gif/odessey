#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_FILE="$SCRIPT_DIR/odysseus-ui.service"
APP_LABEL="com.odysseus.ui"
PORT="${PORT:-7000}"
HOST="${HOST:-127.0.0.1}"

pick_python() {
  if [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
    printf '%s\n' "$SCRIPT_DIR/.venv/bin/python"
  elif [ -x "$SCRIPT_DIR/venv/bin/python" ]; then
    printf '%s\n' "$SCRIPT_DIR/venv/bin/python"
  else
    command -v python3
  fi
}

xml_escape() {
  "$(pick_python)" - "$1" <<'PY'
import html
import sys
print(html.escape(sys.argv[1], quote=False))
PY
}

if [ "$(uname -s)" = "Darwin" ]; then
  PYTHON_BIN="$(pick_python)"
  PLIST_DIR="$HOME/Library/LaunchAgents"
  PLIST_FILE="$PLIST_DIR/$APP_LABEL.plist"
  LOG_DIR="$SCRIPT_DIR/logs"
  mkdir -p "$PLIST_DIR" "$LOG_DIR"

  PY_ESCAPED="$(xml_escape "$PYTHON_BIN")"
  SCRIPT_ESCAPED="$(xml_escape "$SCRIPT_DIR")"
  OUT_ESCAPED="$(xml_escape "$LOG_DIR/odysseus-ui.out.log")"
  ERR_ESCAPED="$(xml_escape "$LOG_DIR/odysseus-ui.err.log")"

  cat > "$PLIST_FILE" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$APP_LABEL</string>
  <key>WorkingDirectory</key>
  <string>$SCRIPT_ESCAPED</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY_ESCAPED</string>
    <string>-m</string>
    <string>uvicorn</string>
    <string>app:app</string>
    <string>--host</string>
    <string>$HOST</string>
    <string>--port</string>
    <string>$PORT</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$OUT_ESCAPED</string>
  <key>StandardErrorPath</key>
  <string>$ERR_ESCAPED</string>
</dict>
</plist>
EOF

  launchctl bootout "gui/$UID" "$PLIST_FILE" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$UID" "$PLIST_FILE"
  launchctl kickstart -k "gui/$UID/$APP_LABEL" >/dev/null 2>&1 || true

  echo "Installed macOS LaunchAgent: $PLIST_FILE"
  echo "Service: $APP_LABEL"
  echo "URL: http://$HOST:$PORT"
  echo "Logs:"
  echo "  $LOG_DIR/odysseus-ui.out.log"
  echo "  $LOG_DIR/odysseus-ui.err.log"
  echo ""
  echo "Stop:   launchctl bootout gui/$UID $PLIST_FILE"
  echo "Start:  launchctl bootstrap gui/$UID $PLIST_FILE"
  exit 0
fi

if ! command -v systemctl >/dev/null 2>&1; then
  echo "Error: this installer supports macOS launchd or Linux systemd."
  echo "Manual start: uvicorn app:app --host 127.0.0.1 --port 7000"
  exit 1
fi

if [ ! -f "$SERVICE_FILE" ]; then
  echo "Error: odysseus-ui.service not found in $SCRIPT_DIR"
  exit 1
fi

echo "Installing Odysseus UI service..."
echo "Make sure you've edited odysseus-ui.service with your username and paths first!"
echo ""

sudo cp "$SERVICE_FILE" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable odysseus-ui
sudo systemctl start odysseus-ui
sudo systemctl status odysseus-ui
