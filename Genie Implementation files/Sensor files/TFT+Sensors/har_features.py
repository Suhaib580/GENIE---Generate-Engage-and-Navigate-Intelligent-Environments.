"""
har_features.py - the ONE feature extractor, shared by the board and the
training script. MPU-6050 only.

UPLOAD TO NODE 1. Also imported directly by the laptop's recorder and
trainer in GENIE/Services/.

--------------------------------------------------------------------------
WHY THIS FILE IS SHARED RATHER THAN COPIED
--------------------------------------------------------------------------
The single most important decision in the edge-AI path, so it goes first.

A model learns a relationship between FEATURES and LABELS. If the board
computes those features even slightly differently from the script that
built the training set -- a different divisor in the standard deviation, a
window one sample longer, tilt in radians instead of degrees -- then the
model was trained on one thing and is being asked about another.

Nothing errors when that happens. Training accuracy looks excellent and
the device is simply wrong, in a way that looks like "needs more data" and
is not fixed by more data. It is called train/serve skew and it is the
standard way a working TinyML project becomes a mystery.

So there is exactly one implementation, in a file that runs unmodified on
MicroPython and CPython. The board imports it to classify; the laptop
imports THE SAME FILE to build the dataset. Skew is not unlikely here, it
is impossible.

Keep it that way. The moment this needs numpy it has stopped being
shareable and the guarantee is gone.

--------------------------------------------------------------------------
WHAT A WINDOW IS
--------------------------------------------------------------------------
Samples arrive at 50 Hz from node1_report._poll_fall(), the same feed the
fall detector uses. One second of them (WINDOW_N = 50) becomes
FEATURE_COUNT numbers, and a new summary is produced every HOP_N samples,
so windows overlap by half.

Overlap matters for transients. A shove lasts 60-800 ms; with no overlap
it can straddle a boundary and appear as half a shove in each window. At
50% overlap it lands whole in at least one.

--------------------------------------------------------------------------
MPU ONLY, ON PURPOSE
--------------------------------------------------------------------------
No vibration, no ToF. Both would help -- the SW-420 hears the SURFACE and
goes silent when the unit is lifted, which no accelerometer can infer --
but a feature vector that changes shape later invalidates every recording
made before it. When another sensor is added it goes on the END of the
vector and FEATURE_VERSION goes up, so an old model refuses to load rather
than silently reading vibration where it expects a tilt.
"""

# Bump whenever the vector changes shape or meaning. The trainer stamps
# this into the model file and the board refuses a mismatch -- a model
# quietly reading the wrong column is the failure this prevents.
FEATURE_VERSION = 2

WINDOW_N = 50
HOP_N = 10

# MEASURED, 28-08-2026: the board feeds this at about 20 Hz, not the 50 Hz
# FALL_SAMPLE_MS asks for -- the TFT render loop eats the budget, which is
# the same starvation fall_fsm's stalled() has been reporting all along.
#
# So WINDOW_N = 50 is ~2.5 SECONDS of real time, not 1.0. That is left
# alone deliberately: 2-3 s is a normal window for activity recognition,
# and the rate is stable (six consecutive takes produced 17, 16, 16, 16,
# 16, 16 vectors), so the features stay comparable across recordings.
#
# THE MODEL IS THEREFORE TIED TO THE LOOP RATE. jitter, crossings and
# max_jerk are all per-sample measures; change the display frame rate or
# add work to the loop and they shift meaning, and the dataset has to be
# recorded again. Do not tune the UI after training.
#
# HOP_N was 25 (50% overlap) and is now 10 (80%). This changes NOTHING
# about what a feature means -- the window is still WINDOW_N samples and
# the arithmetic is identical -- it only emits them more often, so a
# 60-second performance yields ~120 examples instead of ~48. The extra
# windows are correlated rather than independent, which is worth stating
# in the report, but a small model still learns better from them than from
# sixteen examples per class.

