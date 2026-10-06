"""
GENIE FastAPI server: relay control + sensor readings.

RELAY ESP32:
    whisper_service.py -> POST /relay  -> this file -> GET (relay ESP32)/relay
    whisper_service.py -> GET  /status -> this file (returns stored state)

SENSOR ESP32 (separate board, independent of the relay ESP32):
    Sensor ESP32 -> POST /update_sensor -> this file (stores latest reading)
    whisper_service.py -> POST /sensor  -> this file (returns latest stored reading)

The two ESP32 boards never talk to each other -- this file is the only
thing that knows about both. Run standalone:

    python api.py
"""

import json
import os
import socket
import sys
import threading
import time

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "Services"))

from fastapi import FastAPI
from pydantic import BaseModel
import requests
import uvicorn

from config import (
    IOT_ESP_IP,
    IOT_API_PORT,
    LINK_STALE_SECONDS,
    NODE1_TCP_PORT,
    DISCOVERY_PORT,
    HOST,
    FACE_EMOTIONS,
)

# The one translation from board vocabulary to api.py vocabulary. Imported
# rather than reimplemented: a board posting here and a board arriving over
# the serial port must land in exactly the same state, and two copies of a
# mapping table is how that stops being true.
import node_bridge


def _check_relay_ip():
    """Complain at STARTUP about a malformed IOT_ESP_IP, not at first use.

    A stray character here is invisible until somebody says "turn on the
    fan", and then it surfaces as a DNS error in the middle of an
    unrelated conversation:

        Failed to resolve '10.187.53.63%60' ([Errno 11001] getaddrinfo
        failed)

    %60 is a URL-encoded backtick -- a typo that travelled all the way
    from this file into a hostname lookup because nothing between here
    and requests ever looked at it. Two seconds of checking at import
    turns that into a line you cannot miss while the server is starting.
    """
    ip = (IOT_ESP_IP or "").strip()
    parts = ip.split(".")
    ok = len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255
                                 for p in parts)
    if not ok:
        print("!! IOT_ESP_IP is not a plain IPv4 address: %r" % IOT_ESP_IP)
        print("   Relay commands will fail with a DNS error. Fix it in "
              "config.py.")
    return ok


_check_relay_ip()

app = FastAPI()

# ---------------------------------------
# Relay state
# 0 = OFF, 1 = ON
# ---------------------------------------
light_state = 0
fan_state = 0

# relay.aiml captures whatever word the user actually says as the device
# (a wildcard, not a fixed value) -- e.g. "turn on the light" produces
# device="LIGHT", "turn on the bulb" produces device="BULB". This maps
# any of those spoken synonyms onto the single internal "LIGHT" channel.
# (A sensor-integration prototype's /relay endpoint checked for the
# literal string "BULB" only, with no alias table -- so "turn on the
# light" would have fallen through to "Unknown device" on that version.)
DEVICE_ALIASES = {
    "LIGHT": "LIGHT",
    "LIGHTS": "LIGHT",
    "BULB": "LIGHT",
    "BULBS": "LIGHT",
    "LAMP": "LIGHT",
    "LAMPS": "LIGHT",
    "FAN": "FAN",
    "FANS": "FAN",
    # Relay 1 has a FAN on it and answers to every word people use for
    # "move the air". GENEROUS IN, HONEST OUT: this maps the synonyms, and
    # api.py reports FAN back, because a fan is what is actually plugged
    # in. Saying "starting the dehumidifier" when there is no
    # dehumidifier is a false claim about the world, and it is the kind an
    # examiner asks about.
    #
    # The scalability is real regardless: swap the appliance on relay 1
    # and only the spoken name has to change.
    "EXHAUST": "FAN",
    "AC": "FAN",
    "AIR CONDITIONER": "FAN",
    "COOLING": "FAN",
    "COOLER": "FAN",
    "DEHUMIDIFIER": "FAN",
    "HUMIDIFIER": "FAN",
    "AIR": "FAN",
    "BOTH": "BOTH",
}

