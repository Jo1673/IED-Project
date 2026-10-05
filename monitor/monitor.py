#!/usr/bin/env python3
"""
Vibration-based 3D print failure monitor.

Reads vibration windows from the ESP32 + GY-521 over USB serial, watches the
OctoPrint job state, and pauses (or cancels) the print when vibration leaves
the normal range.

Commands:
  python monitor.py ports       list serial ports
  python monitor.py test        show live sensor readings (no printer needed)
  python monitor.py calibrate   record a normal print and write thresholds to config.json
  python monitor.py run         watch a print and pause on failure

Run `python monitor.py <command> -h` for options.
"""
import argparse
import csv
import json
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import requests
import serial
from serial.tools import list_ports

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = HERE / "config.json"

DEFAULT_CONFIG = {
    "calibrated": False,
    "serial": {"port": "auto", "baud": 115200},
    "octoprint": {"url": "http://localhost:5000", "api_key": "", "poll_s": 2.0},
    # Detection is only armed while OctoPrint says "printing" AND the hotend has
    # been above min_hotend_c for grace_s seconds. This skips homing, bed
    # leveling and heat-up, which are loud but not failures.
    "gating": {"min_hotend_c": 190, "grace_s": 45},
    "detection": {
        "spike_peak_g": 2.0,    # one window with peak above this = crash/impact
        "spike_windows": 1,
        "drift_rms_g": 0.30,    # RMS above this ...
        "drift_windows": 6,     # ... for this many windows in a row (x0.5 s) = mechanical fault
    },
    "min_samples_per_window": 100,  # windows with fewer good samples are treated as sensor errors
    "sensor_timeout_s": 10,         # no good data for this long while armed = sensor lost
    "on_sensor_loss": "pause",      # "pause" or "warn"
    "action": "pause",              # "pause", "cancel" or "none"
    "log_dir": "logs",
}


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------
def deep_merge(base, over):
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path):
    path = Path(path)
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return deep_merge(DEFAULT_CONFIG, json.load(f))
    return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(path, cfg):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")


# --------------------------------------------------------------------------
# sensor (ESP32)
# --------------------------------------------------------------------------
class Reading:
    __slots__ = ("t_ms", "rms", "peak", "n", "errs")

    def __init__(self, t_ms, rms, peak, n, errs):
        self.t_ms, self.rms, self.peak, self.n, self.errs = t_ms, rms, peak, n, errs


def parse_line(line):
    """'V,<ms>,<rms>,<peak>,<n>,<errs>' -> Reading, else None."""
    parts = line.strip().split(",")
    if len(parts) != 6 or parts[0] != "V":
        return None
    try:
        return Reading(int(parts[1]), float(parts[2]), float(parts[3]),
                       int(parts[4]), int(parts[5]))
    except ValueError:
        return None


def find_port():
    cands = []
    for p in list_ports.comports():
        if p.vid == 0x2C99:      # Prusa printer: never touch it, OctoPrint owns it
            continue
        desc = "%s %s" % (p.description or "", p.manufacturer or "")
        desc = desc.lower()
        if any(k in desc for k in ("cp210", "ch340", "ch343", "ch9102", "silicon labs",
                                   "usb serial", "usb-serial", "ftdi", "uart")):
            cands.append(p.device)
    if len(cands) == 1:
        return cands[0]
    if not cands:
        raise RuntimeError("no ESP32 serial port found. Run 'python monitor.py ports' and set "
                           "serial.port in config.json (e.g. \"COM5\")")
    raise RuntimeError("several possible ports %s. Set serial.port in config.json" % cands)


