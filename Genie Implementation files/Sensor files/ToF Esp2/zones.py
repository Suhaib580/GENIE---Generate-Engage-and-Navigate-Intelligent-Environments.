"""Turn raw mm readings into stable named zones.

MUST be saved onto the BOARD if you import it from other scripts.

WHY THIS EXISTS
    Raw mm is not what your application logic wants. "Is someone in the bed"
    is a better question than "is the number 412". But a naive threshold
    chatters: a reading sitting exactly on a boundary flips zone every frame,
    and downstream that becomes an alert firing 8 times a second.

    Two defences, both needed:

    HYSTERESIS  the reading must cross a boundary by HYST_MM before the zone
                is allowed to change. Kills boundary chatter.
    STABILITY   the new zone must persist for STABLE_N consecutive reads
                before it is accepted. Kills single-frame glitches, which
                these sensors do produce.

UNITS
    Everything here is millimetres. Convert to cm only when printing.
"""

VERSION = "zones 1.0  2026-08-15"

# Zone bands: (upper_bound_mm, name). Must be sorted ascending.
# Tune these to the actual mounting, they are a starting point.
DEFAULT_ZONES = (
    (40,    "TOUCH"),      # below sensor minimum - object against the lens
    (300,   "NEAR"),       # hand-wave gesture range
    (800,   "PRESENT"),    # person at the bed / chair
    (1500,  "FAR"),        # someone in the room, doorway crossing
    (99999, "CLEAR"),      # nothing meaningful in range
)

HYST_MM = 30        # must overshoot a boundary by this much to change zone
STABLE_N = 3        # consecutive agreeing reads before a change is accepted


def zone_of(mm, zones=DEFAULT_ZONES):
    """Raw classification, no hysteresis. mm=None means no valid reading."""
    if mm is None:
        return "CLEAR"
    for upper, name in zones:
        if mm < upper:
            return name
    return zones[-1][1]


class ZoneTracker:
    """Debounced zone for ONE sensor. Feed it every reading."""

    def __init__(self, name, zones=DEFAULT_ZONES,
                 hyst_mm=HYST_MM, stable_n=STABLE_N):
        self.name = name
        self.zones = zones
        self.hyst = hyst_mm
        self.stable_n = stable_n
        self.zone = "CLEAR"
        self._candidate = "CLEAR"
        self._count = 0
        self.last_mm = None

    def _boundary_ok(self, mm, new_zone):
        """True if `mm` has left the CURRENT zone by more than the hysteresis.

        Gate on the boundary we are leaving, not the one we are arriving at.
        Gating on the destination created a 30 mm dead band below every inner
        boundary that could never be entered from CLEAR -- a person standing
        still at 29 cm read CLEAR forever.
        """
        if mm is None:
            return True
        lo, hi = 0, None                 # bounds of the zone we are in now
        for upper, nm in self.zones:
            if nm == self.zone:
                hi = upper
                break
            lo = upper
        if hi is None:
            return True
        return mm >= hi + self.hyst or mm <= lo - self.hyst

    def update(self, mm):
        """Returns the new zone name if it CHANGED this call, else None."""
        self.last_mm = mm
        raw = zone_of(mm, self.zones)

        if raw == self.zone:
            self._candidate = raw
            self._count = 0
            return None

        if not self._boundary_ok(mm, raw):
            self._count = 0
            return None

        if raw == self._candidate:
            self._count += 1
        else:
            self._candidate = raw
            self._count = 1

        if self._count >= self.stable_n:
            self.zone = raw
            self._count = 0
            return raw
        return None


def cm(mm, width=5):
    """Format mm as cm for display. Never use the result for comparisons.

    MicroPython's str has no rjust/ljust/center, and %-formatting there does
    not support a "*" dynamic width either. Pad by hand.
    """
    if mm is None:
        s = "----"
    elif mm <= 0:
        s = "<0.4"                      # saturated / object against the lens
    else:
        s = "%.1f" % (mm / 10.0)
    pad = width - len(s)
    return (" " * pad + s) if pad > 0 else s


def m(mm, width=6):
    """Format mm as METRES for display. Never use the result for comparisons.

    Same manual padding as cm() -- MicroPython has no str.rjust and no "*"
    dynamic width in %-formatting.
    """
    if mm is None:
        s = "--.--"
    elif mm <= 0:
        s = "0.00"
    else:
        s = "%.2f" % (mm / 1000.0)
    pad = width - len(s)
    return (" " * pad + s) if pad > 0 else s
