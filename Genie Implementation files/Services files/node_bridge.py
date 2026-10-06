"""
Node 1 (GENIE FINAL face node) -> api.py bridge.

Runs as a daemon thread inside whisper_service.py, alongside sensor_watch,
the playback server and the emergency link.

    Node 1  --newline JSON, USB serial-->  THIS FILE  --POST /update_sensors-->  api.py
      ^                                        |                                    |
      |  {"emotion":"thinking"}                |  watcher.poll_once()               |
      +----------------------------------------+                          sensor_watch, ai.py

WHAT THIS FILE IS FOR
---------------------
Two systems that were built separately now have to agree on what a sensor
reading IS. Node 1 speaks the vocabulary in GENIE FINAL's PROTOCOL.md --
`gas_alarm`, `flame`, `present`, `temp_c` -- and api.py stores SMOKE, FLAME,
MOTION, TEMPERATURE. Nothing in either system translated. This file is that
translation and nothing else: it owns no policy, decides no alerts, and
speaks to no hardware except the one serial port.

THREE THINGS IT DELIBERATELY DOES NOT DO
----------------------------------------
1. IT DOES NOT DECIDE ALERTS. sensor_watch already owns edge-detection,
   debounce and the 600 s cooldown. A second place that can call an
   ambulance is a second place that can call it twice -- and the two would
   not share a cooldown, so one smoke event would ring the responder's
   phone from both. Instead, after pushing an urgent reading this file
   calls watcher.poll_once() so the ONE owner evaluates it immediately
   instead of up to SENSOR_WATCH_INTERVAL later.

2. IT DOES NOT HOLD THE LAST READING WHEN THE NODE GOES QUIET. When Node 1
   stops talking, every channel it owns is pushed as "unknown", not left at
   its last value. A frozen reading that looks live is worse than a visibly
   dead sensor: "no smoke" from a sensor unplugged an hour ago is a
   confident all-clear over a blind spot. "unknown" lands on sensor.aiml's
   own default branch ("I cannot read the smoke sensor right now") and is
   in no SENSOR_ALERT_STATES set, so it can neither alarm nor reassure.

3. IT DOES NOT MATTER TO THE AUDIO PATH. No serial port, no board, no
   pyserial, a port held open by Thonny -- every one of those logs once and
   leaves the wake word, Whisper, the LLM and TTS running exactly as they
   were. A sensor bridge that can take the voice assistant down with it is
   a worse trade than no sensor bridge.

WHY A SERIAL PORT AND NOT WIFI: see the NODE1_SERIAL_PORT note in config.py.
Short version -- Node 1 on an AP kills the ESP-NOW link from Node 2 silently.
"""

import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import socket
import threading
import time

import requests

from config import (
    IOT_API_URL,
    NODE1_SERIAL_PORT,
    NODE1_SERIAL_BAUD,
    NODE1_RECONNECT_SECONDS,
    NODE1_STALE_SECONDS,
    NODE_REFRESH_SECONDS,
    NODE1_TCP_PORT,
    NODE1_ACCEPT_TCP,
    DISCOVERY_PORT,
    HOST,
    FACE_ENABLED,
    FACE_EMOTIONS,
    TOF_SIDES,
    TOF_BANDS,
    TOF_DRIVES_IR,
    TOF_DISTANCE_ENABLED,
)
from genie_serial import GenieNode

try:
    from serial.tools import list_ports
except ImportError:                     # pyserial absent; autodetect disabled
    list_ports = None


# ==================================================================
# TRANSLATION
# ==================================================================
# Node 1's field names -> api.py's sensor names. This is code and not a
# config table on purpose: two entries need a fallback chain and one needs
# a three-way level lookup, and expressing that as data would just be a
# worse programming language.
#
# UNAVAILABLE is what every channel of a silent node reports. It is not a
# reading -- it is the absence of one, kept distinct from "clear" for the
# reason in point 2 of the module docstring.
UNAVAILABLE = "unknown"

# Channels api.py holds that this bridge does not write.
#
# CHANGED 26-08-2026: IR used to be in here, on the assumption that the
# original sensor ESP32 owned it. Nothing on this machine has ever written
# it, and Node 1 has no IR sensor -- the pin-map docs list one on GPIO 13
# and the firmware never reads that pin (pins.py has no IR entry;
# sensors.py's CHANNELS tuple has no IR channel).
#
# Meanwhile sensor.aiml carries a complete IR category -- "is someone in
# front of you", "is your path blocked", "do you detect anyone" -- which
# answered "I cannot read the presence sensor right now" to every one of
# them, forever, because ir_status was never set by anything.
#
# The ToF FRONT sensor is what those questions are actually asking about,
# so it drives IR now. Set TOF_DRIVES_IR = False in config.py to put it
# back the way it was.
NOT_OURS = () if TOF_DRIVES_IR else ("IR",)

# Everything Node 1 owns. Used to blank the right things when it drops.
NODE1_STATUS_SENSORS = ["SMOKE", "FLAME", "MOTION", "PRESENCE", "FALL", "KNOCK"]
if TOF_DRIVES_IR:
    NODE1_STATUS_SENSORS.append("IR")
if TOF_DISTANCE_ENABLED:
    NODE1_STATUS_SENSORS.append("DISTANCE")
NODE1_STATUS_SENSORS = tuple(NODE1_STATUS_SENSORS)

NODE1_VALUE_SENSORS = ("TEMPERATURE", "HUMIDITY", "TILT", "ACCEL")

# DISTANCE carries BOTH a status (which side) and a value (how far), so it
# has to be blanked on both axes, unlike everything else here.
DUAL_FIELD_SENSORS = ("DISTANCE",)

