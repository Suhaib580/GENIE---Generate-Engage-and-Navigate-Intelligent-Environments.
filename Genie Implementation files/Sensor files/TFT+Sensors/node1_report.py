"""
node1_report.py - Node 1's half of the laptop link. Non-blocking.

UPLOAD TO NODE 1:  genie_link.py  node1_link.py  fall_fsm.py  node1_report.py

--------------------------------------------------------------------------
WHAT THIS FILLS IN
--------------------------------------------------------------------------
`03_LAPTOP/PROTOCOL.md` describes newline-JSON over USB serial, and
`INTEGRATE.md` step 8 says "Node 1 already speaks it". It did not. Nothing
in UPLOAD_TO_ESP32/ ever wrote a telemetry line, read an `{"emotion":...}`
line, or handled `{"cmd":"recalibrate"}` -- so `03_LAPTOP/genie_serial.py`
was a faithful reader for a protocol with no writer. This is the writer.

Every field name here comes from PROTOCOL.md and PROTOCOL_ADDENDUM.md. The
few that do not are listed under EXTENSIONS below and are additive: a
laptop that ignores them sees exactly the documented protocol.

--------------------------------------------------------------------------
HOW TO WIRE IT IN
--------------------------------------------------------------------------
In genie_ui.py's UI.__init__, after self.sen exists:

    import node1_report
    self.rep = node1_report.Reporter(self.sen)

and once per iteration of UI.loop(), anywhere in the body:

    self.rep.tick()

That is the whole integration. `tick()` returns in microseconds when there
is nothing to do; the two eyes cost 15.6 ms of a 33.3 ms budget and this
must not eat into that.

--------------------------------------------------------------------------
THE THREE THINGS THAT MAKE THIS HARDER THAN IT LOOKS
--------------------------------------------------------------------------
1. STDIN CANNOT BE READ THE OBVIOUS WAY. `sys.stdin.readline()` blocks
   until a newline arrives, which on an idle link is forever, which stops
   the eyes. `uselect.poll()` with timeout 0 is the only safe read here.

2. THE FALL DETECTOR NEEDS ITS OWN POLL. sensors.py samples `imu1` every
   120 ms, which is right for a tilt display and useless for catching an
   impact spike that lasts tens of milliseconds. So the IMU is read again
   here at FALL_SAMPLE_MS, straight off the same I2C bus. Two reads of the
   same register from one cooperative loop is a few hundred microseconds
   of extra bus traffic and it is what makes the detector real.

3. A FAILED SENSOR LOOKS LIKE A MILD ALARM. sensors.py's `_rate()` returns
   1 -- the same value as "elevated" -- for any channel sitting in
   `self.errors`. Reported as a level alone, a disconnected MQ-2 becomes a
   permanent "smoke is slightly elevated". Hence `gas_ok` / `flame_ok`
   below, which the laptop checks first.

--------------------------------------------------------------------------
EXTENSIONS to PROTOCOL.md, all additive
--------------------------------------------------------------------------
    gas_level    "clear" | "elevated" | "alarm"   the graded level
                 sensors.py already computes, instead of two booleans
    gas_ok       false when the MQ-2 channel has FAILED, as opposed to
                 reading clear. See point 3
    flame_ok     the same, for the flame module
    fall_state   "idle" | "freefall" | "impact" | "fallen" - the FSM's real
                 state. `fall` stays as the documented boolean
    fall_rate_ok false when the fall detector is being starved of samples

--------------------------------------------------------------------------
SERIAL IS SHARED WITH THONNY
--------------------------------------------------------------------------
This writes to stdout, which on Node 1 is UART0 - the same port Thonny
uses. The laptop bridge and Thonny cannot both hold it. That is a working
constraint, not a bug: close Thonny before starting the laptop, and the
bridge reconnects on its own within NODE1_RECONNECT_SECONDS.
"""

import sys
import json
import time

try:
    import uselect as select
except ImportError:                     # CPython, for the self-test
    import select

# CPython on Windows has neither time.ticks_ms nor select.poll. Both exist
# on the board; these shims only ever run during the self-test at the
# bottom of this file, so it can be checked on a laptop before upload.
if not hasattr(time, "ticks_ms"):
    time.ticks_ms = lambda: int(time.monotonic() * 1000)
    time.ticks_diff = lambda a, b: a - b
    time.ticks_add = lambda a, b: a + b

try:
    import node1_link
except ImportError:                     # not uploaded yet; ToF fields go absent
    node1_link = None

try:
    import fall_fsm
except ImportError:
    fall_fsm = None

try:
    import handling_fsm
except ImportError:                     # not uploaded; no push/lift reactions
    handling_fsm = None

try:
    import har_features
except ImportError:                     # not uploaded; no edge-AI features
    har_features = None

try:
    import har_model
except (ImportError, ValueError):
    # ImportError: not uploaded. ValueError: har_model was trained against a
    # different har_features version and refused to load -- which is the
    # point of that check. Either way the rule-based handling_fsm keeps
    # working, so a bad model costs the reactions nothing.
    har_model = None

try:
    import net_uplink
except ImportError:                     # not uploaded; serial only
    net_uplink = None


# HEARTBEAT, not a poll rate. Read the note below before changing it.
#
# Every HEARTBEAT_MS a telemetry line goes out whether or not anything
# moved, so the laptop can tell "nothing is happening" from "this board is
# dead", and so api.py's staleness clock has something to measure.
#
# A CHANGE DOES NOT WAIT FOR IT. The moment any confirmed status flips --
# smoke clear->alarm, flame, fall, the unit being picked up -- a line goes
# out immediately. That distinction is the whole design: a flat 6 s poll
# would mean a flame sensor that confirmed in 150 ms sat on the board for
# up to another six seconds, which is most of the argument for having a
# fast rule-based path at all.
#
# So: quiet room, one line every 6 s. Something happens, a line at once.
HEARTBEAT_MS = 6000

