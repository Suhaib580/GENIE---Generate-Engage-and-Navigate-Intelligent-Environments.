"""
fall_fsm.py - the three-stage fall detector, as a non-blocking state machine.

UPLOAD TO NODE 1 alongside sensors.py.

WHY THIS EXISTS SEPARATELY FROM fall_detect.py
----------------------------------------------
`fall_detect.py` is the bench version and it stays exactly as it is: it owns
its own I2C bus, sleeps 20 ms in a `while True`, and prints tuning numbers.
That is the right shape for a test script and the wrong shape for Node 1,
which is ONE cooperative loop with a 33.3 ms budget and no threads. A
blocking sample loop in there stalls the eyes.

So the algorithm is lifted out unchanged and re-expressed as `feed(m, tilt)`
-- hand it one magnitude sample whenever you happen to have one, get back a
state. It keeps no timer of its own and never sleeps. The bench script
remains the place to retune the thresholds; keep the two sets of numbers the
same.

THE ALGORITHM, unchanged from fall_detect.py
--------------------------------------------
A real fall has a three-stage signature that ordinary movement does not:

  1. FREE-FALL   total acceleration collapses toward 0 g (0.3-0.6 g)
  2. IMPACT      a sharp spike, 2-6 g, as the body hits the floor
  3. STILLNESS   they do not get up, and their orientation has changed

Requiring all three IN SEQUENCE is what separates a fall from setting the
unit down hard. Stillness is measured against gravity -- |m - 1.0| -- and
NOT against a sample grabbed at the end of the impact, because that sample
is taken mid-bounce and is not 1 g, so everything after it looks like
movement. That was the original bug in the bench script and it is worth not
reintroducing here.

SAMPLE RATE IS PART OF THE ALGORITHM
------------------------------------
The impact spike lasts tens of milliseconds. Sample at 120 ms -- which is
what sensors.py's `imu1` channel does, correctly, for a tilt display that
does not need more -- and the spike falls between two samples and the fall
is silently missed. Nothing errors; the FSM simply sits in FREEFALL until
it times out.

So whoever drives this must feed it at SAMPLE_MS or faster, from its own
poll, and not piggyback on the display's IMU channel. node1_report.py does
exactly that. `stalled()` below reports when it is being starved, so a
degraded detector announces itself instead of quietly detecting nothing.

WHAT THIS IS NOT
----------------
A credible demonstrator, not a medical device. Tuned on one person, one
surface, one mounting position. Say so plainly in the report.
"""

try:
    import utime as time
except ImportError:                     # CPython, for the self-test below
    import time
    if not hasattr(time, "ticks_ms"):
        time.ticks_ms = lambda: int(time.monotonic() * 1000)
        time.ticks_diff = lambda a, b: a - b


# --- thresholds. Keep these identical to fall_detect.py -----------------
FREEFALL_G       = 0.55     # below this = unsupported
IMPACT_G         = 2.00     # above this = hit something
FREEFALL_MIN_MS  = 80       # ignore blips shorter than this
FREEFALL_MAX_MS  = 1200     # longer than this is waving, not falling
SETTLE_MS        = 600      # let the bounce die down before judging stillness
STILL_CONFIRM_MS = 2000     # how long they must stay down
STILL_TOL_G      = 0.25     # |magnitude - 1.0| under this counts as still
STILL_FRACTION   = 0.75     # this share of the window must be still
MOVE_ABORT_N     = 15       # consecutive moving samples = they got up
TILT_CHANGE_DEG  = 30.0     # orientation must have changed by this much

SAMPLE_MS = 20              # 50 Hz. Feed at least this often.

# A confirmed fall stays reported for this long, so a laptop polling every
# 2 s cannot miss it between ticks. PROTOCOL.md documents `fall` as latched
# for 15 s and this is that latch.
LATCH_MS = 15000

# Feeding gaps longer than this mean the FSM is being starved and the
# impact spike is being sampled through. See the sample-rate note above.
STARVED_MS = SAMPLE_MS * 4

IDLE, FREEFALL, IMPACTED, CONFIRMING = "idle", "freefall", "impact", "confirming"