# Sensors carrying numeric detail beyond status/value, which has to be
# cleared alongside it when the node drops -- a stale raw ADC count of 812
# looks exactly as authoritative as a live one.
EXTRA_FIELDS = {"SMOKE": ("raw", "ratio"), "KNOCK": ("count",)}


def _level(telemetry):
    """SMOKE, as clear / elevated / alarm / unknown.

    Prefers Node 1's graded `gas_level` (which sensors.py already computes
    with a median, an EMA and a 3-consecutive-poll confirm) and falls back
    to the two booleans PROTOCOL.md defines, so this works against a Node 1
    that only implements the base contract.

    THE TRAP: sensors.py's _rate() returns 1 -- the same value as
    "elevated" -- for a channel in self.errors. A dead MQ-2 therefore looks
    exactly like a mild gas rise if you read the level alone. Node 1's
    emitter sends `gas_ok: false` for that case and it is checked FIRST; a
    bridge that skipped it would have GENIE calmly announcing elevated
    smoke from a sensor with a broken wire, forever.
    """
    if telemetry.get("gas_ok") is False:
        return UNAVAILABLE

    level = telemetry.get("gas_level")
    if isinstance(level, str) and level.lower() in ("clear", "elevated", "alarm"):
        return level.lower()

    if telemetry.get("gas_alarm"):
        return "alarm"
    if telemetry.get("gas_alert"):
        return "elevated"
    if "gas_alarm" in telemetry or "gas_alert" in telemetry:
        return "clear"
    return UNAVAILABLE


def _flame(telemetry):
    """FLAME, as clear / alarm / unknown. Same errors-look-like-warnings
    trap as _level, same fix."""
    if telemetry.get("flame_ok") is False:
        return UNAVAILABLE
    if "flame" not in telemetry:
        return UNAVAILABLE
    return "alarm" if telemetry.get("flame") else "clear"


def _fall(telemetry):
    """FALL, in sensor.aiml's vocabulary: idle / freefall / impact / fallen.

    THREE vocabularies existed for this and none of them agreed:
    PROTOCOL.md's boolean `fall`, config.SENSOR_ALERT_STATES' "detected",
    and sensor.aiml's four-state FSM. sensor.aiml's wins -- it is the only
    one that can say "something hit the ground hard, give me a moment"
    instead of committing to an answer mid-event, and the FSM on Node 1
    genuinely has that state. The other two are accepted as inputs so
    nothing that still speaks them breaks.
    """
    state = telemetry.get("fall_state")
    if isinstance(state, str) and state.lower() in (
            "idle", "freefall", "impact", "fallen"):
        return state.lower()

    if "fall" not in telemetry:
        return UNAVAILABLE
    return "fallen" if telemetry.get("fall") else "idle"


def _presence(telemetry):
    """PRESENCE: somebody in the room, from any of the four ToF sensors.

    node2_link false is NOT "nobody is there" -- it is "the four sensors
    that would know are unreachable". PROTOCOL_ADDENDUM.md is explicit that
    a frozen last-known presence reading is worse than a visibly dead one.

    CHANGED 27-08-2026: this used to feed MOTION. It was the wrong channel.
    sensor.aiml's MOTION category is written entirely about the UNIT being
    moved -- "did you move", "are you tilted", "has someone picked you up",
    "are you shaking" -- and its replies say things like "I felt a change
    in my position". Answering those from the ToF array meant GENIE claimed
    it had been moved because somebody walked past it. See _moved() below
    for what MOTION reads now.
    """
    if telemetry.get("node2_link") is False:
        return UNAVAILABLE
    if "present" not in telemetry:
        return UNAVAILABLE
    return "true" if telemetry.get("present") else "false"


def _moved(telemetry):
    """MOTION: the unit itself was jolted, tilted or picked up.

    Reads the MPU-6050s' CONFIRMED level, not raw acceleration. Node 1
    scores 2 for an impact (>1.8 g) and 1 for free-fall (<0.7 g) across 3
    consecutive polls, so this is a real jolt rather than a reading that
    wobbled once -- which matters, because MOTION is in
    SENSOR_ALERT_STATES and a channel that twitched would place phone
    calls. A unit sitting on a desk reports false indefinitely.
    """
    if telemetry.get("imu_ok") is False:
        return UNAVAILABLE

    level = telemetry.get("imu_level")
    if isinstance(level, str) and level.lower() in ("clear", "elevated", "alarm"):
        return "false" if level.lower() == "clear" else "true"

    if "moved" in telemetry:
        return "true" if telemetry.get("moved") else "false"
    return UNAVAILABLE


def _tilt(telemetry):
    """Degrees from flat, or None. The MPU's own number, stored so a dead
    IMU can be told from a still one -- `moved: false` reads the same
    either way, but a tilt of null does not."""
    if telemetry.get("imu_ok") is False:
        return None
    return telemetry.get("tilt")


def _accel(telemetry):
    """Total acceleration in g. 1.00 at rest on a levelled module."""
    if telemetry.get("imu_ok") is False:
        return None
    return telemetry.get("accel_g")


def _number(telemetry, key):
    """DHT11 readings. -99 is PROTOCOL.md's "the read failed" sentinel and
    must never reach a spoken reply -- "it is minus ninety nine degrees" is
    the kind of thing an examiner remembers."""
    v = telemetry.get(key)
    if v is None or v == -99:
        return None
    return v


def _tof_valid(telemetry):
    """Is the ToF array actually reporting?

    Node 2 reaches the laptop only through Node 1, so `node2_link: false`
    and "no ToF fields at all" both mean the same thing: the four sensors
    that would know are unreachable. Neither means the room is empty.
    """
    if telemetry.get("node2_link") is False:
        return False
    return isinstance(telemetry.get("tof_mm"), (list, tuple))