class SensorReader(threading.Thread):
    """Reads the ESP32 in the background, reconnects if the cable drops."""

    def __init__(self, port, baud, min_samples, open_fn=None, echo_info=False):
        super().__init__(daemon=True)
        self.port, self.baud, self.min_samples = port, baud, min_samples
        self.open_fn = open_fn or self._open_serial
        self.echo_info = echo_info
        self.q = queue.Queue()
        self.last_rx = time.monotonic()   # last GOOD reading
        self.connected = False
        self.bad_windows = 0
        self._halt = threading.Event()

    def _open_serial(self):
        port = find_port() if self.port == "auto" else self.port
        return serial.Serial(port, self.baud, timeout=1)

    def stop(self):
        self._halt.set()

    def run(self):
        last_err = None
        while not self._halt.is_set():
            try:
                dev = self.open_fn()
            except Exception as e:  # noqa: BLE001
                if str(e) != last_err:
                    print("[sensor] cannot open: %s" % e)
                    last_err = str(e)
                self.connected = False
                self._halt.wait(2.0)
                continue
            last_err = None
            self.connected = True
            print("[sensor] connected")
            try:
                while not self._halt.is_set():
                    raw = dev.readline()
                    if not raw:
                        continue
                    line = raw.decode("ascii", "ignore").strip()
                    if not line:
                        continue
                    if line.startswith("#"):
                        if self.echo_info:
                            print("[esp32] %s" % line[1:].strip())
                        continue
                    rd = parse_line(line)
                    if rd is None:
                        continue
                    if rd.n < self.min_samples:
                        self.bad_windows += 1
                        continue
                    self.last_rx = time.monotonic()
                    self.q.put(rd)
            except Exception as e:  # noqa: BLE001
                print("[sensor] connection lost: %s" % e)
            finally:
                self.connected = False
                try:
                    dev.close()
                except Exception:  # noqa: BLE001
                    pass
            self._halt.wait(1.0)


# --------------------------------------------------------------------------
# OctoPrint
# --------------------------------------------------------------------------
class OctoPoller(threading.Thread):
    """Polls OctoPrint for print state + hotend temperature; can send pause/cancel."""

    def __init__(self, url, api_key, poll_s):
        super().__init__(daemon=True)
        self.url = url.rstrip("/")
        self.headers = {"X-Api-Key": api_key}
        self.poll_s = poll_s
        self._lock = threading.Lock()
        self._state = {"ok": False, "printing": False, "hotend": 0.0, "err": "not polled yet"}
        self._halt = threading.Event()
        self._last_err = None

    def stop(self):
        self._halt.set()

    def snapshot(self):
        with self._lock:
            return dict(self._state)

    def poll_once(self):
        s = {"ok": False, "printing": False, "hotend": 0.0, "err": ""}
        try:
            r = requests.get(self.url + "/api/printer", headers=self.headers, timeout=3)
            if r.status_code == 409:
                s.update(ok=True, err="printer not connected to OctoPrint")
            elif r.status_code in (401, 403):
                s["err"] = "OctoPrint rejected the API key"
            else:
                r.raise_for_status()
                j = r.json()
                flags = j.get("state", {}).get("flags", {})
                s["printing"] = bool(flags.get("printing")) and not (
                    flags.get("pausing") or flags.get("paused") or flags.get("cancelling"))
                s["hotend"] = float(j.get("temperature", {}).get("tool0", {}).get("actual") or 0.0)
                s["ok"] = True
        except (requests.RequestException, ValueError) as e:
            s["err"] = "OctoPrint unreachable: %s" % e
        with self._lock:
            self._state = s
        if s["err"] and s["err"] != self._last_err:
            print("[octoprint] %s" % s["err"])
        self._last_err = s["err"] or None

    def run(self):
        while not self._halt.is_set():
            self.poll_once()
            self._halt.wait(self.poll_s)

    def send_action(self, action):
        if action == "pause":
            body = {"command": "pause", "action": "pause"}
        elif action == "cancel":
            body = {"command": "cancel"}
        else:
            return False
        for _ in range(3):
            try:
                r = requests.post(self.url + "/api/job", headers=self.headers, json=body, timeout=5)
                if r.status_code in (200, 204):
                    return True
                print("[octoprint] %s returned HTTP %s: %s" % (action, r.status_code, r.text[:200]))
            except requests.RequestException as e:
                print("[octoprint] %s failed: %s" % (action, e))
            time.sleep(0.5)
        return False


class NoOcto:
    """Stand-in when running without OctoPrint (bench testing with no printer)."""

    def snapshot(self):
        return {"ok": True, "printing": True, "hotend": 9999.0, "err": ""}

    def send_action(self, action):
        return False

    def stop(self):
        pass