# ---------------------------------------
# Sensor state
# ---------------------------------------
# Latest values received from the SENSOR ESP32 (a separate board from
# the relay ESP32). That board periodically POSTs to /update_sensor;
# ai.py later reads the latest stored value via POST /sensor. This file
# does NOT contact the sensor ESP32 on demand -- /sensor only returns
# whatever was most recently pushed to /update_sensor.
sensor_state = {
    # `raw` is the MQ-2 ADC count and `ratio` is raw/baseline -- the two
    # numbers the 1.20 alert and 1.50 alarm thresholds are actually made
    # of. Stored alongside the level because a level alone cannot be
    # tested: "clear" from a working sensor in clean air and "clear" from
    # a sensor whose AO wire fell off look identical, and the raw count
    # (0 or 4095 versus a plausible 300-900) tells them apart instantly.
    "SMOKE": {"status": "false", "raw": None, "ratio": None},
    "FLAME": {"status": "false"},
    "IR": {"status": "false"},
    "MOTION": {"status": "false"},
    "FALL": {"status": "idle"},
    "TEMPERATURE": {"value": None},
    "HUMIDITY": {"value": None},
    # Node 1's two SW-420 vibration switches, plus the ones Node 2
    # relays. Not in SENSOR_ALERT_STATES on purpose -- a knock is an
    # attention signal, not an emergency, and a door slamming must not
    # place a phone call.
    "KNOCK": {"status": "false", "count": None},
    # Nearest valid ToF reading. `value` is millimetres, `status` is which
    # of the four sides saw it. Two fields for one sensor because "1.2 m"
    # and "on your left" are different answers to different questions and
    # the array can give both.
    "DISTANCE": {"value": None, "status": "unknown"},

    # ------------------------------------------------------------------
    # Added 27-08-2026. Everything below was being MEASURED by the face
    # node and thrown away by the bridge -- it went out on the wire in the
    # telemetry line and was never stored, so none of it could be read
    # back, and none of it could be used to tell a working sensor from a
    # dead one.
    # ------------------------------------------------------------------

    # Someone in the room, from any of the four ToF sensors. Split out
    # from MOTION on 27-08-2026: sensor.aiml's MOTION category is entirely
    # about the UNIT being moved ("did you move", "are you tilted", "has
    # someone picked you up"), so answering it from the ToF array made
    # GENIE say "I felt a change in my position" because somebody walked
    # past. Two different questions, two channels.
    "PRESENCE": {"status": "unknown"},

    # Degrees from flat, and total acceleration in g. The MPU-6050's
    # actual output. FALL was already derived from it; these are the
    # numbers themselves, which is what you need to tell "the IMU is
    # working" from "the IMU is answering".
    "TILT": {"value": None},
    "ACCEL": {"value": None},
}

# The full ToF picture, one entry per direction. Kept OUT of sensor_state
# because every consumer of that dict expects one scalar per sensor, and
# four distances is not a scalar. sensor_state carries the two summaries
# that ARE scalars -- MOTION (any side triggered) and DISTANCE (nearest) --
# and this carries the detail behind them.
#
# Read it at GET /tof. `sides` is the XSHUT bring-up order from Node 2 and
# also the order of the enclosure apertures; do not reorder it to make a
# display look nicer.
tof_state = {
    "sides": ["front", "left", "right", "rear"],
    "mm": [None, None, None, None],
    "zone": ["CLEAR", "CLEAR", "CLEAR", "CLEAR"],
    "hit": [False, False, False, False],
    "nearest_side": None,
    "nearest_mm": None,
    "link": False,
    "updated_at": 0.0,
}

# Which nodes are actually talking. Reported alongside the readings but
# kept OUT of sensor_state, because everything that iterates that dict
# (ai.py's predicate map, sensor_watch's alert scan) expects every entry
# to be a sensor.
#
# WHY THIS EXISTS. Node 2 reaches the laptop only by way of Node 1, so a
# dead Node 2 and a Node 2 reading "nothing there" look identical from
# here -- both are absence of data. That distinction is the difference
# between "no one has fallen" and "we cannot tell whether anyone has
# fallen", and a safety system that confuses the two reports a confident
# all-clear over a blind spot. node_bridge.py sets these; when either
# goes false it also blanks the readings that node owns rather than
# leaving the last live-looking value in place.
link_state = {
    "node1": False,     # face node, over USB serial
    "node2": False,     # ToF array, over ESP-NOW via Node 1
    "updated_at": 0.0,
}


class RelayCommand(BaseModel):
    command: str
    device: str


class SensorUpdate(BaseModel):
    sensor: str
    status: str | None = None
    value: float | None = None


class SensorRequest(BaseModel):
    sensor: str


class EmotionRequest(BaseModel):
    """The face expression the laptop wants shown.

    Sent down the board's own TCP socket, so it works with the USB cable
    unplugged -- which is the point, since the cable stopped being the
    command path when NODE1_SERIAL_PORT went to "off".
    """
    emotion: str


class HarLabel(BaseModel):
    """Which class the board is currently being made to perform.

    None means "not recording" -- vectors still arrive and are discarded.
    That is deliberate: the board streams continuously, so recording is a
    property of the LAPTOP's attention rather than something the board has
    to be told to start and stop. One less round trip to get wrong, and no
    way to end up with rows whose label is whatever the last run left set.
    """
    label: str | None = None

    # Which recording session this row belongs to, as YYYYMMDD-HHMM.
    #
    # THE POINT OF THIS COLUMN: accuracy from a random split of one
    # session is not a measurement. Windows overlap by 80%, so a test
    # window can share 40 of its 50 samples with a training window and the
    # model has very nearly seen it already. Only a split that holds out a
    # WHOLE session -- different day, different mounting, different mood --
    # measures generalisation. Without this column that split cannot be
    # made at all, so it goes in before the second session rather than
    # after.
    session: str | None = None