def _tof_list(telemetry, key, default):
    v = telemetry.get(key)
    if not isinstance(v, (list, tuple)):
        return [default] * len(TOF_SIDES)
    out = list(v[:len(TOF_SIDES)])
    while len(out) < len(TOF_SIDES):
        out.append(default)
    return out


def zone_of(mm):
    """Name a raw distance. Only for readings that arrive WITHOUT a zone --
    i.e. the ToF board's direct line. Relayed readings keep the name Node 1
    already gave them."""
    if mm is None:
        return "CLEAR"
    for upper, name in TOF_BANDS:
        if mm < upper:
            return name
    return "CLEAR"


def _zones(telemetry):
    """The four zone names for a message, however it arrived.

    Node 1 relays readings already named by node1_link._BANDS. The ToF
    board's direct line sends raw millimetres and no names at all, because
    naming them is the laptop's job and duplicating the band table on a
    third device is how the project ended up with three that disagree.

    Everything that needs a zone goes through here. It used to be derived
    in tof_payload() only, so _front() read an all-CLEAR list off a direct
    message and IR answered "nothing in front of me" with a hand 18 cm from
    the sensor.
    """
    if isinstance(telemetry.get("tof_zone"), (list, tuple)):
        return _tof_list(telemetry, "tof_zone", "CLEAR")
    return [zone_of(v) for v in _tof_list(telemetry, "tof_mm", None)]


def tof_payload(telemetry):
    """The per-direction picture for api.py's /tof endpoint.

    `hit` is derived here rather than read off the wire, because
    node1_link.telemetry_fields() sends `present` (any side) and the zone
    words, not the per-side trigger bitmask that Node 2 actually computed.
    A side counts as hit when its ZONE is one of the close bands -- which
    is a coarser answer than Node 2's calibrated baseline test, and is
    flagged as such rather than passed off as the real thing.
    """
    if not _tof_valid(telemetry):
        return {
            "sides": list(TOF_SIDES),
            "mm": [None] * len(TOF_SIDES),
            "zone": [UNAVAILABLE] * len(TOF_SIDES),
            "hit": [False] * len(TOF_SIDES),
            "nearest_side": None,
            "nearest_mm": None,
            "link": False,
        }

    mm = _tof_list(telemetry, "tof_mm", None)
    zone = _zones(telemetry)
    hit = [z in ("TOUCH", "NEAR", "PRESENT") for z in zone]

    side, near = _nearest(mm)

    return {
        "sides": list(TOF_SIDES),
        "mm": mm,
        "zone": zone,
        "hit": hit,
        "nearest_side": side,
        "nearest_mm": near,
        "link": True,
    }


def _nearest(mm):
    """(side, mm) of the closest VALID reading, or (None, None).

    None entries are failed reads, not zero distance. PROTOCOL_ADDENDUM.md
    is explicit about why Node 2 encodes those as 0xFFFF rather than 0:
    treating a failed read as "something is touching the lens" produces
    exactly the reading an intrusion would, from a sensor that has simply
    stopped working.
    """
    best_i, best = None, None
    for i, v in enumerate(mm):
        if v is None:
            continue
        if best is None or v < best:
            best_i, best = i, v
    if best_i is None:
        return None, None
    return TOF_SIDES[best_i], best


def _front(telemetry):
    """IR, from the ToF FRONT sensor -- see the NOT_OURS note above.

    Front only, deliberately. "Is someone in front of you" is not "is
    someone in the room": answering the first with the second means GENIE
    says something is ahead of it when the trigger came from the rear
    sensor, which is worse than not answering.
    """
    if not _tof_valid(telemetry):
        return UNAVAILABLE
    return ("true" if _zones(telemetry)[0] in ("TOUCH", "NEAR", "PRESENT")
            else "false")


def translate(telemetry):
    """One Node 1 telemetry object -> the /update_sensors readings dict."""
    gas_level = _level(telemetry)

    readings = {
        # raw and ratio ride along so the sensor can be TESTED, not just
        # read: "clear" from clean air and "clear" from a detached AO wire
        # are the same word, and 0 or 4095 versus a plausible 300-900 is
        # not. Dropped when the channel has failed, for the usual reason.
        "SMOKE":       {"status": gas_level,
                        "raw": None if gas_level == UNAVAILABLE
                               else telemetry.get("gas_raw"),
                        "ratio": None if gas_level == UNAVAILABLE
                                 else telemetry.get("gas_ratio")},
        "FLAME":       {"status": _flame(telemetry)},
        "MOTION":      {"status": _moved(telemetry)},
        "PRESENCE":    {"status": _presence(telemetry)},
        "FALL":        {"status": _fall(telemetry)},
        "KNOCK":       {"status": "true" if telemetry.get("knock") else "false",
                        "count": telemetry.get("knock_count")},
        "TEMPERATURE": {"value": _number(telemetry, "temp_c")},
        "HUMIDITY":    {"value": _number(telemetry, "humidity")},
        "TILT":        {"value": _tilt(telemetry)},
        "ACCEL":       {"value": _accel(telemetry)},
    }

    if TOF_DRIVES_IR:
        readings["IR"] = {"status": _front(telemetry)}

    if TOF_DISTANCE_ENABLED:
        if _tof_valid(telemetry):
            side, near = _nearest(_tof_list(telemetry, "tof_mm", None))
        else:
            side, near = None, None
        readings["DISTANCE"] = {
            "value": near,
            "status": side if side else UNAVAILABLE,
        }

    return readings