class FallFSM:
    """Feed it magnitudes; ask it for a state. Owns no I2C and no timers."""

    def __init__(self, base_tilt=0.0, log=None):
        self.base_tilt = base_tilt
        self.log = log

        self.state = IDLE
        self.peak_g = 0.0
        self.last_fall_ms = None        # when the last confirmed fall landed
        self.last_peak_g = 0.0
        self.last_tilt = 0.0

        self._t_state = time.ticks_ms()
        self._last_feed = None
        self._max_gap_ms = 0
        self._still_n = 0
        self._total_n = 0
        self._moving_run = 0

    # ------------------------------------------------------------- input
    def feed(self, m, tilt=None):
        """One acceleration magnitude in g, plus the current tilt in
        degrees if you have it. Returns the state name.

        `m` of None (no IMU answering) is not a reading and must not be
        treated as 0 g -- which is exactly what free-fall looks like. A
        disconnected IMU would otherwise report a fall roughly forever.
        """
        now = time.ticks_ms()

        if self._last_feed is not None:
            gap = time.ticks_diff(now, self._last_feed)
            if gap > self._max_gap_ms:
                self._max_gap_ms = gap
        self._last_feed = now

        if m is None:
            self._reset(now)
            return self.state

        elapsed = time.ticks_diff(now, self._t_state)

        if self.state == IDLE:
            if m < FREEFALL_G:
                self.state, self._t_state = FREEFALL, now
                self._say("free-fall %.2fg" % m)

        elif self.state == FREEFALL:
            if m > IMPACT_G and elapsed >= FREEFALL_MIN_MS:
                self.state, self._t_state = IMPACTED, now
                self.peak_g = m
                self._say("impact %.2fg" % m)
            elif elapsed > FREEFALL_MAX_MS:
                self._say("free-fall too long, not a fall")
                self._reset(now)
            elif m > FREEFALL_G and elapsed < FREEFALL_MIN_MS:
                self._reset(now)        # a blip, not a drop

        elif self.state == IMPACTED:
            if m > self.peak_g:
                self.peak_g = m
            if elapsed > SETTLE_MS:
                self.state, self._t_state = CONFIRMING, now
                self._still_n = self._total_n = self._moving_run = 0

        elif self.state == CONFIRMING:
            self._total_n += 1
            # Still means "at rest under gravity alone", i.e. 1.0 g --
            # NOT "close to whatever it read when the bounce ended".
            if abs(m - 1.0) <= STILL_TOL_G:
                self._still_n += 1
                self._moving_run = 0
            else:
                self._moving_run += 1

            if self._moving_run >= MOVE_ABORT_N:
                self._say("sustained movement - they got up. Not a fall.")
                self._reset(now)

            elif elapsed >= STILL_CONFIRM_MS:
                self._judge(now, tilt)

        return self.state

    # ------------------------------------------------------------- output
    def status(self):
        """The value node_bridge.py maps straight onto api.py's FALL, in
        sensor.aiml's vocabulary: idle / freefall / impact / fallen.

        CONFIRMING deliberately reports as "impact" rather than inventing a
        fifth word: from outside, "something hit the ground hard and I am
        working out whether someone is down" is exactly what it means, and
        sensor.aiml already has a line for it.
        """
        if self.fallen():
            return "fallen"
        if self.state == CONFIRMING:
            return IMPACTED
        return self.state

    def fallen(self):
        """True while a confirmed fall is still latched."""
        if self.last_fall_ms is None:
            return False
        return time.ticks_diff(time.ticks_ms(), self.last_fall_ms) < LATCH_MS

    def stalled(self):
        """True if this is being fed too slowly to catch an impact spike.

        Report it rather than hiding it. A fall detector sampling at 120 ms
        is not a fall detector that occasionally misses; it is one that
        mostly does, and it looks identical to a working one right up until
        somebody actually falls.
        """
        return self._max_gap_ms > STARVED_MS

    def worst_gap_ms(self):
        return self._max_gap_ms

    def clear_latch(self):
        self.last_fall_ms = None

    # ------------------------------------------------------------- internal
    def _judge(self, now, tilt):
        frac = (self._still_n / self._total_n) if self._total_n else 0.0
        still_ok = frac >= STILL_FRACTION

        # No tilt reading means the orientation test cannot be run. Passing
        # it by default is the safe direction here: the two hard stages
        # (free-fall THEN impact THEN two seconds of stillness) have already
        # been met, and refusing to report on a missing third signal is how
        # a fall detector stays quiet during a real fall.
        if tilt is None:
            tilt_ok, delta, new_tilt = True, 0.0, self.base_tilt
        else:
            new_tilt = tilt
            delta = abs(new_tilt - self.base_tilt)
            tilt_ok = delta >= TILT_CHANGE_DEG

        if still_ok and tilt_ok:
            self.last_fall_ms = now
            self.last_peak_g = self.peak_g
            self.last_tilt = new_tilt
            self._say("FALL CONFIRMED  peak %.2fg  tilt %.0f->%.0f  still %d%%"
                      % (self.peak_g, self.base_tilt, new_tilt, frac * 100))
        else:
            # A rejection is tuning data. Print the numbers either way.
            self._say("no fall: still %d%% (need %d)  tilt delta %.0f (need %.0f)"
                      % (frac * 100, STILL_FRACTION * 100, delta, TILT_CHANGE_DEG))

        self._reset(now)

    def _reset(self, now):
        self.state = IDLE
        self._t_state = now
        self.peak_g = 0.0
        self._still_n = self._total_n = self._moving_run = 0

    def _say(self, msg):
        if self.log:
            self.log("[fall] " + msg)


