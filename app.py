"""
Vehicle Gate Management System — Backend
Flask server providing MJPEG live stream, OCR-based plate detection,
unauthorized detection, activity logging, allowlist management, and CSV export.
"""

from flask import Flask, render_template, Response, jsonify, request, make_response, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
import os
import cv2
import numpy as np
import easyocr
import threading
import io
import csv
import time
import difflib

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'vehicle-gate-secret-key-39d8e72f1a')
app.permanent_session_lifetime = timedelta(days=7)

# ── User Accounts ─────────────────────────────────────────────────────────────
USERS = {
    "admin": generate_password_hash("admin123"),
    "guard": generate_password_hash("guard123")
}

# ── OCR Reader (Lazy Loaded) ──────────────────────────────────────────────────
reader = None

def get_reader():
    global reader
    if reader is None:
        reader = easyocr.Reader(['en'])
    return reader

# ── License Plate Localization (Haar + Contour Morphological Hybrid) ──────────
_plate_cascade = None

def get_plate_cascade():
    global _plate_cascade
    if _plate_cascade is None:
        cascade_path = cv2.data.haarcascades + 'haarcascade_russian_plate_number.xml'
        _plate_cascade = cv2.CascadeClassifier(cascade_path)
    return _plate_cascade


def locate_license_plates(frame):
    """
    Locates candidate license plate regions in the frame using a hybrid
    Haar cascade + morphological contour edge detection pipeline with NMS.
    Returns candidate bounding boxes: [(x, y, w, h), ...]
    """
    if frame is None or frame.size == 0:
        return []

    h_frame, w_frame = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    candidates = []

    # 1. Pretrained Haar Cascade Plate Detector
    cascade = get_plate_cascade()
    if cascade is not None and not cascade.empty():
        haar_boxes = cascade.detectMultiScale(
            gray,
            scaleFactor=1.1,
            minNeighbors=3,
            minSize=(int(w_frame * 0.08), int(h_frame * 0.03)),
            maxSize=(int(w_frame * 0.85), int(h_frame * 0.55))
        )
        for (x, y, w, h) in haar_boxes:
            candidates.append((int(x), int(y), int(w), int(h)))

    # 2. Morphological Edge & Aspect Ratio Contour Localization
    blur = cv2.bilateralFilter(gray, 9, 75, 75)
    edges = cv2.Canny(blur, 50, 180)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (13, 5))
    closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(closed, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)

    frame_area = w_frame * h_frame
    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        aspect = float(w) / h if h > 0 else 0
        area = w * h
        # Standard license plates typically have aspect ratio between 1.8 and 6.0
        if 1.8 <= aspect <= 6.0 and (frame_area * 0.005) <= area <= (frame_area * 0.45):
            candidates.append((int(x), int(y), int(w), int(h)))

    if not candidates:
        return []

    # Non-Maximum Suppression (NMS) to eliminate duplicate/nested overlapping boxes
    boxes = np.array(candidates)
    x1 = boxes[:, 0]
    y1 = boxes[:, 1]
    x2 = boxes[:, 0] + boxes[:, 2]
    y2 = boxes[:, 1] + boxes[:, 3]
    areas = (x2 - x1) * (y2 - y1)
    order = np.argsort(areas)[::-1]

    keep = []
    while len(order) > 0:
        i = order[0]
        keep.append((int(boxes[i][0]), int(boxes[i][1]), int(boxes[i][2]), int(boxes[i][3])))
        if len(order) == 1:
            break
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        w_inter = np.maximum(0, xx2 - xx1)
        h_inter = np.maximum(0, yy2 - yy1)
        inter = w_inter * h_inter
        iou = inter / (areas[i] + areas[order[1:]] - inter)
        inds = np.where(iou <= 0.35)[0]
        order = order[inds + 1]

    return keep[:4]