FEATURE_NAMES = (
    # --- acceleration magnitude, in g -------------------------------
    "acc_mean",         # where it sits. ~1.00 at rest, whatever the angle.
    "acc_std",          # how much it moves. The single best "was this
                        # eventful" number.
    "acc_min",
    "acc_max",
    "acc_range",        # max - min. One big spike (a shove) versus
                        # continuous motion (carried), when read with std.
    "acc_dev1g",        # mean |m - 1|. Deviation from GRAVITY, not from
                        # the window mean -- in free-fall the mean is
                        # itself wrong, so a mean-relative measure hides
                        # exactly the event worth seeing.
    "acc_energy",       # mean (m - 1)^2. Emphasises big excursions the
                        # way dev1g deliberately does not.
    "acc_jitter",       # mean |m[i] - m[i-1]|. Sample-to-sample roughness:
                        # high in a hand (tremor), near zero on a desk at
                        # ANY angle. handling_fsm's entire signal, demoted
                        # to one input among sixteen.
    "acc_crossings",    # mean-crossings, normalised. Shaking crosses many
                        # times, one shove barely crosses at all, even when
                        # their std is similar.
    "acc_max_jerk",     # the single sharpest step. A tap on the case is a
                        # brief violent edge; a push is a softer, longer
                        # excursion. They have similar peaks and very
                        # different jerk.
    "acc_frac_low",     # fraction of samples under 0.7 g. Free-fall.
    "acc_frac_high",    # fraction over 1.3 g. Impact and vigorous handling.

    # --- orientation, in degrees ------------------------------------
    "tilt_mean",        # absolute angle. ~104 on this unit, so the model
                        # learns the resting pose instead of being handed
                        # a baseline somebody had to set.
    "tilt_std",
    "tilt_range",
    "tilt_delta",       # last minus first. SIGNED, unlike range: it says
                        # the unit ended up somewhere new rather than
                        # merely having wobbled. Being turned around and
                        # being jiggled look identical without it.
)

FEATURE_COUNT = len(FEATURE_NAMES)

FREEFALL_G = 0.70           # matches fall_fsm's neighbourhood
IMPACT_G = 1.30


class FeatureWindow:
    """Push samples in; take a feature vector out every HOP_N samples.

    Owns a fixed ring buffer -- 50 floats twice, about 800 bytes.
    Deliberately NOT incremental statistics: with overlapping windows the
    bookkeeping for a running std is fiddly and easy to get subtly wrong,
    and subtly wrong is exactly the skew this file exists to prevent.
    """

    def __init__(self, window_n=WINDOW_N, hop_n=HOP_N):
        self.window_n = window_n
        self.hop_n = hop_n
        self._m = [0.0] * window_n
        self._t = [0.0] * window_n
        self._i = 0                 # next write position
        self._filled = 0
        self._since_hop = 0
        self.windows = 0

    def reset(self):
        self._i = 0
        self._filled = 0
        self._since_hop = 0

    def push(self, m, tilt):
        """One sample. Returns a feature list when a window closes, else None.

        `m` of None (no IMU answered) drops the whole window. A gap in the
        middle of a window is not a shorter window -- the statistics would
        be computed over samples that are not adjacent in time, and jitter,
        jerk and crossings would all be nonsense.
        """
        if m is None:
            self.reset()
            return None

        self._m[self._i] = m
        self._t[self._i] = 0.0 if tilt is None else tilt
        self._i = (self._i + 1) % self.window_n
        if self._filled < self.window_n:
            self._filled += 1

        self._since_hop += 1
        if self._filled < self.window_n or self._since_hop < self.hop_n:
            return None

        self._since_hop = 0
        self.windows += 1
        return self._compute()

    def ready(self):
        return self._filled >= self.window_n

    # ------------------------------------------------------------------
    def _ordered(self, buf):
        """The ring buffer in time order. jitter, jerk, crossings and
        tilt_delta all depend on adjacency, so getting this wrong would
        silently corrupt four features."""
        if self._filled < self.window_n:
            return buf[:self._filled]
        return buf[self._i:] + buf[:self._i]

    def _compute(self):
        m = self._ordered(self._m)
        t = self._ordered(self._t)
        n = len(m)

        total = 0.0
        lo = hi = m[0]
        dev1 = 0.0
        energy = 0.0
        n_low = 0
        n_high = 0
        for v in m:
            total += v
            if v < lo:
                lo = v
            if v > hi:
                hi = v
            d = v - 1.0
            if d < 0:
                d = -d
            dev1 += d
            energy += d * d
            if v < FREEFALL_G:
                n_low += 1
            if v > IMPACT_G:
                n_high += 1
        mean = total / n

        var = 0.0
        for v in m:
            d = v - mean
            var += d * d
        # `** 0.5` rather than a hand-rolled Newton loop. An earlier
        # version iterated six times from a guess of 1.0, which for a
        # near-zero variance can only halve six times -- a perfectly still
        # board reported a standard deviation of 0.015625 instead of 0.
        std = (var / n) ** 0.5

        jitter = 0.0
        max_jerk = 0.0
        for i in range(1, n):
            d = m[i] - m[i - 1]
            if d < 0:
                d = -d
            jitter += d
            if d > max_jerk:
                max_jerk = d
        jitter = jitter / (n - 1) if n > 1 else 0.0

        crossings = 0
        above = m[0] >= mean
        for v in m[1:]:
            now = v >= mean
            if now != above:
                crossings += 1
                above = now

        t_total = 0.0
        t_lo = t_hi = t[0]
        for v in t:
            t_total += v
            if v < t_lo:
                t_lo = v
            if v > t_hi:
                t_hi = v
        t_mean = t_total / n
        t_var = 0.0
        for v in t:
            d = v - t_mean
            t_var += d * d
        t_std = (t_var / n) ** 0.5

        return [
            mean,
            std,
            lo,
            hi,
            hi - lo,
            dev1 / n,
            energy / n,
            jitter,
            crossings / float(n),
            max_jerk,
            n_low / float(n),
            n_high / float(n),
            t_mean,
            t_std,
            t_hi - t_lo,
            t[-1] - t[0],
        ]