def blanked():
    """What every Node 1 channel reads as when Node 1 is not talking."""
    readings = {s: {"status": UNAVAILABLE} for s in NODE1_STATUS_SENSORS}
    readings.update({s: {"value": None} for s in NODE1_VALUE_SENSORS})
    # DISTANCE has both fields, and the number is the dangerous half: a
    # stale "0.4 m" reads as something right in front of GENIE long after
    # the sensor stopped reporting.
    for name in DUAL_FIELD_SENSORS:
        if name in readings:
            readings[name] = {"status": UNAVAILABLE, "value": None}
    for name, extras in EXTRA_FIELDS.items():
        if name in readings:
            for extra in extras:
                readings[name][extra] = None
    return readings


def blanked_tof():
    """The /tof picture when nothing is reporting."""
    return tof_payload({"node2_link": False})


# ==================================================================
# THE BRIDGE
# ==================================================================

# Sensors whose transition into an alerting state should be evaluated NOW
# rather than at the next sensor_watch poll. These are the ones where the
# gap between "the reading arrived" and "somebody was called" is the whole
# argument for the rule-based path existing.
URGENT = {
    "SMOKE": {"alarm", "true"},
    "FLAME": {"alarm", "true"},
    "FALL":  {"fallen", "impact", "detected", "true"},
}


