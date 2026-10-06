"""
handling_fsm.py - "somebody is touching me": push / lift / set-down, as a
non-blocking state machine.

UPLOAD TO NODE 1 alongside fall_fsm.py and sensors.py.

WHY THIS EXISTS
---------------
Every sensor on this board answers questions and does nothing else. The IMU
knows the unit was shoved, but only if somebody asks "did you move". A
device that reports state when queried is an instrument; a device that
reacts is a presence. This turns the IMU into the second kind.

It is deliberately NOT an assistive feature and the report should not claim
it is. "Why are you pushing me" helps nobody do anything -- it makes GENIE
read as aware of its own situation, which is worth something on its own.

SAME SHAPE AS fall_fsm.py, ON PURPOSE
-------------------------------------
`feed(m, tilt, ...)` -> an event name or None. No I2C, no threads, no
sleeps, no timers of its own beyond ticks_ms. Node 1 is ONE cooperative
loop with a 33.3 ms budget, so anything that blocks here freezes the face.
node1_report._poll_fall() already reads the IMU at 50 Hz for the fall
detector; this rides on the same sample and costs one function call.

THE THREE SIGNALS, AND WHY JITTER RATHER THAN TILT
--------------------------------------------------
  PUSH      a brief excursion in |a| while the unit stays where it is.
  LIFTED    sustained IRREGULAR motion -- a hand always trembles, so |a|
            keeps changing. A unit on a desk does not.
  SET DOWN  that irregular motion stops.

The obvious approach is absolute tilt: "it is at a different angle, so it
was picked up". That is wrong here for a specific reason -- this board is
mounted at an angle (tilt reads ~104 degrees at rest, and tilt_zero is
[0.946, 0.354], almost the whole gravity vector in the x-y plane). Any
absolute-angle rule needs a calibrated "normal" and breaks the moment the
unit is re-mounted or set down facing a different way.

Jitter needs no baseline. Held in a hand it is high, on any surface at any
angle it is near zero, and "set down somewhere new" resolves by itself
because the new resting angle is still a resting angle. That is the whole
argument for it.

WHAT MUST NOT HAPPEN, AND HOW EACH IS PREVENTED
-----------------------------------------------
  A fall being called a push.       fall_active suppresses everything and
                                    resets. The fall FSM owns that event.
  A knock being called a push.      Tapping the case spikes |a| to 1.5 g
                                    (measured). knocked suppresses pushes
                                    for KNOCK_SUPPRESS_MS.
  Nagging.                          One "pushed" per PUSH_COOLDOWN_MS, and
                                    "still_lifted" repeats at LIFT_REPEAT_MS
                                    and gives up after LIFT_MAX_REPEATS.
  Reacting to a dead IMU.           m is None resets and emits nothing. A
                                    disconnected chip must never look like
                                    being handled.
  Reacting to being installed.      WARMUP_MS of silence after construction,
                                    because the unit IS being handled while
                                    somebody powers it up.
  A vibrating surface reading as    LIFT_CONFIRM_MS is long, the threshold
  a hand forever.                   sits above desk noise, and the repeat
                                    cap ends the nagging even if tuning is
                                    wrong on a particular desk.

NOT A MEDICAL DEVICE, and not tuned on more than one unit. The thresholds
below are starting points measured on this board at rest (|a| 0.98-1.01,
knocks peaking 1.14-1.53). Retune on the real enclosure.
"""

try:
    import utime as time
except ImportError:                     # CPython, for the self-test below
    import time
    if not hasattr(time, "ticks_ms"):
        time.ticks_ms = lambda: int(time.monotonic() * 1000)
        time.ticks_diff = lambda a, b: a - b
        time.ticks_add = lambda a, b: a + b


# --- resting -----------------------------------------------------------
# |a| at rest measured 0.98-1.01 on this board after levelling, so a
# deviation of 0.08 is well clear of noise without being deaf to a nudge.
REST_TOL_G = 0.08

# --- push --------------------------------------------------------------
# How far |a| must leave 1 g to count as a shove. Knocks reach 1.5 g
# (delta 0.5) so this ALONE cannot separate the two -- KNOCK_SUPPRESS_MS
# does that. This only rejects ordinary settling.
PUSH_G = 0.30
PUSH_MIN_MS = 60            # 3 samples. Shorter is ringing, not a shove.
PUSH_MAX_MS = 800           # Longer is being carried, and LIFTED owns it.
PUSH_COOLDOWN_MS = 4000     # One objection per episode. Five shoves in
                            # three seconds must not queue five sentences
                            # -- each spoken reply is 3-8 s of audio, so
                            # that is half a minute of GENIE complaining.