# ---------------------------------------------------------------- self-test
if __name__ == "__main__":
    # Runs on the laptop with plain python3 as well as on the board with F5.
    # No I2C, no board: the FSM is fed a synthesised fall.
    import time as _t

    def drive(fsm, samples, dt_ms=SAMPLE_MS, tilt=None):
        """Feed samples, advancing a fake clock so the test does not sleep
        for the full 2.6 s confirmation window."""
        for m in samples:
            fsm.feed(m, tilt)
            _shift(fsm, dt_ms)

    def _shift(fsm, ms):
        """Pull every timestamp back, which is the same as time passing."""
        fsm._t_state -= ms
        if fsm._last_feed is not None:
            fsm._last_feed -= ms
        if fsm.last_fall_ms is not None:
            fsm.last_fall_ms -= ms

    print("--- ordinary movement is not a fall ---")
    f = FallFSM(base_tilt=2.0)
    drive(f, [1.0, 1.1, 0.9, 1.3, 1.0] * 10)
    assert f.status() == "idle", f.status()
    print("  ok")

    print("--- a hard knock with no free-fall first is not a fall ---")
    f = FallFSM(base_tilt=2.0)
    drive(f, [1.0, 1.0, 3.5, 1.0, 1.0] * 5)
    assert f.status() == "idle", f.status()
    print("  ok")

    print("--- free-fall then impact then stillness IS a fall ---")
    f = FallFSM(base_tilt=2.0, log=lambda s: print("   ", s))
    drive(f, [1.0] * 3)                       # at rest
    drive(f, [0.3] * 8)                       # 160 ms of free-fall
    drive(f, [3.2])                           # impact
    drive(f, [1.4, 1.2] * 20)                 # bounce through SETTLE_MS
    drive(f, [1.0] * 120, tilt=88.0)          # 2.4 s still, orientation changed
    assert f.status() == "fallen", f.status()
    assert f.fallen()
    print("  ok  (peak %.2fg)" % f.last_peak_g)

    print("--- getting up again aborts the confirmation ---")
    f = FallFSM(base_tilt=2.0)
    drive(f, [1.0] * 3)
    drive(f, [0.3] * 8)
    drive(f, [3.2])
    drive(f, [1.4, 1.2] * 20)
    drive(f, [1.8] * 20, tilt=88.0)           # sustained movement
    assert f.status() == "idle", f.status()
    print("  ok")

    print("--- a disconnected IMU must not read as free-fall ---")
    f = FallFSM(base_tilt=2.0)
    drive(f, [None] * 200)
    assert f.status() == "idle", f.status()
    print("  ok")

    print("--- being fed too slowly is REPORTED, not hidden ---")
    f = FallFSM()
    drive(f, [1.0] * 5, dt_ms=SAMPLE_MS)
    assert not f.stalled(), f.worst_gap_ms()
    drive(f, [1.0] * 5, dt_ms=120)            # sensors.py's imu channel rate
    assert f.stalled(), "a 120 ms feed must announce itself"
    print("  ok  (worst gap %d ms)" % f.worst_gap_ms())

    print("\nall self-tests passed")
