"""
node1_link.py — Node 1 side of the ESP-NOW link. Drop-in, non-blocking.

UPLOAD TO NODE 1:   genie_link.py   node1_link.py

--------------------------------------------------------------------------
HOW TO WIRE IT INTO THE EXISTING FACE FIRMWARE
--------------------------------------------------------------------------
Node 1 runs one cooperative loop. MicroPython has no threads, so NOTHING in
this file may block — a blocking recv() would stall the eye animation and the
frame budget (two eyes = 15.6 ms of a 33.3 ms budget) has no room for it.

In main.py / genie_ui.py:

    import node1_link
    node1_link.begin()                       # once, after boot

    while True:
        ...
        node1_link.poll()                    # once per loop, costs ~0 when idle
        ...

Then read whenever you need it:

    tof = node1_link.state()                 # dict, always safe to call
    if tof["presence"]:
        ...

and fold it into the JSON you already send the laptop:

    msg.update(node1_link.telemetry_fields())

--------------------------------------------------------------------------
STALENESS
--------------------------------------------------------------------------
If Node 2 loses power, the last frame it sent would otherwise sit there
forever looking like a live reading. After LINK_TIMEOUT_MS with no frame the
state reports link_up: False and presence goes False. Show that on the TFT —
a silently frozen sensor is worse than a visibly dead one.
"""
import network
import espnow
import time

import genie_link as link

LINK_TIMEOUT_MS = 4000          # 4x the Node 2 heartbeat

_e = None
_last_rx = None
_last_frame_ms = 0
_rx_count = 0
_bad_count = 0

# Zone bands applied on this side so the TFT can show words, not millimetres.
_BANDS = ((40, "TOUCH"), (300, "NEAR"), (800, "PRESENT"),
          (1500, "FAR"), (99999, "CLEAR"))


def _zone(mm):
    if mm is None:
        return "CLEAR"
    for upper, name in _BANDS:
        if mm < upper:
            return name
    return "CLEAR"


def begin():
    """Bring ESP-NOW up. Safe to call twice."""
    global _e
    if _e is not None:
        return _e
    sta = network.WLAN(network.STA_IF)
    sta.active(True)
    _e = espnow.ESPNow()
    _e.active(True)
    print("node1_link up. My MAC:", sta.config('mac'),
          "<- put this in node2_main.NODE1_MAC")
    return _e


def poll():
    """Call once per main-loop iteration. Never blocks, never raises.

    Returns the decoded dict if a frame arrived this call, else None.
    """
    global _last_rx, _last_frame_ms, _rx_count, _bad_count
    if _e is None:
        return None
    try:
        _host, frame = _e.recv(0)           # timeout 0 = return immediately
    except OSError:
        return None
    if frame is None:
        return None
    d = link.unpack(frame)
    if d is None:
        _bad_count += 1                     # foreign or corrupt, ignore quietly
        return None
    _last_rx = d
    _last_frame_ms = time.ticks_ms()
    _rx_count += 1
    return d


def link_up():
    if _last_rx is None:
        return False
    return time.ticks_diff(time.ticks_ms(), _last_frame_ms) < LINK_TIMEOUT_MS


def state():
    """Always returns a full dict, even before the first frame."""
    up = link_up()
    if not up or _last_rx is None:
        return {"link_up": False, "presence": False, "knock": False,
                "tof_mm": [None] * 4, "tof_zone": ["CLEAR"] * 4,
                "tof_hit": [False] * 4, "tof_fault": False,
                "rx": _rx_count, "bad": _bad_count}
    d = _last_rx
    return {
        "link_up": True,
        "presence": any(d["tof_hit"]),
        "knock": d["knock"],
        "tof_mm": d["tof_mm"],
        "tof_zone": [_zone(v) for v in d["tof_mm"]],
        "tof_hit": d["tof_hit"],
        "tof_fault": d["tof_fault"],
        "rx": _rx_count,
        "bad": _bad_count,
    }


def telemetry_fields():
    """The subset that belongs in the JSON telemetry line to the laptop.

    Extends Docs/PROTOCOL.md - see PROTOCOL_ADDENDUM.md in this bundle.
    """
    s = state()
    return {
        "node2_link": s["link_up"],
        "present": s["presence"],           # overrides the old IR-only 'present'
        "tof_mm": s["tof_mm"],
        "tof_zone": s["tof_zone"],
        "knock": s["knock"],
    }


def nearest():
    """(side, mm) of the closest valid reading, or (None, None)."""
    s = state()
    best_i, best = None, None
    for i, v in enumerate(s["tof_mm"]):
        if v is not None and (best is None or v < best):
            best_i, best = i, v
    return (link.SIDE[best_i] if best_i is not None else None, best)


# Standalone test. Run this alone on Node 1 with F5 BEFORE merging it into the
# face firmware, with node2_main.py running on Node 2.
if __name__ == "__main__":
    begin()
    print("listening for Node 2...")
    last = 0
    while True:
        poll()
        if time.ticks_diff(time.ticks_ms(), last) > 500:
            last = time.ticks_ms()
            s = state()
            side, mm = nearest()
            print("link=%s  %s  nearest=%s %s  rx=%d bad=%d" % (
                "UP " if s["link_up"] else "DOWN",
                " ".join("%s:%s" % (z[0], "----" if v is None else "%4d" % v)
                         for z, v in zip(s["tof_zone"], s["tof_mm"])),
                side, mm, s["rx"], s["bad"]))
        time.sleep_ms(20)