class NodeBridge:

    def __init__(self, watcher=None, api_url=None, port=None, log=print):
        self.watcher = watcher          # a sensor_watch.SensorWatcher, or None
        self.api_url = api_url or IOT_API_URL
        self.port = port if port is not None else NODE1_SERIAL_PORT
        self.log = log

        self.node = None                # genie_serial.GenieNode once connected
        self._node_lock = threading.Lock()

        self._last_readings = None      # last dict pushed, to skip no-op POSTs
        self._last_tof = None
        self._last_push = 0.0
        self._last_telemetry = 0.0
        self._connected = False
        self._warned_api = False
        self._warned_missing = False

        # One line per DISTINCT fault, not one per retry. See _report_error.
        self._last_error = None
        self._error_repeats = 0

        # Assign to receive knocks and hand waves. Left unwired by default:
        # turning a knock into a wake trigger means reaching into
        # AudioServer's wake/VAD state machine, which is a separate change
        # with its own failure modes, and half-wiring it would give GENIE a
        # second way to start listening that nothing tests.
        self.on_attention = None

    # ------------------------------------------------------------- port
    def _autodetect(self):
        """Find Node 1 by asking each candidate port whether it speaks the
        protocol -- never by picking the first ESP32 it sees.

        THIS MATTERS. The modem ESP32 is on a COM port too (COM21 in the
        esp32/ tooling), and it is the board that places the emergency
        call. Opening its port to see what happens is not a harmless probe:
        it resets the board on most USB-serial bridges. So a candidate is
        accepted ONLY after it emits a line that parses as GENIE telemetry.
        """
        if list_ports is None:
            return None

        candidates = []
        for p in list_ports.comports():
            blob = "%s %s" % (p.description or "", p.manufacturer or "")
            if (p.vid == 0x303A                      # Espressif native USB
                    or "CP210" in blob or "CH34" in blob
                    or "USB Serial" in blob or "ESP32" in blob.upper()):
                candidates.append(p.device)

        for device in candidates:
            if self._probe(device):
                self.log("[node_bridge] Node 1 found on %s" % device)
                return device

        if candidates and not self._warned_missing:
            self.log("[node_bridge] serial ports %s exist but none spoke "
                     "GENIE telemetry - is main.py running on Node 1, or is "
                     "Thonny holding the port?" % candidates)
            self._warned_missing = True
        return None

    def _probe(self, device, seconds=4.0):
        """Listen briefly for one parseable telemetry line. Never writes."""
        try:
            import serial
            with serial.Serial(device, NODE1_SERIAL_BAUD, timeout=0.5) as s:
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    raw = s.readline()
                    if not raw:
                        continue
                    try:
                        msg = json.loads(raw.decode(errors="ignore").strip())
                    except ValueError:
                        continue        # partial line mid-boot, keep listening
                    if isinstance(msg, dict) and msg.get("t") in (
                            "telemetry", "boot", "calibrated", "event"):
                        return True
        except Exception:
            return False                # busy, missing, or not a serial port
        return False

    # ------------------------------------------------------------- api.py
    def _push(self, readings, links, force=False, tof=None):
        """POST one tick. Skips the round trip when nothing changed, unless
        forced -- Node 1 ticks every 2 s and the readings are identical most
        of the time.

        The change test covers the ToF block as well as the readings. Raw
        millimetres jitter by a few counts on every sample, so a naive
        comparison would make every tick "changed" and this would POST at
        0.5 Hz forever -- which works, but hides the far more useful signal
        that the picture is genuinely static.
        """
        if not force and readings == self._last_readings and tof == self._last_tof:
            return

        body = {"readings": readings, "links": links}
        if tof is not None:
            body["tof"] = tof

        try:
            requests.post(
                "%s/update_sensors" % self.api_url,
                json=body,
                timeout=4,
            )
        except Exception as e:
            if not self._warned_api:
                self.log("[node_bridge] cannot reach api.py (%r) - sensor "
                         "readings are not being stored until it answers" % e)
                self._warned_api = True
            return

        if self._warned_api:
            self.log("[node_bridge] api.py reachable again")
            self._warned_api = False

        self._last_readings = readings
        if tof is not None:
            self._last_tof = tof
        self._last_push = time.monotonic()

    def _urgent(self, readings):
        """Did any sensor just ENTER a state worth evaluating immediately?

        Entering, not sitting in. A real fire holds the flame sensor high
        for as long as it burns; that is one emergency, not one per tick.
        """
        previous = self._last_readings or {}
        for sensor, alerting in URGENT.items():
            now = (readings.get(sensor) or {}).get("status")
            was = (previous.get(sensor) or {}).get("status")
            if now in alerting and was not in alerting:
                return sensor
        return None

    # ------------------------------------------------------------- hooks
    def _on_telemetry(self, state):
        self._last_telemetry = time.monotonic()
        readings = translate(state)
        fired = self._urgent(readings)

        self._push(readings,
                   {"node1": True, "node2": bool(state.get("node2_link"))},
                   tof=tof_payload(state))

        if fired and self.watcher is not None:
            # The ONE alert owner evaluates it now instead of up to
            # SENSOR_WATCH_INTERVAL from now. It keeps its own edge
            # detection, debounce and cooldown, so calling it early cannot
            # produce a duplicate call -- only an earlier one.
            self.log("[node_bridge] %s went urgent - waking the watcher" % fired)
            try:
                self.watcher.poll_once()
            except Exception as e:
                self.log("[node_bridge] watcher.poll_once() raised: %r" % e)

        if state.get("knock") and self.on_attention:
            try:
                self.on_attention("knock", state)
            except Exception as e:
                self.log("[node_bridge] on_attention raised: %r" % e)

    def _on_tof(self, msg):
        """A message from the ToF board directly, not relayed by Node 1.

        Updates the three ToF-derived channels and NOTHING else. The face
        node's smoke, flame, DHT and IMU readings are not this board's to
        touch -- it has no idea what they are, and writing them would blank
        a perfectly healthy sensor.

        Deliberately does NOT set links["node1"]: whether the face node is
        alive is a separate fact, and a live ToF board must not be able to
        vouch for a dead one.
        """
        self._last_telemetry = time.monotonic()

        readings = dict(self._last_readings or blanked())
        readings["PRESENCE"] = {"status": _presence(msg)}
        if TOF_DRIVES_IR:
            readings["IR"] = {"status": _front(msg)}
        if TOF_DISTANCE_ENABLED:
            side, near = (_nearest(_tof_list(msg, "tof_mm", None))
                          if _tof_valid(msg) else (None, None))
            readings["DISTANCE"] = {"value": near,
                                    "status": side if side else UNAVAILABLE}

        links = {"node2": True}
        if self._last_readings is None:
            # Nothing has ever reported. Say node1 is down rather than
            # leaving it unstated -- api.py would otherwise keep its
            # startup default, which reads as healthy.
            links["node1"] = False

        self._push(readings, links, tof=tof_payload(msg))

    def _on_alarm(self, kind, msg):
        """genie_serial's Path A hook. Node 1 fires this the instant a fall
        FSM completes or a flame confirms, without waiting for its 2 s tick.

        Note what this does NOT do: it does not speak, text or call. It
        pushes the reading and pokes the watcher, so the decision stays in
        one place. The latency saved is the 2 s tick, which is the part
        worth saving.
        """
        self.log("[node_bridge] event from Node 1: %s" % kind)
        merged = dict(msg)
        if kind == "fall":
            merged.setdefault("fall", True)
            merged.setdefault("fall_state", "fallen")
        self._on_telemetry(merged)

    def _on_wake(self, msg):
        if self.on_attention:
            try:
                self.on_attention("wave", msg)
            except Exception as e:
                self.log("[node_bridge] on_attention raised: %r" % e)

    # ------------------------------------------------------------- face
    def set_emotion(self, name):
        """Laptop -> Node 1. Best effort, never raises, never blocks the
        caller for long -- speak() calls this, and speak() is on the path to
        the user hearing anything at all.

        The node forgets this after 20 s by design (see the face section of
        config.py), so it is re-sent per turn rather than held.
        """
        if not FACE_ENABLED or name not in FACE_EMOTIONS:
            return False
        with self._node_lock:
            node = self.node
        if node is None:
            return False
        try:
            node.set_emotion(name)
            return True
        except Exception as e:
            self.log("[node_bridge] set_emotion(%s) failed: %r" % (name, e))
            return False

    # ------------------------------------------------------------- run
    def run(self):
        if self.port == "off":
            self.log("[node_bridge] disabled (NODE1_SERIAL_PORT = 'off')")
            return

        while True:
            try:
                self._connect_and_serve()
                # A clean run clears the throttle, so if the SAME fault
                # returns later it is reported again rather than silently
                # swallowed as a repeat of something long since fixed.
                self._last_error = None
            except Exception as e:
                self._report_error(e)
            self._drop()
            time.sleep(NODE1_RECONNECT_SECONDS)

    def _report_error(self, exc):
        """Name the fault once, and name the usual cause by name.

        Two problems with printing every failure: the retry loop runs
        forever by design, so an unchanging fault scrolls everything else
        off the console -- and "bridge loop blew up: SerialException(...)"
        does not tell anybody the actual answer, which is almost always
        that Thonny is holding the port. serial_bridge.py already said
        this properly; node_bridge is the one people actually run.
        """
        msg = str(exc)
        busy = ("Access is denied" in msg
                or "PermissionError" in repr(exc)
                or isinstance(exc, PermissionError))

        key = "busy" if busy else repr(exc)
        if key == self._last_error:
            self._error_repeats += 1
            return

        self._last_error = key
        self._error_repeats = 0

        if busy:
            self.log("[node_bridge] %s is busy - almost certainly Thonny. "
                     "A COM port has exactly one owner."
                     % (self.port or "the serial port"))
            self.log("[node_bridge] Close Thonny and the sensors come back "
                     "on their own; no restart needed.")
        else:
            self.log("[node_bridge] serial error: %r" % exc)

        self.log("[node_bridge] retrying every %gs. This will not repeat "
                 "until the fault changes." % NODE1_RECONNECT_SECONDS)

    def _connect_and_serve(self):
        port = self.port or self._autodetect()

        if not port:
            self._drop()
            time.sleep(NODE1_RECONNECT_SECONDS)
            return

        node = GenieNode(port, NODE1_SERIAL_BAUD)
        node.on_telemetry = self._on_telemetry
        node.on_alarm = self._on_alarm
        node.on_wake = self._on_wake
        node.start()

        with self._node_lock:
            self.node = node
        self._connected = True
        self._warned_missing = False
        self._last_telemetry = time.monotonic()
        self.log("[node_bridge] Node 1 connected on %s at %d baud"
                 % (port, NODE1_SERIAL_BAUD))

        # Watchdog. Node 1 ticks every 2 s; NODE1_STALE_SECONDS of nothing
        # means the board is gone, not merely quiet.
        while True:
            time.sleep(1.0)
            quiet = time.monotonic() - self._last_telemetry
            if quiet > NODE1_STALE_SECONDS:
                self.log("[node_bridge] no telemetry for %.0fs - Node 1 is "
                         "gone, blanking its readings" % quiet)
                return
            # Refresh api.py periodically even when nothing changed. Two
            # reasons: a restarted api.py must not sit on its startup
            # defaults (which read as a confident all-clear), and api.py's
            # own staleness clock needs a heartbeat to measure against.
            if time.monotonic() - self._last_push > NODE_REFRESH_SECONDS:
                self._push(self._last_readings or blanked(),
                           {"node1": True, "node2": False}, force=True,
                           tof=self._last_tof or blanked_tof())

    def _drop(self):
        with self._node_lock:
            node, self.node = self.node, None
        if node is not None:
            try:
                node.stop()
            except Exception:
                pass
        self._connected = False
        self._push(blanked(), {"node1": False, "node2": False}, force=True,
                   tof=blanked_tof())


