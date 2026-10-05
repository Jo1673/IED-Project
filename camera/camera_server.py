#!/usr/bin/env python3
"""
Tiny webcam server for OctoPrint (Windows-friendly, uses OpenCV).

  python camera_server.py --list                 find which camera index is the Brio
  python camera_server.py --index 0              serve camera 0 on http://127.0.0.1:8080
  python camera_server.py --test-pattern         no camera needed, serves a moving test image

URLs:
  http://127.0.0.1:8080/           preview page
  http://127.0.0.1:8080/stream     MJPEG stream   -> OctoPrint "Stream URL"
  http://127.0.0.1:8080/snapshot   single JPEG    -> OctoPrint "Snapshot URL"

Optional evidence capture: --save-dir photos --save-every 60 writes a JPEG per minute.
"""
import argparse
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cv2
import numpy as np

BACKEND = cv2.CAP_DSHOW if sys.platform.startswith("win") else cv2.CAP_ANY


def open_camera(index, width, height, fps):
    cap = cv2.VideoCapture(index, BACKEND)
    if not cap.isOpened():
        return None
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))  # needed for 720p+ at 30 fps
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_FPS, fps)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap


def list_cameras(max_index=6):
    print("Probing camera indexes 0-%d ..." % (max_index - 1))
    found = False
    for i in range(max_index):
        cap = cv2.VideoCapture(i, BACKEND)
        if cap.isOpened():
            ok, frame = cap.read()
            if ok:
                h, w = frame.shape[:2]
                print("  index %d: works, %dx%d" % (i, w, h))
                found = True
            else:
                print("  index %d: opens but gives no frames (in use by another app?)" % i)
        cap.release()
    if not found:
        print("No working camera found. Close other apps using it (Teams, Zoom, Camera app).")
    print("The laptop's built-in webcam is usually index 0; an external Brio is then 1.")
    print("Open http://127.0.0.1:8080/ after starting the server to confirm which one you are seeing.")


class Grabber(threading.Thread):
    def __init__(self, args):
        super().__init__(daemon=True)
        self.args = args
        self.cond = threading.Condition()
        self.jpeg = None
        self.seq = 0
        self.stamp = 0.0
        self.quality = [int(cv2.IMWRITE_JPEG_QUALITY), args.quality]

    def _publish(self, frame):
        ok, buf = cv2.imencode(".jpg", frame, self.quality)
        if not ok:
            return
        with self.cond:
            self.jpeg = buf.tobytes()
            self.seq += 1
            self.stamp = time.time()
            self.cond.notify_all()

    def wait_new(self, last_seq, timeout=5.0):
        with self.cond:
            if self.seq == last_seq:
                self.cond.wait(timeout)
            if self.seq == last_seq:
                return None, last_seq
            return self.jpeg, self.seq

    def latest(self, max_age=5.0):
        with self.cond:
            if self.jpeg is None or time.time() - self.stamp > max_age:
                return None
            return self.jpeg

    def run(self):
        a = self.args
        if a.test_pattern:
            self._run_pattern()
            return
        interval = 1.0 / a.fps
        while True:
            cap = open_camera(a.index, a.width, a.height, a.fps)
            if cap is None:
                print("[camera] cannot open index %d, retrying in 3 s" % a.index)
                time.sleep(3)
                continue
            print("[camera] opened index %d (%dx%d)" % (
                a.index, int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))))
            fails = 0
            while fails < 30:
                t0 = time.time()
                ok, frame = cap.read()
                if not ok:
                    fails += 1
                    time.sleep(0.05)
                    continue
                fails = 0
                self._publish(frame)
                dt = time.time() - t0
                if dt < interval:
                    time.sleep(interval - dt)
            print("[camera] lost frames, reopening")
            cap.release()

    def _run_pattern(self):
        a = self.args
        i = 0
        while True:
            frame = np.full((a.height, a.width, 3), 40, np.uint8)
            x = int((i * 7) % (a.width - 60))
            cv2.rectangle(frame, (x, a.height // 2 - 30), (x + 60, a.height // 2 + 30), (0, 200, 255), -1)
            cv2.putText(frame, "TEST PATTERN " + datetime.now().strftime("%H:%M:%S"), (20, 40),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
            self._publish(frame)
            i += 1
            time.sleep(1.0 / a.fps)


def saver(grabber, folder, every):
    folder.mkdir(parents=True, exist_ok=True)
    while True:
        time.sleep(every)
        jpg = grabber.latest()
        if jpg:
            name = folder / datetime.now().strftime("cam_%Y%m%d_%H%M%S.jpg")
            name.write_bytes(jpg)


PAGE = (b"<!doctype html><title>Print camera</title><body style='margin:0;background:#111;color:#ddd;"
        b"font-family:sans-serif'><p style='margin:8px'>Print camera preview "
        b"(stream: /stream, snapshot: /snapshot)</p><img src='/stream' style='max-width:100%'></body>")


def make_handler(grabber):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _send(self, code, ctype, body):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            try:
                if path == "/":
                    self._send(200, "text/html", PAGE)
                elif path == "/snapshot":
                    jpg = grabber.latest()
                    if jpg is None:
                        self._send(503, "text/plain", b"no recent frame")
                    else:
                        self._send(200, "image/jpeg", jpg)
                elif path == "/health":
                    self._send(200, "text/plain", b"ok" if grabber.latest() else b"no frames")
                elif path == "/stream":
                    self._stream()
                else:
                    self._send(404, "text/plain", b"not found")
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            seq = -1
            while True:
                jpg, seq = grabber.wait_new(seq, timeout=5.0)
                if jpg is None:
                    continue
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                 + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="probe camera indexes and exit")
    ap.add_argument("--index", type=int, default=0, help="camera index (default 0)")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--fps", type=int, default=15)
    ap.add_argument("--quality", type=int, default=80, help="JPEG quality 1-100")
    ap.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to allow other devices")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--test-pattern", action="store_true", help="serve a synthetic image (no camera)")
    ap.add_argument("--save-dir", help="also save snapshots into this folder")
    ap.add_argument("--save-every", type=float, default=60, help="seconds between saved snapshots")
    args = ap.parse_args()

    if args.list:
        list_cameras()
        return

    g = Grabber(args)
    g.start()
    if args.save_dir:
        threading.Thread(target=saver, args=(g, Path(args.save_dir), args.save_every), daemon=True).start()
    srv = ThreadingHTTPServer((args.host, args.port), make_handler(g))
    srv.daemon_threads = True
    print("Camera server on http://%s:%d/   (stream: /stream, snapshot: /snapshot)" % (args.host, args.port))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