KNOCK_SUPPRESS_MS = 1500    # After any tap, no pushes. Taps are a command
                            # channel (2/3/4 taps); objecting to each one
                            # would make that channel unusable.

# --- lift --------------------------------------------------------------
# Exponential moving average of |m - previous m|. O(1) memory, no ring
# buffer, which matters on a board already fighting for RAM.
JITTER_ALPHA = 0.25
JITTER_LIFT_G = 0.045       # Above this, sustained = in a hand.
JITTER_REST_G = 0.025       # Below this, sustained = on a surface.
                            #
                            # An EMA decays TOWARD its input, so a rest
                            # threshold set at exactly the resting jitter
                            # is never crossed and "set down" never fires.
                            # This must sit strictly above whatever a
                            # settled board actually produces, with room
                            # to spare -- a desk someone is typing on is
                            # noisier than a bench. The gap up to
                            # JITTER_LIFT_G is the hysteresis band.
LIFT_CONFIRM_MS = 700       # Hand tremor must persist this long. Long
                            # enough that a single shove does not read as
                            # a lift.
SETTLE_CONFIRM_MS = 900     # And stillness this long before "set down".
LIFT_REPEAT_MS = 10000      # "Still in the air? say it again" cadence.
LIFT_MAX_REPEATS = 6        # ~1 minute, then stop. If somebody is carrying
                            # it across a building, it has made its point.

# --- housekeeping ------------------------------------------------------
WARMUP_MS = 3000            # The unit is being handled while it boots.
SAMPLE_MS = 20              # 50 Hz, same feed as fall_fsm.
STARVED_MS = SAMPLE_MS * 4  # Gaps longer than this mean pushes are being
                            # sampled through, exactly as for the fall FSM.

RESTING, PUSHING, LIFTED, SETTLING = "resting", "pushing", "lifted", "settling"

# Event names. These are the wire contract -- api.py and whisper_service
# match on them, so they are not free to rename.
EV_PUSHED = "pushed"
EV_SHAKEN = "shaken"        # a shove while ALREADY in the air
EV_LIFTED = "lifted"
EV_STILL_LIFTED = "still_lifted"
EV_SET_DOWN = "set_down"


