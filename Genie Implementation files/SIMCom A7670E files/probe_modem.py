"""
Probe the A7670E and report which of the emergency-call features it actually has.

Run this BEFORE building the analog interface circuit. Three of the design
decisions in docs/emergency_call_audio.md are marked "unverified", and every one
of them is settled by a query this script sends:

  - AT+CCMXPLAY   if present, the modem plays a WAV into the call by itself and
                  the whole PCM5102A + attenuator chain becomes unnecessary
  - AT+DDET       which spelling of DTMF detection this firmware accepts
  - AT+CTXGAIN    whether uplink gain is adjustable in software

Nothing here transmits, dials, or sends a message. It only asks questions, so it
is safe to run on a live SIM.

    pip install pyserial
    python tools/probe_modem.py                 # lists ports and exits
    python tools/probe_modem.py --port COM5
    python tools/probe_modem.py --port COM5 --baud 115200

Paste the REPORT block at the end into the design discussion.
"""

import argparse
import sys
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("pyserial is not installed.  pip install pyserial")


# ----------------------------------------------------------------- probe table
#
# (command, label, kind)
#
# kind drives how the result is judged:
#   "info"     - whatever comes back is just recorded
#   "required" - the build does not work without this
#   "feature"  - an optional capability; ABSENT is a valid answer that changes
#                the design rather than breaking it
#
PROBES = [
    # --- identity -------------------------------------------------------
    ("ATI",              "Module identity",              "info"),
    ("AT+CGMM",          "Model",                        "info"),
    ("AT+CGMR",          "Firmware revision",            "info"),
    ("AT+CGSN",          "IMEI",                         "info"),

    # --- the basics the alert path needs --------------------------------
    ("AT+CPIN?",         "SIM ready",                    "required"),
    ("AT+CSQ",           "Signal quality",               "required"),
    ("AT+CEREG?",        "LTE (EPS) registration",       "required"),
    ("AT+CREG?",         "2G registration",              "info"),
    ("AT+CMGF=?",        "SMS text mode",                "required"),
    ("AT+CLCC=?",        "Call state query",             "required"),

    # --- SMS acknowledgement window (step 4 of the escalation) ----------
    ("AT+CMGL=?",        "List stored SMS (ack poll)",   "required"),
    ("AT+CNMI=?",        "New-message indications",      "feature"),

    # --- the question that can delete the analog circuit ----------------
    ("AT+CCMXPLAY=?",    "Play file into call uplink",   "feature"),
    ("AT+CCMXSTOP=?",    "Stop file playback",           "feature"),
    ("AT+CFTRANRX=?",    "Upload file to module flash",  "feature"),
    ("AT+FSMEM",         "Module flash free space",      "feature"),

    # --- DTMF return path ------------------------------------------------
    ("AT+DDET=?",        "DTMF detect (SIM800 spelling)", "feature"),
    ("AT+CDTMFDET=?",    "DTMF detect (A76XX spelling)",  "feature"),
    ("AT+VTS=?",         "Send DTMF outbound",            "feature"),

    # --- audio levels ----------------------------------------------------
    ("AT+CSDVC=?",       "Audio channel select",          "feature"),
    ("AT+CLVL=?",        "Speaker volume",                "feature"),
    ("AT+CTXGAIN=?",     "Uplink/mic gain (A76XX)",       "feature"),
    ("AT+CRXGAIN=?",     "Downlink gain (A76XX)",         "feature"),
    ("AT+CMICAMP1=?",    "Mic amplifier stage",           "feature"),
    ("AT+CMIC=?",        "Mic gain (SIM800 spelling)",    "feature"),
]

ERRORS = ("ERROR", "+CME ERROR", "+CMS ERROR")


def talk(port, command, timeout=2.0):
    """Send one command, return everything received until it goes quiet.

    Deliberately does not raise and does not care whether the reply looks like
    success - an ERROR here is a real answer, not a failure of the probe.
    """
    port.reset_input_buffer()
    port.write((command + "\r").encode())

    buf = b""
    deadline = time.monotonic() + timeout
    settled = None

    while time.monotonic() < deadline:
        chunk = port.read(port.in_waiting or 1)
        if chunk:
            buf += chunk
            settled = time.monotonic() + 0.15   # keep reading briefly after data
        elif settled and time.monotonic() > settled:
            break

    text = buf.decode("utf-8", "ignore")

    # Strip the echoed command and the bare OK so the report shows content only.
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln and ln != command]
    return "\n".join(lines)