class SensorBulkUpdate(BaseModel):
    """One tick from Node 1, all channels at once.

    `readings` maps SENSOR_NAME -> {"status": ...} or {"value": ...},
    exactly the shape /sensor hands back. `links` is optional and carries
    node1/node2 reachability.
    """
    readings: dict[str, dict] = {}
    links: dict[str, bool] | None = None
    tof: dict | None = None


# ============================================================
# HOME
# ============================================================

@app.get("/")
def home():
    return {
        "message": "GENIE FastAPI server running",
        "relay_system": "online",
        "sensor_system": "online",
    }


# ============================================================
# RELAY ESP32
# ============================================================

@app.get("/status")
def status():
    """Read by whisper_service.py's get_iot_status() for "is the fan
    on?"-style queries. Keep this endpoint name stable -- whisper_service.py
    depends on it and explicitly checks for these two keys."""
    return {"light": light_state, "fan": fan_state}


@app.get("/relays")
def get_relays():
    """Same data as /status, human-readable -- handy for checking state
    in a browser during debugging."""
    return {
        "LIGHT": "ON" if light_state else "OFF",
        "FAN": "ON" if fan_state else "OFF",
    }


@app.post("/relay")
def relay(data: RelayCommand):
    global light_state, fan_state

    command = data.command.upper()
    device_raw = data.device.upper()
    device = DEVICE_ALIASES.get(device_raw, device_raw)

    if command not in ("ON", "OFF"):
        return {"message": f"Unknown command: {command}"}

    value = 1 if command == "ON" else 0

    if device == "FAN":
        fan_state = value
    elif device == "LIGHT":
        light_state = value
    elif device == "BOTH":
        light_state = value
        fan_state = value
    else:
        return {"message": f"Unknown device: {device_raw}"}

    print("--------------------------------")
    print("Current Relay States")
    print("Light:", "ON" if light_state else "OFF")
    print("Fan  :", "ON" if fan_state else "OFF")
    print("--------------------------------")

    try:
        response = requests.get(
            f"http://{IOT_ESP_IP}/relay",
            params={"light": light_state, "fan": fan_state},
            timeout=5,
        )
        return {
            "message": f"{device} {command}",
            "light": light_state,
            "fan": fan_state,
            "esp_response": response.text,
        }
    except Exception as e:
        return {"message": f"ESP32 Connection Failed: {e}"}


# ============================================================
# SENSOR ESP32
# ============================================================

@app.post("/update_sensor")
def update_sensor(data: SensorUpdate):
    """The sensor ESP32 calls this periodically to push its latest
    reading, e.g. {"sensor": "SMOKE", "status": "true"} or
    {"sensor": "TEMPERATURE", "value": 27}."""
    sensor = data.sensor.upper()

    if sensor not in sensor_state:
        return {"message": "Unknown sensor", "sensor": sensor}

    if data.status is not None:
        sensor_state[sensor]["status"] = str(data.status).lower()

    if data.value is not None:
        sensor_state[sensor]["value"] = data.value

    print("--------------------------------")
    print("SENSOR ESP32 UPDATE")
    print("Sensor:", sensor)
    print("Data  :", sensor_state[sensor])
    print("--------------------------------")

    return {"message": "updated", "sensor": sensor, "data": sensor_state[sensor]}


@app.post("/update_sensors")
def update_sensors(data: SensorBulkUpdate):
    """Bulk sibling of /update_sensor, used by node_bridge.py.

    Node 1 carries seven channels and reports every 2 s. Pushed one at a
    time that is seven HTTP round trips per tick, and -- worse -- seven
    moments where /sensors can be read half-updated, so a caller can see
    the new flame reading against the old smoke one. One POST makes the
    whole tick atomic from a reader's point of view.

    Unknown sensor names are reported back rather than silently dropped,
    because a typo in a mapping table is otherwise invisible: readings
    just quietly never arrive and the sensor looks dead.
    """
    updated, unknown = [], []

    for raw_name, payload in (data.readings or {}).items():
        sensor = raw_name.upper()

        if sensor not in sensor_state:
            unknown.append(sensor)
            continue

        if not isinstance(payload, dict):
            unknown.append(sensor)
            continue

        if payload.get("status") is not None:
            sensor_state[sensor]["status"] = str(payload["status"]).lower()

        # Extra numeric detail some sensors carry (SMOKE's raw + ratio).
        # Written even when None, for the same reason `value` is: that is
        # how the bridge says "this is no longer trustworthy".
        for extra in ("raw", "ratio", "count"):
            if extra in payload:
                sensor_state[sensor][extra] = payload[extra]

        # `value` is written even when None: that is how the bridge says
        # "this reading is no longer trustworthy" when its node drops.
        # Suppressing the None here would leave the last good number
        # sitting there looking current -- see link_state's note.
        if "value" in payload:
            sensor_state[sensor]["value"] = payload["value"]

        updated.append(sensor)

    if data.tof is not None:
        # Replaced wholesale rather than merged. A partial ToF update would
        # leave three fresh distances beside one stale one, which is the
        # worst of both -- it looks live and is not. The bridge always
        # sends all four or none.
        for key in ("sides", "mm", "zone", "hit", "nearest_side",
                    "nearest_mm", "link"):
            if key in data.tof:
                tof_state[key] = data.tof[key]
        tof_state["updated_at"] = time.time()

    if data.links is not None:
        link_state["node1"] = bool(data.links.get("node1", link_state["node1"]))
        link_state["node2"] = bool(data.links.get("node2", link_state["node2"]))

    link_state["updated_at"] = time.time()

    if unknown:
        print(f"BULK UPDATE: unknown sensors ignored: {unknown}")

    return {"message": "updated", "updated": updated, "unknown": unknown,
            "links": link_state}