# --------------------------------------------------------------------------
# detection logic (pure, unit-tested in selftest.py)
# --------------------------------------------------------------------------
class Gate:
    """Armed only after printing with a hot nozzle for grace_s seconds."""

    def __init__(self, min_hotend_c, grace_s):
        self.min_hotend_c, self.grace_s = min_hotend_c, grace_s
        self.armed_since = None

    def update(self, printing, hotend, now):
        if printing and hotend >= self.min_hotend_c:
            if self.armed_since is None:
                self.armed_since = now
            return (now - self.armed_since) >= self.grace_s
        self.armed_since = None
        return False


class Detector:
    def __init__(self, spike_peak_g, spike_windows, drift_rms_g, drift_windows):
        self.spike_peak_g, self.spike_windows = spike_peak_g, spike_windows
        self.drift_rms_g, self.drift_windows = drift_rms_g, drift_windows
        self.reset()

    def reset(self):
        self.spike_n = 0
        self.drift_n = 0

    def update(self, rms, peak):
        self.spike_n = self.spike_n + 1 if peak > self.spike_peak_g else 0
        self.drift_n = self.drift_n + 1 if rms > self.drift_rms_g else 0
        if self.spike_n >= self.spike_windows:
            return "impact: peak %.2f g > %.2f g" % (peak, self.spike_peak_g)
        if self.drift_n >= self.drift_windows:
            return "abnormal vibration: rms %.2f g > %.2f g for %d windows" % (
                rms, self.drift_rms_g, self.drift_n)
        return None


def percentile(sorted_vals, p):
    if not sorted_vals:
        raise ValueError("empty")
    k = min(len(sorted_vals) - 1, max(0, int(round(p / 100.0 * (len(sorted_vals) - 1)))))
    return sorted_vals[k]


def compute_thresholds(rms_list, peak_list, margin):
    rs, ps = sorted(rms_list), sorted(peak_list)
    stats = {
        "windows": len(rs),
        "rms_mean": sum(rs) / len(rs),
        "rms_p99_9": percentile(rs, 99.9),
        "rms_max": rs[-1],
        "peak_p99_9": percentile(ps, 99.9),
        "peak_max": ps[-1],
        "margin": margin,
    }
    drift = max(stats["rms_p99_9"] * margin, 0.02)
    spike = max(stats["peak_max"] * margin, 0.2)
    return round(drift, 3), round(spike, 3), stats


# --------------------------------------------------------------------------
# monitor loop
# --------------------------------------------------------------------------
class Monitor:
    def __init__(self, cfg, sensor, octo, dry_run, log_path=None, quiet=False):
        d = cfg["detection"]
        self.cfg, self.sensor, self.octo, self.dry_run, self.quiet = cfg, sensor, octo, dry_run, quiet
        self.gate = Gate(cfg["gating"]["min_hotend_c"], cfg["gating"]["grace_s"])
        self.det = Detector(d["spike_peak_g"], d["spike_windows"], d["drift_rms_g"], d["drift_windows"])
        self.tripped = False
        self.events = []
        self._retry_after = 0.0
        self._last_status = 0.0
        self._last_warn = 0.0
        self._csv = None
        if log_path:
            self._f = open(log_path, "w", newline="", encoding="utf-8")
            self._csv = csv.writer(self._f)
            self._csv.writerow(["time", "esp_ms", "rms_g", "peak_g", "samples", "i2c_errors",
                                "printing", "hotend_c", "armed", "event"])

    def close(self):
        if self._csv:
            self._f.close()

    def _trigger(self, reason, now):
        action = self.cfg["action"]
        print("\n*** FAILURE DETECTED: %s ***\a" % reason)
        self.det.reset()
        if self.dry_run or action == "none":
            print("    (dry run: no command sent to the printer)")
            self.tripped = True
            ev = "TRIGGER dry-run: " + reason
        else:
            ok = self.octo.send_action(action)
            self.tripped = ok
            if ok:
                print("    OctoPrint: %s command sent" % action)
                ev = "TRIGGER %s sent: %s" % (action, reason)
            else:
                print("    !! could not send %s to OctoPrint, will retry" % action)
                self._retry_after = now + 5.0
                ev = "TRIGGER %s FAILED: %s" % (action, reason)
        self.events.append(ev)
        return ev

    def step(self, timeout=0.5):
        try:
            rd = self.sensor.q.get(timeout=timeout)
        except queue.Empty:
            rd = None
        now = time.monotonic()
        st = self.octo.snapshot()
        armed = self.gate.update(st["printing"], st["hotend"], now)
        if not st["printing"]:
            self.tripped = False        # re-arm for the next print / after a resume
        can_act = armed and not self.tripped and now >= self._retry_after
        event = ""

        if rd is not None:
            if can_act:
                reason = self.det.update(rd.rms, rd.peak)
                if reason:
                    event = self._trigger(reason, now)
            else:
                self.det.reset()
            if self._csv:
                self._csv.writerow([datetime.now().isoformat(timespec="milliseconds"), rd.t_ms,
                                    "%.4f" % rd.rms, "%.4f" % rd.peak, rd.n, rd.errs,
                                    int(st["printing"]), "%.1f" % st["hotend"], int(armed), event])
                self._f.flush()

        if armed and not self.tripped and now >= self._retry_after \
                and now - self.sensor.last_rx > self.cfg["sensor_timeout_s"]:
            msg = "no good sensor data for %.0f s" % (now - self.sensor.last_rx)
            if self.cfg["on_sensor_loss"] == "pause":
                self._trigger("sensor lost: " + msg, now)
            elif now - self._last_warn > 10:
                print("WARNING: %s (printing is NOT being monitored)\a" % msg)
                self._last_warn = now

        if not self.quiet and now - self._last_status > 10:
            self._last_status = now
            stamp = datetime.now().strftime("%H:%M:%S")
            if rd is not None:
                vib = "rms=%.3f g peak=%.3f g" % (rd.rms, rd.peak)
            else:
                vib = "no new reading"
            state = "ARMED" if armed else ("printing, waiting to arm" if st["printing"] else "idle")
            extra = "  [%s]" % st["err"] if st.get("err") else ""
            print("[%s] %s | %s | hotend %.0f C%s" % (stamp, state, vib, st["hotend"], extra))
        return rd, st, armed


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def make_sensor(cfg, echo_info=False):
    s = SensorReader(cfg["serial"]["port"], cfg["serial"]["baud"],
                     cfg["min_samples_per_window"], echo_info=echo_info)
    s.start()
    return s


