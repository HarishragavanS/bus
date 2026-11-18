"""
VehicleGate — Smart Vehicle Entry System (Raspberry Pi Edition)

Flask backend serving MJPEG live stream, OCR-based license plate detection,
unauthorized vehicle alerts, activity logging, allowlist management, and CSV export.

Optimized for Raspberry Pi (ARM Linux):
  - Camera capture and OCR run on separate daemon threads so the MJPEG stream
    never stalls during inference.
  - EasyOCR explicitly runs on CPU (gpu=False).
  - OpenCV headless build (opencv-python-headless) — no display needed.
  - Activity log and allowlist are persisted to data.json so reboots don't wipe state.
  - Optional: set GPIO_RELAY_PIN env var to a BCM pin number to trigger a relay
    on AUTHORIZED detections (e.g. export GPIO_RELAY_PIN=17).

Run:  python3 app.py
Env:  SECRET_KEY (optional, random default used if absent)
      GPIO_RELAY_PIN (optional, BCM pin, e.g. 17)
"""

from flask import (
    Flask, render_template, Response, jsonify,
    request, make_response, session, redirect, url_for
)
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
import os
import cv2
import numpy as np
import easyocr
import threading
import queue
import io
import csv
import time
import difflib
import json
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "vehicle-gate-secret-key-39d8e72f1a")
app.permanent_session_lifetime = timedelta(days=7)

DATA_FILE = os.path.join(os.path.dirname(__file__), "data.json")

USERS = {
    "admin": generate_password_hash("admin123"),
    "guard": generate_password_hash("guard123"),
}

GPIO_PIN = None
try:
    _pin_env = os.environ.get("GPIO_RELAY_PIN")
    if _pin_env:
        import RPi.GPIO as GPIO
        GPIO_PIN = int(_pin_env)
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(GPIO_PIN, GPIO.OUT, initial=GPIO.LOW)
        log.info("GPIO relay armed on BCM pin %d", GPIO_PIN)
except Exception as _gpio_err:
    log.warning("GPIO not available: %s", _gpio_err)
    GPIO_PIN = None


def _trigger_relay(seconds: float = 3.0):
    """Pulse the relay GPIO pin high for `seconds` then return it low."""
    if GPIO_PIN is None:
        return
    try:
        import RPi.GPIO as GPIO
        GPIO.output(GPIO_PIN, GPIO.HIGH)
        time.sleep(seconds)
        GPIO.output(GPIO_PIN, GPIO.LOW)
    except Exception as exc:
        log.error("Relay trigger failed: %s", exc)