@app.post("/telemetry")
def post_telemetry(data: dict):
    """A sensor board's raw telemetry line, posted straight here.

    This is the endpoint that lets a board reach api.py on its own, with
    NOTHING else running -- no node_bridge, no whisper_service, no Whisper
    model loading for forty seconds first. `python api.py` and the boards
    is a complete sensor rig.

    The body is exactly what the board puts on its serial port, unchanged:
    the object described by 03_PROTOCOL/PROTOCOL.md. It is translated here
    by the same node_bridge.translate() the serial path uses, so a reading
    that arrives by HTTP and the same reading over a cable produce
    identical state. Two copies of that mapping is how they stop matching.

    Accepts three shapes, told apart by "t":
        telemetry   the face board's full picture
        tof         the ToF board alone -- updates ONLY its three channels
        event       fall / wave / knock, sent the moment it happens
    """
    kind = str(data.get("t", "telemetry")).lower()

    if kind == "har":
        # Training data, not a reading. It never touches sensor_state --
        # a feature vector is a summary of the last second, and storing it
        # as a "reading" would put statistics where channels belong.
        return {"status": "ok", "har": record_har(data)}

    if kind == "tof":
        return _store_tof_only(data)

    if kind == "event":
        # An event carries no full picture. Merge it onto what is already
        # stored so the rest of the board's readings are not blanked by a
        # message that never claimed to describe them.
        record_event(data)
        merged = dict(data)
        if data.get("event") == "fall":
            merged.setdefault("fall", True)
            merged.setdefault("fall_state", "fallen")
        return _store_telemetry(merged, partial=True)

    return _store_telemetry(data)


def _store_telemetry(data, partial=False):
    readings = node_bridge.translate(data)

    if partial:
        # Keep whatever the board did not mention. `translate` fills every
        # channel, so an event would otherwise wipe smoke and temperature.
        for name, payload in list(readings.items()):
            if payload.get("status") == node_bridge.UNAVAILABLE:
                readings.pop(name)
            elif payload.get("value", "keep") is None:
                readings.pop(name)

    updated = _apply(readings)

    if node_bridge._tof_valid(data):
        _apply_tof(node_bridge.tof_payload(data))
        link_state["node2"] = True
    link_state["node1"] = True
    link_state["updated_at"] = time.time()

    return {"message": "stored", "updated": updated, "links": live_links()}


def _store_tof_only(data):
    """The ToF board reporting directly, not relayed by the face board.

    Touches PRESENCE, IR and DISTANCE and nothing else. This board has no
    smoke sensor, no thermometer and no IMU -- a message that overwrote
    those would blank four working channels on the word of a device that
    cannot see them.
    """
    readings = {"PRESENCE": {"status": node_bridge._presence(data)}}
    readings["IR"] = {"status": node_bridge._front(data)}

    side, near = (node_bridge._nearest(
        node_bridge._tof_list(data, "tof_mm", None))
        if node_bridge._tof_valid(data) else (None, None))
    readings["DISTANCE"] = {"value": near,
                            "status": side or node_bridge.UNAVAILABLE}

    updated = _apply(readings)
    _apply_tof(node_bridge.tof_payload(data))

    link_state["node2"] = True
    link_state["updated_at"] = time.time()
    return {"message": "stored", "updated": updated, "links": live_links()}


def _apply(readings):
    updated = []
    for raw_name, payload in readings.items():
        sensor = raw_name.upper()
        if sensor not in sensor_state or not isinstance(payload, dict):
            continue
        if payload.get("status") is not None:
            sensor_state[sensor]["status"] = str(payload["status"]).lower()
        if "value" in payload:
            sensor_state[sensor]["value"] = payload["value"]
        for extra in ("raw", "ratio", "count"):
            if extra in payload:
                sensor_state[sensor][extra] = payload[extra]
        updated.append(sensor)
    return updated


