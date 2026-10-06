"""
serial_bridge.py - the face board's USB cable -> api.py. Nothing else.

    python Services/serial_bridge.py            auto-detect the port
    python Services/serial_bridge.py COM7       name it yourself
    python Services/serial_bridge.py --list     show candidate ports

--------------------------------------------------------------------------
WHY THIS EXISTS
--------------------------------------------------------------------------
The face board prints telemetry to its USB serial port. Until now the only
thing that read that port was node_bridge, which starts inside
whisper_service.py -- so getting a temperature reading into api.py meant
loading Whisper, Mistral and Piper first and waiting about a minute.

Worse, it was easy to end up with NOTHING reading the port at all: with
WIFI_UPLINK = False the board's only output is the cable, and if Thonny is
open, Thonny owns it. The board streams perfectly, Thonny shows the JSON,
and api.py sits empty -- which looks exactly like a broken board.

This is that missing piece: one process, one serial port, straight into
api.py. No models, no audio, no waiting.

--------------------------------------------------------------------------
CLOSE THONNY FIRST
--------------------------------------------------------------------------
A COM port has exactly one owner. If Thonny is connected to the board, this
cannot open it, and the error you get says "Access is denied" rather than
anything useful. Close Thonny, or use its Stop/Disconnect, then start this.

You can have Thonny OR this, never both. That is the cost of the cable
being the transport; it is not a bug to be worked around.
"""

import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import json
import time

import requests

from config import IOT_API_URL, NODE1_SERIAL_BAUD, NODE1_SERIAL_PORT
import node_bridge

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None
    list_ports = None


PRINT_EVERY_S = 5.0


def candidates():
    if list_ports is None:
        return []
    out = []
    for p in list_ports.comports():
        blob = "%s %s" % (p.description or "", p.manufacturer or "")
        looks_right = (p.vid == 0x303A or "CP210" in blob or "CH34" in blob
                       or "USB Serial" in blob or "ESP32" in blob.upper())
        out.append((p.device, blob.strip(), looks_right))
    return out


def show_ports():
    ports = candidates()
    if not ports:
        print("No serial ports found at all. Is the board plugged in?")
        return
    print("\nSerial ports on this machine:\n")
    for device, blob, looks_right in ports:
        print("  %-8s %-45s %s" % (device, blob[:45],
                                   "<- likely the board" if looks_right else ""))
    print()


def probe(device, seconds=4.0):
    """Listen for one line that parses as GENIE telemetry.

    Accepting a port only after it SAYS something matters: the modem ESP32
    is on a COM port too, and opening it resets the board that places the
    emergency call. A port is never claimed on the strength of its name.
    """
    try:
        with serial.Serial(device, NODE1_SERIAL_BAUD, timeout=0.5) as s:
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                raw = s.readline()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw.decode(errors="ignore").strip())
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("t") in (
                        "telemetry", "boot", "calibrated", "event"):
                    return True
    except Exception:
        return False
    return False


def find_port():
    print("Looking for the face board...")
    for device, blob, looks_right in candidates():
        if not looks_right:
            continue
        print("  trying %s (%s)" % (device, blob[:40]))
        if probe(device):
            print("  found it on %s" % device)
            return device
    return None


def push(msg):
    """One board line into api.py, through api.py's own endpoint.

    POST /telemetry rather than reimplementing the translation here: the
    same line arriving by cable and by WiFi has to land in identical state,
    and two copies of that mapping is how they stop matching.
    """
    try:
        r = requests.post("%s/telemetry" % IOT_API_URL, json=msg, timeout=4)
        return r.status_code == 200
    except Exception as e:
        return e


def summarise(msg):
    kind = str(msg.get("t", "telemetry")).lower()
    if kind == "event":
        return "EVENT     %s" % msg.get("event")
    if kind == "tof":
        mm = msg.get("tof_mm") or []
        return "tof       %s mm" % " ".join(
            "----" if v is None else "%4d" % v for v in mm[:4])
    return ("telemetry smoke=%-8s flame=%-6s temp=%-4s hum=%-4s fall=%-6s "
            "tilt=%s" % (msg.get("gas_level"), msg.get("flame"),
                         msg.get("temp_c"), msg.get("humidity"),
                         msg.get("fall_state"), msg.get("tilt")))


def run(port):
    print()
    print("Reading %s at %d baud -> %s" % (port, NODE1_SERIAL_BAUD, IOT_API_URL))
    print("Ctrl-C to stop.")
    print()

    sent = skipped = 0
    last_print = 0.0
    last_summary = ""
    warned_api = False

    with serial.Serial(port, NODE1_SERIAL_BAUD, timeout=1) as s:
        s.reset_input_buffer()
        while True:
            raw = s.readline()
            if not raw:
                continue

            text = raw.decode(errors="ignore").strip()
            if not text:
                continue

            try:
                msg = json.loads(text)
            except ValueError:
                # The board prints plain-English status lines too --
                # "[uplink] ...", "PANEL RESET ...". They are worth seeing,
                # not worth discarding silently.
                if text.startswith("[") or "PANEL" in text or "!!" in text:
                    print("  board: %s" % text)
                skipped += 1
                continue

            if not isinstance(msg, dict) or "t" not in msg:
                skipped += 1
                continue

            result = push(msg)
            if result is True:
                sent += 1
                if warned_api:
                    print("  api.py is answering again")
                    warned_api = False
            elif not warned_api:
                warned_api = True
                print()
                print("  Cannot reach api.py at %s" % IOT_API_URL)
                print("  (%s)" % result)
                print("  Start it in another terminal:  python api.py")
                print()

            now = time.time()
            line = summarise(msg)
            changed = line != last_summary
            if msg.get("t") == "event" or changed or now - last_print >= PRINT_EVERY_S:
                last_print = now
                last_summary = line
                print("  %s   [sent %d]" % (line, sent))


def main(argv):
    if serial is None:
        print("pyserial is not installed:  pip install pyserial")
        return 1

    if "--list" in argv:
        show_ports()
        return 0

    port = None
    for a in argv:
        if not a.startswith("-"):
            port = a
    if port is None and NODE1_SERIAL_PORT not in (None, "off"):
        port = NODE1_SERIAL_PORT
    if port is None:
        port = find_port()

    if port is None:
        print()
        print("No board found. In order of likelihood:")
        print()
        print("  1. THONNY IS HOLDING THE PORT. A COM port has one owner.")
        print("     Close Thonny (or press its Stop button) and try again.")
        print("     This is by far the most common cause.")
        print("  2. The board is not running main.py -- nothing to read.")
        print("  3. Wrong port. Run with --list and name it yourself:")
        print("       python Services/serial_bridge.py COM7")
        print()
        return 1

    while True:
        try:
            run(port)
        except KeyboardInterrupt:
            print("\nstopped\n")
            return 0
        except Exception as e:
            msg = str(e)
            print()
            if "Access is denied" in msg or "PermissionError" in repr(e):
                print("  %s is busy -- almost certainly Thonny." % port)
                print("  Close it, and this will pick the board up.")
            else:
                print("  serial error: %r" % e)
            print("  retrying in 5 s...")
            print()
            time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