def _load_data():
    """Load persisted allowlist and activity log from disk, returning defaults on error."""
    try:
        with open(DATA_FILE, "r") as f:
            d = json.load(f)
        return d.get("allowed_vehicles", _default_allowlist()), d.get("activity_log", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return _default_allowlist(), []


def _default_allowlist():
    return ["TN09AB1234", "TN10XY5678", "TN82Y8388"]


def _save_data():
    """Flush the current allowlist and activity log to disk (called under state_lock)."""
    try:
        with open(DATA_FILE, "w") as f:
            json.dump(
                {"allowed_vehicles": allowed_vehicles, "activity_log": activity_log[:200]},
                f, indent=2
            )
    except Exception as exc:
        log.error("Failed to persist data: %s", exc)


_initial_allowed, _initial_log = _load_data()

state_lock = threading.Lock()
allowed_vehicles: list = _initial_allowed
activity_log: list = _initial_log
stats = {"authorized": 0, "unauthorized": 0, "total": 0}

latest_detection = {
    "plate": "Scanning...",
    "time": "--:--:--",
    "date": "----",
    "status": "IDLE",
    "bbox": None,
    "bbox_time": 0,
}

frame_lock = threading.Lock()
latest_frame: bytes | None = None

# Single-slot queue: OCR worker always processes the most recent frame, never queues stale ones
_ocr_queue: queue.Queue = queue.Queue(maxsize=1)

COOLDOWN_SECONDS = 5
_last_plate = None
_last_plate_time = 0.0

_ocr_reader = None
_ocr_reader_lock = threading.Lock()


def _get_reader() -> easyocr.Reader:
    global _ocr_reader
    if _ocr_reader is None:
        with _ocr_reader_lock:
            if _ocr_reader is None:
                log.info("Loading EasyOCR model (first run may take ~30 s on Pi)...")
                _ocr_reader = easyocr.Reader(["en"], gpu=False)
                log.info("EasyOCR ready.")
    return _ocr_reader


_plate_cascade = None


def _get_cascade() -> cv2.CascadeClassifier | None:
    global _plate_cascade
    if _plate_cascade is None:
        path = cv2.data.haarcascades + "haarcascade_russian_plate_number.xml"
        _plate_cascade = cv2.CascadeClassifier(path)
    return _plate_cascade


def _locate_plates(frame: np.ndarray) -> list[tuple]:
    """
    Hybrid Haar cascade + morphological contour locator.
    Returns up to 4 bounding boxes [(x, y, w, h)] after NMS deduplication.
    """
    if frame is None or frame.size == 0:
        return []

    h_f, w_f = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    candidates = []

    cascade = _get_cascade()
    if cascade and not cascade.empty():
        for (x, y, w, h) in cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=3,
            minSize=(int(w_f * 0.08), int(h_f * 0.03)),
            maxSize=(int(w_f * 0.85), int(h_f * 0.55)),
        ):
            candidates.append((int(x), int(y), int(w), int(h)))

    blur = cv2.bilateralFilter(gray, 9, 75, 75)
    edges = cv2.Canny(blur, 50, 180)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (13, 5))
    closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(closed, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)

    frame_area = w_f * h_f
    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        aspect = float(w) / h if h > 0 else 0
        area = w * h
        if 1.8 <= aspect <= 6.0 and (frame_area * 0.005) <= area <= (frame_area * 0.45):
            candidates.append((int(x), int(y), int(w), int(h)))

    if not candidates:
        return []

    boxes = np.array(candidates)
    x1, y1 = boxes[:, 0], boxes[:, 1]
    x2, y2 = boxes[:, 0] + boxes[:, 2], boxes[:, 1] + boxes[:, 3]
    areas = (x2 - x1) * (y2 - y1)
    order = np.argsort(areas)[::-1]

    keep = []
    while len(order) > 0:
        i = order[0]
        keep.append((int(boxes[i][0]), int(boxes[i][1]), int(boxes[i][2]), int(boxes[i][3])))
        if len(order) == 1:
            break
        iou = (
            np.maximum(0, np.minimum(x2[i], x2[order[1:]]) - np.maximum(x1[i], x1[order[1:]])) *
            np.maximum(0, np.minimum(y2[i], y2[order[1:]]) - np.maximum(y1[i], y1[order[1:]]))
        ) / (areas[i] + areas[order[1:]] - (
            np.maximum(0, np.minimum(x2[i], x2[order[1:]]) - np.maximum(x1[i], x1[order[1:]])) *
            np.maximum(0, np.minimum(y2[i], y2[order[1:]]) - np.maximum(y1[i], y1[order[1:]]))
        ) + 1e-6)
        order = order[np.where(iou <= 0.35)[0] + 1]

    return keep[:4]


def _preprocess_roi(roi: np.ndarray) -> np.ndarray | None:
    """Scale plate ROI to a fixed height so OCR always sees consistently sized characters."""
    if roi is None or roi.size == 0:
        return None
    h, w = roi.shape[:2]
    target_h = max(70, min(140, h))
    if h != target_h and h > 0:
        scale = target_h / float(h)
        roi = cv2.resize(roi, (max(100, int(w * scale)), target_h), interpolation=cv2.INTER_CUBIC)
    return roi


