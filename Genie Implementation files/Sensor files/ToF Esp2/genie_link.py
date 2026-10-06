"""
genie_link.py — the ESP-NOW frame shared by Node 2 (sensors) and Node 1 (face).

UPLOAD THIS FILE TO BOTH BOARDS. It is the single definition of the wire
format; if only one side has it, the link cannot drift.

--------------------------------------------------------------------------
WHY THIS FILE EXISTS
--------------------------------------------------------------------------
The previous link in 08_Sensors_TestBench/09_GENIE_Integration/ was broken:

    espnow_sensor_node.py   packed  "<BBhHHBB"   -> struct.calcsize() == 10
    espnow_receiver.py      checked len(frame) != 12  -> rejected everything

Ten bytes went out, twelve were expected, every packet was silently dropped and
neither side printed an error. The size is now DERIVED from the format string
(see SIZE below) so that failure mode cannot come back.

--------------------------------------------------------------------------
DIRECTION
--------------------------------------------------------------------------
Node 2 -> Node 1 only. Node 1 owns DHT11 / MQ-2 / flame / MPU-6050 and speaks
newline-JSON to the laptop (see Docs/PROTOCOL.md). Node 2 owns the four ToF
sensors and the vibration switches, and nothing else.

Keep it that way. Two boards each reading the same sensor is how you get two
different temperatures on screen during a viva.
"""
try:
    import ustruct as struct
except ImportError:                     # CPython, for the self-test below
    import struct

VERSION = 2

# ver, type, seq, flags, tof1..tof4, tof_hit, vib_mask
FMT = "<BBBBHHHHBB"
SIZE = struct.calcsize(FMT)             # 14 — derived, never hardcoded

# --- message types ---------------------------------------------------------
MSG_TELEMETRY = 0x01                    # routine heartbeat
MSG_EVENT     = 0x02                    # send immediately, do not wait for the tick

# --- flags -----------------------------------------------------------------
F_CALIBRATED  = 0b00000001              # baseline captured, thresholds valid
F_TOF_FAULT   = 0b00000010              # at least one sensor failed bring-up
F_KNOCK       = 0b00000100              # vibration switch fired this frame

NO_READING = 0xFFFF                     # ToF returned None / bad range status


def pack(seq, tof_mm, tof_hit=0, vib_mask=0, flags=0, msg_type=MSG_TELEMETRY):
    """
    tof_mm   : list/tuple of 4 ints in millimetres, or None for a dead read
    tof_hit  : bitmask, bit N set = sensor N is currently triggered
    vib_mask : bitmask of vibration switches active
    """
    d = [NO_READING if v is None else max(0, min(0xFFFE, int(v))) for v in tof_mm]
    while len(d) < 4:
        d.append(NO_READING)
    return struct.pack(FMT, VERSION, msg_type, seq & 0xFF, flags & 0xFF,
                       d[0], d[1], d[2], d[3], tof_hit & 0x0F, vib_mask & 0x0F)


def unpack(frame):
    """Returns a dict, or None if the frame is not one of ours.

    Never raises. A partial or foreign ESP-NOW frame must not kill the loop
    that is also driving the display.
    """
    if frame is None or len(frame) != SIZE:
        return None
    try:
        ver, mt, seq, flags, t0, t1, t2, t3, hit, vib = struct.unpack(FMT, frame)
    except (ValueError, OSError):
        return None
    if ver != VERSION:
        return None
    tof = [None if v == NO_READING else v for v in (t0, t1, t2, t3)]
    return {
        "type": mt,
        "seq": seq,
        "tof_mm": tof,                              # front, left, right, rear
        "tof_hit": [bool(hit & (1 << i)) for i in range(4)],
        "vib": [bool(vib & (1 << i)) for i in range(4)],
        "calibrated": bool(flags & F_CALIBRATED),
        "tof_fault": bool(flags & F_TOF_FAULT),
        "knock": bool(flags & F_KNOCK),
        "event": mt == MSG_EVENT,
    }


# Sensor index -> where it physically points. Matches the XSHUT order
# GPIO10, 11, 12, 13 and the enclosure apertures.
SIDE = ("front", "left", "right", "rear")


if __name__ == "__main__":
    # Runs on the laptop with plain python3 as well as on the board with F5.
    print("FMT", FMT, "SIZE", SIZE)
    f = pack(7, [212, None, 1400, 65535], tof_hit=0b0101, vib_mask=0b0010,
             flags=F_CALIBRATED)
    assert len(f) == SIZE, "pack produced the wrong length"
    d = unpack(f)
    assert d["seq"] == 7 and d["tof_mm"][1] is None and d["calibrated"]
    assert d["tof_mm"][3] == 0xFFFE, "saturating clamp failed"
    assert unpack(f[:-1]) is None and unpack(b"") is None
    print("round trip OK ->", d)
