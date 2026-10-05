# Print failure monitor: ESP32 + GY-521 vibration sensor + webcam (Windows)

What you get:

- **ESP32 + GY-521** measures frame vibration and reports it twice a second.
- **`monitor.py`** (on your laptop) watches that data plus OctoPrint's print state. When vibration leaves the normal range it tells OctoPrint to **pause** the print (or cancel, if you set that).
- **`camera_server.py`** streams the Logitech Brio 100 to OctoPrint so you can watch the print and capture photos for your HW04 evidence.

```
GY-521 --I2C--> ESP32 --USB (COM5)--> monitor.py --HTTP API--> OctoPrint --USB (COM3)--> MK3.5S
Brio 100 --USB--> camera_server.py --HTTP--> OctoPrint webcam panel
```

The monitor only catches **mechanical** failures (crash, skipped axis, loose belt, jammed extruder shaking the machine). It will **not** reliably catch spaghetti or a part that peels off the bed. Say that clearly in your write-up.

> I tested the detection logic, the OctoPrint calls, the firmware math and the camera server on a simulator. I could not test against your real sensor, printer or Brio, so the first real run (steps 5-8) is where you find out what needs tuning.

---

## Folder contents

```
print_monitor/
  esp32_vibration/esp32_vibration.ino   ESP32 firmware (Arduino IDE)
  monitor/monitor.py                    laptop script: test / calibrate / run
  monitor/config.json                   settings (put your OctoPrint API key here)
  monitor/selftest.py                   simulator test, no hardware needed
  camera/camera_server.py               webcam server
  requirements.txt  start_camera.bat  start_monitor.bat
```

---

## 1. Wire the sensor

| GY-521 | ESP32 |
|---|---|
| VCC | 3V3 |
| GND | GND |
| SDA | GPIO21 |
| SCL | GPIO22 |
| AD0 | GND (or leave open) |
| INT | not connected |

Use short wires (under about 20 cm). If your ESP32 board has no pin labeled "21"/"22", look up your board's pinout, or change `SDA_PIN`/`SCL_PIN` at the top of the `.ino`.

**Mounting matters a lot.** Fix the GY-521 *rigidly* to the printer frame (screws, or hard double-sided tape plus a zip tie), not to the moving bed or a flexible panel. Put it in the same place every time. Orientation does not matter, since the firmware uses the magnitude. Secure the wire so it can't flap and cause its own vibration.

## 2. Flash the ESP32

