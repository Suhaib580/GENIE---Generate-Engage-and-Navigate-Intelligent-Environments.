# ==========================================================================
# VENDORED FILE -- DO NOT EDIT HERE.
#
# Upstream: GENIE_SENSORS/03_PROTOCOL/genie_serial.py (Arman's, sensor side).
# Copied byte for byte on 26-08-2026; re-checked against the GENIE_SENSORS
# pack on 27-08-2026 and byte-identical, so the contract did not move. It is the reference implementation of
# the Node 1 <-> laptop contract, and the whole point of a contract is that
# both sides parse with the SAME code -- edit it here and the two drift
# apart silently, which is exactly the failure that killed the old ESP-NOW
# link (10 bytes sent, 12 expected, no error on either side).
#
# GENIE-side behaviour belongs in node_bridge.py, which uses this file's
# hooks and adds nothing to it. If a field is genuinely missing, change the
# PROTOCOL document and the upstream file, then re-copy.
# ==========================================================================
"""
genie_serial.py — laptop side of the GENIE link. Runnable, no framework.

    pip install pyserial

    from genie_serial import GenieNode
    node = GenieNode("COM5")          # Linux/Mac: "/dev/ttyUSB0"
    node.on_alarm = my_alarm_handler  # Path A - rule based, no LLM
    node.on_wake  = my_wake_handler
    node.start()

    ...
    node.set_emotion("thinking")
    prompt = node.sensor_context() + "User: " + transcript

Implements exactly what Docs/PROTOCOL.md and PROTOCOL_ADDENDUM.md specify.
Nothing here needs the Flask app; wire it into whatever you already have.

--------------------------------------------------------------------------
THE ONE THING THAT MATTERS
--------------------------------------------------------------------------
There are two paths and they must not touch.

  PATH A  gas / flame / fall / presence -> canned TTS + SMS, under 200 ms.
          NEVER goes through Whisper or Mistral.
  PATH B  conversation -> Whisper -> Mistral -> TTS, 8-15 s end to end.

The pipeline being 8-15 s is fine for "what is the temperature" and
unacceptable for a fire. An examiner will ask about alarm latency; a
documented bypass is the answer. `on_alarm` below is that bypass.
"""
import json
import re
import threading
import time

try:
    import serial                      # pyserial
except ImportError:                    # allow import for the offline self-test
    serial = None

EMOTIONS = ("neutral", "happy", "sad", "thinking", "concerned", "alarm")

# Matches the tag ANYWHERE, not just at the start of a line, and tolerates
# the spacing variants a language model actually produces: [EMOTION:happy],
# [emotion: happy], [ EMOTION : Happy ]. See strip_emotion_tag for why the
# line-anchored version had to go.
_EMOTION_RE = re.compile(r"\[\s*EMOTION\s*:\s*([A-Za-z_]+)\s*\]",
                         re.IGNORECASE)

# Node 1 forgets a laptop-set emotion after this long and falls back to
# sensor-driven. Deliberate: if this process dies, GENIE must not sit there
# with a happy face during a gas leak. Re-send on every LLM turn to hold it.
EMOTION_TTL_S = 20

# No telemetry for this long means Node 1 is gone, not quiet.
NODE_TIMEOUT_S = 8