# ==================================================================
# WIFI TRANSPORT
# ==================================================================
# Node 1 over WiFi sends byte-for-byte what it sends over the serial port:
# one JSON object per line. So this listener does no parsing of its own --
# it hands each line to the same _on_telemetry / _on_alarm the serial
# reader uses, and the two transports cannot drift apart.
#
# Serial and WiFi may both be live at once. That is fine and deliberate:
# the readings are idempotent, api.py stores the newest, and having a
# cable as well as a network is the difference between a demo that
# survives a dead router and one that does not.

def _serve_line(bridge, line):
    """One JSON line from a board, whatever carried it."""
    try:
        msg = json.loads(line)
    except ValueError:
        return                      # partial line during boot, ignore
    if not isinstance(msg, dict):
        return

    kind = msg.get("t")
    if kind == "telemetry":
        bridge._on_telemetry(msg)
    elif kind == "tof":
        bridge._on_tof(msg)
    elif kind == "event":
        ev = msg.get("event")
        if ev == "fall":
            bridge._on_alarm("fall", msg)
        elif ev in ("wave", "knock"):
            bridge._on_wake(msg)
    elif kind in ("boot", "calibrated"):
        bridge.log("[node_bridge] node says: %s" % msg)


def tcp_server(bridge, port=None, log=print):
    """Accept Node 1 connections over WiFi. One thread per board.

    Never raises out of the thread. A board that resets mid-line, a
    half-open socket after the AP drops, a second board connecting -- all
    of those must cost nothing but a log line.
    """
    port = port or NODE1_TCP_PORT
    try:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((HOST, port))
        server.listen(4)
    except OSError as e:
        log("[node_bridge] cannot listen on %d (%r) - WiFi sensor uplink "
            "is OFF, serial still works" % (port, e))
        return

    log("[node_bridge] WiFi uplink listening on %d" % port)

    while True:
        try:
            conn, addr = server.accept()
        except OSError:
            continue
        log("[node_bridge] sensor node connected over WiFi: %s" % (addr,))
        threading.Thread(target=_tcp_client, args=(bridge, conn, addr, log),
                         daemon=True).start()


def _tcp_client(bridge, conn, addr, log):
    buf = b""
    try:
        # A board that has crashed leaves a socket that accepts writes and
        # never sends. The timeout turns that into a clean reconnect
        # instead of a thread parked forever on recv().
        conn.settimeout(NODE1_STALE_SECONDS * 2)
        while True:
            chunk = conn.recv(1024)
            if not chunk:
                break
            buf += chunk
            # Cap the buffer: a peer that never sends a newline must not
            # be able to grow this without limit.
            if len(buf) > 16384:
                buf = b""
                continue
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                _serve_line(bridge, line.decode("utf-8", "ignore").strip())
    except (OSError, socket.timeout):
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass
        log("[node_bridge] sensor node disconnected: %s" % (addr,))