# ======================================================================
# self-test -- runs under CPython:  python har_features.py
# ======================================================================
def _selftest():
    fails = [0]

    def check(label, cond, detail=""):
        print("  %-5s %s%s" % ("PASS" if cond else "FAIL", label,
                               "" if cond else "   <-- %s" % (detail,)))
        if not cond:
            fails[0] += 1

    IDX = {name: i for i, name in enumerate(FEATURE_NAMES)}

    def run(samples, tilts=None):
        w = FeatureWindow()
        out = []
        for i, mv in enumerate(samples):
            tilt = 104.0 if tilts is None else tilts[i]
            f = w.push(mv, tilt)
            if f:
                out.append(f)
        return out

    def best(vecs, name):
        """The largest value of a feature across the windows.

        Transients do NOT live in the last window. A shove at sample 40
        with a 50-sample window appears in the windows that overlap it and
        is long gone by the end -- taking [-1] and asserting on it is a
        test bug, and it has been one twice: once for acc_range, and again
        for tilt_delta after HOP_N changed and the last window no longer
        contained the turn at all.
        """
        return max(v[IDX[name]] for v in vecs)

    def biggest(vecs, name):
        """Like best(), but by MAGNITUDE -- tilt_delta is signed, and a
        turn from 104 to 30 degrees is -74, not +74."""
        return max((v[IDX[name]] for v in vecs), key=lambda x: abs(x))

    print("--- shape and contract ---")
    check("FEATURE_NAMES matches FEATURE_COUNT",
          len(FEATURE_NAMES) == FEATURE_COUNT)
    # Derived from the constants, not hardcoded -- this assertion said "3"
    # and broke the moment HOP_N was retuned, which is a test that fails
    # for a change it was supposed to tolerate.
    vecs = run([1.0] * 100)
    expected = 1 + (100 - WINDOW_N) // HOP_N
    check("a window closes every HOP_N after the first fill",
          len(vecs) == expected, "%d, expected %d" % (len(vecs), expected))
    check("every vector is FEATURE_COUNT long",
          all(len(v) == FEATURE_COUNT for v in vecs))
    check("no window before the buffer fills",
          run([1.0] * (WINDOW_N - 1)) == [])

    print("--- a perfectly still board has zero spread ---")
    still = run([1.0] * 120)[-1]
    check("std is exactly 0, not a rounding artefact",
          still[IDX["acc_std"]] == 0.0, still[IDX["acc_std"]])
    check("jitter is exactly 0", still[IDX["acc_jitter"]] == 0.0)

    print("--- a desk and a hand are separable ---")
    rest = [1.0 + (0.005 if i % 2 else -0.005) for i in range(120)]
    hand_pat = (0.93, 1.07, 0.96, 1.10, 0.90, 1.05, 0.99, 1.12)
    hand = [hand_pat[i % len(hand_pat)] for i in range(120)]
    r = run(rest)[-1]
    h = run(hand)[-1]
    check("jitter is far higher in a hand",
          h[IDX["acc_jitter"]] > 5 * r[IDX["acc_jitter"]],
          (r[IDX["acc_jitter"]], h[IDX["acc_jitter"]]))
    check("std is higher in a hand", h[IDX["acc_std"]] > r[IDX["acc_std"]])

    print("--- one shove is not continuous motion ---")
    shove = run([1.0] * 40 + [1.6] * 6 + [1.0] * 74)
    check("range picks up the spike", best(shove, "acc_range") > 0.5,
          best(shove, "acc_range"))
    check("but crossings stay low, unlike shaking",
          best(shove, "acc_crossings") < h[IDX["acc_crossings"]],
          (best(shove, "acc_crossings"), h[IDX["acc_crossings"]]))

    print("--- a tap and a push differ in JERK, not in peak ---")
    tap = run([1.0] * 40 + [1.6] * 2 + [1.0] * 78)
    push = run([1.0] * 40 + [1.15, 1.3, 1.45, 1.6, 1.45, 1.3, 1.15]
               + [1.0] * 73)
    check("same peak", abs(best(tap, "acc_max") - best(push, "acc_max")) < 0.01)
    check("tap has the sharper edge",
          best(tap, "acc_max_jerk") > best(push, "acc_max_jerk"),
          (best(tap, "acc_max_jerk"), best(push, "acc_max_jerk")))

    print("--- deviation is measured from GRAVITY, not the window mean ---")
    ff = run([0.1] * 120)[-1]
    check("free-fall shows a large dev1g", ff[IDX["acc_dev1g"]] > 0.8,
          ff[IDX["acc_dev1g"]])
    # Not `== 0.0`, and the tolerance is chosen from the SENSOR, not from
    # the float format.
    #
    # 1.0 is exactly representable so the still-board case above lands on
    # a true zero. 0.1 is not: fifty of them do not average to exactly 0.1
    # and the residuals do not cancel to bit-zero. What is left depends on
    # the runtime -- CPython leaves ~4e-17 at 64-bit, MicroPython on ESP32
    # leaves ~5e-8 because it uses 32-bit floats. A tolerance of 1e-12
    # therefore passed on a laptop and failed on the board, which is the
    # sort of portability trap this file has to be free of: it is imported
    # by BOTH.
    #
    # The honest bound is the hardware's. An MPU-6050 at +/-2 g resolves
    # 1/16384 g = 6.1e-5 g, so any spread below 1e-5 g is smaller than the
    # sensor's least significant bit and is not a measurement at all.
    # Both runtimes clear that by three orders of magnitude.
    check("its std is zero to within the sensor's own resolution",
          ff[IDX["acc_std"]] < 1e-5, ff[IDX["acc_std"]])
    check("and frac_low catches it", ff[IDX["acc_frac_low"]] == 1.0)

    print("--- turned around vs merely wobbled ---")
    turned = run([1.0] * 120, tilts=[104.0] * 60 + [30.0] * 60)
    wobble = run([1.0] * 120,
                 tilts=[104.0 + (8 if i % 2 else -8) for i in range(120)])
    check("turning shows a large signed delta",
          abs(biggest(turned, "tilt_delta")) > 60,
          biggest(turned, "tilt_delta"))
    check("wobbling nets out to nearly nothing",
          abs(biggest(wobble, "tilt_delta")) < 20,
          biggest(wobble, "tilt_delta"))
    check("even though wobbling has a real range",
          best(wobble, "tilt_range") > 10)

    print("--- a gap discards the window rather than faking one ---")
    w = FeatureWindow()
    for mv in [1.0] * 49:
        w.push(mv, 104.0)
    check("nearly-full window is not ready", not w.ready())
    w.push(None, None)
    check("a None sample resets it", not w.ready() and w._filled == 0)

    print()
    if fails[0]:
        print("%d FAILED" % fails[0])
    else:
        print("all har_features self-tests passed  (%d features, v%d)"
              % (FEATURE_COUNT, FEATURE_VERSION))
    return fails[0]


if __name__ == "__main__":
    raise SystemExit(1 if _selftest() else 0)
