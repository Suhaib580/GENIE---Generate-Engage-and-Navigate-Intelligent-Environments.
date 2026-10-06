"""
Live sensor readout. Polls api.py and redraws a table in the terminal.

    python Services/sensor_monitor.py

This is the thing to have open while you wave a hand at a ToF sensor, hold a
lighter near the flame module, or tap the vibration switch. It shows every
channel api.py holds, whether each one is live or stale, and the raw numbers
underneath the words -- which is what tells a working sensor from a dead one.

    --once       print one snapshot and exit (good for a script or a log)
    --json       raw JSON instead of the table
    --interval   seconds between polls (default 1.0)
    --url        api.py base URL (default from config)

WHY RAW NUMBERS ARE ON SCREEN
-----------------------------
A boolean cannot be tested. "SMOKE: clear" from a working MQ-2 in clean air
and "SMOKE: clear" from one whose AO wire fell off are the same word. The
raw ADC count is not: a plausible 300-900 versus a pinned 0 or 4095 tells
you which one you have, in one glance, without a multimeter.

Same reasoning for TILT and ACCEL. `MOTION: false` reads identically whether
the IMU is still or absent; an ACCEL of 1.00 g says the chip is answering
and a null says it is not.
"""

import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import time

import requests

from config import IOT_API_URL

# ANSI, degraded gracefully -- Windows terminals that do not handle it just
# show the text. Nothing here depends on colour to be readable.
DIM = "\033[2m"
RED = "\033[31m"
YEL = "\033[33m"
GRN = "\033[32m"
CYN = "\033[36m"
OFF = "\033[0m"
CLEAR = "\033[2J\033[H"

ALARMING = {"alarm", "true", "fallen", "impact", "detected"}
ELEVATED = {"elevated", "freefall", "warning"}
UNKNOWN = {"unknown", "none", ""}


def colour(status):
    s = str(status).lower()
    if s in ALARMING:
        return RED
    if s in ELEVATED:
        return YEL
    if s in UNKNOWN:
        return DIM
    return GRN


def fmt(v, places=1):
    if v is None:
        return "--"
    if isinstance(v, float):
        return ("%%.%df" % places) % v
    return str(v)


# Which extra numbers to show beside a channel, and how to label them.
DETAIL = {
    "SMOKE": lambda d: "raw %s  ratio %s" % (fmt(d.get("raw"), 0),
                                             fmt(d.get("ratio"), 2)),
    "KNOCK": lambda d: "%s taps total" % fmt(d.get("count"), 0),
}

ORDER = ("SMOKE", "FLAME", "FALL", "MOTION", "PRESENCE", "IR", "KNOCK",
         "TEMPERATURE", "HUMIDITY", "TILT", "ACCEL", "DISTANCE")

UNITS = {"TEMPERATURE": "C", "HUMIDITY": "%", "TILT": "deg",
         "ACCEL": "g", "DISTANCE": "mm"}

WHICH_BOARD = {
    "SMOKE": "face", "FLAME": "face", "FALL": "face", "MOTION": "face",
    "KNOCK": "face", "TEMPERATURE": "face", "HUMIDITY": "face",
    "TILT": "face", "ACCEL": "face",
    "PRESENCE": "ToF", "IR": "ToF", "DISTANCE": "ToF",
}


def _ago(seconds):
    if seconds < 90:
        return "%.0fs" % seconds
    if seconds < 5400:
        return "%.0f min" % (seconds / 60.0)
    return "%.1f h" % (seconds / 3600.0)


def fetch(url):
    sensors = requests.get("%s/sensors" % url, timeout=4).json()
    tof = requests.get("%s/tof" % url, timeout=4).json()
    return sensors, tof