def _apply_tof(payload):
    for key in ("sides", "mm", "zone", "hit", "nearest_side", "nearest_mm",
                "link"):
        if key in payload:
            tof_state[key] = payload[key]
    tof_state["updated_at"] = time.time()


@app.get("/tof")
def get_tof():
    """The four ToF sensors, per direction.

    `link` false means Node 2 is not reporting -- treat every distance
    here as unknown, NOT as "nothing is there". Those are opposite
    answers and only one of them is safe to act on.

    Order is always front, left, right, rear.
    """
    return tof_state


def live_links():
    """link_state, with the clock applied.

    A stored `node1: true` only means "true as of the last push". If
    nothing has pushed for LINK_STALE_SECONDS the bridge is gone -- the
    process died, was never started, or the boards are unplugged -- and
    the honest answer is false, not the last thing anyone said.

    Reported rather than mutated, so a bridge that comes back does not
    have to re-establish anything.
    """
    age = time.time() - link_state["updated_at"]
    fresh = link_state["updated_at"] > 0 and age < LINK_STALE_SECONDS
    return {
        "node1": bool(link_state["node1"]) and fresh,
        "node2": bool(link_state["node2"]) and fresh,
        "updated_at": link_state["updated_at"],
        "age_seconds": round(age, 1) if link_state["updated_at"] else None,
        "stale": not fresh,
    }


@app.get("/links")
def get_links():
    """Node reachability, separate from the readings themselves.

    Read this before trusting anything in /sensors: node1 false means
    every Node 1 channel is stale, node2 false means PRESENCE, IR and
    DISTANCE are. `stale: true` means nothing has pushed recently at all,
    which is a different fault -- the bridge is down, not the boards."""
    return live_links()


# ======================================================================
# Edge-AI training data
# ======================================================================
# The board streams one har_features vector every 0.5 s while HAR_STREAM
# is on. Rows are written ONLY while a label is set, so the recorder can
# say "perform 'shaken' now", collect for twenty seconds, and stop --
# without the board needing to know anything about the session.
#
# The feature VERSION rides along in every row. A vector's meaning is
# positional; if har_features ever changes shape, rows recorded before and
# after are not the same data, and a mixed CSV would train a model on two
# different languages with no error anywhere. The trainer refuses a file
# with more than one version in it.
HAR_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "har_data.csv")

_har_label = [None]
_har_session = ["unknown"]
_har_rows = [0]
_har_warned = [False]           # header mismatch: say it once, not per row
_har_lock = threading.Lock()


def record_har(msg):
    """Append one feature vector, if a label is currently set."""
    label = _har_label[0]
    if label is None:
        return None

    feats = msg.get("f")
    if not isinstance(feats, (list, tuple)) or not feats:
        return None

    version = msg.get("v", 0)
    line = "%s,%s,%s,%s\n" % (label, _har_session[0], version,
                              ",".join("%.6g" % float(x) for x in feats))
    header = ("label,session,version,%s"
              % ",".join("f%d" % i for i in range(len(feats))))

    with _har_lock:
        new_file = not os.path.exists(HAR_CSV)

        # Appending a row whose column count differs from the file's header
        # is silent, total corruption: csv.DictReader maps by POSITION, so
        # every field after the mismatch shifts and `version` fills up with
        # accelerometer readings. It happened once -- the session column was
        # added here while a copy of api.py from before the change was still
        # running, and 684 rows landed one column short under a header that
        # had already been migrated.
        #
        # Nothing errored. It surfaced only because the trainer refuses a
        # mixed-version file, which it was doing for an unrelated reason.
        if not new_file:
            try:
                with open(HAR_CSV, "r", encoding="utf-8") as fh:
                    existing = fh.readline().strip()
            except OSError:
                existing = header
            if existing and existing != header:
                if not _har_warned[0]:
                    _har_warned[0] = True
                    print("HAR       !! REFUSING TO WRITE - %s has a header "
                          "this build does not match." % HAR_CSV)
                    print("HAR          file: %s" % existing[:70])
                    print("HAR          this: %s" % header[:70])
                    print("HAR          Restart api.py, or move the old CSV "
                          "aside. Appending would corrupt every row.")
                return None

        with open(HAR_CSV, "a", encoding="utf-8") as fh:
            if new_file:
                fh.write(header + "\n")
            fh.write(line)
        _har_rows[0] += 1
    return label


@app.post("/har_label")
def set_har_label(body: HarLabel):
    """Start or stop recording, and say what is being performed."""
    with _har_lock:
        _har_label[0] = body.label
        if body.session:
            _har_session[0] = body.session
        rows = _har_rows[0]
    print("HAR       %s" % ("recording %r (session %s)"
                            % (body.label, _har_session[0]) if body.label
                            else "stopped (%d rows this session)" % rows))
    return {"label": _har_label[0], "session": _har_session[0],
            "rows": rows, "csv": HAR_CSV}