# Kept as an alias: PROTOCOL.md documents "telemetry every 2 s" and older
# notes refer to this name.
TELEMETRY_MS = HEARTBEAT_MS
FALL_SAMPLE_MS = 20         # 50 Hz, matching fall_fsm.SAMPLE_MS
KNOCK_HOLD_MS = 2500        # how long a tap keeps `knock` true

# How often to restate the WiFi uplink's state when nothing has changed.
# Long enough not to clutter, short enough that "it has been down the whole
# time" is obvious within one screen of scrollback.
UPLINK_NOTE_MS = 30000

# Face board: try to reach the laptop over WiFi as well as the serial port.
#
# SET THIS False IF YOUR FACE BOARD CANNOT BRING THE RADIO UP. The symptom
# is OSError('WiFi Out of Memory') from node1_link.begin() or the uplink,
# and the cause is almost always firmware, not code: an N8R8 module running
# QUAD-PSRAM MicroPython has no PSRAM at all, so the TFT framebuffer, the
# eye engine and eight sensor channels are all fighting over ~300 KB of
# internal SRAM, and the WiFi stack needs contiguous buffers it cannot get.
#
# The real fix is to reflash with ESP32_GENERIC_S3-SPIRAM_OCT. Until then,
# False here costs nothing that matters: the USB serial port carries
# byte-identical telemetry and the laptop reads both the same way. It just
# stops the board spending frame budget retrying a radio that will not
# start.
#
# 28-08-2026: DONE. Reflashed with ESP32_GENERIC_S3-SPIRAM_OCT (v1.28.0).
# gc.mem_free() went 99,984 -> 8,320,240 and os.uname().machine now reads
# "Generic ESP32S3 module with Octal-SPIRAM". node1_link.begin() no longer
# raises, and the board joins GENIE1 and holds the association. So: True.
#
# Why True matters beyond convenience: with False the cable is the ONLY
# transport, and a COM port has exactly one owner -- so Thonny and the
# laptop's reader cannot both be attached, and every debugging session
# meant stopping the thing that feeds api.py. _emit() writes to stdout AND
# the socket every tick, so True means Thonny keeps its serial console
# while api.py is fed over WiFi. They stop competing.
WIFI_UPLINK = True

# Stream one har_features vector every 0.5 s as {"t":"har","f":[...]}.
#
# TURN THIS ON ONLY TO COLLECT TRAINING DATA. Services/har_record.py walks
# you through performing each class and labels whatever arrives while it is
# on. Once a model exists this goes back to False: the board classifies
# locally and sends the ANSWER, not the evidence, which is the whole reason
# for putting the model on the edge in the first place.
HAR_STREAM = False

# Which detector speaks: the trained model, or the hand-tuned rules.
#
#   False  handling_fsm.py drives the reactions (thresholds somebody
#          picked). har_model still runs and still prints its verdict, so
#          the two can be compared on the very same samples.
#   True   har_model drives them. The FSM keeps running -- it costs
#          microseconds and is the fallback when the model file is absent
#          or refuses to load.
#
# Measured on a session the model had never seen: 72.6% across four
# classes against a 25% chance line, rising to 82.6% on the 78% of windows
# clearing CONFIDENCE_MIN. Below that it says nothing, which for a
# reaction beats a confident wrong answer.
HAR_DRIVES_REACTIONS = True

# The model's class names are not the FSM's event names. Mapping them here
# means sensor.aiml, api.py and reaction_watch need no change at all --
# the trained model speaks the vocabulary that already exists.
#
# "idle" is absent on purpose: there is no reaction to becoming still,
# except the one below.
HAR_EVENT = {
    "contact":   "pushed",
    "picked_up": "lifted",
    "shaken":    "shaken",
}

# Going quiet AFTER being held is the one idle transition worth speaking:
# it is the unit being set down. Coming to rest after a shove is not.
HAR_SETTLE_FROM = ("picked_up", "shaken")

# How many CONSECUTIVE windows must agree before the class is allowed to
# change. Every other detector on this board already works this way --
# flame needs 3 agreeing polls, the fall FSM needs three stages in
# sequence, handling_fsm needs sustained jitter. The classifier had none,
# and single stray windows flipped it.
#
# MEASURED by replaying a recorded session through this exact logic:
#
#     K   events / 6 min   matched what was actually happening
#     1        15                60%      <- flip-flopped
#     3        10                70%
#     4         8                75%
#     5         7                86%
#
# At K=1 the log read: lifted, pushed, lifted, pushed, lifted -- all while
# the unit was simply being held. GENIE alternating "put me down" and
# "stop pushing me" is worse than saying nothing.
#
# Each step costs 0.5 s of latency (one hop). 3 is the point where the
# flapping stops without the reaction feeling detached from the event;
# raise it if wrong reactions bother you more than slow ones.
HAR_CONFIRM_N = 2

# ...but returning to IDLE needs only one. Asymmetric on purpose.
#
# WHY. Entering a class and leaving it were both 3 windows, so after one
# reaction the board needed ~4 s of complete stillness before it could
# react to anything again -- 1.5 s to confirm idle plus a 2.5 s feature
# window that still had the handling in it. Handle the unit at any normal
# rhythm and it never re-armed, which is why ten pushes produced two
# replies.
#
# Making the release cheap costs nothing in false positives: idle is the
# one class the model gets right (81% recall, and 42/42 on the random
# split), so trusting a single idle window is safe in a way that trusting
# a single 'picked_up' would not be.
HAR_RELEASE_N = 1

