"""
Vehicle Gate Management System — Backend
Flask server providing MJPEG live stream, OCR-based plate detection,
unauthorized detection, activity logging, allowlist management, and CSV export.
"""

from flask import Flask, render_template, Response, jsonify, request, make_response
import cv2
import easyocr
from datetime import datetime
import threading
import io
import csv
import time
import difflib

app = Flask(__name__)

# ── OCR Reader (English) ──────────────────────────────────────────────────────
reader = easyocr.Reader(['en'])

# ── Shared State ──────────────────────────────────────────────────────────────
state_lock = threading.Lock()

allowed_vehicles = [
    "TN09AB1234",
    "TN10XY5678",
    "TN82Y8388"
]

activity_log = []  # newest first; max 200 entries

latest_detection = {
    "plate": "Scanning...",
    "time": "--:--:--",
    "date": "----",
    "status": "IDLE"
}

stats = {
    "authorized": 0,
    "unauthorized": 0,
    "total": 0
}

# ── Camera Frame Buffer (MJPEG) ───────────────────────────────────────────────
latest_frame = None
frame_lock = threading.Lock()

# ── Cooldown to prevent duplicate log spam ────────────────────────────────────
last_detected_plate = None
last_detected_time = 0
COOLDOWN_SECONDS = 5


# ── Camera + OCR Detection Thread ─────────────────────────────────────────────
def camera_detection():
    global latest_frame, latest_detection, last_detected_plate, last_detected_time

    cap = cv2.VideoCapture(0)
    frame_count = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            time.sleep(0.1)
            continue

        frame_count += 1

        # Run OCR every 5 frames to keep CPU load manageable
        if frame_count % 5 == 0:
            results = reader.readtext(frame)

            for result in results:
                text = result[1]
                confidence = result[2]

                # Skip low-confidence reads
                if confidence < 0.3:
                    continue

                cleaned = text.replace(" ", "").upper()

                # Filter obvious noise (too short or too long for a plate)
                if len(cleaned) < 5 or len(cleaned) > 12:
                    continue

                now = time.time()

                status = "UNAUTHORIZED"
                # Fuzzy matching to handle OCR errors (e.g., '0' read as 'O', '1' as 'I')
                for allowed in allowed_vehicles:
                    if difflib.SequenceMatcher(None, cleaned, allowed).ratio() > 0.85:
                        status = "AUTHORIZED"
                        cleaned = allowed  # Auto-correct to the true plate
                        break

                # Cooldown: ignore if same plate was just logged
                if cleaned == last_detected_plate and (now - last_detected_time) < COOLDOWN_SECONDS:
                    continue

                last_detected_plate = cleaned
                last_detected_time = now
                ts = datetime.now()

                entry = {
                    "plate": cleaned,
                    "time": ts.strftime("%H:%M:%S"),
                    "date": ts.strftime("%Y-%m-%d"),
                    "status": status
                }

                with state_lock:
                    latest_detection.update(entry)
                    activity_log.insert(0, entry)
                    if len(activity_log) > 200:
                        activity_log.pop()
                    if status == "AUTHORIZED":
                        stats["authorized"] += 1
                    else:
                        stats["unauthorized"] += 1
                    stats["total"] += 1

                print(f"[{status}] {cleaned} at {entry['time']}")

        # Draw overlay text on the frame before streaming
        with state_lock:
            det = dict(latest_detection)

        color = (
            (50, 220, 120)  if det["status"] == "AUTHORIZED"   else
            (60,  60, 255)  if det["status"] == "UNAUTHORIZED" else
            (180, 180, 180)
        )
        overlay = frame.copy()
        cv2.rectangle(overlay, (0, 0), (frame.shape[1], 80), (10, 10, 25), -1)
        cv2.addWeighted(overlay, 0.6, frame, 0.4, 0, frame)
        cv2.putText(frame, f"Plate : {det['plate']}", (10, 28),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.75, color, 2)
        cv2.putText(frame, f"Status: {det['status']}", (10, 62),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.75, color, 2)

        # Encode to JPEG and push to frame buffer
        _, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        with frame_lock:
            latest_frame = jpeg.tobytes()

    cap.release()


# ── MJPEG Generator ───────────────────────────────────────────────────────────
def generate_frames():
    while True:
        with frame_lock:
            frame = latest_frame
        if frame is None:
            time.sleep(0.05)
            continue
        yield (
            b'--frame\r\n'
            b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n'
        )
        time.sleep(0.033)  # ~30 fps cap


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def home():
    return render_template('index.html')


@app.route('/video_feed')
def video_feed():
    """Live MJPEG camera stream."""
    return Response(generate_frames(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route('/status')
def status():
    """Latest detection result."""
    with state_lock:
        return jsonify(latest_detection)


@app.route('/log')
def log():
    """Last 50 activity log entries."""
    with state_lock:
        return jsonify(activity_log[:50])


@app.route('/stats')
def get_stats():
    """Dashboard statistics."""
    with state_lock:
        return jsonify({**stats, "allowed_count": len(allowed_vehicles)})


@app.route('/allowed_vehicles')
def get_allowed():
    """Return current allowlist."""
    with state_lock:
        return jsonify(allowed_vehicles)


@app.route('/add_vehicle', methods=['POST'])
def add_vehicle():
    """Add a plate to the allowlist."""
    data = request.get_json()
    plate = data.get('plate', '').replace(' ', '').upper()
    if not plate:
        return jsonify({"success": False, "error": "Empty plate"})
    with state_lock:
        if plate not in allowed_vehicles:
            allowed_vehicles.append(plate)
    return jsonify({"success": True, "plate": plate})


@app.route('/remove_vehicle', methods=['POST'])
def remove_vehicle():
    """Remove a plate from the allowlist."""
    data = request.get_json()
    plate = data.get('plate', '').replace(' ', '').upper()
    with state_lock:
        if plate in allowed_vehicles:
            allowed_vehicles.remove(plate)
    return jsonify({"success": True})


@app.route('/export_log')
def export_log():
    """Download activity log as CSV."""
    with state_lock:
        log_copy = list(activity_log)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=['date', 'time', 'plate', 'status'])
    writer.writeheader()
    writer.writerows(log_copy)
    response = make_response(output.getvalue())
    response.headers['Content-Type'] = 'text/csv'
    response.headers['Content-Disposition'] = 'attachment; filename=vehicle_log.csv'
    return response


# ── Entry Point ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    # Camera runs in a daemon thread; Flask serves everything including MJPEG
    cam_thread = threading.Thread(target=camera_detection, daemon=True)
    cam_thread.start()
    app.run(debug=False, host='0.0.0.0', port=5000)