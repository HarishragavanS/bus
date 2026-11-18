#!/usr/bin/env bash
# setup.sh — One-time setup script for VehicleGate on Raspberry Pi.
# Run once after cloning / transferring the project to your Pi.
# Usage: bash setup.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "============================================="
echo "  VehicleGate — Raspberry Pi Setup"
echo "============================================="
echo ""

echo "[1/4] Installing system-level dependencies..."
sudo apt-get update -qq
sudo apt-get install -y \
    python3-pip \
    python3-venv \
    libgl1-mesa-glx \
    libglib2.0-0 \
    libatlas-base-dev \
    libopenblas-dev \
    v4l-utils \
    ffmpeg

echo "[2/4] Creating Python virtual environment..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo "      Virtual environment created."
else
    echo "      Virtual environment already exists, skipping."
fi

echo "[3/4] Installing Python packages..."
source venv/bin/activate
pip install --upgrade pip --quiet
pip install -r requirements.txt

echo "[4/4] Verifying camera device..."
if ls /dev/video* 1>/dev/null 2>&1; then
    echo "      Camera detected: $(ls /dev/video*)"
else
    echo "      WARNING: No /dev/video* device found."
    echo "      If using USB webcam, ensure it is plugged in."
    echo "      If using Raspberry Pi Camera Module (CSI), enable it:"
    echo "        sudo raspi-config -> Interface Options -> Camera -> Enable"
fi

echo ""
echo "Setup complete!"
echo "  To start manually:  bash run.sh"
echo "  To install service: bash install_service.sh"
echo ""