# Laptop emotion -> the mood vocabulary sensors.mood() speaks, which
# genie_ui's MOOD table already maps onto eye expressions. Going through
# that table rather than reaching into eyes.py directly means the face
# keeps one owner: change an expression in genie_ui and this follows.
EMOTION_TO_MOOD = {
    "neutral":   "rest",
    "happy":     "calm",
    "sad":       "offline",
    # WAS "sleep", and that was backwards. FACE_THINKING is sent on every
    # single LLM call, so mapping it to the dozing eyes made GENIE look
    # asleep at exactly the moment it was working hardest -- and produced
    # the observed cycle of sleepy -> happy -> sleepy through a normal
    # conversation. "rest" is NEUTRAL: awake, attentive, no strong feeling,
    # which is what thinking actually looks like.
    "thinking":  "rest",
    "concerned": "warn",
    "alarm":     "alarm",
}

# No laptop interaction for this long and the face goes dormant.
#
# THIS REVERSES A DELIBERATE RULE, so it is written down rather than
# quietly done. sensors.mood() says: "'calm' is the resting state and it is
# a POSITIVE one. Nothing here returns an unhappy face because time passed;
# only because a sensor measured something."
#
# That rule stays true where it was written -- sensors.py still never
# reports a mood on the strength of a clock. What changed is a product
# decision above it: an assistant that has not been spoken to in a minute
# should look dormant rather than smiling at an empty room, and should
# visibly wake when addressed. Dormancy here means "nobody is talking to
# me", which is an interaction fact and not a sensor reading, so it is
# decided here and not there.
#
# Sleep NEVER overrides a sensor. An alarm, a knock, an offline module all
# win -- see mood_override.
IDLE_SLEEP_MS = 45000

# PROTOCOL.md: the override expires and the face falls back to
# sensor-driven. DELIBERATE, and not to be "fixed" -- if the laptop
# process dies mid-conversation, GENIE must not sit there wearing a happy
# face during a gas leak. The laptop re-sends on every turn to hold one.
EMOTION_TTL_MS = 20000


# Set by Reporter.__init__ when net_uplink is available. Module-level so
# _emit stays a plain function -- it is called from half a dozen places and
# threading a handle through all of them buys nothing.
_uplink = None


def _emit(obj):
    """One JSON object, one line, to every transport that is up.

    Serial and WiFi both, deliberately. They are idempotent -- the laptop
    stores the newest reading and a value delivered twice is the same
    value -- so running both costs one extra write and buys a demo that
    survives either a pulled cable or a dead router.

    Never raises. A serial write that fails (nobody attached, buffer full)
    or a socket that has gone half-open must not take the display down.
    """
    try:
        line = json.dumps(obj)
    except Exception:
        return

    try:
        print(line)
    except Exception:
        pass

    if _uplink is not None:
        try:
            _uplink.send(line)
        except Exception:
            pass                        # uplink handles its own retries


