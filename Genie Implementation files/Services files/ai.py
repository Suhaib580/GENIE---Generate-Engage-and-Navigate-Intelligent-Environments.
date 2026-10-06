"""
Intent classifier wrapping a SINGLE AIML interpreter (the aiml.py package)
that has BOTH relay.aiml and sensor.aiml loaded into it.

One shared kernel, not one kernel per topic -- aiml.py's Kernel.learn()
can be called more than once on the same kernel; each call just adds
more categories to the same pattern graph and predicate space. This
matters here specifically because relay.aiml's device-control patterns
depend on two fixes that only exist in THIS classify() method:
politeness-prefix stripping (see _strip_politeness) and the adjacent-
wildcard raw_tail split-and-refeed workaround below. Loading either
file into a fresh, separate kernel that skips those steps silently
reintroduces both bugs -- confirmed by testing: "please turn on the
fan" and "turn the light off" both fail outright without them.

Recognizes:
  - device-control phrasings from relay.aiml: "turn on the fan",
    "switch the light off", "turn on both", etc.
  - status queries from relay.aiml: "is the fan on?"
  - sensor questions from sensor.aiml: "is there smoke?", "what's the
    temperature?", "check all sensors", etc.
  - emergency phrases from relay.aiml: "help me", "this is an emergency"

classify() returns a dict describing what matched, or None if nothing
did -- meaning the caller should fall through to the normal LLM
pipeline, unchanged.

Pure classification only -- no networking here. whisper_service.py is
the one that talks to api.py (for relay commands/status and live sensor
data), same separation used by llm_service.py and tts_service.py. For
sensor replies specifically, build_sensor_reply()/build_all_sensors_reply()
below take already-fetched data and hand it to sensor.aiml's own
<condition> templates to phrase the response -- so the nuanced wording
sensor.aiml was written with (per-severity phrasing, randomized lines)
is what actually gets spoken, instead of that logic being reimplemented
in Python.

Install: pip install aiml.py
"""

import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time

# aiml.py (v0.9.2) still calls the long-removed time.clock() internally
# (Kernel.py, learn()). Removed from Python itself in 3.8. Patching it
# back in as an alias for perf_counter avoids needing to edit the
# installed package (which would just get overwritten on next install).
if not hasattr(time, "clock"):
    time.clock = time.perf_counter

import aiml
from config import AIML_FILE, SENSOR_AIML_FILE

# AIML's default predicate value when nothing has ever been <set> for it
# is the literal string "unknown" (per AIML convention) -- NOT None or
# empty string. Checking `if command:` alone is not enough, since
# "unknown" is truthy in Python. Guard against it explicitly.
UNSET_PREDICATE_VALUES = {None, "", "unknown"}


# Leading wildcards (e.g. "* SWITCH * THE *") turned out to be a third
# unreliable spot in this library's matcher -- "switch on the fan" and
# "switch the fan on" both match fine on their own, but the identical
# categories with a leading "*" to absorb "please"/"can you" never
# matched at all in testing. Rather than hunt for a fourth AIML-side
# workaround, politeness prefixes are stripped here in plain Python
# before the text ever reaches the AIML kernel, sidestepping the bug
# entirely. Longer phrases listed before their shorter substrings so
# the first match found is the most specific one.
POLITENESS_PREFIXES = [
    "CAN YOU PLEASE ",
    "COULD YOU PLEASE ",
    "WOULD YOU PLEASE ",
    "CAN YOU ",
    "COULD YOU ",
    "WOULD YOU ",
    "PLEASE ",
]


def _strip_politeness(text):
    upper = text.strip().upper()

    # A couple of common contractions worth normalizing so status-query
    # patterns don't need duplicate categories for "WHAT'S" vs "WHAT IS".
    upper = upper.replace("WHAT'S", "WHAT IS").replace("HOW'S", "HOW IS")

    for prefix in POLITENESS_PREFIXES:
        if upper.startswith(prefix):
            return upper[len(prefix):].strip()
    return upper