def _ocr_worker():
    """
    Dedicated OCR thread — consumes frames from _ocr_queue and updates shared state.
    Runs independently of the camera thread so the MJPEG stream is never blocked.
    """
    global _last_plate, _last_plate_time

    while True:
        frame = _ocr_queue.get()
        if frame is None:
            break

        candidates = _locate_plates(frame)
        if not candidates:
            continue

        reader = _get_reader()

        for (cx, cy, cw, ch) in candidates:
            pad_x, pad_y = int(cw * 0.06), int(ch * 0.12)
            x1 = max(0, cx - pad_x)
            y1 = max(0, cy - pad_y)
            x2 = min(frame.shape[1], cx + cw + pad_x)
            y2 = min(frame.shape[0], cy + ch + pad_y)

            roi = _preprocess_roi(frame[y1:y2, x1:x2])
            if roi is None:
                continue

            results = reader.readtext(roi)
            matched = False

            for _, text, confidence in results:
                if confidence < 0.3:
                    continue

                cleaned = "".join(c for c in text.replace(" ", "").upper() if c.isalnum())
                if not (5 <= len(cleaned) <= 12):
                    continue

                now = time.time()

                status = "UNAUTHORIZED"
                with state_lock:
                    local_allowed = list(allowed_vehicles)

                for allowed in local_allowed:
                    if difflib.SequenceMatcher(None, cleaned, allowed).ratio() > 0.85:
                        status = "AUTHORIZED"
                        cleaned = allowed
                        break

                if cleaned == _last_plate and (now - _last_plate_time) < COOLDOWN_SECONDS:
                    with state_lock:
                        latest_detection["bbox"] = [cx, cy, cw, ch]
                        latest_detection["bbox_time"] = now
                    matched = True
                    break

                _last_plate = cleaned
                _last_plate_time = now
                ts = datetime.now()

                entry = {
                    "plate": cleaned,
                    "time": ts.strftime("%H:%M:%S"),
                    "date": ts.strftime("%Y-%m-%d"),
                    "status": status,
                    "bbox": [cx, cy, cw, ch],
                    "bbox_time": now,
                }

                with state_lock:
                    latest_detection.update(entry)
                    activity_log.insert(0, entry)
                    if len(activity_log) > 200:
                        activity_log.pop()
                    stats["total"] += 1
                    if status == "AUTHORIZED":
                        stats["authorized"] += 1
                        threading.Thread(target=_trigger_relay, daemon=True).start()
                    else:
                        stats["unauthorized"] += 1
                    _save_data()

                log.info("[%s] %s  bbox=(x=%d y=%d w=%d h=%d)", status, cleaned, cx, cy, cw, ch)
                matched = True
                break

            if matched:
                break