@app.get("/har_status")
def har_status():
    with _har_lock:
        return {"label": _har_label[0], "session": _har_session[0],
                "rows": _har_rows[0],
                "csv": HAR_CSV, "exists": os.path.exists(HAR_CSV)}


# ======================================================================
# Handling events -- the board reacting to being touched
# ======================================================================
# The face board's IMU can tell it was shoved or picked up (handling_fsm.py).
# Those are not readings and they are not emergencies: they are moments, and
# a moment that arrives 2 s after a poll happens to run is no use. So they
# go in an append-only log with a cursor, and whoever wants to react drains
# it. Nothing here decides anything.
#
# WHY ONLY THESE FIVE. `fall` already has a path -- sensor_watch turns it
# into a phone call, and a spoken reaction on top would mean GENIE remarks on
# a fall it is simultaneously calling an ambulance for. `knock` and `wave`
# belong to the tap command channel and the presence path respectively. A log
# that quietly accepted everything would grow consumers that fight.
HANDLING_EVENTS = ("pushed", "shaken", "lifted", "still_lifted", "set_down")

# Enough to survive a consumer being slow or restarting, small enough that a
# board stuck emitting cannot eat memory. Oldest entries are dropped, and the
# cursor tells a late reader it missed some.
EVENT_LOG_MAX = 64

_event_log = []
_event_seq = [0]
_event_lock = threading.Lock()


def record_event(msg):
    """Append one board event to the log. Ignores anything not ours."""
    name = str(msg.get("event") or "").lower()
    if name not in HANDLING_EVENTS:
        return None

    with _event_lock:
        _event_seq[0] += 1
        entry = {"seq": _event_seq[0], "event": name, "at": time.time()}
        for extra in ("tilt", "peak_g"):
            if msg.get(extra) is not None:
                entry[extra] = msg[extra]
        _event_log.append(entry)
        if len(_event_log) > EVENT_LOG_MAX:
            del _event_log[:-EVENT_LOG_MAX]
        return entry


@app.get("/events")
def get_events(since: int = 0):
    """Everything newer than `since`. Poll with the last seq you saw.

    A first-time reader should take `latest` and start from there rather
    than from 0, or it will speak a backlog of things that happened while
    it was not running -- "why are you pushing me" about a shove from ten
    minutes ago is worse than saying nothing.
    """
    with _event_lock:
        return {"latest": _event_seq[0],
                "oldest": _event_log[0]["seq"] if _event_log else 0,
                "events": [e for e in _event_log if e["seq"] > since]}


def blank_like(state):
    """The same shape with no readings in it: status -> unknown, all
    other fields -> None.

    Derived from the live dict rather than a hardcoded list so a channel
    added to sensor_state cannot be forgotten here -- forgetting one
    means that channel alone keeps answering after the board is gone.
    """
    return {name: {k: (node_bridge.UNAVAILABLE if k == "status" else None)
                   for k in fields}
            for name, fields in state.items()}


def readable_state():
    """sensor_state, or nothing at all if the boards have gone quiet.

    WHY THIS EXISTS. The serial path already did this: node_bridge._drop()
    pushes blanked() the moment the node stops talking, so every channel
    it owns reads `unknown`. The WiFi path had no equivalent -- a board
    that crashed, dropped off the AP or was simply unplugged left its last
    readings sitting in sensor_state forever, and /sensor served them with
    no indication of age. Asked the temperature, GENIE would answer "26
    degrees" in a confident voice about a board that had been dead for
    minutes.

    That is the failure this project keeps writing rules against: a frozen
    reading that looks live is worse than a visibly dead sensor, because it
    reads as a confident all-clear over a blind spot. LINK_STALE_SECONDS
    already ages out `_links`; this ages out the readings themselves.

    It also fixes the cold-start case. Before any board has ever connected,
    the defaults in sensor_state say SMOKE "false" and FALL "idle" -- which
    are indistinguishable from a real all-clear. Now they read `unknown`
    until something actually reports.
    """
    if live_links()["stale"]:
        return blank_like(sensor_state)
    return dict(sensor_state)


@app.post("/sensor")
def get_sensor(data: SensorRequest):
    """ai.py/whisper_service.py call this to read the latest stored
    value for one sensor. Does NOT contact the sensor ESP32 directly --
    just returns whatever /update_sensor most recently stored, and only
    while it is still fresh (see readable_state)."""
    sensor = data.sensor.upper()

    if sensor not in sensor_state:
        return {"error": "sensor not found", "sensor": sensor}

    result = {"sensor": sensor}
    result.update(readable_state()[sensor])
    return result