def preprocess_plate_roi(roi):
    """
    Standardize and enhance the cropped license plate ROI for OCR:
    - Normalizes resolution so characters are clearly readable.
    - Preserves aspect ratio.
    """
    if roi is None or roi.size == 0:
        return None
    h, w = roi.shape[:2]
    target_h = max(70, min(140, h))
    if h != target_h and h > 0:
        scale = target_h / float(h)
        new_w = max(100, int(w * scale))
        roi = cv2.resize(roi, (new_w, target_h), interpolation=cv2.INTER_CUBIC)
    return roi

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
    "status": "IDLE",
    "bbox": None,
    "bbox_time": 0
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
    current_candidates = []

    while True:
        ret, frame = cap.read()
        if not ret:
            time.sleep(0.1)
            continue

        frame_count += 1

        # Run Plate Localization + OCR every 5 frames
        if frame_count % 5 == 0:
            # Step 1: Localize license plate candidate regions in the frame
            current_candidates = locate_license_plates(frame)

            # Step 2: If candidates found, run OCR strictly on cropped & preprocessed ROIs
            if current_candidates:
                ocr_reader = get_reader()

                for (cx, cy, cw, ch) in current_candidates:
                    # Crop ROI with safe margin (avoids clipping outer characters)
                    pad_x = int(cw * 0.06)
                    pad_y = int(ch * 0.12)
                    x1 = max(0, cx - pad_x)
                    y1 = max(0, cy - pad_y)
                    x2 = min(frame.shape[1], cx + cw + pad_x)
                    y2 = min(frame.shape[0], cy + ch + pad_y)
                    plate_roi = frame[y1:y2, x1:x2]

                    # Standardize & enhance ROI resolution for OCR
                    processed_roi = preprocess_plate_roi(plate_roi)
                    if processed_roi is None:
                        continue

                    results = ocr_reader.readtext(processed_roi)

                    detected_in_candidate = False
                    for result in results:
                        text = result[1]
                        confidence = result[2]

                        # Skip low-confidence reads
                        if confidence < 0.3:
                            continue

                        # Clean string: uppercase alphanumeric characters only
                        cleaned = text.replace(" ", "").upper()
                        cleaned = "".join(c for c in cleaned if c.isalnum())

                        # Filter noise (standard plate lengths)
                        if len(cleaned) < 5 or len(cleaned) > 12:
                            continue

                        now = time.time()
                        status = "UNAUTHORIZED"

                        # Fuzzy matching against allowed vehicles
                        for allowed in allowed_vehicles:
                            if difflib.SequenceMatcher(None, cleaned, allowed).ratio() > 0.85:
                                status = "AUTHORIZED"
                                cleaned = allowed  # Auto-correct to true plate
                                break

                        # Cooldown check
                        if cleaned == last_detected_plate and (now - last_detected_time) < COOLDOWN_SECONDS:
                            detected_in_candidate = True
                            with state_lock:
                                latest_detection["bbox"] = [int(cx), int(cy), int(cw), int(ch)]
                                latest_detection["bbox_time"] = now
                            break

                        last_detected_plate = cleaned
                        last_detected_time = now
                        ts = datetime.now()

                        entry = {
                            "plate": cleaned,
                            "time": ts.strftime("%H:%M:%S"),
                            "date": ts.strftime("%Y-%m-%d"),
                            "status": status,
                            "bbox": [int(cx), int(cy), int(cw), int(ch)],
                            "bbox_time": now
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

                        print(f"[{status}] Localized plate {cleaned} at (x={cx}, y={cy}, w={cw}, h={ch}) at {entry['time']}")
                        detected_in_candidate = True
                        break

                    if detected_in_candidate:
                        break

        # Draw overlays on the frame before streaming
        with state_lock:
            det = dict(latest_detection)

        now = time.time()
        color = (
            (50, 220, 120)  if det["status"] == "AUTHORIZED"   else
            (60,  60, 255)  if det["status"] == "UNAUTHORIZED" else
            (180, 180, 180)
        )

        # 1. Draw localized plate bounding box on vehicle if recently detected
        if det.get("bbox") and (now - det.get("bbox_time", 0)) < COOLDOWN_SECONDS:
            bx, by, bw, bh = det["bbox"]
            # Plate bounding box
            cv2.rectangle(frame, (bx, by), (bx + bw, by + bh), color, 2)

            # High-tech corner bracket accents
            corner_len = min(15, max(6, bw // 4), max(6, bh // 4))
            cv2.line(frame, (bx, by), (bx + corner_len, by), color, 3)
            cv2.line(frame, (bx, by), (bx, by + corner_len), color, 3)
            cv2.line(frame, (bx + bw, by), (bx + bw - corner_len, by), color, 3)
            cv2.line(frame, (bx + bw, by), (bx + bw, by + corner_len), color, 3)
            cv2.line(frame, (bx, by + bh), (bx + corner_len, by + bh), color, 3)
            cv2.line(frame, (bx, by + bh), (bx, by + bh - corner_len), color, 3)
            cv2.line(frame, (bx + bw, by + bh), (bx + bw - corner_len, by + bh), color, 3)
            cv2.line(frame, (bx + bw, by + bh), (bx + bw, by + bh - corner_len), color, 3)

            # Floating classification badge pinned to localized plate
            tag = f"PLATE: {det['plate']} [{det['status']}]"
            (tw, th), _ = cv2.getTextSize(tag, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
            tag_y = max(th + 6, by - 6)
            cv2.rectangle(frame, (bx, tag_y - th - 6), (bx + tw + 10, tag_y + 4), color, -1)
            cv2.putText(frame, tag, (bx + 5, tag_y - 2),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (10, 10, 20), 1, cv2.LINE_AA)
        elif current_candidates:
            # Draw subtle candidate locator boxes while scanning
            for (cx, cy, cw, ch) in current_candidates:
                cv2.rectangle(frame, (cx, cy), (cx + cw, cy + ch), (255, 180, 40), 1)
                cv2.putText(frame, "PLATE CANDIDATE", (cx, max(12, cy - 4)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 180, 40), 1, cv2.LINE_AA)

        # 2. Draw Top Gate HUD
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


# ── Authentication Middleware ─────────────────────────────────────────────────
@app.before_request
def require_login():
    # Allow login page and static assets without authentication
    if request.endpoint in ('login', 'static'):
        return
    if 'user' not in session:
        # For background polling and AJAX requests, respond with 401 JSON
        if request.path in ('/status', '/log', '/stats', '/allowed_vehicles', '/add_vehicle', '/remove_vehicle', '/export_log'):
            return jsonify({"error": "Unauthorized", "login_required": True}), 401
        return redirect(url_for('login'))


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Portal authentication page."""
    if 'user' in session:
        return redirect(url_for('home'))

    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        remember = request.form.get('remember_me')

        if username in USERS and check_password_hash(USERS[username], password):
            session['user'] = username
            session.permanent = bool(remember)
            return redirect(url_for('home'))
        else:
            error = "Invalid username or password. Please try again."

    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    """Clear session and log out."""
    session.clear()
    return redirect(url_for('login'))


@app.route('/')
def home():
    return render_template('index.html', current_user=session.get('user', 'admin'))


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