#!/usr/bin/env bash
# run.sh — Start VehicleGate manually (for testing).
# For production / auto-start on boot, use install_service.sh instead.
# Usage: bash run.sh [--relay-pin <BCM_PIN>]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -f "venv/bin/activate" ]; then
    echo "[ERROR] Virtual environment not found. Run:  bash setup.sh"
    exit 1
fi

source venv/bin/activate

# Optional GPIO relay pin passed as argument, e.g.:  bash run.sh --relay-pin 17
RELAY_PIN=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --relay-pin) RELAY_PIN="$2"; shift 2 ;;
        *) shift ;;
    esac
done

if [ -n "$RELAY_PIN" ]; then
    export GPIO_RELAY_PIN="$RELAY_PIN"
    echo "GPIO relay armed on BCM pin $RELAY_PIN"
fi

echo ""
echo "============================================="
echo "  VehicleGate — Smart Vehicle Entry System"
echo "  URL: http://$(hostname -I | awk '{print $1}'):5000"
echo "  Press Ctrl+C to stop."
echo "============================================="
echo ""

exec python3 app.py