@app.get("/sensors")
def get_all_sensors():
    """Every sensor's current stored state at once. Polled by
    sensor_watch.py, and readable in a browser at
    http://127.0.0.1:8000/sensors for debugging.

    `_links` rides along under a leading underscore so the callers that
    walk this dict looking for sensors (sensor_watch's alert scan) skip
    it naturally -- they only ever look up the names they already know.
    """
    payload = readable_state()
    payload["_links"] = live_links()
    payload["_tof"] = tof_state
    return payload


# ============================================================
# BOARDS OVER WIFI
# ============================================================
# A board could POST /telemetry above and be done. It does not, and the
# reason is the face board's frame budget.
#
# Node 1 is ONE cooperative loop with 33.3 ms per frame, and two eyes cost
# 15.6 ms of it. MicroPython's urequests.post() is blocking end to end --
# connect, send, wait for the response headers -- and does not keep the
# connection alive, so every post pays the connect again. On a healthy LAN
# that is 40-150 ms; on a busy one, seconds. Every millisecond of it is a
# frozen face.
#
# So the boards hold ONE socket open and write a line per tick, which costs
# microseconds. Same newline-JSON as the serial port, same translation at
# this end. POST /telemetry stays for curl, for scripts, and for anything
# that is not inside a render loop.
#
# This listener lives HERE rather than in node_bridge so that `python
# api.py` plus the boards is a complete sensor rig -- no whisper_service,
# no forty-second Whisper load, just sensors and the endpoints that read
# them back.

# Console chatter from the boards. Every line is printed, at a rate that
# stays readable.
#
# WHY: this used to store lines silently, so a correctly working board and
# a board sending nothing at all produced exactly the same console -- empty.
# There was no way to tell "connected and streaming" from "connected and
# mute", which is the first question you ask when a value does not appear.
_seen = {"telemetry": 0, "tof": 0, "event": 0}
_last_print = [0.0]
_last_summary = [""]
PRINT_EVERY_S = 5.0


def _summarise(kind, msg):
    """One short line. Full JSON would scroll a 2 s heartbeat off screen."""
    if kind == "tof":
        mm = msg.get("tof_mm") or []
        shown = " ".join("----" if v is None else "%4d" % v for v in mm[:4])
        return "tof       %s mm   present=%s" % (shown, msg.get("present"))
    if kind == "event":
        return "EVENT     %s" % msg.get("event")
    # Every channel the face board owns, because a line that shows five of
    # nine reads as "the other four are not arriving". They were arriving
    # the whole time -- this print just never mentioned them.
    #
    # accel and knock_count earn their place over prettier fields: 1.00 g
    # is the one number that proves the IMU is answering (MOTION false
    # looks identical whether the chip is still or absent), and the knock
    # COUNT cannot be missed by a slow poll the way the 2.5 s flag can.
    return ("telemetry smoke=%-8s ratio=%-5s flame=%-6s temp=%-4s hum=%-4s "
            "fall=%-8s accel=%-5s tilt=%-4s knocks=%-4s present=%s"
            % (msg.get("gas_level"), msg.get("gas_ratio"), msg.get("flame"),
               msg.get("temp_c"), msg.get("humidity"), msg.get("fall_state"),
               msg.get("accel_g"), msg.get("tilt"), msg.get("knock_count"),
               msg.get("present")))


def _handle_line(line):
    """One JSON line from a board. Never raises."""
    try:
        msg = json.loads(line)
    except ValueError:
        return                          # partial line during boot, ignore
    if not isinstance(msg, dict):
        return
    try:
        kind = str(msg.get("t", "telemetry")).lower()
        if kind == "har":
            record_har(msg)
            return                      # never printed: 2 lines/second
        if kind == "tof":
            _store_tof_only(msg)
        elif kind == "event":
            record_event(msg)
            merged = dict(msg)
            if msg.get("event") == "fall":
                merged.setdefault("fall", True)
                merged.setdefault("fall_state", "fallen")
            _store_telemetry(merged, partial=True)
        elif kind == "telemetry":
            _store_telemetry(msg)
        else:
            print("BOARD     %s" % msg)      # boot / calibrated lines
            return

        _seen[kind] = _seen.get(kind, 0) + 1

        # Print on three conditions, and the middle one is the important
        # one. A pure timer suppressed the only line worth seeing: a hand
        # appearing in front of a sensor landed inside the rate-limit
        # window and never showed, so the console said "nothing is
        # happening" at the exact moment something was.
        #
        #   event    never suppressed -- a fall must not lose to a heartbeat
        #   changed  the readings actually moved
        #   timer    proof of life when nothing is moving
        now = time.time()
        summary = _summarise(kind, msg)
        changed = summary != _last_summary[0]

        if kind == "event" or changed or now - _last_print[0] >= PRINT_EVERY_S:
            _last_print[0] = now
            _last_summary[0] = summary
            print("  %s   [%s]" % (summary,
                                   ", ".join("%s=%d" % (k, v)
                                             for k, v in sorted(_seen.items())
                                             if v)))
    except Exception as e:
        print("could not store a board line: %r" % e)


