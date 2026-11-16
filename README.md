# 🚗 VehicleGate — Smart Vehicle Entry Management System

A real-time license plate detection dashboard built with Python, Flask, and EasyOCR. It reads your webcam feed, recognizes vehicle plate numbers, and instantly classifies them as **Authorized** or **Unauthorized** — all from a sleek browser dashboard.

---

## ✨ Features

- 🔐 **Authentication & Security** — Login portal with secure password hashing and session management
- 🎯 **Plate Localization (Haar + Morphological)** — Detects & isolates candidate plate bounding boxes before running OCR, eliminating background clutter
- 🎥 **Live Camera Stream** — Webcam feed with real-time localized bounding boxes & status HUD
- 🔍 **Targeted OCR Detection** — High-accuracy character recognition on cropped, preprocessed plate ROIs
- ✅ **Authorized / 🚫 Unauthorized Detection** — Instantly flags unknown vehicles
- 🔔 **Alert System** — Flashing banner + audio alert on unauthorized detection
- 📋 **Activity Log** — Scrollable, color-coded history of all detections
- 📊 **Dashboard Stats** — Authorized, unauthorized, total scan counters
- ✏️ **Manage Allowlist** — Add or remove plates from the UI (no code changes needed)
- 📤 **Export CSV** — Download full activity log with one click

---

## 📁 Project Structure

```
VMS/
├── app.py              # Flask backend (API + OCR detection)
├── requirements.txt    # Python dependencies
├── run.bat             # One-click launcher (Windows)
└── templates/
    ├── index.html      # Premium dark dashboard UI
    └── login.html      # Sleek security portal authentication UI
```

---

## 🔐 Default Credentials

| Username | Password | Role |
|---|---|---|
| `admin` | `admin123` | Administrator (Full access) |
| `guard` | `guard123` | Security Guard (Gate operations) |

---

## 🚀 How to Run

### Option 1 — Double-click (Recommended)

Just double-click **`run.bat`**.  
It will automatically:
1. Create a Python virtual environment
2. Install all dependencies
3. Launch the app

Then open your browser at → **http://localhost:5000**

---

### Option 2 — Manual

```bash
# 1. Create and activate virtual environment
python -m venv venv
venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run
python app.py
```

Then open → **http://localhost:5000**

---

## ⚙️ Configuration

To pre-set your allowed vehicles, edit the list in `app.py`:

```python
allowed_vehicles = [
    "TN09AB1234",
    "TN10XY5678",
    "TN82Y8388"
]
```

You can also add/remove plates live from the dashboard's **Allowlist Manager** panel — no restart needed.

---

## 🛠️ Requirements

- Python 3.8+
- Webcam connected
- Windows OS (for `run.bat`)

---

## 📦 Dependencies

| Package | Purpose |
|---|---|
| `Flask` | Web server & API |
| `opencv-python` | Webcam capture & frame processing |
| `easyocr` | License plate text recognition |