class Reporter:

    def __init__(self, sensors_obj, log=print):
        """log defaults to print, and that matters.

        It used to default to None, and genie_ui constructs this with no
        log argument -- so every WiFi message was thrown away. The uplink
        could join the network, fail to find the laptop, and retry for ten
        minutes, and the console showed nothing but telemetry lines. There
        was no way to tell a working uplink from a dead one.

        Printing here is safe: the laptop parses lines as JSON and skips
        anything that is not, so plain-English status lines ride alongside
        telemetry without confusing either end.
        """
        self.sen = sensors_obj
        self.log = log

        now = time.ticks_ms()
        self._next_telemetry = now
        self._next_fall = now
        self._boot_sent = False
        self._cal_sent = False

        # Laptop-set expression, and when it expires.
        self.emotion = None
        self._emotion_until = 0
        # When the laptop last said anything to the face. Seeded at boot so
        # a board that never hears from a laptop still dozes off rather
        # than smiling at the wall forever.
        self._last_interaction = time.ticks_ms()

        self._was_present = False
        # None, not the current signature: the first pass must always
        # count as a change, so the laptop gets a reading immediately at
        # boot rather than up to a heartbeat later.
        self._last_sig = None
        self._last_uplink_state = None
        self._next_uplink_note = now
        self._knock_at = None
        self._last_knock_counts = (None, None)

        # No poll() at all (CPython on Windows), or a stdin that cannot be
        # registered: reading is disabled, writing still works. Losing the
        # emotion channel must never cost the telemetry channel.
        try:
            self._stdin = select.poll()
            self._stdin.register(sys.stdin, select.POLLIN)
        except Exception:
            self._stdin = None

        self.fall = None
        if fall_fsm is not None:
            self.fall = fall_fsm.FallFSM(base_tilt=self._tilt_deg() or 0.0,
                                         log=log)

        # Push / lift / set-down. Rides on the SAME 50 Hz sample the fall
        # detector takes, so it costs one function call per poll and no
        # extra I2C traffic. Optional in exactly the way fall_fsm is: a
        # board without the file keeps every other channel.
        self.handling = None
        if handling_fsm is not None:
            self.handling = handling_fsm.HandlingFSM(log=log)

        # Edge-AI feature window. Rides on the same 50 Hz sample again --
        # three consumers, one pair of I2C reads.
        #
        # Streaming is OFF by default and is turned on for DATA COLLECTION
        # only: two vectors a second is nothing for WiFi, but it is pure
        # noise in the telemetry log once a model exists. At inference time
        # the board classifies locally and sends only the answer, which is
        # the entire point of doing this on the edge.
        self.har = None
        self.har_stream = HAR_STREAM
        # Last class the model was confident about. None until it has an
        # opinion, so the first real classification always counts as a
        # change and is reported.
        self._har_class = None
        # Run-length bookkeeping for HAR_CONFIRM_N.
        self._har_cand = None
        self._har_run = 0
        # Set by {"cmd":"quiet"} while the speaker is active. Events are
        # withheld until then; classification continues regardless.
        self._har_mute_until = 0
        # Last opinion the model expressed, published in telemetry.
        self.har_class = None
        self.har_conf = 0.0
        if har_features is not None:
            self.har = har_features.FeatureWindow()

        # WiFi uplink. Built before node1_link.begin() so the radio is
        # already associated when ESP-NOW comes up -- bringing ESP-NOW up
        # first and then joining an AP moves the radio to the AP's channel
        # underneath it.
        global _uplink
        if not WIFI_UPLINK:
            if log:
                log("[report] WiFi uplink disabled (WIFI_UPLINK = False). "
                    "Serial carries the same telemetry.")
        elif net_uplink is not None and _uplink is None:
            try:
                _uplink = net_uplink.Uplink(log=log)
            except Exception as e:
                if log:
                    log("[report] uplink unavailable: %r" % e)
        self.uplink = _uplink

        if node1_link is not None:
            try:
                node1_link.begin()
            except Exception as e:
                if log:
                    log("[report] node1_link.begin() failed: %r" % e)

    # ================================================================
    # the one call genie_ui makes
    # ================================================================
    def tick(self):
        """Call once per main-loop iteration. Returns fast when idle."""
        now = time.ticks_ms()

        # ESP-NOW from Node 2. recv(0) -- returns immediately, never blocks.
        if node1_link is not None:
            try:
                frame = node1_link.poll()
                if frame is not None:
                    self._on_node2(frame)
            except Exception:
                pass

        self._read_stdin()
        self._read_uplink()
        self._expire_emotion(now)

        if time.ticks_diff(now, self._next_fall) >= 0:
            self._next_fall = time.ticks_add(now, FALL_SAMPLE_MS)
            self._poll_fall()

        if not self._boot_sent:
            self._boot_sent = True
            _emit({"t": "boot", "mpu": bool(self.sen.imus)})

        self._report_uplink(now)

        if not self._cal_sent and self.sen.calibrated():
            self._cal_sent = True
            self._emit_calibrated()

        # Heartbeat due, or something actually changed. Either sends.
        due = time.ticks_diff(now, self._next_telemetry) >= 0
        if due or self._changed():
            self._next_telemetry = time.ticks_add(now, HEARTBEAT_MS)
            _emit(self.telemetry())

    def _report_uplink(self, now):
        """Say where the WiFi uplink stands -- on change, and now and then.

        On change so a drop is noticed immediately; periodically so an
        uplink that has been down since boot cannot hide behind a quiet
        console. Both are needed: a one-off line at startup scrolls away
        under telemetry within seconds.
        """
        if _uplink is None:
            return

        state = _uplink.status()
        due = time.ticks_diff(now, self._next_uplink_note) >= 0

        if state != self._last_uplink_state:
            self._last_uplink_state = state
            self._next_uplink_note = time.ticks_add(now, UPLINK_NOTE_MS)
            print("[uplink] %s   (sent %d, dropped %d)"
                  % (state, _uplink.sent, _uplink.dropped))
        elif due:
            self._next_uplink_note = time.ticks_add(now, UPLINK_NOTE_MS)
            print("[uplink] %s   (sent %d, dropped %d)"
                  % (state, _uplink.sent, _uplink.dropped))

    def _signature(self):
        """What counts as "something happened".

        Confirmed statuses and the two link flags only -- deliberately NOT
        the raw numbers. gas_raw wobbles by a few counts on every sample
        and accel_g never reads the same twice, so including them would
        make every single pass "changed" and turn the heartbeat into a
        30 Hz firehose that saturates the socket and the display loop with
        it.

        Temperature and humidity are also left out: they move slowly and
        by whole degrees, and the heartbeat carries them soon enough.
        """
        sen = self.sen
        return (
            self._level("smoke1", "smoke2"),
            self._level("flame"),
            self._level("imu1", "imu2"),
            self.fall.status() if self.fall else None,
            self._knock_active(),
            self._present(),
        )

    def _present(self):
        if node1_link is None:
            return None
        try:
            st = node1_link.state()
            return (st["link_up"], st["presence"])
        except Exception:
            return None

    def _changed(self):
        """True once, on the pass where the signature moved."""
        sig = self._signature()
        if sig == self._last_sig:
            return False
        self._last_sig = sig
        return True

    # ================================================================
    # node 2
    # ================================================================
    def _on_node2(self, frame):
        """A frame arrived. Presence and knock are on the rule-based path
        and go out as an EVENT immediately, not on the next 2 s tick --
        that is the whole point of Node 2 sending events separately from
        its heartbeat (PROTOCOL_ADDENDUM.md)."""
        state = node1_link.state()

        self.sen.set_remote("tof", state["tof_mm"][0])

        present = state["presence"]
        if present and not self._was_present:
            _emit({"t": "event", "event": "wave",
                   "tof_zone": state["tof_zone"]})
        self._was_present = present

        if state["knock"]:
            self._knock_at = time.ticks_ms()
            _emit({"t": "event", "event": "knock"})

    # ================================================================
    # fall
    # ================================================================
    def _poll_fall(self):
        """Read the IMU on OUR schedule, not the display's.

        sensors.py's imu1 channel runs at 120 ms because that is all a
        tilt readout needs. An impact spike is tens of milliseconds wide,
        so at 120 ms it lands between samples and the fall is missed with
        no error anywhere. See point 2 in the module docstring.
        """
        if self.fall is None and self.handling is None and self.har is None:
            return

        # ONE pair of I2C reads, shared by all three consumers. _accel_mag()
        # and _tilt_deg() each go to the chip, so taking them once costs
        # half what calling through twice would -- and guarantees the fall
        # detector, the handling FSM and the feature window are all
        # reasoning about the same instant rather than samples milliseconds
        # apart. Three consumers, one read.
        m = self._accel_mag()
        tilt = self._tilt_deg()

        if self.fall is not None:
            was = self.fall.fallen()
            self.fall.feed(m, tilt)

            if self.fall.fallen() and not was:
                # PROTOCOL.md's `fall` event, sent the instant it confirms.
                # This is Path A: the laptop turns it into a call without
                # ever touching Whisper or Mistral.
                _emit({"t": "event", "event": "fall",
                       "peak_g": round(self.fall.last_peak_g, 2),
                       "tilt": int(self.fall.last_tilt),
                       "fall_state": "fallen"})

        # A fall must never be reported as a shove, and a deliberate tap
        # must never be reported as one either -- taps are the 2/3/4
        # command channel and objecting to each would make it unusable.
        #
        # Computed HERE, once, because BOTH detectors need them. They used
        # to live inside the handling_fsm branch, which meant the trained
        # model -- once it took over the reactions -- had neither: it would
        # have called an ambulance-worthy fall "shaken" and objected to
        # every tap of a deliberate command.
        suppress_ms = (handling_fsm.KNOCK_SUPPRESS_MS
                       if handling_fsm is not None else 1500)
        fall_active = (self.fall is not None
                       and self.fall.status() != "idle")
        knocked = (self._knock_at is not None
                   and time.ticks_diff(time.ticks_ms(), self._knock_at)
                   < suppress_ms)

        if self.handling is not None:
            event = self.handling.feed(m, tilt,
                                       knocked=knocked,
                                       fall_active=fall_active)
            if event:
                if HAR_DRIVES_REACTIONS and har_model is not None:
                    # The model is speaking. The FSM still RUNS -- it is the
                    # fallback if the model file goes missing, and it is the
                    # baseline the model is measured against -- but only one
                    # of them may put an event on the wire, or GENIE says
                    # everything twice.
                    self.log("[fsm] %s (model is driving)" % event)
                else:
                    msg = {"t": "event", "event": event}
                    if tilt is not None:
                        msg["tilt"] = int(tilt)
                    if event in (handling_fsm.EV_PUSHED,
                                 handling_fsm.EV_SHAKEN):
                        msg["peak_g"] = round(self.handling.last_peak_g, 2)
                    _emit(msg)

        if self.har is not None:
            self._pump_har(m, tilt, fall_active, knocked)

    def _pump_har(self, m, tilt, fall_active=False, knocked=False):
        """Feed the feature window; ship a vector when one closes.

        The window is fed even when streaming is off, deliberately. Turning
        HAR_STREAM on mid-session then has a full, correctly-ordered second
        of history behind it rather than a window half-filled from the
        moment somebody flipped the flag -- and the very first vector after
        enabling would otherwise be computed over a partly stale buffer.

        Rounded to five places on the wire. The features are ratios and
        small g values; five places is far below the MPU's own resolution
        and roughly halves the line length at two vectors a second.
        """
        feats = self.har.push(m, tilt)
        if feats is None:
            return

        if self.har_stream:
            _emit({"t": "har",
                   "v": har_features.FEATURE_VERSION,
                   "f": [round(x, 5) for x in feats]})

        if har_model is not None:
            self._classify(feats, fall_active, knocked)

    def _classify(self, feats, fall_active=False, knocked=False):
        """Run the model on one window and speak only on a CHANGE.

        Edge-triggered, not level-triggered. Windows close twice a second,
        so reporting the current class every time would emit 120 events a
        minute for a unit sitting still -- the state is not news, the
        transition is.

        Low confidence is treated as "no opinion", which deliberately does
        NOT reset the remembered class. A single uncertain window in the
        middle of being carried must not read as having been put down and
        picked up again.

        THE TWO SUPPRESSIONS ARE NOT OPTIONAL, and they are handled
        differently on purpose.

        A FALL wipes the remembered class. Dropping the unit produces
        exactly the accelerations the model was trained to call 'shaken',
        so without this GENIE remarks on a fall it is simultaneously
        phoning an ambulance about -- and then, once the fall latch
        cleared, would follow up with "thank you, that is better" because
        'shaken' is in HAR_SETTLE_FROM. Resetting to None makes the first
        classification after a fall a fresh start with nothing to settle
        from.

        A KNOCK only silences the speech. Taps are the deliberate 2/3/4
        command channel; objecting to each one would make that channel
        unusable, which is the same reason handling_fsm suppresses them.
        But the unit really is being touched, so the remembered class is
        still updated -- it is the reaction that is wrong here, not the
        classification.
        """
        if fall_active:
            self._har_class = None
            return

        cls, conf = har_model.predict(feats)

        # Published in the telemetry line whatever happens next, so the
        # model's opinion is visible in api.py instead of only on a serial
        # console Thonny is usually holding. Without this the only evidence
        # the classifier exists is the events it chooses to emit -- and the
        # interesting failure is exactly when it emits nothing.
        self.har_class = cls
        self.har_conf = conf

        if conf < har_model.CONFIDENCE_MIN:
            return

        # HAR_CONFIRM_N agreeing windows before the state may move. A
        # confident single window is not enough: the model is confident
        # about individual windows it gets wrong, which is exactly how the
        # flip-flopping happened.
        if cls == self._har_cand:
            self._har_run += 1
        else:
            self._har_cand, self._har_run = cls, 1
        need = HAR_RELEASE_N if cls == "idle" else HAR_CONFIRM_N
        if self._har_run < need:
            return

        if cls == self._har_class:
            return

        was, self._har_class = self._har_class, cls

        if not HAR_DRIVES_REACTIONS:
            self.log("[har] %s (%.2f)" % (cls, conf))
            return

        if time.ticks_diff(self._har_mute_until, time.ticks_ms()) > 0:
            # The speaker is running. The state above is already updated,
            # so nothing is lost -- only this announcement.
            self.log("[har] %s (%.2f) - muted, speaker active" % (cls, conf))
            return

        if knocked:
            # Classification kept, speech dropped. See the docstring.
            self.log("[har] %s (%.2f) - silent, a tap is being counted"
                     % (cls, conf))
            return

        if cls == "idle":
            # Only worth saying if it was being held. Coming to rest after
            # a shove is just the shove ending.
            if was in HAR_SETTLE_FROM:
                _emit({"t": "event", "event": "set_down", "conf": round(conf, 2)})
            return

        event = HAR_EVENT.get(cls)
        if event:
            _emit({"t": "event", "event": event, "conf": round(conf, 2),
                   "src": "har"})

    def _accel_mag(self):
        """Total acceleration in g, or None if no IMU answered.

        None is not zero. A disconnected IMU reporting 0 g would look
        exactly like free-fall, and the detector would call an ambulance
        for a loose Dupont wire -- which, per INTEGRATE.md, is a wire that
        breaks inside its own insulation on this project regularly.
        """
        for addr_key, addr in self._imu_addrs():
            try:
                a = self.sen._accel(addr)
            except Exception:
                continue
            m = (a[0] ** 2 + a[1] ** 2 + a[2] ** 2) ** 0.5
            return m * self.sen.imu_gain.get(addr_key, 1.0)
        return None

    def _imu_addrs(self):
        out = []
        for i, addr in enumerate(self.sen.imus):
            out.append(("imu1" if i == 0 else "imu2", addr))
        return out

    def _tilt_deg(self):
        """Degrees from flat, from whichever IMU is answering."""
        try:
            for _key, addr in self._imu_addrs():
                a = self.sen._accel(addr)
                m = (a[0] ** 2 + a[1] ** 2 + a[2] ** 2) ** 0.5
                if m <= 0:
                    continue
                z = a[2] / m
                z = 1.0 if z > 1.0 else (-1.0 if z < -1.0 else z)
                # acos without importing math: MicroPython has math, but
                # keeping this dependency-free means fall_fsm's self-test
                # runs anywhere. atan2 form, same answer.
                return _acos_deg(z)
        except Exception:
            pass
        return None

    # ================================================================
    # telemetry
    # ================================================================
    def telemetry(self):
        """The whole line. Names from PROTOCOL.md; see EXTENSIONS above."""
        sen = self.sen
        v = sen.v

        temp = v.get("temp")
        hum = v.get("hum")

        raw1, raw2 = v.get("smoke1"), v.get("smoke2")
        gas_raw = self._worst_raw(raw1, raw2)
        gas_ratio = self._worst_ratio()
        gas_level = self._level("smoke1", "smoke2")
        gas_ok = not ("smoke1" in sen.errors and "smoke2" in sen.errors)

        flame_level = self._level("flame")
        flame_ok = "flame" not in sen.errors

        # The IMU's CONFIRMED state, not its instantaneous one. sensors.py
        # scores 2 for an impact (>1.8 g) and 1 for free-fall (<0.7 g),
        # across CONFIRM_N consecutive polls -- so this is "the unit was
        # actually jolted", not "the reading wobbled once".
        #
        # Needed as its own field because accel_g alone cannot answer "did
        # you move": a single sample at 1.02 g is indistinguishable from a
        # unit at rest, and the interesting event is a transient that any
        # 2 s snapshot will miss.
        imu_level = self._level("imu1", "imu2")
        imu_ok = not ("imu1" in sen.errors and "imu2" in sen.errors)             and bool(sen.imus)

        msg = {
            "t": "telemetry",

            # PROTOCOL.md: -99 means the read failed. Kept as the
            # documented sentinel rather than null, because that is what
            # the contract says and genie_serial.py already filters it.
            "temp_c": temp if temp is not None else -99,
            "humidity": hum if hum is not None else -99,

            "gas_raw": gas_raw if gas_raw is not None else 0,
            "gas_ratio": gas_ratio,
            "gas_alert": gas_level in ("elevated", "alarm"),
            "gas_alarm": gas_level == "alarm",
            "gas_level": gas_level,
            "gas_ok": gas_ok,

            "flame": flame_level == "alarm",
            "flame_ok": flame_ok,

            "accel_g": self._round(self._accel_mag(), 2),
            "tilt": self._round(self._tilt_deg(), 0),
            "imu_level": imu_level,
            "imu_ok": imu_ok,
            "moved": imu_level in ("elevated", "alarm"),

            "knock": self._knock_active(),
            "knock_count": self._knock_total(),
        }

        if self.fall is not None:
            msg["fall"] = self.fall.fallen()
            msg["fall_state"] = self.fall.status()
            msg["fall_rate_ok"] = not self.fall.stalled()

        # The classifier's live opinion, whether or not it produced an
        # event. Events are edge-triggered, so "nothing happened" and "it
        # thinks you are pushing it and already said so" look identical
        # from the laptop -- and the second is the case worth seeing.
        if self.har_class is not None:
            msg["har_class"] = self.har_class
            msg["har_conf"] = round(self.har_conf, 2)
            msg["har_said"] = self._har_class

        # ToF fields, exactly as PROTOCOL_ADDENDUM.md defines them. Absent
        # rather than faked when node1_link is not uploaded -- a laptop
        # that sees no `present` key knows it has no presence data, where a
        # hardcoded false would read as "nobody is there".
        if node1_link is not None:
            try:
                msg.update(node1_link.telemetry_fields())
            except Exception:
                pass

        return msg

    def _emit_calibrated(self):
        sen = self.sen
        base = sen.base1 or sen.base2
        _emit({
            "t": "calibrated",
            "gas_base": base,
            # PROTOCOL.md: an MQ-2 baseline above the ADC's linear region
            # means the sensor is not burned in and its numbers are not
            # trustworthy. Surface it rather than silently reporting.
            "baseline_ok": bool(base) and base < 3000,
            "tilt": self._round(self._tilt_deg(), 0),
        })

    # ---------------------------------------------------------------- bits
    def _level(self, *keys):
        """Worst confirmed status across some channels, as a word.

        A channel in sen.errors scores 1 from sensors.py's _rate(), which
        is indistinguishable from "elevated". Excluded here; the caller
        reports the failure through gas_ok / flame_ok instead.
        """
        worst = 0
        seen = False
        for k in keys:
            if k in self.sen.errors:
                continue
            seen = True
            s = self.sen.status(k)
            if s > worst:
                worst = s
        if not seen:
            return "unknown"
        return ("alarm" if worst >= 2 else
                "elevated" if worst == 1 else "clear")

    def _worst_raw(self, a, b):
        if a is None:
            return b
        if b is None:
            return a
        return a if a > b else b

    def _worst_ratio(self):
        best = None
        for key, base in (("smoke1", self.sen.base1), ("smoke2", self.sen.base2)):
            raw = self.sen.v.get(key)
            if raw is None or not base:
                continue
            q = raw / base
            if best is None or q > best:
                best = q
        return self._round(best, 2) if best is not None else 1.0

    def _knock_total(self):
        """Lifetime tap count across both SW-420s.

        A monotonic counter, not a boolean, because it is the only field
        here that survives being read at the wrong moment: `knock` is true
        for 2.5 s after a tap and a 2 s poll can straddle it, but a counter
        that went from 4 to 7 proves three taps happened even if every one
        of them fell between two reads. Makes the vibration sensors
        testable without staring at the screen.
        """
        total = 0
        for key in ("knock1", "knock2"):
            v = self.sen.v.get(key)
            if isinstance(v, int):
                total += v
        return total

    def _knock_active(self):
        c1 = self.sen.v.get("knock1")
        c2 = self.sen.v.get("knock2")
        counts = (c1, c2)
        if self._last_knock_counts != (None, None) and counts != self._last_knock_counts:
            self._knock_at = time.ticks_ms()
        self._last_knock_counts = counts

        if self._knock_at is None:
            return False
        return time.ticks_diff(time.ticks_ms(), self._knock_at) < KNOCK_HOLD_MS

    def _round(self, v, places):
        if v is None:
            return None
        return round(v, places) if places else int(v)

    # ================================================================
    # laptop -> node
    # ================================================================
    def _read_uplink(self):
        """The same commands, arriving over WiFi instead of the cable.

        WHY THIS EXISTS. The emotion channel was stdin-only, so the laptop
        reached the face through the USB cable and nothing else. When the
        laptop moved to WiFi-primary and NODE1_SERIAL_PORT went to "off",
        node_bridge stopped running -- and set_emotion() began returning
        False on every call, silently. The face had no expression channel
        at all and nothing said so.

        Both paths feed _handle(), so a command means the same thing
        whichever way it arrived, and neither can drift from the other.
        """
        if self.uplink is None:
            return
        for _ in range(8):              # bounded, same as stdin
            line = self.uplink.poll_line()
            if not line:
                return
            self._handle(line)

    def _read_stdin(self):
        """Drain whatever is waiting. NEVER blocks.

        readline() would block until a newline arrives, which on an idle
        link never happens, which stops the eyes. poll(0) is the only safe
        way to touch stdin from a cooperative loop.
        """
        if self._stdin is None:
            return
        for _ in range(8):              # bounded: a flood must not own the loop
            if not self._stdin.poll(0):
                return
            line = self._read_line()
            if not line:
                return
            self._handle(line)

    def _read_line(self):
        chars = []
        for _ in range(256):            # bounded: a missing newline must end
            if not self._stdin.poll(0):
                break
            c = sys.stdin.read(1)
            if not c or c == "\n":
                break
            if c != "\r":
                chars.append(c)
        return "".join(chars).strip()

    def _handle(self, line):
        try:
            msg = json.loads(line)
        except Exception:
            return                      # noise or a partial line, ignore
        if not isinstance(msg, dict):
            return

        # Any command from the laptop counts as interaction, not just an
        # expression -- being asked to recalibrate is somebody paying
        # attention to it too.
        self._last_interaction = time.ticks_ms()

        emotion = msg.get("emotion")
        if emotion in EMOTION_TO_MOOD:
            self.emotion = emotion
            self._emotion_until = time.ticks_add(time.ticks_ms(), EMOTION_TTL_MS)

        if msg.get("cmd") == "quiet":
            # GENIE is about to talk, or just stopped. The speaker shares
            # this enclosure with the IMU, so its own voice shakes the case
            # hard enough to classify as being handled -- which made it
            # react, which made it talk, which shook it again. An untouched
            # unit talked continuously.
            #
            # Muting HERE rather than on the laptop matters: a blanket
            # "ignore reactions for N seconds after speech" also throws
            # away the real ones, so putting the unit down right after a
            # reply produced no acknowledgement. This suppresses exactly
            # the window the speaker is active for, and nothing else.
            #
            # The classifier keeps running and the state keeps tracking --
            # only the EVENT is withheld, so the unit does not come out of
            # the mute believing it is still where it was before.
            try:
                ms = int(msg.get("ms", 0))
            except Exception:
                ms = 0
            ms = 0 if ms < 0 else (30000 if ms > 30000 else ms)
            self._har_mute_until = time.ticks_add(time.ticks_ms(), ms)
            return

        if msg.get("cmd") == "recalibrate":
            # PROTOCOL.md: reboot and redo the warm-up plus clean-air
            # baseline. Acknowledged BEFORE resetting -- after the reset
            # there is nothing left to answer with.
            _emit({"t": "event", "event": "recalibrating"})
            try:
                import machine
                machine.reset()
            except Exception:
                pass

    def _expire_emotion(self, now):
        if self.emotion is None:
            return
        if time.ticks_diff(now, self._emotion_until) >= 0:
            self.emotion = None

    def mood_override(self):
        """The mood genie_ui should render, or None to use the sensors'.

        ALARMS ALWAYS WIN, per PROTOCOL.md. A laptop that asked for a
        happy face does not get to keep it while the flame sensor is lit --
        and since the laptop's request expires in 20 s anyway, the failure
        mode of ignoring this rule is a frozen smile during a fire.
        """
        sensed = self.sen.mood()

        if self.emotion is not None:
            if sensed == "alarm":
                return None
            return EMOTION_TO_MOOD.get(self.emotion)

        # Nobody has spoken to it in a while: doze.
        #
        # Only from "calm" -- the sensors' own resting state. If they are
        # reporting anything at all (alarm, offline, a knock two seconds
        # ago, a warn-level reading) that is real information about the
        # room and it outranks "no one is talking to me". Falling asleep
        # over a live smoke reading would be exactly the frozen-smile
        # failure this method exists to prevent, wearing different eyes.
        if sensed == "calm" and self._last_interaction is not None:
            idle = time.ticks_diff(time.ticks_ms(), self._last_interaction)
            if idle > IDLE_SLEEP_MS:
                return "sleep"

        return None


