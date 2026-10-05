#!/usr/bin/env python3
"""
Self-test for monitor.py. No hardware needed:  python selftest.py

Runs the real detection/gating/OctoPrint code against a fake OctoPrint HTTP server
and a fake ESP32, and checks that pauses happen when they should (and only then).
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import monitor as m

FAILS = []


def check(name, cond, detail=""):
    print("  [%s] %s %s" % ("PASS" if cond else "FAIL", name, detail))
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------- unit tests
def unit_tests():
    print("unit tests")
    d = m.Detector(spike_peak_g=2.0, spike_windows=1, drift_rms_g=0.3, drift_windows=3)
    check("normal data: no trigger", all(d.update(0.1, 0.5) is None for _ in range(20)))
    check("spike triggers", d.update(0.1, 2.5) is not None)
    d.reset()
    r = [d.update(0.5, 1.0) for _ in range(3)]
    check("drift needs 3 windows in a row", r[0] is None and r[1] is None and r[2] is not None)
    d.reset()
    d.update(0.5, 1.0); d.update(0.5, 1.0); d.update(0.1, 1.0)
    check("drift counter resets on a normal window", d.update(0.5, 1.0) is None)

    g = m.Gate(min_hotend_c=190, grace_s=45)
    check("gate: idle not armed", g.update(False, 215, 0) is False)
    check("gate: cold nozzle not armed", g.update(True, 100, 1) is False)
    check("gate: hot but inside grace", g.update(True, 215, 2) is False)
    check("gate: after grace armed", g.update(True, 215, 48) is True)
    check("gate: nozzle cools -> disarmed", g.update(True, 150, 50) is False)
    check("gate: re-heat restarts grace", g.update(True, 215, 51) is False)
    check("gate: paused -> disarmed", g.update(False, 215, 100) is False)

    check("parse good line", m.parse_line("V,1234,0.1234,0.5678,498,2").n == 498)
    check("parse rejects junk", m.parse_line("garbage") is None and m.parse_line("V,1,x,3,4,5") is None)

    rms = [0.1] * 500 + [0.12] * 5
    peak = [0.5] * 500 + [0.8]
    drift, spike, stats = m.compute_thresholds(rms, peak, 1.5)
    # 5 of 505 windows sit at 0.12 g, so the 99.9th percentile is 0.12 -> 0.12 * 1.5 = 0.18
    check("thresholds scale with margin", abs(drift - 0.18) < 0.01 and abs(spike - 1.2) < 0.01,
          "(drift %.3f spike %.3f)" % (drift, spike))


# ---------------------------------------------------------- end-to-end pieces
class FakeOcto:
    def __init__(self, hotend=215.0):
        self.hotend = hotend
        self.printing = True
        self.posts = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.headers.get("X-Api-Key") != "KEY":
                    self.send_response(403); self.end_headers(); return
                body = json.dumps({
                    "state": {"flags": {"printing": outer.printing, "paused": not outer.printing}},
                    "temperature": {"tool0": {"actual": outer.hotend, "target": outer.hotend}},
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                if self.headers.get("X-Api-Key") != "KEY":
                    self.send_response(403); self.end_headers(); return
                outer.posts.append(body)
                outer.printing = False
                self.send_response(204)
                self.end_headers()

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


class FakeEsp:
    """Emits a 'V,...' line every 50 ms (10x faster than the real thing) from script(t)."""

    def __init__(self, script):
        self.script, self.t0 = script, time.monotonic()

    def readline(self):
        time.sleep(0.05)
        out = self.script(time.monotonic() - self.t0)
        if out is None:
            return b""
        rms, peak = out
        return ("V,%d,%.4f,%.4f,498,0\n" % (int((time.monotonic() - self.t0) * 1000), rms, peak)).encode()

    def close(self):
        pass


def scenario(script, hotend=215.0, duration=4.0, on_sensor_loss="pause"):
    octo = FakeOcto(hotend)
    cfg = json.loads(json.dumps(m.DEFAULT_CONFIG))
    cfg["gating"] = {"min_hotend_c": 190, "grace_s": 0.5}
    cfg["detection"] = {"spike_peak_g": 2.0, "spike_windows": 1, "drift_rms_g": 0.3, "drift_windows": 6}
    cfg["sensor_timeout_s"] = 1.0
    cfg["on_sensor_loss"] = on_sensor_loss
    sensor = m.SensorReader("fake", 115200, 100, open_fn=lambda: FakeEsp(script))
    sensor.start()
    poller = m.OctoPoller(octo.url, "KEY", 0.1)
    poller.start()
    mon = m.Monitor(cfg, sensor, poller, dry_run=False, quiet=True)
    t0 = time.monotonic()
    first_post_at = None
    while time.monotonic() - t0 < duration:
        mon.step(timeout=0.1)
        if octo.posts and first_post_at is None:
            first_post_at = time.monotonic() - t0
    sensor.stop(); poller.stop(); octo.close()
    return octo.posts, first_post_at


def e2e_tests():
    print("end-to-end tests (fake OctoPrint + fake ESP32, takes ~25 s)")
    posts, _ = scenario(lambda t: (0.10, 0.45))
    check("normal print: no pause", posts == [])

    posts, at = scenario(lambda t: (0.10, 3.0) if t > 2.0 else (0.10, 0.45), duration=4.0)
    check("impact spike: pause sent", posts == [{"command": "pause", "action": "pause"}],
          "(at %.1f s)" % (at or -1))
    check("impact spike: sent promptly after the spike", at is not None and 2.0 <= at < 3.0)

    posts, _ = scenario(lambda t: (0.9, 1.0) if t > 2.0 else (0.10, 0.45), duration=4.0)
    check("sustained vibration: pause sent", len(posts) == 1)

    posts, _ = scenario(lambda t: (0.10, 3.0), hotend=100.0, duration=3.0)
    check("cold nozzle (heat-up/leveling): spike ignored", posts == [])

    posts, _ = scenario(lambda t: (0.10, 3.0) if t < 0.3 else (0.10, 0.45), duration=3.0)
    check("spike during grace period: ignored", posts == [])

    posts, _ = scenario(lambda t: (0.10, 0.45) if t < 2.0 else None, duration=5.0)
    check("sensor goes silent: pause sent", len(posts) == 1)

    posts, _ = scenario(lambda t: (0.10, 0.45) if t < 2.0 else None, duration=5.0, on_sensor_loss="warn")
    check("sensor goes silent with on_sensor_loss=warn: no pause", posts == [])

    # wrong API key must never crash, and must not pause
    octo = FakeOcto()
    p = m.OctoPoller(octo.url, "WRONG", 0.1)
    p.poll_once()
    check("wrong API key reported", "API key" in p.snapshot()["err"] and not p.snapshot()["printing"])
    check("wrong API key: send_action fails cleanly", p.send_action("pause") is False)
    octo.close()


if __name__ == "__main__":
    unit_tests()
    e2e_tests()
    print()
    if FAILS:
        print("FAILED: %s" % ", ".join(FAILS))
        sys.exit(1)
    print("ALL TESTS PASSED")