def render(sensors, tof, url):
    links = sensors.get("_links", {})
    out = []

    node1 = links.get("node1")
    node2 = links.get("node2")
    stale = links.get("stale")
    age = links.get("age_seconds")

    out.append("GENIE sensors   %s   %s" % (url, time.strftime("%H:%M:%S")))
    out.append("")

    def board(label, live):
        if stale:
            # Nothing has pushed recently at all. The readings below are
            # whatever was last stored and are NOT current -- say so
            # loudly, because a stale table looks exactly like a live one.
            return "  %-48s %sNO DATA%s" % (label, RED, OFF)
        return "  %-48s %s%s%s" % (label, GRN if live else RED,
                                   "LIVE" if live else "DOWN", OFF)

    out.append(board("face node (TFT, MQ-2, flame, DHT11, MPU, SW-420)", node1))
    out.append(board("ToF node  (4x VL53L1X)", node2))

    if stale:
        out.append("")
        if age is None:
            out.append("  %sNothing has ever written to this api.py.%s" % (RED, OFF))
            out.append("  %sStart the pipeline, or: python Services/fake_node.py%s"
                       % (DIM, OFF))
        else:
            out.append("  %sLAST UPDATE %s AGO - everything below is a frozen"
                       " snapshot,%s" % (RED, _ago(age), OFF))
            out.append("  %snot a reading. The bridge is not running, or the"
                       " boards are unplugged.%s" % (RED, OFF))
    out.append("")
    out.append("  %-12s %-10s %-24s %s" % ("SENSOR", "STATE", "DETAIL", "FROM"))
    out.append("  " + "-" * 62)

    for name in ORDER:
        d = sensors.get(name)
        if not isinstance(d, dict):
            continue

        status = d.get("status")
        value = d.get("value")

        if status is not None and value is not None:
            # DISTANCE: a number AND a direction.
            state = "%s %s" % (fmt(value, 0), UNITS.get(name, ""))
            detail = str(status)
            col = DIM if value is None else GRN
        elif status is not None:
            state = str(status)
            detail = DETAIL.get(name, lambda _d: "")(d)
            col = colour(status)
        else:
            state = "%s %s" % (fmt(value), UNITS.get(name, ""))
            detail = ""
            col = DIM if value is None else GRN

        out.append("  %-12s %s%-10s%s %-24s %s%s%s"
                   % (name, col, state, OFF, detail,
                      DIM, WHICH_BOARD.get(name, ""), OFF))

    out.append("")
    sides = tof.get("sides", [])
    mm = tof.get("mm", [])
    zone = tof.get("zone", [])
    if sides:
        out.append("  ToF array  %s%s%s"
                   % (GRN if tof.get("link") else RED,
                      "" if tof.get("link") else "  (link down - values are not readings)",
                      OFF))
        row_n = "   "
        row_v = "   "
        row_z = "   "
        for i, side in enumerate(sides):
            row_n += " %-9s" % side
            row_v += " %-9s" % (fmt(mm[i] if i < len(mm) else None, 0))
            row_z += " %-9s" % (zone[i] if i < len(zone) else "?")
        out.append(DIM + row_n + OFF)
        out.append(row_v + DIM + " mm" + OFF)
        out.append(DIM + row_z + OFF)

    out.append("")
    out.append(DIM + "  Ctrl-C to stop.  --once for a single snapshot,"
                     "  --json for raw." + OFF)
    return "\n".join(out)


def main(argv):
    url = IOT_API_URL
    interval = 1.0
    once = "--once" in argv
    as_json = "--json" in argv

    if "--url" in argv:
        url = argv[argv.index("--url") + 1]
    if "--interval" in argv:
        interval = float(argv[argv.index("--interval") + 1])

    while True:
        try:
            sensors, tof = fetch(url)
        except Exception as e:
            print("cannot reach api.py at %s (%r)" % (url, e))
            print("start it with:  python api.py")
            if once:
                return 1
            time.sleep(interval)
            continue

        if as_json:
            print(json.dumps({"sensors": sensors, "tof": tof}, indent=2))
        else:
            print(CLEAR + render(sensors, tof, url))

        if once:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print()