class HandlingFSM:
    """Feed it magnitudes; get back an event name or None."""

    def __init__(self, log=None):
        self.log = log
        self.state = RESTING

        self._born = time.ticks_ms()
        self._last_feed = None
        self._max_gap_ms = 0

        self._prev_m = None
        self.jitter = 0.0

        self._excursion_started = None   # when |a| first left the rest band
        self._peak_g = 0.0
        self._last_push_at = None

        self._quiet_since = None         # jitter has been low since
        self._busy_since = None          # jitter has been high since
        self._lifted_at = None
        self._last_repeat_at = None
        self._repeats = 0

        self.last_event = None
        self.last_peak_g = 0.0

    # ------------------------------------------------------------------
    def feed(self, m, tilt=None, knocked=False, fall_active=False):
        """One sample. Returns an event name, or None.

        `m`            gain-corrected |a| in g, or None if no IMU answered.
        `tilt`         degrees from flat. Recorded on events, not used to
                       decide them -- see the module note on jitter.
        `knocked`      a tap landed recently (node1_report._knock_at).
        `fall_active`  the fall FSM is not idle. Suppresses everything.
        """
        now = time.ticks_ms()

        if self._last_feed is not None:
            gap = time.ticks_diff(now, self._last_feed)
            if gap > self._max_gap_ms:
                self._max_gap_ms = gap
        self._last_feed = now

        # A dead IMU is not a still one, and it is not a handled one
        # either. Reset and say nothing -- inventing an event from absent
        # data is the failure this whole project keeps guarding against.
        if m is None:
            self._reset(now)
            return None

        # The fall detector owns anything that looks like a fall. Someone
        # dropping the unit must hear about the fall, not be asked why
        # they are pushing it.
        if fall_active:
            self._reset(now)
            return None

        # Boot grace. The unit is in somebody's hands while it powers up,
        # and announcing that is noise.
        if time.ticks_diff(now, self._born) < WARMUP_MS:
            self._track_jitter(m)
            return None

        self._track_jitter(m)

        # Order matters: the lift tracker runs first so that a shove while
        # airborne is reported as shaking rather than pushing.
        event = self._track_lift(now)
        if event is not None:
            return self._fire(event, tilt)

        event = self._track_push(now, m, knocked)
        if event is not None:
            return self._fire(event, tilt)

        return None

    # ------------------------------------------------------------------
    def _track_jitter(self, m):
        if self._prev_m is None:
            self._prev_m = m
            return
        delta = m - self._prev_m
        if delta < 0:
            delta = -delta
        self.jitter += JITTER_ALPHA * (delta - self.jitter)
        self._prev_m = m

    def _track_lift(self, now):
        """Sustained irregular motion means a hand is holding it."""
        busy = self.jitter >= JITTER_LIFT_G
        quiet = self.jitter <= JITTER_REST_G

        if busy:
            self._quiet_since = None
            if self._busy_since is None:
                self._busy_since = now
        elif quiet:
            self._busy_since = None
            if self._quiet_since is None:
                self._quiet_since = now
        else:
            # Between the two thresholds: hysteresis band, commit to
            # nothing. Without this the state strobes at the boundary.
            return None

        if self.state in (RESTING, PUSHING):
            if busy and time.ticks_diff(now, self._busy_since) >= LIFT_CONFIRM_MS:
                self.state = LIFTED
                self._lifted_at = now
                self._last_repeat_at = now
                self._repeats = 0
                self._excursion_started = None
                return EV_LIFTED
            return None

        if self.state == LIFTED:
            if quiet and time.ticks_diff(now, self._quiet_since) >= SETTLE_CONFIRM_MS:
                self.state = RESTING
                self._lifted_at = None
                return EV_SET_DOWN
            # Still airborne. Repeat, but not forever.
            if (self._repeats < LIFT_MAX_REPEATS
                    and time.ticks_diff(now, self._last_repeat_at) >= LIFT_REPEAT_MS):
                self._last_repeat_at = now
                self._repeats += 1
                return EV_STILL_LIFTED
            return None

        return None

    def _track_push(self, now, m, knocked):
        """A brief excursion from 1 g, while not airborne."""
        dev = m - 1.0
        if dev < 0:
            dev = -dev

        if dev >= PUSH_G:
            if self._excursion_started is None:
                self._excursion_started = now
                self._peak_g = m
            elif m > self._peak_g:
                self._peak_g = m
            return None

        # Excursion just ended -- decide what it was.
        if self._excursion_started is None:
            return None

        held = time.ticks_diff(now, self._excursion_started)
        peak = self._peak_g
        self._excursion_started = None
        self._peak_g = 0.0

        if held < PUSH_MIN_MS or held > PUSH_MAX_MS:
            return None

        # A tap on the case spikes the IMU exactly like a shove does, and
        # taps are a deliberate command channel. Objecting to each one
        # would make that channel unusable.
        if knocked:
            return None

        if (self._last_push_at is not None
                and time.ticks_diff(now, self._last_push_at) < PUSH_COOLDOWN_MS):
            return None

        self._last_push_at = now
        self.last_peak_g = peak
        return EV_SHAKEN if self.state == LIFTED else EV_PUSHED

    # ------------------------------------------------------------------
    def _fire(self, event, tilt):
        self.last_event = event
        if self.log:
            self.log("[handling] %s (jitter %.3f, state %s)"
                     % (event, self.jitter, self.state))
        return event

    def _reset(self, now):
        self.state = RESTING
        self._prev_m = None
        self.jitter = 0.0
        self._excursion_started = None
        self._peak_g = 0.0
        self._quiet_since = None
        self._busy_since = None
        self._lifted_at = None
        self._last_repeat_at = None
        self._repeats = 0

    # ------------------------------------------------------------------
    def stalled(self):
        """True if being fed too slowly to catch a push.

        Same contract as fall_fsm.stalled(): a high-water mark, never
        reset, so one bad stall is remembered rather than averaged away.
        """
        return self._max_gap_ms > STARVED_MS

    def worst_gap_ms(self):
        return self._max_gap_ms

    def status(self):
        return self.state


# ======================================================================
# self-test -- runs under CPython:  python handling_fsm.py
# ======================================================================
def _drive(f, samples, dt_ms=SAMPLE_MS, knocked=False, fall_active=False):
    """Feed samples as if dt_ms apart, collecting events."""
    out = []
    for m in samples:
        # Pretend time passed, without sleeping.
        if f._last_feed is not None:
            f._last_feed -= dt_ms
        if f._busy_since is not None:
            f._busy_since -= dt_ms
        if f._quiet_since is not None:
            f._quiet_since -= dt_ms
        if f._excursion_started is not None:
            f._excursion_started -= dt_ms
        if f._last_repeat_at is not None:
            f._last_repeat_at -= dt_ms
        if f._last_push_at is not None:
            f._last_push_at -= dt_ms
        f._born -= dt_ms
        ev = f.feed(m, tilt=104, knocked=knocked, fall_active=fall_active)
        if ev:
            out.append(ev)
    return out