def verdict(reply):
    """PRESENT / ABSENT / SILENT for one probe reply."""
    if not reply:
        return "SILENT"
    if any(e in reply for e in ERRORS):
        return "ABSENT"
    return "PRESENT"


def choose_port():
    ports = list(list_ports.comports())
    if not ports:
        print("No serial ports found. Is the modem plugged in over USB?")
        return
    print("Serial ports on this machine:\n")
    for p in ports:
        print("  %-8s  %s" % (p.device, p.description))
    print("\nRe-run with:  python tools/probe_modem.py --port <PORT>")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="e.g. COM5 or /dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=115200,
                    help="default %(default)s; the A7670E autobauds, "
                         "try 9600 if nothing answers")
    args = ap.parse_args()

    if not args.port:
        choose_port()
        return

    try:
        port = serial.Serial(args.port, args.baud, timeout=0.2)
    except serial.SerialException as e:
        sys.exit("could not open %s: %s" % (args.port, e))

    time.sleep(0.3)

    # Wake it up and quiet the echo before the real probes run.
    print("probing %s at %d baud\n" % (args.port, args.baud))
    for _ in range(3):
        if "OK" in talk(port, "AT", timeout=1.0):
            break
        time.sleep(0.5)
    else:
        port.close()
        sys.exit("no response to AT.\n"
                 "  - wrong port or baud?\n"
                 "  - modem powered from its OWN 5V/2A supply, not the ESP rail?\n"
                 "  - if wired through the ESP32, is its TXD actually 3.3V? (see docs)")

    talk(port, "ATE0")
    talk(port, "AT+CMEE=2")          # verbose errors make ABSENT unambiguous

    results = []
    width = max(len(c) for c, _, _ in PROBES)

    for command, label, kind in PROBES:
        reply = talk(port, command)
        state = verdict(reply)
        results.append((command, label, kind, state, reply))

        first = reply.splitlines()[0] if reply else ""
        if len(first) > 46:
            first = first[:43] + "..."
        print("  %-*s  %-8s %s" % (width, command, state, first))

    port.close()

    # ------------------------------------------------------------- report
    def has(cmd):
        return any(c == cmd and s == "PRESENT" for c, _, _, s, _ in results)

    print("\n" + "=" * 68)
    print("REPORT")
    print("=" * 68)

    missing = [(c, l) for c, l, k, s, _ in results if k == "required" and s != "PRESENT"]
    if missing:
        print("\nBLOCKING - these are needed and did not answer:")
        for c, l in missing:
            print("  %-16s %s" % (c, l))
    else:
        print("\nAll required commands answered.")

    print("\nAnalog audio chain (PCM5102A + attenuator):")
    if has("AT+CCMXPLAY=?") and has("AT+CFTRANRX=?"):
        print("  NOT NEEDED for the pre-recorded message.")
        print("  The modem can play a WAV from its own flash into the call.")
        print("  Upload with AT+CFTRANRX, play with AT+CCMXPLAY.")
        print("  Still needed if you want live TTS replies (DTMF digit 3).")
    elif has("AT+CCMXPLAY=?"):
        print("  PARTIAL - CCMXPLAY exists but CFTRANRX did not answer.")
        print("  Check how files get onto this firmware's filesystem before")
        print("  dropping the analog path.")
    else:
        print("  REQUIRED. Build the circuit in docs/emergency_call_audio.md.")

    print("\nDTMF return path:")
    if has("AT+DDET=?"):
        print("  Use AT+DDET=1   (URC arrives as +DTMF: <digit>)")
    elif has("AT+CDTMFDET=?"):
        print("  Use AT+CDTMFDET=1")
    else:
        print("  NEITHER spelling answered. The responder cannot press keys to")
        print("  acknowledge; escalation must fall back to timeout only.")

    print("\nUplink gain:")
    if has("AT+CTXGAIN=?"):
        print("  AT+CTXGAIN is available - some level trim can happen in software,")
        print("  so R2 does not have to carry the whole adjustment.")
    elif has("AT+CMICAMP1=?"):
        print("  AT+CMICAMP1 is available; CTXGAIN is not.")
    else:
        print("  No software gain control. R2 (the trimpot) is the ONLY way to")
        print("  set the level - do not substitute a fixed resistor yet.")

    print("\nFull replies:\n" + "-" * 68)
    for command, label, kind, state, reply in results:
        print("\n%s   [%s]  %s" % (command, state, label))
        for line in (reply or "(no response)").splitlines():
            print("    " + line)


if __name__ == "__main__":
    main()