# Boards currently connected on the TCP listener, so the laptop can talk
# BACK down the same socket.
#
# WHY THIS EXISTS. Commands to the board -- the face expression, and
# {"cmd":"recalibrate"} -- went out over the USB cable and nowhere else.
# When NODE1_SERIAL_PORT went to "off", node_bridge stopped running and
# set_emotion() started returning False on every call. Nothing errored;
# the face simply stopped taking direction, and it took a user noticing
# the eyes never changed to surface it.
#
# The board already holds a socket open to this process. Writing back down
# it costs nothing and removes the cable from the command path entirely.
_board_conns = []
_board_conns_lock = threading.Lock()


def send_to_board(obj):
    """One JSON line to every connected board. Returns how many took it."""
    line = (json.dumps(obj) + "\n").encode("utf-8")
    with _board_conns_lock:
        conns = list(_board_conns)

    ok, dead = 0, []
    for c in conns:
        try:
            c.sendall(line)
            ok += 1
        except OSError:
            dead.append(c)

    if dead:
        with _board_conns_lock:
            for c in dead:
                if c in _board_conns:
                    _board_conns.remove(c)
    return ok


@app.post("/to_board")
def post_to_board(body: dict):
    """Forward one arbitrary JSON object down the board's socket.

    Deliberately unvalidated: node1_report._handle() already ignores
    anything it does not recognise, and putting a second copy of the
    command vocabulary here is how the two drift apart. /emotion is
    validated because the face has a fixed set of expressions; commands
    do not.
    """
    return {"boards": send_to_board(body)}


@app.post("/emotion")
def post_emotion(body: EmotionRequest):
    """Set the face. Best effort -- a board that is not connected is not an
    error, it is a board that is not connected."""
    name = (body.emotion or "").strip().lower()
    if name not in FACE_EMOTIONS:
        return {"error": "unknown emotion", "emotion": name,
                "known": list(FACE_EMOTIONS)}
    sent = send_to_board({"emotion": name})
    return {"emotion": name, "boards": sent}


def _board_client(conn, addr):
    buf = b""
    with _board_conns_lock:
        _board_conns.append(conn)
    try:
        # A board that has crashed leaves a socket that accepts writes and
        # never sends. Without this the thread parks on recv() forever and
        # the board can never reconnect.
        conn.settimeout(60)
        while True:
            chunk = conn.recv(1024)
            if not chunk:
                break
            buf += chunk
            if len(buf) > 16384:        # no newline in 16 KB: not our board
                buf = b""
                continue
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                _handle_line(line.decode("utf-8", "ignore").strip())
    except (OSError, socket.timeout):
        pass
    finally:
        with _board_conns_lock:
            if conn in _board_conns:
                _board_conns.remove(conn)
        try:
            conn.close()
        except OSError:
            pass
        print("Sensor board disconnected:", addr)


def board_listener(port=None):
    port = port or NODE1_TCP_PORT
    try:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((HOST, port))
        server.listen(4)
    except OSError as e:
        print("Cannot listen for boards on %d (%r)." % (port, e))
        print("Something else is using it - another api.py, most likely.")
        return

    print("Sensor boards: listening on TCP %d" % port)
    while True:
        try:
            conn, addr = server.accept()
        except OSError:
            continue
        print("Sensor board connected:", addr)
        threading.Thread(target=_board_client, args=(conn, addr),
                         daemon=True).start()


def discovery_broadcaster():
    """Tell the boards where this laptop is.

    Same message and port AudioServer.py uses, so a board only has to learn
    one protocol and the mic ESP is unaffected. Broadcast from here as well
    because during sensor bring-up AudioServer usually is not running, and
    a hardcoded IP means reflashing two boards every time DHCP moves.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    except OSError as e:
        print("Discovery broadcast unavailable (%r) - set wifi_cfg.LAPTOP_IP "
              "on the boards instead." % e)
        return
    while True:
        try:
            sock.sendto(b"GENIE_SERVER_HELLO",
                        ("255.255.255.255", DISCOVERY_PORT))
        except OSError:
            pass
        time.sleep(2)


if __name__ == "__main__":
    threading.Thread(target=board_listener, daemon=True).start()
    threading.Thread(target=discovery_broadcaster, daemon=True).start()
    print()
    print("GENIE api.py")
    print("  sensors    http://127.0.0.1:%d/sensors" % IOT_API_PORT)
    print("  distances  http://127.0.0.1:%d/tof" % IOT_API_PORT)
    print("  board health  /links      raw board push  POST /telemetry")
    print()
    uvicorn.run(app, host="0.0.0.0", port=IOT_API_PORT)