def _acos_deg(z):
    """acos in degrees, without importing math -- so this module and its
    self-test run identically on the board and on a laptop."""
    # atan2-free series-free version: use the identity via math when
    # available, and a bisection fallback that is plenty accurate for a
    # tilt readout when it is not.
    try:
        import math
        return math.degrees(math.acos(z))
    except Exception:
        lo, hi = 0.0, 180.0
        for _ in range(24):
            mid = (lo + hi) / 2.0
            # cos is monotonically decreasing on [0, 180]
            c = _cos_deg(mid)
            if c > z:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2.0


def _cos_deg(d):
    r = d * 3.141592653589793 / 180.0
    # Taylor around 0, folded into [-pi, pi] -- adequate for a tilt display.
    x2 = r * r
    return 1 - x2 / 2 + x2 * x2 / 24 - x2 * x2 * x2 / 720


# ---------------------------------------------------------------- self-test
if __name__ == "__main__":
    # Runs on a laptop with plain python3. No board, no I2C, no serial:
    # a fake Sensors proves the telemetry shape matches what
    # 03_LAPTOP/genie_serial.py and the laptop bridge expect.
    class FakeSensors:
        def __init__(self):
            self.v = {"temp": 31, "hum": 54, "smoke1": 500, "smoke2": 480,
                      "flame": 1, "knock1": 0, "knock2": 0}
            self.errors = {}
            self.imus = []
            self.imu_gain = {}
            self.base1 = self.base2 = 490
            self._st = {"smoke1": 0, "smoke2": 0, "flame": 0}

        def status(self, key):
            return self._st.get(key, 0)

        def calibrated(self):
            return True

        def mood(self):
            return "calm"

        def set_remote(self, key, value):
            pass

        def _accel(self, addr):
            raise OSError("no imu")

    sen = FakeSensors()
    r = Reporter(sen)

    print("--- a healthy tick ---")
    t = r.telemetry()
    print(json.dumps(t))
    for field in ("t", "temp_c", "humidity", "gas_raw", "gas_ratio",
                  "gas_alert", "gas_alarm", "flame"):
        assert field in t, "PROTOCOL.md field missing: " + field
    assert t["gas_level"] == "clear" and t["gas_ok"] is True
    assert t["flame"] is False
    print("  ok")

    print("--- a failed DHT reports -99, the documented sentinel ---")
    sen.v["temp"] = None
    assert r.telemetry()["temp_c"] == -99
    sen.v["temp"] = 31
    print("  ok")

    print("--- a dead MQ-2 must say so, not report 'elevated' ---")
    sen.errors["smoke1"] = "OSError"
    sen._st["smoke1"] = 1
    t = r.telemetry()
    assert t["gas_level"] == "clear", t["gas_level"]   # smoke2 still fine
    assert t["gas_ok"] is True                         # one of two is alive
    sen.errors["smoke2"] = "OSError"
    t = r.telemetry()
    assert t["gas_level"] == "unknown", t["gas_level"]
    assert t["gas_ok"] is False
    sen.errors.clear()
    sen._st["smoke1"] = 0
    print("  ok")

    print("--- a real alarm still gets through ---")
    sen._st["smoke1"] = 2
    t = r.telemetry()
    assert t["gas_alarm"] is True and t["gas_level"] == "alarm"
    assert t["gas_alert"] is True, "alarm must also read as alert"
    sen._st["smoke1"] = 0
    print("  ok")

    print("--- emotion: set, then expire ---")
    r._handle('{"emotion":"thinking"}')
    assert r.mood_override() == "sleep", r.mood_override()
    r._emotion_until = time.ticks_ms() - 1
    r._expire_emotion(time.ticks_ms())
    assert r.mood_override() is None, "the 20 s expiry did not fire"
    print("  ok")

    print("--- alarms beat whatever the laptop asked for ---")
    r._handle('{"emotion":"happy"}')
    sen.mood = lambda: "alarm"
    assert r.mood_override() is None, "a laptop smile survived an alarm"
    sen.mood = lambda: "calm"
    assert r.mood_override() == "calm"
    print("  ok")

    print("--- garbage on stdin is ignored, not fatal ---")
    r._handle("not json at all")
    r._handle('{"emotion":"purple"}')
    r._handle("[]")
    print("  ok")

    print("--- heartbeat vs send-on-change ---")
    r2 = Reporter(FakeSensors())
    assert r2._changed() is True, "first pass must always send"
    assert r2._changed() is False, "a quiet board must not send every pass"
    r2.sen._st["flame"] = 2                      # flame confirms
    assert r2._changed() is True, "a confirmed alarm must not wait"
    assert r2._changed() is False, "...and must not re-send while latched"
    print("  ok")

    print("--- jittery raw numbers must NOT count as a change ---")
    # gas_raw wobbles every sample and accel_g never repeats. If those
    # were in the signature this would fire on every pass and the
    # heartbeat would become a 30 Hz firehose.
    r3 = Reporter(FakeSensors())
    r3._changed()
    for raw in (500, 507, 494, 512, 488):
        r3.sen.v["smoke1"] = raw
        assert r3._changed() is False, "raw ADC jitter triggered a send"
    print("  ok")

    print("--- the laptop's own reader parses what we emit ---")
    line = json.dumps(r.telemetry())
    parsed = json.loads(line)
    assert parsed["t"] == "telemetry" and "\n" not in line
    print("  ok")

    print("\nall self-tests passed")
