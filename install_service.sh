#!/usr/bin/env bash
# install_service.sh — Installs VehicleGate as a systemd service so it
# starts automatically on boot and restarts on crashes.
# Run once after setup.sh completes.
# Usage: bash install_service.sh [--relay-pin <BCM_PIN>]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="vehiclegate"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
CURRENT_USER="$(whoami)"
PYTHON_BIN="$SCRIPT_DIR/venv/bin/python3"

RELAY_ENV=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --relay-pin) RELAY_ENV="Environment=GPIO_RELAY_PIN=$2"; shift 2 ;;
        *) shift ;;
    esac
done

if [ ! -f "$PYTHON_BIN" ]; then
    echo "[ERROR] Virtual environment not found at $SCRIPT_DIR/venv"
    echo "        Run:  bash setup.sh"
    exit 1
fi

echo "Installing systemd service as user: $CURRENT_USER"

sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=VehicleGate Smart Vehicle Entry System
After=network.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$SCRIPT_DIR
ExecStart=$PYTHON_BIN $SCRIPT_DIR/app.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
${RELAY_ENV}

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl start "$SERVICE_NAME"

echo ""
echo "Service installed and started."
echo "  Status:    sudo systemctl status $SERVICE_NAME"
echo "  Live logs: sudo journalctl -u $SERVICE_NAME -f"
echo "  Stop:      sudo systemctl stop $SERVICE_NAME"
echo "  Disable:   sudo systemctl disable $SERVICE_NAME"
echo ""