def _rest(n):
    """n samples of a board sitting on a desk.

    Measured on this board at 50 Hz the sample-to-sample step is well
    under 0.01 g; the readings that LOOK like 0.98-1.01 are 2 s apart in
    api.py, not consecutive. Alternating +/-0.005 gives a per-sample
    delta of 0.01 -- pessimistic for a settled board, and still clear of
    JITTER_REST_G.
    """
    out = []
    for i in range(n):
        out.append(1.00 + (0.005 if i % 2 else -0.005))
    return out


def _hand(n):
    """n samples of a board held in a hand: bigger, irregular steps."""
    out = []
    pattern = (0.93, 1.07, 0.96, 1.10, 0.90, 1.05, 0.99, 1.12)
    for i in range(n):
        out.append(pattern[i % len(pattern)])
    return out


def _selftest():
    fails = [0]

    def check(label, cond):
        print("  %-5s %s" % ("PASS" if cond else "FAIL", label))
        if not cond:
            fails[0] += 1

    def fresh():
        f = HandlingFSM()
        f._born -= WARMUP_MS + 100          # past the boot grace
        _drive(f, _rest(40))                # settle the jitter EMA
        return f

    print("--- a board at rest says nothing ---")
    f = fresh()
    check("no events from 200 still samples", _drive(f, _rest(200)) == [])

    print("--- a shove is a push ---")
    f = fresh()
    ev = _drive(f, [1.45] * 5 + _rest(20))
    check("pushed once", ev == [EV_PUSHED])

    print("--- a shove during a KNOCK is not a push ---")
    f = fresh()
    ev = _drive(f, [1.45] * 5 + _rest(20), knocked=True)
    check("silent while knocked", ev == [])

    print("--- a shove during a FALL is not a push ---")
    f = fresh()
    ev = _drive(f, [1.45] * 5 + _rest(20), fall_active=True)
    check("silent while falling", ev == [])

    print("--- five shoves in three seconds object ONCE ---")
    f = fresh()
    ev = []
    for _ in range(5):
        ev += _drive(f, [1.45] * 5 + _rest(15))
    check("one objection, not five", ev.count(EV_PUSHED) == 1)

    print("--- a ringing tap is too brief to be a push ---")
    f = fresh()
    ev = _drive(f, [1.50] * 2 + _rest(20))     # 40 ms < PUSH_MIN_MS
    check("ignored", ev == [])

    print("--- being picked up ---")
    f = fresh()
    ev = _drive(f, _hand(80))
    check("lifted announced", EV_LIFTED in ev)

    print("--- still in the air, repeats then gives up ---")
    f = fresh()
    ev = _drive(f, _hand(60))
    long_hold = _drive(f, _hand(int(LIFT_REPEAT_MS / SAMPLE_MS) *
                                (LIFT_MAX_REPEATS + 3)))
    check("repeats", long_hold.count(EV_STILL_LIFTED) > 0)
    check("stops nagging at the cap",
          long_hold.count(EV_STILL_LIFTED) <= LIFT_MAX_REPEATS)

    print("--- set back down ---")
    f = fresh()
    _drive(f, _hand(80))
    ev = _drive(f, _rest(120))
    check("set_down announced", EV_SET_DOWN in ev)

    print("--- set down at a NEW angle is still set down ---")
    f = fresh()
    _drive(f, _hand(80))
    ev = []
    for m in _rest(120):
        if f._last_feed is not None:
            f._last_feed -= SAMPLE_MS
        if f._quiet_since is not None:
            f._quiet_since -= SAMPLE_MS
        e = f.feed(m, tilt=31, knocked=False)   # 73 degrees from before
        if e:
            ev.append(e)
    check("angle is irrelevant, jitter decided", EV_SET_DOWN in ev)

    print("--- a shove while airborne is SHAKEN, not pushed ---")
    f = fresh()
    _drive(f, _hand(80))
    ev = _drive(f, [1.60] * 5 + _hand(20))
    check("shaken", EV_SHAKEN in ev and EV_PUSHED not in ev)

    print("--- a dead IMU is not a handled one ---")
    f = fresh()
    _drive(f, _hand(60))
    ev = _drive(f, [None] * 30)
    check("no events from None", ev == [])
    check("and it reset to resting", f.state == RESTING)

    print("--- boot grace: being installed says nothing ---")
    f = HandlingFSM()                       # NOT past the warmup
    check("silent during warmup", _drive(f, _hand(60)) == [])

    print("--- starvation is reported, not hidden ---")
    f = fresh()
    _drive(f, _rest(10), dt_ms=200)
    check("stalled() true after a 200 ms gap", f.stalled())
    check("and the gap is readable", f.worst_gap_ms() >= 200)

    print()
    if fails[0]:
        print("%d FAILED" % fails[0])
    else:
        print("all handling_fsm self-tests passed")
    return fails[0]


if __name__ == "__main__":
    raise SystemExit(1 if _selftest() else 0)