def make_octo(cfg, no_octoprint):
    if no_octoprint:
        return NoOcto()
    if not cfg["octoprint"]["api_key"]:
        sys.exit("octoprint.api_key is empty in config.json. In OctoPrint: Settings > API, "
                 "copy the key, paste it in config.json. (Or use --no-octoprint.)")
    o = OctoPoller(cfg["octoprint"]["url"], cfg["octoprint"]["api_key"], cfg["octoprint"]["poll_s"])
    o.start()
    return o


def cmd_ports(args, cfg):
    ports = list(list_ports.comports())
    if not ports:
        print("no serial ports found")
    for p in ports:
        vid = "%04X:%04X" % (p.vid, p.pid) if p.vid is not None else "-"
        print("%-8s %-10s %s" % (p.device, vid, p.description))


def cmd_test(args, cfg):
    sensor = make_sensor(cfg, echo_info=True)
    n, rs, ps = 0, [], []
    print("Reading sensor. Tap the frame / shake the sensor to see it react. Ctrl-C to stop.\n")
    try:
        while True:
            try:
                rd = sensor.q.get(timeout=1.0)
            except queue.Empty:
                if time.monotonic() - sensor.last_rx > 5:
                    print("... no data (is the ESP32 plugged in and flashed? bad windows: %d)"
                          % sensor.bad_windows)
                continue
            n += 1
            rs.append(rd.rms)
            ps.append(rd.peak)
            bar = "#" * min(50, int(rd.rms / 0.01))
            print("rms %.3f g  peak %.3f g  n=%d err=%d  %s" % (rd.rms, rd.peak, rd.n, rd.errs, bar))
    except KeyboardInterrupt:
        pass
    finally:
        sensor.stop()
    if rs:
        print("\n%d windows: rms mean %.3f max %.3f g | peak max %.3f g" % (
            n, sum(rs) / n, max(rs), max(ps)))


