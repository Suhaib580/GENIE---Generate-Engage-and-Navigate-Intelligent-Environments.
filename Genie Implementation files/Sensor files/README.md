# Sensor files

Firmware for the two sensor boards. Both run **MicroPython v1.27.0**, edited
in **Thonny**.

Not PlatformIO, not Arduino C++ — both were tried and abandoned. The display
driver is hand-written MicroPython using `@micropython.viper` and reaches
2477 kpx/s, which is enough.

---

## Which board is which

| Folder | Board | Has | Talks to |
|---|---|---|---|
| `TFT+Sensors/` | **face board** | 2.4" TFT face, 2× MQ-2 gas, flame, DHT11, 2× MPU-6050, 2× SW-420 | the laptop, by USB serial **and** WiFi |
| `ToF Esp2/` | **ToF board** | 4× VL53L1X distance sensors, vibration switches | the face board by ESP-NOW, **and** the laptop directly by WiFi |

> Some project documents number these "Node 1" and "Node 2". Node 1 is the
> face board, Node 2 is the ToF board. This README says "face" and "ToF"
> because those cannot be read backwards.

**The ToF board sends both ways on purpose.** The ESP-NOW frame is what lets
the face board draw distances on its screen; the direct WiFi line is what
keeps the distance sensors reporting when the face board is off, crashed, or
being reflashed.

---

## Flashing

**Copy the whole contents of one folder to that board's device root. Flat — no
folders on the device.** Every import in this firmware is flat, and a folder
breaks them.

In Thonny: open each file, then **File → Save as… → MicroPython device**,
keeping the same name.

### Face board — 16 files
```
main.py          genie_ui.py      node1_report.py   sensors.py
pins.py          eyes.py          ili9341_par.py    fall_fsm.py
handling_fsm.py  har_features.py  har_model.py      genie_link.py
node1_link.py    wifi_cfg.py      net_uplink.py     genie_menu.py
```

### ToF board — 9 files
```
main.py       node2_main.py   vl53l1x.py   zones.py   genie_link.py
wifi_cfg.py   net_uplink.py   pins3.py     tof3.py
```

**Two files are imported by nothing and must still be uploaded:**

- **`genie_menu.py`** — option 9 is the only way to set the gas baseline.
  Without it **smoke can never reach the alarm threshold.**
- **`pins.py`** — the pin map, and the source of truth when a document and the
  code disagree.

**Three files must be identical on both boards**: `genie_link.py` (the ESP-NOW
frame), `wifi_cfg.py` (the network) and `net_uplink.py`. One copy edited and
the other forgotten is how two boards end up on different networks, or
speaking different frame formats — silently, with no error on either side.

---

## Bring-up order

Each step proves the one before it. Skipping is how a day disappears.

1. **WiFi, on both boards.** Run `wifi_cfg.py` with F5 on each:
   ```python
   import wifi_cfg; wifi_cfg.connect(); print(wifi_cfg.channel())
   ```
   **The two channel numbers must match.** ESP-NOW and WiFi share one radio.
   If the boards sit on different channels, every distance reading is dropped
   in silence and neither board prints an error.

2. **ToF board alone.** F5 `node2_main.py`. Four distances print at 10 Hz.

3. **Face board alone.** F5 `main.py`. Eyes appear; JSON lines scroll past. It
   runs with no laptop, no WiFi and no ToF board — if it will not start here,
   the fault is on that board.

4. **The pair.** Both running: the face board folds ToF distances into its own
   telemetry.

5. **The laptop.** `python api.py`, then `python Services\sensor_monitor.py`.
   Values appear within about three seconds.

**Before the demo:** put the face board's real MAC address into `NODE1_MAC` at
the top of `node2_main.py`. Broadcast works for testing, but every ESP-NOW
device in the room receives it.

---

## Pin maps

### Face board

> **The rule: the P1 header is the display. P2 is the sensors.**

| Signal | GPIO |
|---|---|
| TFT data D0–D7 | 4, 5, 6, 7, 15, 16, 17, 18 |
| TFT WR / RD / RS / CS / RST | 8, 9, 10, 11, 12 |
| MQ-2 #1 / #2 (analog) | **1, 2** |
| Flame (digital, active low) | **42** |
| SW-420 vibration, top / bottom | **41, 40** |
| DHT11 | 21 |
| 2× MPU-6050 (I²C, `0x68` + `0x69`) | SCL 38, SDA 47 |
| Button | 39 |
| WS2812 strip | 48 |

There is **no IR sensor**, despite what older documents say. Presence comes
from the ToF array. GPIO 13 is free.

### ToF board

| Signal | GPIO |
|---|---|
| I²C | SDA 8, SCL 9 |
| XSHUT ×4 — front, left, right, rear | 10, 11, 12, 13 |
| Vibration switches | 14, 15, 21, 47 |
| WS2812 status LED | 5 |

Addresses `0x30`–`0x33`, assigned in that order at every boot.

### Never use, on either board

| GPIO | Why |
|---|---|
| 0, 3, 45, 46 | strapping pins — affect boot mode |
| 19, 20 | native USB |
| 26–32 | SPI flash |
| **33–37** | **octal PSRAM on the N8R8 module.** They look free on the silkscreen and are not. Random crashes that look like a power fault |
| 43, 44 | UART0 — the Thonny console |

**Only GPIO 1 and 2 work as analog inputs.** The second ADC returns garbage
whenever WiFi is active, and both boards use WiFi.

---

## Traps that have already cost this project days

- ⚡ **Power first.** Four ToF sensors ranging draw ~160 mA between them, and
  the radio adds 350 mA bursts. USB alone does not carry it. The symptom is
  brutal to diagnose: **a different random subset of the four sensors works on
  each boot.** That is not a code bug — code fails the same way every time.
  Use the external 5 V supply and tie the grounds.

- **Distance sensor addresses live in RAM, not flash.** All four reset to
  `0x29` on every power cycle. The startup sequence re-assigns them on every
  boot, forever. When four sensors "stop working" after a reset, this is why.

- **Nothing goes in front of a ToF lens.** Not tape, not clear acrylic. A
  baseline under 150 mm with almost no noise means the lens is covered.

- **ToF VIN is 3V3, never 5 V.** The ESP32-S3 is not 5 V tolerant. **One**
  4.7 kΩ pull-up pair for the whole I²C bus, not one per board.

- **Dupont wires break inside the insulation.** They look perfect and test
  dead. **Replace** the wire; do not re-seat it. Root cause twice here.

- **The MQ-2 wants 5 V** and needs 24–48 hours powered before its baseline
  settles.

- **MicroPython is not Python.** `str` has no `rjust`, `ljust`, `center` or
  `zfill`, and `%` formatting has no `*` width. It only fails on the device.

---

## If a board will not start

Neither `main.py` retries. Both print the real error and stop, because a boot
loop on an autostarting board fills the console faster than you can type and
the only way out is a full erase.

Both give you a **three-second window at startup — press Ctrl-C then** to get
a REPL with nothing running. If you are locked out anyway: Ctrl-C repeatedly
while pressing reset, or hold BOOT while plugging in.

An **`ImportError` is never a wiring fault.** Both boards print a checklist of
which modules are present and which are missing.