def discovery_broadcaster(port=None, log=print):
    """Tell the boards where the laptop is, so no IP has to be hardcoded.

    Same message AudioServer.py already broadcasts, on the same port, so
    the mic ESP is unaffected and a board only has to learn one protocol.
    Sent from here as well because the sensor path has to work when
    AudioServer is not running -- during bring-up it usually is not.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    except OSError as e:
        log("[node_bridge] discovery broadcast unavailable (%r)" % e)
        return

    while True:
        try:
            sock.sendto(b"GENIE_SERVER_HELLO",
                        ("255.255.255.255", port or DISCOVERY_PORT))
        except OSError:
            pass
        time.sleep(2)


# The process-wide instance, so whisper_service.speak() can reach the face
# without threading a handle through every call.
_bridge = None


def start(watcher=None, **kwargs):
    """Spawn the bridge as a daemon thread. Returns the NodeBridge.

    Three threads, all optional and all independent: the serial reader,
    the WiFi listener, and the discovery broadcaster. Any of them failing
    leaves the others running.
    """
    global _bridge
    _bridge = NodeBridge(watcher=watcher, **kwargs)
    threading.Thread(target=_bridge.run, daemon=True).start()

    if NODE1_ACCEPT_TCP:
        threading.Thread(target=tcp_server, args=(_bridge,),
                         kwargs={"log": _bridge.log}, daemon=True).start()
        threading.Thread(target=discovery_broadcaster,
                         kwargs={"log": _bridge.log}, daemon=True).start()

    return _bridge


def set_emotion(name):
    """Laptop -> the face. Serial if the bridge is up, WiFi otherwise.

    WHY THE FALLBACK EXISTS. This used to be serial-only, and returned
    False the moment NODE1_SERIAL_PORT went to "off" -- which is now the
    normal configuration, because two writers into api.py's sensor_state
    corrupted it. So every call quietly failed and the face simply stopped
    taking direction. Nothing errored; the eyes just never changed again.

    api.py already holds the board's own TCP socket open. Posting there
    reaches the same _handle() on the board that the cable did, so the
    command means the same thing whichever way it travelled.
    """
    if not FACE_ENABLED or name not in FACE_EMOTIONS:
        return False

    if _bridge is not None and _bridge.set_emotion(name):
        return True

    try:
        r = requests.post("%s/emotion" % IOT_API_URL.rstrip("/"),
                          json={"emotion": name}, timeout=2)
        return bool(r.ok and (r.json() or {}).get("boards"))
    except Exception:
        # Best effort, always. speak() is on the path to the user hearing
        # anything at all and must never wait on a face.
        return False


def mute_handling(ms):
    """Tell the board to withhold handling reactions for `ms`.

    The speaker sits in the same enclosure as the IMU, so GENIE's own
    voice shakes the case hard enough to classify as being handled. Muting
    at the BOARD, for exactly the window the speaker is active, is what
    makes that survivable -- the laptop alternative was "ignore reactions
    for N seconds after any speech", which also threw away the real ones
    and left putting the unit down unacknowledged.

    Best effort and non-blocking. speak() calls this, and speak() is on
    the path to the user hearing anything at all.
    """
    try:
        r = requests.post("%s/to_board" % IOT_API_URL.rstrip("/"),
                          json={"cmd": "quiet", "ms": int(ms)}, timeout=2)
        return bool(r.ok)
    except Exception:
        return False


def connected():
    return _bridge is not None and _bridge._connected


if __name__ == "__main__":
    # Standalone: python Services/node_bridge.py
    # Proves the translation table without a board, a serial port or api.py.
    print("--- translation, healthy node ---")
    print(json.dumps(translate({
        "t": "telemetry", "temp_c": 31.0, "humidity": 54.0,
        "gas_level": "elevated", "gas_alert": True, "gas_alarm": False,
        "flame": False, "present": True, "node2_link": True,
        "fall_state": "idle", "knock": False,
    }), indent=2))

    print("\n--- a dead MQ-2 must not read as 'elevated' ---")
    assert _level({"gas_ok": False, "gas_level": "elevated"}) == UNAVAILABLE
    assert _level({"gas_alert": True}) == "elevated"
    assert _level({"gas_alarm": True, "gas_alert": True}) == "alarm"
    assert _level({}) == UNAVAILABLE
    print("  ok")

    print("--- a dead Node 2 must not read as 'nobody there' ---")
    assert _presence({"present": True, "node2_link": False}) == UNAVAILABLE
    assert _presence({"present": False, "node2_link": True}) == "false"
    assert _presence({}) == UNAVAILABLE
    print("  ok")

    print("--- DHT -99 must never reach a spoken reply ---")
    assert _number({"temp_c": -99}, "temp_c") is None
    assert _number({"temp_c": 31.0}, "temp_c") == 31.0
    print("  ok")

    print("--- fall vocabularies all land on sensor.aiml's ---")
    assert _fall({"fall_state": "impact"}) == "impact"
    assert _fall({"fall": True}) == "fallen"
    assert _fall({"fall": False}) == "idle"
    assert _fall({}) == UNAVAILABLE
    print("  ok")

    print("--- blanking covers every channel Node 1 owns, and nothing else ---")
    b = blanked()
    assert set(b) == set(NODE1_STATUS_SENSORS) | set(NODE1_VALUE_SENSORS)
    assert not set(b) & set(NOT_OURS), "the bridge must not write IR"
    assert set(translate({})) == set(b), "translate and blanked must agree"
    assert "IR" in b, "IR is ours now -- see the NOT_OURS note"
    print("  ok")

    print("--- ToF: the four sides, and which one is nearest ---")
    live = {"node2_link": True, "tof_mm": [1800, 412, None, 2400],
            "tof_zone": ["FAR", "PRESENT", "CLEAR", "CLEAR"]}
    t = tof_payload(live)
    assert t["link"] is True
    assert t["nearest_side"] == "left" and t["nearest_mm"] == 412, t
    assert t["hit"] == [False, True, False, False], t["hit"]
    print("  ok  nearest = %s at %d mm" % (t["nearest_side"], t["nearest_mm"]))

    print("--- a failed read is not zero distance ---")
    # PROTOCOL_ADDENDUM.md: Node 2 encodes a bad read as 0xFFFF, never 0,
    # because 0 is indistinguishable from something touching the lens --
    # which is exactly the reading an intrusion produces.
    t = tof_payload({"node2_link": True, "tof_mm": [None, None, None, None],
                     "tof_zone": ["CLEAR"] * 4})
    assert t["nearest_side"] is None and t["nearest_mm"] is None, t
    print("  ok")

    print("--- a dead Node 2 blanks the array, it does not zero it ---")
    t = tof_payload({"node2_link": False, "tof_mm": [100, 100, 100, 100],
                     "tof_zone": ["TOUCH"] * 4})
    assert t["link"] is False
    assert t["mm"] == [None] * 4 and t["zone"] == [UNAVAILABLE] * 4, t
    assert t["nearest_side"] is None
    print("  ok")

    print("--- IR is the FRONT sensor only, not any sensor ---")
    # Answering "is someone in front of you" with "something is in the room"
    # would have GENIE point at empty space.
    assert _front({"node2_link": True, "tof_zone":
                   ["CLEAR", "NEAR", "CLEAR", "CLEAR"],
                   "tof_mm": [None, 200, None, None]}) == "false"
    assert _front({"node2_link": True, "tof_zone":
                   ["NEAR", "CLEAR", "CLEAR", "CLEAR"],
                   "tof_mm": [200, None, None, None]}) == "true"
    assert _front({"node2_link": False, "tof_zone": ["NEAR"] * 4}) == UNAVAILABLE
    assert _front({}) == UNAVAILABLE
    print("  ok")

    print("--- PRESENCE is ANY side; IR is the front one ---")
    rear_only = {"node2_link": True, "present": True,
                 "tof_zone": ["CLEAR", "CLEAR", "CLEAR", "NEAR"],
                 "tof_mm": [None, None, None, 250]}
    r = translate(rear_only)
    assert r["PRESENCE"]["status"] == "true", r["PRESENCE"]
    assert r["IR"]["status"] == "false", r["IR"]
    assert r["DISTANCE"]["status"] == "rear", r["DISTANCE"]
    print("  ok  (someone behind: PRESENCE true, IR false)")

    print("--- IR works on a direct line with no zone names ---")
    # The ToF board sends raw mm only. _front() used to read tof_zone,
    # get an all-CLEAR default, and answer "nothing in front of me" with
    # a hand 18 cm from the sensor.
    direct = {"node2_link": True, "present": True,
              "tof_mm": [180, 2000, 2100, 2200]}
    assert _front(direct) == "true", _front(direct)
    assert _zones(direct)[0] == "NEAR", _zones(direct)
    assert tof_payload(direct)["zone"][1] == "CLEAR"
    assert _front({"node2_link": True, "present": True,
                   "tof_mm": [2000, 180, 2100, 2200]}) == "false"
    print("  ok")

    print("--- MOTION is the IMU, NOT the ToF array ---")
    # The bug this replaces: MOTION used to read ToF presence, and
    # sensor.aiml's MOTION category is entirely about the UNIT moving
    # ("did you move", "are you tilted", "has someone picked you up").
    # So somebody walking past made GENIE claim it had been moved.
    assert r["MOTION"]["status"] == UNAVAILABLE, (
        "presence must not reach MOTION: %r" % r["MOTION"])

    walking_past = translate({"node2_link": True, "present": True,
                              "tof_zone": ["NEAR", "CLEAR", "CLEAR", "CLEAR"],
                              "tof_mm": [250, None, None, None],
                              "imu_ok": True, "imu_level": "clear"})
    assert walking_past["PRESENCE"]["status"] == "true"
    assert walking_past["MOTION"]["status"] == "false", (
        "somebody walking past must not read as the unit being moved")

    picked_up = translate({"node2_link": True, "present": False,
                           "tof_zone": ["CLEAR"] * 4, "tof_mm": [2000] * 4,
                           "imu_ok": True, "imu_level": "alarm",
                           "tilt": 41, "accel_g": 2.3})
    assert picked_up["MOTION"]["status"] == "true", picked_up["MOTION"]
    assert picked_up["PRESENCE"]["status"] == "false"
    assert picked_up["TILT"]["value"] == 41
    assert picked_up["ACCEL"]["value"] == 2.3
    print("  ok  (walking past is not being picked up)")

    print("--- a dead IMU is not a still one ---")
    # MOTION false reads identically either way; the numbers do not.
    dead = translate({"imu_ok": False, "imu_level": "elevated",
                      "tilt": 41, "accel_g": 2.3})
    assert dead["MOTION"]["status"] == UNAVAILABLE, dead["MOTION"]
    assert dead["TILT"]["value"] is None and dead["ACCEL"]["value"] is None
    print("  ok")

    print("--- the raw MQ-2 numbers survive, so it can be tested ---")
    live = translate({"gas_ok": True, "gas_level": "clear",
                      "gas_raw": 812, "gas_ratio": 1.04})
    assert live["SMOKE"]["raw"] == 812 and live["SMOKE"]["ratio"] == 1.04
    broken = translate({"gas_ok": False, "gas_raw": 812, "gas_ratio": 1.04})
    assert broken["SMOKE"]["raw"] is None, broken["SMOKE"]
    assert blanked()["SMOKE"]["raw"] is None
    print("  ok")

    print("--- a stale distance must never survive a dropout ---")
    b = blanked()
    assert b["DISTANCE"] == {"status": UNAVAILABLE, "value": None}, b["DISTANCE"]
    print("  ok")

    print("--- urgent transitions fire once, not every tick ---")
    br = NodeBridge.__new__(NodeBridge)
    br._last_readings = translate({"gas_alarm": False, "flame": False,
                                   "fall": False, "node2_link": True})
    hot = translate({"gas_alarm": True, "gas_alert": True, "flame": False,
                     "fall": False, "node2_link": True})
    assert br._urgent(hot) == "SMOKE"
    br._last_readings = hot
    assert br._urgent(hot) is None, "re-fired while the sensor sat latched"
    print("  ok")

    print("\nall self-tests passed")