1. Install **Arduino IDE 2** from arduino.cc.
2. *File > Preferences > Additional boards manager URLs*: add `https://espressif.github.io/arduino-esp32/package_esp32_index.json`. Then *Tools > Board > Boards Manager*, search **esp32**, install **"esp32 by Espressif Systems"**. (If the URL has changed, Espressif's install docs have the current one.)
3. Open `esp32_vibration/esp32_vibration.ino`. Choose *Tools > Board > ESP32 Arduino > ESP32 Dev Module* and your COM port.
4. If no COM port appears, install the USB driver for your board's chip (CP210x or CH340, printed on the chip near the USB port).
5. Click Upload. If it hangs on "Connecting...", hold the **BOOT** button until it starts.
6. Open *Tools > Serial Monitor* at **115200 baud**. You should see:

```
# esp32_vibration starting
# MPU-6050 OK, +/-4 g, 1 kHz. Letting the gravity estimate settle...
# ready, streaming
V,2910,0.0123,0.0456,500,0
```

`V,<ms>,<rms>,<peak>,<samples>,<errors>`. Samples should be about 500 and errors 0. If you see "MPU-6050 not responding", recheck wiring. Close the Serial Monitor before step 5, since only one program can use the port.

## 3. Install the Python side

Install **Python 3.11 or newer** from python.org (tick "Add python.exe to PATH"). Then in PowerShell:

```powershell
cd path\to\print_monitor
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If PowerShell blocks the activate script, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.

Check everything works without hardware:

```powershell
python monitor\selftest.py
```

It should end with `ALL TESTS PASSED` (about 25 seconds).

## 4. Camera

```powershell
python camera\camera_server.py --list
```

Find the index that works. A laptop with a built-in webcam usually has that as 0 and the Brio as 1. Then:

```powershell
python camera\camera_server.py --index 1
```

Open <http://127.0.0.1:8080/> and confirm you see the Brio's image. (`start_camera.bat` does the same; edit the `--index` in it.)

Add to OctoPrint under *Settings > Webcam & Timelapse*:

- Stream URL: `http://127.0.0.1:8080/stream`
- Snapshot URL: `http://127.0.0.1:8080/snapshot`

If a browser on another device must see the stream, OctoPrint needs a URL that device can reach. Run the server with `--host 0.0.0.0` and use the laptop's LAN IP instead. Allow it through Windows Firewall when asked.

For photos to include in HW04: `python camera\camera_server.py --index 1 --save-dir photos --save-every 60` saves one JPEG a minute.

Camera tips: the Brio 100 is fixed focus, so mount it about 20-30 cm from the part rather than right next to the nozzle. Use steady, even lighting.

## 5. Connect OctoPrint and test the sensor

1. Install and set up OctoPrint for the MK3.5S as described earlier. The printer's COM port belongs to OctoPrint. The ESP32 is a *different* COM port, and `monitor.py` ignores the Prusa automatically.
2. In OctoPrint, open *Settings > API* and copy the API key (or create an Application Key). Paste it into `monitor\config.json` as `"api_key": "..."`.
3. Plug in the ESP32 (with the Serial Monitor closed), then:

```powershell
python monitor\monitor.py ports      # shows your COM ports; the ESP32 is usually "CP210x" or "CH340"
python monitor\monitor.py test
```

If `test` says it can't find a port, or finds several, set `"port": "COM5"` under `serial` in `config.json`. Tap the frame and shake the sensor: the `rms` and `peak` numbers and the bar should react. Ctrl-C prints a summary.

## 6. Calibrate (important)

The default thresholds are placeholders. Every printer, mount and print speed is different, so teach the system what *normal* looks like:

1. Slice and start a **normal, healthy print** (a 20-30 minute one is plenty) from OctoPrint.
2. At the same time run:

```powershell
python monitor\monitor.py calibrate
```

3. It records only while OctoPrint reports "printing" and the hotend has been hot for 45 s. This skips heat-up, homing and bed leveling. When the print ends (or on Ctrl-C) it writes `drift_rms_g` and `spike_peak_g` into `config.json`, with a 1.5x safety margin (`--margin 2` for fewer false alarms).

**Set `min_hotend_c` in `config.json` to about 10-15 °C below your print temperature** (default 190 suits PLA at about 215 °C). The MK3.5S levels the bed with the nozzle at about 170 °C, so the detector has to stay off below that. Re-run calibration if you change material, speed or sensor position. Calibrating on 2-3 different prints gives the most reliable thresholds.

## 7. Dry run, then live

First log without touching the printer:

```powershell
python monitor\monitor.py run --dry-run
```

Do a whole print. It prints a status line every 10 s, and writes a CSV to `monitor\logs\`. Check there were no false triggers. If there were, raise the thresholds in `config.json` or increase `drift_windows`.

Then go live (`start_monitor.bat` does this):

```powershell
python monitor\monitor.py run
```

On a trigger it prints `FAILURE DETECTED`, beeps, and sends **pause** to OctoPrint. The MK3.5S handles the pause itself (parks the head, retracts). Always check the printer before resuming.

### What it detects

| Trigger | Default | Meant for |
|---|---|---|
| Impact | one 0.5 s window with peak above `spike_peak_g` | nozzle crash, something hits the frame |
| Drift | rms above `drift_rms_g` for 6 windows (3 s) | skipped axis, loose belt, grinding extruder |
| Sensor lost | no good data for 10 s | ESP32 unplugged or I2C dead (set `on_sensor_loss` to `"warn"` to only alert) |

`action` in `config.json` can be `"pause"`, `"cancel"` or `"none"`.

## 8. Test it deliberately (this is your HW04 evidence)

Run these with `run` live and watch the printer. Record the result and the time from fault to pause for each:

| Test | How | Expected |
|---|---|---|
| Normal print | print something small, touch nothing | no pause |
| Impact | during a print, firmly knock the frame | pause within about 1 s |
| Loose belt / axis load | hold the print head briefly (careful: low speed, keep fingers clear) | drift trigger |
| Sensor unplugged | pull the ESP32 USB during a print | pause after about 10 s |

Keep the CSV logs from `monitor\logs\` and the camera photos. State your assumptions in the report: sensor location, print speed, material, calibration margin.

## Which Prusa printers this works with

- **MK3.5S** (your target), MK3S+, MK3/MK2.5: they take G-code over USB, so OctoPrint works and this project works as-is.
- **MK4/MK4S, XL, Mini, Core One:** these are built around PrusaLink and Prusa Connect, and I'm not sure OctoPrint's USB control is supported on them. `monitor.py` currently only talks to OctoPrint. A PrusaLink version would only need a different state-check and pause call, so ask if you need it.

Re-calibrate whenever you move the sensor to a different printer.

## Bench testing without a printer

To test the sensor and detection with no printer or OctoPrint running (for example, tapping the frame while the printer is off):

```powershell
python monitor\monitor.py calibrate --no-octoprint --seconds 120
python monitor\monitor.py run --no-octoprint
```

`--no-octoprint` keeps the detector always armed and never sends commands, so it only logs and beeps.

## Keep the laptop awake

Set sleep to Never, lid-close to "Do nothing", turn off USB selective suspend, and keep it plugged in. A sleeping laptop stops both the monitor and the camera, and the print continues unsupervised.

## Troubleshooting

- **`no ESP32 serial port found`**: run `monitor.py ports` and put the right COM port in `config.json`.
- **Port busy / access denied**: close the Arduino Serial Monitor.
- **`OctoPrint rejected the API key`**: re-copy the key, check there are no extra spaces.
- **`printer not connected to OctoPrint`**: connect the printer in OctoPrint's sidebar first.
- **Samples well below 500, or `i2c_errors` above 0**: loose wire or long I2C leads. Shorten and reseat.
- **False pauses on a normal print**: re-calibrate with `--margin 2`, set `drift_windows` to 10, or `spike_windows` to 2.
- **Camera index opens but gives no frames**: another app (Teams, Camera) is using it.
- **Black or low-FPS camera image**: try `--width 640 --height 480`.