class GenieNode:
    def __init__(self, port, baud=115200):
        self.port = port
        self.baud = baud
        self.state = {}                # last telemetry, merged
        self.last_rx = 0.0
        self.boot = {}
        self.calibration = {}
        self._ser = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

        # --- hooks. Assign your own. -------------------------------------
        self.on_alarm = None           # fn(kind, msg)  kind: gas|flame|fall
        self.on_wake = None            # fn(msg)        hand wave, no wake word
        self.on_telemetry = None       # fn(state)      every 2 s
        self.on_presence = None        # fn(zones)      ToF, someone approached

        self._was_alarm = False
        self._was_present = False

    # ------------------------------------------------------------- lifecycle
    def start(self):
        if serial is None:
            raise RuntimeError("pyserial not installed:  pip install pyserial")
        self._ser = serial.Serial(self.port, self.baud, timeout=1)
        time.sleep(0.2)
        self._ser.reset_input_buffer()
        threading.Thread(target=self._reader, daemon=True).start()
        return self

    def stop(self):
        self._stop.set()
        if self._ser:
            self._ser.close()

    @property
    def online(self):
        return (time.time() - self.last_rx) < NODE_TIMEOUT_S

    # ------------------------------------------------------------- reading
    def _reader(self):
        while not self._stop.is_set():
            try:
                raw = self._ser.readline()
            except Exception as e:
                print("[genie] serial error:", e)
                time.sleep(1)
                continue
            if not raw:
                continue
            try:
                msg = json.loads(raw.decode(errors="ignore").strip())
            except ValueError:
                continue               # partial line during boot, ignore
            if not isinstance(msg, dict):
                continue
            self._dispatch(msg)

    def _dispatch(self, msg):
        kind = msg.get("t")
        self.last_rx = time.time()

        if kind == "telemetry":
            with self._lock:
                self.state.update(msg)
            self._check_alarms(msg)
            self._check_presence(msg)
            if self.on_telemetry:
                self.on_telemetry(dict(self.state))

        elif kind == "event":
            ev = msg.get("event")
            if ev == "fall":
                self._fire("fall", msg)
            elif ev == "wave":
                if self.on_wake:
                    self.on_wake(msg)

        elif kind == "boot":
            self.boot = msg
            print("[genie] node booted:", msg)

        elif kind == "calibrated":
            self.calibration = msg
            if not msg.get("baseline_ok", True):
                # MQ-2 baseline landed above the ADC's linear region. The
                # sensor is not burned in and the gas numbers are not
                # trustworthy. Surface it, do not silently report values.
                print("[genie] WARNING gas baseline not OK - MQ-2 needs "
                      "24-48 h powered before it settles")
            print("[genie] calibrated:", msg)

    def _check_alarms(self, m):
        if m.get("gas_alarm"):
            self._fire("gas", m)
        elif m.get("flame"):
            self._fire("flame", m)

    def _fire(self, kind, msg):
        """PATH A. Must complete in well under a second. No LLM."""
        if self.on_alarm:
            try:
                self.on_alarm(kind, msg)
            except Exception as e:
                print("[genie] alarm handler raised:", e)
        else:
            print("[genie] ALARM (%s) and no handler attached: %s" % (kind, msg))

    def _check_presence(self, m):
        # Requires PROTOCOL_ADDENDUM fields. Absent until node1_link is merged
        # into the face firmware — this stays quiet rather than throwing.
        if "present" not in m:
            return
        now = bool(m.get("present"))
        if now and not self._was_present and self.on_presence:
            self.on_presence(m.get("tof_zone", []))
        self._was_present = now

    # ------------------------------------------------------------- writing
    def _send(self, obj):
        if not self._ser:
            return
        try:
            self._ser.write((json.dumps(obj) + "\n").encode())
        except Exception as e:
            print("[genie] write failed:", e)

    def set_emotion(self, name):
        """Expires after 20 s on the node. Re-send on every LLM turn."""
        if name not in EMOTIONS:
            raise ValueError("emotion must be one of %s" % (EMOTIONS,))
        self._send({"emotion": name})

    def recalibrate(self):
        """Reboots the node and redoes the 30 s warm-up plus clean-air
        baseline. Use after moving the unit to a different room."""
        self._send({"cmd": "recalibrate"})

    # --------------------------------------------------------- for the LLM
    def sensor_context(self):
        """Injected into Mistral's context so it answers with real numbers
        instead of inventing them. Keep it SHORT — every token here eats
        context and Mistral 7B on CPU is already the latency bottleneck."""
        with self._lock:
            s = dict(self.state)
        if not s:
            return ""

        lines = ["[SENSOR STATE]"]
        t, h = s.get("temp_c"), s.get("humidity")
        if t is not None and t != -99:          # -99 means the DHT read failed
            lines.append("temperature_c: %s" % t)
        if h is not None and h != -99:
            lines.append("humidity_pct: %s" % h)
        lines.append("gas_level: %s" % ("ALARM" if s.get("gas_alarm")
                                        else "elevated" if s.get("gas_alert")
                                        else "normal"))
        lines.append("flame: %s" % ("DETECTED" if s.get("flame") else "none"))
        if s.get("present") is not None:
            lines.append("someone_nearby: %s" % ("yes" if s["present"] else "no"))
        lines.append("[/SENSOR STATE]")
        return "\n".join(lines) + "\n\n"

    @staticmethod
    def strip_emotion_tag(reply):
        """Pull [EMOTION:...] out of a model reply, wherever it landed.

            [EMOTION:neutral|happy|sad|thinking|concerned|alarm]

        Returns (clean_text, emotion_or_None).

        This is Option 1 from PROTOCOL.md and it is the one to ship: zero
        extra latency, zero extra model, zero extra RAM. Mistral already
        knows the sentiment of what it just wrote.

        WHY THIS IS A REGEX AND NOT A LINE TEST. The prompt asks for the
        tag on its OWN line, and the original implementation took that at
        its word: it split on newlines and kept any line not starting with
        "[EMOTION:". Mistral does not reliably comply. Observed live:

            I'm unable to provide sensor readings at the moment. You can
            ask for a specific sensor check. [EMOTION:neutral]

        One line, tag at the end, so the startswith() test failed, the tag
        survived, and Piper read "bracket emotion neutral" out loud. The
        expression never reached the face either, because the emotion came
        back None.

        A prompt is a request, not a guarantee. Anything parsing model
        output has to tolerate the model ignoring the format -- so this
        matches the tag anywhere in the text, tolerates whitespace inside
        the brackets, and is case-insensitive.

        The LAST valid tag wins: the model is asked to END with it, so if
        it emits several the final one is its considered answer.
        """
        emotion = None
        for m in _EMOTION_RE.finditer(reply):
            cand = m.group(1).strip().lower()
            if cand in EMOTIONS:
                emotion = cand

        clean = _EMOTION_RE.sub("", reply)
        # Removing an inline tag leaves a trailing space before the
        # newline, and removing an own-line tag leaves a blank line.
        clean = "\n".join(line.rstrip() for line in clean.splitlines())
        while "\n\n\n" in clean:
            clean = clean.replace("\n\n\n", "\n\n")
        return clean.strip(), emotion