class IntentClassifier:

    def __init__(self):
        self.kernel = aiml.Kernel()

        # aiml.py's learn() does `for f in glob.glob(filename): ...` --
        # glob.glob() on a path that matches nothing just returns an
        # empty list, so a missing file loads ZERO categories with no
        # exception and no warning. That failure mode is silent enough
        # that it's worth checking for explicitly here rather than
        # discovering it as "every sensor phrase mysteriously returns
        # None" later.
        for path in (AIML_FILE, SENSOR_AIML_FILE):
            if not os.path.isfile(path):
                raise FileNotFoundError(
                    f"AIML file not found: {path}\n"
                    "aiml.py's Kernel.learn() would silently load zero "
                    "categories from this instead of raising -- make sure "
                    "the file sits in the same folder as config.py."
                )

        # aiml.py 0.9.2's Kernel.learn() signature is learn(filename, log) --
        # `log` is required (no default), it just controls whether load
        # progress gets printed. Confirmed by inspecting the installed
        # package source; calling learn(AIML_FILE) alone raises
        # "TypeError: learn() missing 1 required positional argument: 'log'".
        #
        # Both files load into this ONE kernel -- see the module
        # docstring for why a second, separate kernel for sensor.aiml
        # would silently drop the politeness-stripping and adjacent-
        # wildcard fixes that relay.aiml's matching depends on.
        self.kernel.learn(AIML_FILE, False)
        self.kernel.learn(SENSOR_AIML_FILE, False)

    _TRACKED_PREDICATES = (
        "emergency_triggered", "sensor", "query_device",
        "raw_tail", "command", "device",
    )

    def _reset_predicates(self):
        """Clears every predicate classify() reads. Called before EACH
        matching attempt (not just once per call) -- without this, a
        first-pass match attempt that partially sets something spurious
        could leak into the second-pass retry's result."""
        for predicate in self._TRACKED_PREDICATES:
            self.kernel.setPredicate(predicate, "unknown")

    def _nothing_matched(self):
        """True if none of the tracked predicates are set -- meaning
        the most recent respond() call found no matching category at
        all, and it's safe to retry with a different normalization."""
        return all(
            self.kernel.getPredicate(p) in UNSET_PREDICATE_VALUES
            for p in self._TRACKED_PREDICATES
        )

    def classify(self, text):
        """
        Returns a dict with a "type" field:
            {"type": "command", "command": "ON"/"OFF",
             "device": "FAN"/"LIGHT"/"BOTH", "confirmation": "..."}
        for a device-control phrase, or:
            {"type": "query", "device": "FAN"/"LIGHT"/"ALL"}
        for a relay status question ("is the fan on?"), or:
            {"type": "sensor", "sensor": "SMOKE"/"FLAME"/"IR"/"MOTION"/
                                          "FALL"/"TEMPERATURE"/"HUMIDITY"/"ALL"}
        for a sensor question ("is there smoke?", "check all sensors"),
        or None if the text didn't match anything -- meaning the caller
        should fall through to the normal LLM pipeline.

        Note: for "query" results, the actual ON/OFF state comes from
        api.py (this module has no network access), so the caller is
        responsible for looking that up and building the spoken reply.
        """
        if not text or not text.strip():
            return None

        self._reset_predicates()

        # Try the text AS SPOKEN first (just uppercased + contraction
        # normalization, no prefix stripped). This matters because
        # sensor.aiml has ~25 categories that deliberately include
        # "can you"/"do you" as part of the whole literal pattern (e.g.
        # "CAN YOU DETECT SMOKE") -- stripping the prefix before ever
        # attempting a match made all of those permanently unreachable,
        # since the stripped remainder ("DETECT SMOKE") was never
        # authored as its own category.
        upper = text.strip().upper()
        upper = upper.replace("WHAT'S", "WHAT IS").replace("HOW'S", "HOW IS")
        response = self.kernel.respond(upper)

        if self._nothing_matched():
            # Nothing matched as spoken -- fall back to relay.aiml's
            # assumption: strip a politeness prefix and retry. Needed
            # because leading-wildcard categories meant to absorb
            # "please"/"can you" for relay.aiml's OWN patterns (e.g.
            # "* TURN * THE *") were confirmed unreliable in aiml.py.
            stripped = _strip_politeness(text)
            if stripped != upper:
                self._reset_predicates()
                response = self.kernel.respond(stripped)

        # NOTE: no "if not response: return None" gate here on purpose.
        # relay.aiml's templates always produce visible confirmation
        # text, but sensor.aiml's categories deliberately wrap their
        # <set> in <think> (silent -- the real reply comes from a
        # separate CHECK SENSOR call via build_sensor_reply() below),
        # so an empty response does NOT mean nothing matched. The
        # predicate checks below are the real source of truth; if none
        # of them fire, command/device end up unset and the final
        # check at the bottom of this method returns None anyway.

        # --- Emergency path (checked first: highest priority) ---
        emergency = self.kernel.getPredicate("emergency_triggered")

        if emergency not in UNSET_PREDICATE_VALUES:
            self.kernel.setPredicate("emergency_triggered", "unknown")
            return {"type": "emergency"}

        # --- Sensor path (sensor.aiml) ---
        # All sensor.aiml patterns are literal (no wildcards), so unlike
        # relay.aiml this one has no adjacent-wildcard/politeness bug to
        # work around -- a plain predicate check is enough. The caller
        # still needs to fetch live data and call build_sensor_reply()/
        # build_all_sensors_reply() below to get the actual spoken line;
        # this only identifies WHICH sensor was asked about.
        sensor = self.kernel.getPredicate("sensor")

        if sensor not in UNSET_PREDICATE_VALUES:
            self.kernel.setPredicate("sensor", "unknown")
            return {
                "type": "sensor",
                "sensor": sensor.strip().upper(),
            }

        # --- Status query path ---
        query_device = self.kernel.getPredicate("query_device")

        if query_device not in UNSET_PREDICATE_VALUES:
            self.kernel.setPredicate("query_device", "unknown")
            return {
                "type": "query",
                "device": query_device.strip().upper(),
            }

        # --- Command path (with the adjacent-wildcard workaround) ---
        raw_tail = self.kernel.getPredicate("raw_tail")

        if raw_tail not in UNSET_PREDICATE_VALUES:
            # Matched a "TURN THE <device> <command>"-shaped phrase.
            # aiml.py can't reliably split two adjacent bare wildcards,
            # so the whole tail was captured as ONE wildcard instead
            # (see relay.aiml's SPLIT TAIL category) and split here.
            parts = raw_tail.strip().split()

            if len(parts) < 2:
                return None

            command_word = parts[-1]
            device_words = " ".join(parts[:-1])

            # Reset so a stale raw_tail can't leak into an unrelated
            # later call that happens not to set it again.
            self.kernel.setPredicate("raw_tail", "unknown")

            # Hand it back to AIML in the phrasing it CAN parse
            # correctly (non-adjacent wildcards), which sets
            # command/device normally and generates a real randomized
            # confirmation via FOLLOW COMMAND.
            response = self.kernel.respond(f"TURN {command_word} THE {device_words}")

            if not response or not response.strip():
                return None

        command = self.kernel.getPredicate("command")
        device = self.kernel.getPredicate("device")

        if command in UNSET_PREDICATE_VALUES or device in UNSET_PREDICATE_VALUES:
            # Matched *something* in the AIML file but it never actually
            # set command/device -- not a real relay command.
            return None

        return {
            "type": "command",
            "command": command.strip().upper(),
            "device": device.strip().upper(),
            "confirmation": response.strip(),
        }

    # Maps each sensor name to (aiml predicate name, key to read from the
    # data dict api.py returns, default if that key is missing). Kept
    # here rather than in whisper_service.py since it's really about
    # matching sensor.aiml's own <condition name="..."> predicate names.
    _SENSOR_PREDICATE_MAP = {
        "SMOKE": ("smoke_status", "status", "false"),
        "FLAME": ("flame_status", "status", "false"),
        "IR": ("ir_status", "status", "false"),
        "MOTION": ("motion_status", "status", "false"),
        # Somebody in the room, from the ToF array. Distinct from MOTION,
        # which is the unit itself being moved -- see the note in
        # sensor.aiml's PRESENCE block.
        "PRESENCE": ("presence_status", "status", "false"),
        # The MPU's own numbers. No sensor.aiml category of their own --
        # "are you tilted" already routes to MOTION, which reads better
        # than a number -- but they are in the map so build_all_sensors_
        # reply sets them, and so anything asking directly gets a real
        # value rather than silence.
        "TILT": ("tilt_degrees", "value", ""),
        "ACCEL": ("accel_g", "value", ""),
        "FALL": ("fall_status", "status", "idle"),
        # Like DISTANCE, KNOCK carries two fields -- the 2.5 s boolean and
        # the lifetime count. The map handles the boolean so the ALL sweep
        # sets it; _set_knock below adds the count, which is the half that
        # a poll cannot miss. See the note there.
        "KNOCK": ("knock_status", "status", "false"),
        "TEMPERATURE": ("temperature", "value", ""),
        "HUMIDITY": ("humidity", "value", ""),
        # DISTANCE is the odd one: its `value` is the nearest reading in
        # millimetres and its `status` is which of the four sides saw it,
        # so it needs TWO predicates. The map handles the numeric half;
        # _set_distance below adds distance_side and does the mm -> cm
        # conversion. See the note there for why the unit changes here.
        "DISTANCE": ("distance", "value", ""),
    }

    # Which side of GENIE a ToF sensor faces, spoken.
    #
    # Full prepositional phrases, not bare side words, because no single
    # sentence frame fits all four. "to my left" and "to my right" are
    # natural; "to my front" and "to my back" are not, and "on my rear
    # side" sounds like a manual. Putting the preposition in the lookup
    # lets one template read correctly for every direction.
    _SIDE_WORDS = {
        "front": "in front of me",
        "left": "to my left",
        "right": "to my right",
        "rear": "behind me",
    }

    def _set_distance(self, data):
        """Set the two DISTANCE predicates from one /sensor payload.

        MILLIMETRES BECOME CENTIMETRES HERE, and this is the only place
        that conversion happens. Everything upstream -- Node 2, the ESP-NOW
        frame, node1_link, api.py -- is millimetres end to end, deliberately,
        because mixing units in a sensor chain is how a 1.2 m reading
        becomes a 12 cm one. Converting at the point of SPEECH keeps that
        rule intact: "one thousand two hundred millimetres" is not an answer
        anybody wants out loud.

        Returns True if a real reading was set, False if the predicates were
        left unset so sensor.aiml lands on its "I cannot read" branch.
        """
        mm = (data or {}).get("value")
        side = str((data or {}).get("status", "")).lower()

        if mm is None:
            return False

        self.kernel.setPredicate("distance", str(int(round(mm / 10.0))))
        self.kernel.setPredicate(
            "distance_side", self._SIDE_WORDS.get(side, "nearby"))
        return True

    def _set_knock(self, data):
        """Set the two KNOCK predicates from one /sensor payload.

        THE COUNT IS THE REAL ANSWER, not the boolean. KNOCK is only true
        for 2.5 s after a tap (KNOCK_HOLD_MS on the board) and a 2 s poll
        can straddle that window entirely -- so "is anyone knocking" asked
        a moment too late reads false on a door somebody is hammering.
        The lifetime counter cannot be missed that way: 4 -> 7 proves three
        taps landed even if no poll ever caught the flag up.

        Returns True if a real reading was set, False if the predicates
        were left unset so sensor.aiml lands on "I cannot read".
        """
        if not data:
            return False

        status = data.get("status")
        count = data.get("count")

        if status is None and count is None:
            return False

        if status is not None:
            self.kernel.setPredicate("knock_status", str(status).lower())
        # count is None when the channel has never reported -- not zero
        # taps, which is a real and different answer.
        if count is not None:
            self.kernel.setPredicate("knock_count", str(count))
        return True

    def build_reaction_reply(self, event):
        """The line GENIE says when the board reports being handled.

        Unlike every other method here this answers no question -- the IMU
        noticed something and GENIE remarks on it. The wording still comes
        from sensor.aiml so all of its speech stays in one file and each
        reaction gets a <random> set; saying the identical sentence every
        time it is nudged is what makes a device read as a doorbell rather
        than as aware.

        Returns None for an unknown event name, so a board emitting
        something this build does not recognise produces SILENCE rather
        than an empty utterance handed to the speaker.
        """
        name = str(event or "").strip().lower()
        if not name:
            return None

        self.kernel.setPredicate("reaction", name)
        reply = self.kernel.respond("REACTION EVENT")
        self.kernel.setPredicate("reaction", "unknown")

        reply = (reply or "").strip()
        return reply or None

    def build_sensor_reply(self, sensor, data):
        """Given one sensor's live data (already fetched by
        whisper_service.py from api.py's /sensor endpoint, e.g.
        {"status": "true"} or {"value": 27}), sets the matching AIML
        predicate and asks sensor.aiml's CHECK SENSOR category for the
        phrased reply. `data` can be None/empty (fetch failed) -- in
        that case the underlying predicate is left unset, which lands
        on sensor.aiml's own "I cannot read the sensor right now"
        fallback line rather than a Python-side error string."""
        sensor = sensor.upper()

        if sensor == "DISTANCE":
            # Two predicates, and one of them is a unit conversion. Clearing
            # them first matters: a stale "40 cm to my left" from the last
            # question is a worse answer than admitting the sensors are
            # unreadable, and AIML predicates persist across responses.
            self.kernel.setPredicate("distance", "")
            self.kernel.setPredicate("distance_side", "")
            self._set_distance(data)
            self.kernel.setPredicate("sensor", sensor)
            reply = self.kernel.respond("CHECK SENSOR")
            self.kernel.setPredicate("sensor", "unknown")
            return (reply.strip() if reply and reply.strip()
                    else "I couldn't read that sensor.")

        if sensor == "KNOCK":
            # Two predicates, same reason DISTANCE needs the clear: they
            # persist across responses, and a stale count is a confident
            # wrong number rather than a visible gap.
            self.kernel.setPredicate("knock_status", "")
            self.kernel.setPredicate("knock_count", "")
            self._set_knock(data)
            self.kernel.setPredicate("sensor", sensor)
            reply = self.kernel.respond("CHECK SENSOR")
            self.kernel.setPredicate("sensor", "unknown")
            return (reply.strip() if reply and reply.strip()
                    else "I couldn't read that sensor.")

        mapping = self._SENSOR_PREDICATE_MAP.get(sensor)

        if mapping:
            predicate_name, data_key, default = mapping
            if data:
                value = data.get(data_key, default)
                # value can be None even though the key exists -- that's
                # api.py's "this sensor has never reported" placeholder,
                # not a real reading of zero. Leaving the predicate unset
                # here (rather than stringifying None into the literal
                # text "None") lets it fall through to the same "unknown"
                # state as a failed fetch, which sensor.aiml already has
                # a real fallback line for.
                if value is not None:
                    self.kernel.setPredicate(
                        predicate_name,
                        str(value).lower() if data_key == "status" else str(value),
                    )

        self.kernel.setPredicate("sensor", sensor)
        reply = self.kernel.respond("CHECK SENSOR")
        self.kernel.setPredicate("sensor", "unknown")

        return reply.strip() if reply and reply.strip() else "I couldn't read that sensor."

    def log_blind(self, blind):
        """Say which sensors are unreadable. Spoken replies stay short by
        design, so the detail goes to the console where somebody debugging
        can see WHICH sensor went dark rather than just that one did."""
        print("[ai] safety reading incomplete -- unreadable: %s"
              % ", ".join(blind))

    def build_all_sensors_reply(self, all_data):
        """Given a dict of SENSOR_NAME -> data for every sensor (as
        fetched by whisper_service.py), sets every predicate, computes
        the overall safety_status, then asks for the ALL-branch reply.
        Mirrors the severity ordering the original sensor-check
        prototype used (alarm > warning > ok), extended to also treat
        an "alarm"-level smoke/flame reading as unsafe, not just a
        boolean true -- sensor.aiml supports that finer-grained status
        but nothing feeding it live data uses it yet (see chat notes)."""
        all_data = all_data or {}

        for sensor, (predicate_name, data_key, default) in self._SENSOR_PREDICATE_MAP.items():
            if sensor == "DISTANCE":
                # Handled separately -- the generic path would set the
                # `distance` predicate to raw millimetres, and the ALL
                # branch would then read out "1240" as if it were cm.
                self.kernel.setPredicate("distance", "")
                self.kernel.setPredicate("distance_side", "")
                self._set_distance(all_data.get(sensor))
                continue
            data = all_data.get(sensor)
            if data:
                value = data.get(data_key, default)
                if value is not None:
                    self.kernel.setPredicate(
                        predicate_name,
                        str(value).lower() if data_key == "status" else str(value),
                    )

        smoke = all_data.get("SMOKE", {}) or {}
        flame = all_data.get("FLAME", {}) or {}
        motion = all_data.get("MOTION", {}) or {}
        fall = all_data.get("FALL", {}) or {}

        smoke_s = str(smoke.get("status", "false")).lower()
        flame_s = str(flame.get("status", "false")).lower()
        motion_s = str(motion.get("status", "false")).lower()
        fall_s = str(fall.get("status", "idle")).lower()

        # A sensor that cannot be read is not a sensor reading "clear".
        #
        # THIS IS THE ONE PLACE THAT USED TO LEAK. With the ToF array
        # unplugged, every safety sensor read "unknown", none of them
        # matched the alarm or warning tests, and this fell through to
        # "ok" -- so "check all sensors" answered "Everything looks fine,
        # no smoke, fire or falls detected" while GENIE could not see a
        # thing. A confident all-clear over a blind spot is the worst
        # output this system can produce, and it was the default.
        #
        # sensor.aiml already had the right words for it -- "I do not have
        # a full safety reading right now" -- sitting in the bare default
        # <li> of the ALL branch, which nothing could ever reach because
        # safety_status was always one of the three known values. Setting
        # it to anything else lands there.
        UNREADABLE = ("unknown", "", "none")
        # PRESENCE is deliberately NOT in this list. A dead ToF array means
        # GENIE cannot tell whether anyone is in the room, which is worth
        # saying -- but it is not a gap in the SAFETY picture, and folding
        # it in here would have "check all sensors" refuse to give an
        # all-clear on smoke and fire every time Node 2 was off.
        blind = [name for name, value in (
            ("smoke", smoke_s), ("flame", flame_s),
            ("motion", motion_s), ("fall", fall_s),
        ) if value in UNREADABLE]

        if (
            smoke_s in ("true", "alarm")
            or flame_s in ("true", "alarm")
            or fall_s == "fallen"
        ):
            # An alarm still wins over a blind spot. Something IS wrong and
            # saying so beats hedging about the sensors that agree.
            safety = "alarm"
        elif blind:
            safety = "partial"
            self.log_blind(blind)
        elif motion_s == "true":
            safety = "warning"
        else:
            safety = "ok"

        self.kernel.setPredicate("safety_status", safety)
        self.kernel.setPredicate("sensor", "ALL")
        reply = self.kernel.respond("CHECK SENSOR")
        self.kernel.setPredicate("sensor", "unknown")

        return reply.strip() if reply and reply.strip() else "I couldn't get a full sensor reading."