def _camera_thread():
    """
    Camera capture loop — reads frames at full speed, pushes every Nth frame to the
    OCR queue (non-blocking so the queue never accumulates stale frames), and
    overlays HUD graphics before writing to the shared MJPEG buffer.
    """
    cap = cv2.VideoCapture(0)

    # Reduce resolution to 640×480 — plenty for plate detection, cuts CPU load
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    cap.set(cv2.CAP_PROP_FPS, 15)

    if not cap.isOpened():
        log.error("Cannot open camera. Check connection and /dev/video0 permissions.")
        return

    frame_count = 0
    OCR_EVERY_N = 8  # Submit one frame to OCR worker every N captured frames

    while True:
        ret, frame = cap.read()
        if not ret:
            time.sleep(0.1)
            continue

        frame_count += 1

        if frame_count % OCR_EVERY_N == 0:
            # Drop oldest item if queue is full so worker always gets freshest frame
            if _ocr_queue.full():
                try:
                    _ocr_queue.get_nowait()
                except queue.Empty:
                    pass
            try:
                _ocr_queue.put_nowait(frame.copy())
            except queue.Full:
                pass

        with state_lock:
            det = dict(latest_detection)

        now = time.time()
        color = (
            (50, 220, 120) if det["status"] == "AUTHORIZED" else
            (60, 60, 255)  if det["status"] == "UNAUTHORIZED" else
            (180, 180, 180)
        )

        if det.get("bbox") and (now - det.get("bbox_time", 0)) < COOLDOWN_SECONDS:
            bx, by, bw, bh = det["bbox"]
            cv2.rectangle(frame, (bx, by), (bx + bw, by + bh), color, 2)
            cl = min(15, max(6, bw // 4), max(6, bh // 4))
            for (px, py), (dx, dy) in [
                ((bx, by), (cl, 0)), ((bx, by), (0, cl)),
                ((bx + bw, by), (-cl, 0)), ((bx + bw, by), (0, cl)),
                ((bx, by + bh), (cl, 0)), ((bx, by + bh), (0, -cl)),
                ((bx + bw, by + bh), (-cl, 0)), ((bx + bw, by + bh), (0, -cl)),
            ]:
                cv2.line(frame, (px, py), (px + dx, py + dy), color, 3)

            tag = f"PLATE: {det['plate']} [{det['status']}]"
            (tw, th), _ = cv2.getTextSize(tag, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
            tag_y = max(th + 6, by - 6)
            cv2.rectangle(frame, (bx, tag_y - th - 6), (bx + tw + 10, tag_y + 4), color, -1)
            cv2.putText(frame, tag, (bx + 5, tag_y - 2),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (10, 10, 20), 1, cv2.LINE_AA)

        overlay = frame.copy()
        cv2.rectangle(overlay, (0, 0), (frame.shape[1], 80), (10, 10, 25), -1)
        cv2.addWeighted(overlay, 0.6, frame, 0.4, 0, frame)
        cv2.putText(frame, f"Plate : {det['plate']}", (10, 28),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.75, color, 2)
        cv2.putText(frame, f"Status: {det['status']}", (10, 62),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.75, color, 2)

        _, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
        with frame_lock:
            latest_frame = jpeg.tobytes()

    cap.release()


def _generate_frames():
    """MJPEG multipart generator for /video_feed."""
    while True:
        with frame_lock:
            frame = latest_frame
        if frame is None:
            time.sleep(0.05)
            continue
        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
        )
        time.sleep(0.04)  # ~25 FPS cap; keeps bandwidth sane on a local network


@app.before_request
def _require_login():
    if request.endpoint in ("login", "static"):
        return
    if "user" not in session:
        ajax_paths = {"/status", "/log", "/stats", "/allowed_vehicles",
                      "/add_vehicle", "/remove_vehicle", "/export_log"}
        if request.path in ajax_paths:
            return jsonify({"error": "Unauthorized", "login_required": True}), 401
        return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if "user" in session:
        return redirect(url_for("home"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if username in USERS and check_password_hash(USERS[username], password):
            session["user"] = username
            session.permanent = bool(request.form.get("remember_me"))
            return redirect(url_for("home"))
        error = "Invalid username or password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def home():
    return render_template("index.html", current_user=session.get("user", "admin"))


@app.route("/video_feed")
def video_feed():
    return Response(_generate_frames(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/status")
def status():
    with state_lock:
        return jsonify(latest_detection)


@app.route("/log")
def get_log():
    with state_lock:
        return jsonify(activity_log[:50])


@app.route("/stats")
def get_stats():
    with state_lock:
        return jsonify({**stats, "allowed_count": len(allowed_vehicles)})


@app.route("/allowed_vehicles")
def get_allowed():
    with state_lock:
        return jsonify(allowed_vehicles)


@app.route("/add_vehicle", methods=["POST"])
def add_vehicle():
    plate = request.get_json().get("plate", "").replace(" ", "").upper()
    if not plate:
        return jsonify({"success": False, "error": "Empty plate"})
    with state_lock:
        if plate not in allowed_vehicles:
            allowed_vehicles.append(plate)
        _save_data()
    return jsonify({"success": True, "plate": plate})


@app.route("/remove_vehicle", methods=["POST"])
def remove_vehicle():
    plate = request.get_json().get("plate", "").replace(" ", "").upper()
    with state_lock:
        if plate in allowed_vehicles:
            allowed_vehicles.remove(plate)
        _save_data()
    return jsonify({"success": True})


@app.route("/export_log")
def export_log():
    with state_lock:
        log_copy = list(activity_log)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["date", "time", "plate", "status"])
    writer.writeheader()
    writer.writerows({"date": e["date"], "time": e["time"], "plate": e["plate"], "status": e["status"]} for e in log_copy)
    response = make_response(output.getvalue())
    response.headers["Content-Type"] = "text/csv"
    response.headers["Content-Disposition"] = "attachment; filename=vehicle_log.csv"
    return response


if __name__ == "__main__":
    threading.Thread(target=_ocr_worker, daemon=True).start()
    threading.Thread(target=_camera_thread, daemon=True).start()
    log.info("VehicleGate starting on http://0.0.0.0:5000")
    app.run(debug=False, host="0.0.0.0", port=5000, threaded=True)