# ---------------------------------------------------------------- self-test
if __name__ == "__main__":
    # Offline: proves the parser and the tag stripper without a board attached.
    n = GenieNode.__new__(GenieNode)
    n.state, n._lock, n.last_rx = {}, threading.Lock(), 0.0
    n.on_alarm = lambda k, m: print("  PATH A ->", k)
    n.on_wake = n.on_telemetry = n.on_presence = None
    n._was_alarm = n._was_present = False
    n.boot = n.calibration = {}

    for line in (
        '{"t":"boot","mpu":true}',
        '{"t":"calibrated","gas_base":412,"baseline_ok":false}',
        '{"t":"telemetry","temp_c":31.0,"humidity":54.0,"gas_alarm":false,'
        '"flame":false,"present":true,"tof_zone":["PRESENT","CLEAR","FAR","CLEAR"]}',
        '{"t":"event","event":"fall","peak_g":2.84,"tilt":88}',
        'garbage half line',
        '{"t":"telemetry","gas_alarm":true}',
    ):
        try:
            n._dispatch(json.loads(line))
        except ValueError:
            print("  (ignored partial line, as intended)")

    print("\ncontext for Mistral:\n" + n.sensor_context())
    txt, emo = GenieNode.strip_emotion_tag(
        "It is 31 degrees in here.\n[EMOTION:happy]")
    assert txt == "It is 31 degrees in here." and emo == "happy"
    print("tag strip OK ->", repr(txt), emo)
