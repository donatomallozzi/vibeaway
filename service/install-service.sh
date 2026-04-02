#!/usr/bin/env bash
# install-service.sh — Install bot as a user service (systemd or launchd)
#
# Prerequisites:
#   python service/install.py
#
# Usage:
#   bash service/install-service.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
RUNTIME_DIR="$HOME/.vibeaway"
VENV_DIR="$RUNTIME_DIR/venv"
SERVICE_DIR="$RUNTIME_DIR/service"
WATCHDOG="$SERVICE_DIR/bot_watchdog.pyw"

# ── Verify prerequisites ─────────────────────────────────────────────────────

if [ ! -f "$VENV_DIR/bin/python" ]; then
    echo "ERROR: Runtime venv not found at $VENV_DIR"
    echo "Run first: python service/install.py"
    exit 1
fi

if [ ! -f "$WATCHDOG" ]; then
    echo "ERROR: Watchdog not found at $WATCHDOG"
    echo "Run first: python service/install.py"
    exit 1
fi

# Update watchdog and package
echo "Updating runtime..."
"$VENV_DIR/bin/python" "$REPO_DIR/service/install.py" --update


# ── Linux: systemd user service ──────────────────────────────────────────────

install_systemd() {
    local SERVICE_NAME="vibeaway"
    local UNIT_DIR="$HOME/.config/systemd/user"
    local UNIT_FILE="$UNIT_DIR/$SERVICE_NAME.service"
    local PYTHON="$VENV_DIR/bin/python"

    echo "Installing systemd user service..."

    # Stop existing service if running
    systemctl --user stop "$SERVICE_NAME" 2>/dev/null || true

    mkdir -p "$UNIT_DIR"

    cat > "$UNIT_FILE" << EOF
[Unit]
Description=VibeAway (watchdog)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$PYTHON $WATCHDOG
WorkingDirectory=$SERVICE_DIR
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal
Environment=PATH=$VENV_DIR/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
EOF

    systemctl --user daemon-reload
    systemctl --user enable "$SERVICE_NAME"
    systemctl --user start "$SERVICE_NAME"

    # Enable lingering so the service runs without an active login session
    echo "Enabling lingering for user $(whoami)..."
    sudo loginctl enable-linger "$(whoami)" 2>/dev/null || \
        echo "  WARN: Could not enable-linger (need sudo). Service will only run while logged in."

    echo ""
    echo "=== systemd service installed ==="
    echo "  Unit:    $UNIT_FILE"
    echo "  Status:  systemctl --user status $SERVICE_NAME"
    echo "  Logs:    journalctl --user -u $SERVICE_NAME -f"
    echo "  Stop:    systemctl --user stop $SERVICE_NAME"
    echo "  Restart: systemctl --user restart $SERVICE_NAME"
    echo ""

    systemctl --user status "$SERVICE_NAME" --no-pager || true
}


# ── macOS: launchd user agent ────────────────────────────────────────────────

install_launchd() {
    local LABEL="com.vibeaway"
    local PLIST_DIR="$HOME/Library/LaunchAgents"
    local PLIST_FILE="$PLIST_DIR/$LABEL.plist"
    local PYTHON="$VENV_DIR/bin/python"

    echo "Installing launchd user agent..."

    launchctl unload "$PLIST_FILE" 2>/dev/null || true
    mkdir -p "$PLIST_DIR"

    cat > "$PLIST_FILE" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$WATCHDOG</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$SERVICE_DIR</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>

    <key>ThrottleInterval</key>
    <integer>30</integer>

    <key>StandardOutPath</key>
    <string>$RUNTIME_DIR/logs/launchd-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$RUNTIME_DIR/logs/launchd-stderr.log</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$VENV_DIR/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
EOF

    launchctl load "$PLIST_FILE"

    echo ""
    echo "=== launchd agent installed ==="
    echo "  Plist:   $PLIST_FILE"
    echo "  Status:  launchctl list | grep $LABEL"
    echo "  Stop:    launchctl unload $PLIST_FILE"
    echo "  Restart: launchctl unload $PLIST_FILE && launchctl load $PLIST_FILE"
    echo "  Logs:    tail -f $RUNTIME_DIR/logs/watchdog.log"
    echo ""

    launchctl list | grep "$LABEL" || true
}


# ── Detect platform and install ──────────────────────────────────────────────

case "$(uname -s)" in
    Linux*)  install_systemd ;;
    Darwin*) install_launchd ;;
    *)
        echo "Unsupported platform: $(uname -s)"
        echo "On Windows, use: .\\service\\install-service.ps1"
        exit 1
        ;;
esac