def cmd_calibrate(args, cfg):
    cfg_run = json.loads(json.dumps(cfg))
    if args.no_octoprint:
        cfg_run["gating"]["grace_s"] = 0
    sensor = make_sensor(cfg)
    octo = make_octo(cfg, args.no_octoprint)
    mon = Monitor(cfg_run, sensor, octo, dry_run=True, quiet=False)
    rms, peak = [], []
    t0 = time.monotonic()
    was_armed = False
    idle_since = None
    print("Calibrating. Start (or continue) a NORMAL, healthy print. Only armed windows are recorded.")
    print("Press Ctrl-C when the print is done%s.\n" % (
        "" if args.no_octoprint else " (it also stops by itself when the print ends)"))
    try:
        while True:
            rd, st, armed = mon.step()
            if rd is not None and armed:
                rms.append(rd.rms)
                peak.append(rd.peak)
            if armed:
                was_armed = True
            if args.seconds and time.monotonic() - t0 >= args.seconds:
                break
            if was_armed and not st["printing"] and not args.no_octoprint:
                idle_since = idle_since or time.monotonic()
                if time.monotonic() - idle_since > 10:
                    print("print ended")
                    break
            else:
                idle_since = None
    except KeyboardInterrupt:
        print()
    finally:
        sensor.stop()
        octo.stop()
    if len(rms) < 120:
        sys.exit("only %d armed windows recorded (need at least 120 = 60 s). Config NOT changed." % len(rms))
    drift, spike, stats = compute_thresholds(rms, peak, args.margin)
    cfg["detection"]["drift_rms_g"] = drift
    cfg["detection"]["spike_peak_g"] = spike
    cfg["calibrated"] = True
    stats["date"] = datetime.now().isoformat(timespec="seconds")
    cfg["calibration"] = stats
    save_config(args.config, cfg)
    print("\nRecorded %d windows." % stats["windows"])
    print("  normal rms: mean %.3f g, 99.9th pct %.3f g, max %.3f g" % (
        stats["rms_mean"], stats["rms_p99_9"], stats["rms_max"]))
    print("  normal peak: 99.9th pct %.3f g, max %.3f g" % (stats["peak_p99_9"], stats["peak_max"]))
    print("  new thresholds (x%.2f margin): drift_rms_g=%.3f  spike_peak_g=%.3f" % (args.margin, drift, spike))
    print("Saved to %s" % args.config)


def cmd_run(args, cfg):
    dry = args.dry_run or args.no_octoprint or cfg["action"] == "none"
    if not cfg["calibrated"] and not args.allow_uncalibrated:
        print("NOTE: thresholds are not calibrated yet (run 'calibrate' first). Forcing dry run.")
        dry = True
    cfg_run = json.loads(json.dumps(cfg))
    if args.no_octoprint:
        cfg_run["gating"]["grace_s"] = 0
    log_dir = HERE / cfg["log_dir"]
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / ("run_%s.csv" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    sensor = make_sensor(cfg)
    octo = make_octo(cfg, args.no_octoprint)
    mon = Monitor(cfg_run, sensor, octo, dry_run=dry, log_path=log_path)
    d = cfg["detection"]
    print("Monitoring. action=%s%s | spike > %.2f g | drift rms > %.2f g x %d windows" % (
        cfg["action"], " (DRY RUN)" if dry else "", d["spike_peak_g"], d["drift_rms_g"], d["drift_windows"]))
    print("Logging to %s\nCtrl-C to stop.\n" % log_path)
    try:
        while True:
            mon.step()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        sensor.stop()
        octo.stop()
        mon.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="path to config.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ports", help="list serial ports")
    sub.add_parser("test", help="show live sensor readings")
    c = sub.add_parser("calibrate", help="learn normal vibration from a healthy print")
    c.add_argument("--no-octoprint", action="store_true", help="record everything, ignore printer state")
    c.add_argument("--seconds", type=float, default=0, help="stop after this many seconds")
    c.add_argument("--margin", type=float, default=1.5, help="safety factor above normal (default 1.5)")
    r = sub.add_parser("run", help="monitor a print and pause on failure")
    r.add_argument("--dry-run", action="store_true", help="detect and log, but never touch the printer")
    r.add_argument("--no-octoprint", action="store_true",
                   help="no printer link (bench testing): always armed, never sends commands")
    r.add_argument("--allow-uncalibrated", action="store_true",
                   help="allow real pause commands with the default placeholder thresholds")
    args = ap.parse_args()
    cfg = load_config(args.config)
    {"ports": cmd_ports, "test": cmd_test, "calibrate": cmd_calibrate, "run": cmd_run}[args.cmd](args, cfg)


if __name__ == "__main__":
    main()