if __name__ == "__main__":
    # Standalone test: python Services/ai.py
    classifier = IntentClassifier()
    tests = [
        "turn on the fan",
        "turn the light off",
        "please switch on the fan",
        "turn on both",
        "what's the capital of France",
        "Emergency",
        "Help me",
        "Call for help",
        "I need help",
        "This is an emergency",
        "Call for emergency"
    ]
    for t in tests:
        result = classifier.classify(t)
        print(f"{t!r} -> {result}")

    print("\n--- sensor classification ---")
    sensor_tests = [
        "is there smoke",
        "please tell me the temperature",
        "check all sensors",
        "is anyone there",
    ]
    for t in sensor_tests:
        result = classifier.classify(t)
        print(f"{t!r} -> {result}")

    print("\n--- sensor reply building (with fake live data) ---")
    print(classifier.build_sensor_reply("SMOKE", {"status": "true"}))
    print(classifier.build_sensor_reply("TEMPERATURE", {"value": 27}))
    print(classifier.build_sensor_reply("FALL", None))
    print(classifier.build_all_sensors_reply({
        "SMOKE": {"status": "false"},
        "FLAME": {"status": "false"},
        "MOTION": {"status": "true"},
        "TEMPERATURE": {"value": 24},
        "HUMIDITY": {"value": 55},
    }))