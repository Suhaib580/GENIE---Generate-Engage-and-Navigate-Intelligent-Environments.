"""GENIE Node 2 -- N x VL53L1X on one I2C bus.

MODULE. Upload this to the board; run_tof_test.py and tof_bench.py import it.

WHY XSHUT EXISTS
    Every VL53L1X ships at address 0x29. Two or more on one bus all answer at
    once and the bus returns garbage -- no driver code fixes this. XSHUT holds
    a chip in reset while low, so we wake them one at a time and hand each a
    private address.

    Those addresses live in sensor RAM, NOT flash. Every reset and every power
    cycle throws them away. bring_up() must run at the top of every boot.
    Forgetting this is the single most common way this build fails.

Requires on the board: vl53l1x.py, zones.py, pins3.py

==========================================================================
RESTORED 26-08-2026
==========================================================================
This file and pins3.py were imported by `bench/run_tof_test.py` and
`bench/tof_bench.py` and were missing from the bundle, so both scripts died
on ImportError before touching a sensor.

The docstrings here still refer to `menu3.py` as the runner. menu3.py does
not exist anywhere in the project. Its successor is `bench/run_tof_test.py`,
which is menu-driven and calls exactly the functions below.

THIS IS THE BENCH PATH, NOT THE PRODUCTION PATH. `node2_main.py` inlines its
own bring_up / calibrate / read_all and imports none of this. Two
implementations of the same bring-up sequence now exist; they agree today.
If you fix a bring-up bug here, fix it there too -- the demo runs
node2_main.py, not this.
"""

VERSION = "tof3 1.0  2026-08-15"

from machine import I2C, Pin
import time

from vl53l1x import VL53L1X, SHORT, LONG, MODEL_ID
from zones import ZoneTracker, cm, m
import pins3 as P


# ==================================================================== bus
def make_i2c(freq=None):
    return I2C(0, sda=Pin(P.SDA), scl=Pin(P.SCL), freq=freq or P.I2C_FREQ)


def scan(i2c=None):
    """Raw bus scan. Run this BEFORE bring_up to see the factory address."""
    i2c = i2c or make_i2c()
    found = i2c.scan()
    print("  bus: %s" % [hex(a) for a in found])
    return found


def sleep_all(n=None):
    """Drive every XSHUT low. Returns the Pin objects -- keep the reference,
    or MicroPython may let them drift."""
    n = n or P.N_SENSORS
    pins = [Pin(p, Pin.OUT, value=0) for p in P.XSHUT[:n]]
    time.sleep_ms(30)
    return pins


# =============================================================== bring-up
def bring_up(i2c=None, mode=LONG, n=None, verbose=True):
    """Wake sensors one at a time, give each a private address.

    Returns (i2c, sensors) where sensors is a list of length n. A failed
    sensor becomes None -- one dead sensor must never stop the others.
    """
    n = n or P.N_SENSORS
    i2c = i2c or make_i2c()

    pins = sleep_all(n)                                   # everyone asleep

    if verbose:
        pre = [hex(a) for a in i2c.scan()]
        print("  bus with everything in reset: %s" % pre)
        if "0x29" in pre:
            print("  WARNING: 0x29 answers while ALL XSHUT are LOW.")
            print("           One sensor's XSHUT is not on its GPIO.")
            print("           It will collide during address assignment.")
            print("           Run the XSHUT diagnostic -- it names the wire.")

    sensors = []
    for i, pin in enumerate(pins):
        pin.value(1)
        time.sleep_ms(P.BOOT_MS)
        try:
            if 0x29 not in i2c.scan():
                raise OSError("did not wake at 0x29")

            s = VL53L1X(i2c, 0x29)

            mid = s.model_id()
            if mid != MODEL_ID:
                # Boards silkscreened VL53L0/1XV2 ship with EITHER chip and
                # look identical. One L0X among the L1Xs fails in a way that
                # looks exactly like a wiring fault, so say so plainly.
                print("  %-6s WARNING: model id 0x%04X, expected 0x%04X"
                      % (P.NAMES[i], mid, MODEL_ID))
                print("         this is probably a VL53L0X -- wrong driver")

            s.init(mode)
            s.set_address(P.ADDRESSES[i])
            s.start()
            sensors.append(s)
            if verbose:
                print("  %-6s XSHUT%-3d -> 0x%02X  id=0x%04X  ok"
                      % (P.NAMES[i], P.XSHUT[i], P.ADDRESSES[i], mid))
        except Exception as e:
            sensors.append(None)
            if verbose:
                print("  %-6s XSHUT%-3d -> FAILED: %s"
                      % (P.NAMES[i], P.XSHUT[i], e))
            pin.value(0)                  # park it so it cannot squat on 0x29
            time.sleep_ms(P.BOOT_MS)

    if verbose:
        print("  bus now: %s" % [hex(a) for a in i2c.scan()])
        alive = sum(1 for s in sensors if s)
        print("  %d of %d sensors live" % (alive, n))
        if alive < n:
            print("  -> run the XSHUT diagnostic, it names the bad wire")

    return i2c, sensors


def make_trackers(n=None):
    n = n or P.N_SENSORS
    return [ZoneTracker(P.NAMES[i], zones=P.ROOM_ZONES,
                        hyst_mm=P.ROOM_HYST_MM, stable_n=P.ROOM_STABLE_N)
            for i in range(n)]


def stop_all(sensors):
    for s in sensors:
        if s:
            try:
                s.stop()
            except Exception:
                pass


# ================================================================ reading
def read_all(sensors):
    """One pass. Returns a list of mm, or None where there was no sample.

    Non-blocking: asks data_ready() first, so a slow or dead sensor never
    stalls the other two.
    """
    out = []
    for s in sensors:
        if s is None:
            out.append(None)
            continue
        try:
            if s.data_ready():
                mm = s.distance()
                if mm is not None and mm < P.MIN_VALID_MM:
                    mm = 0                      # saturated: object on the lens
                out.append(mm)
            else:
                out.append(None)                # no new sample this pass
        except OSError:
            out.append(None)
    return out


# ============================================================ diagnostics
def xshut_diagnostic(n=None):
    """Pull each XSHUT low in turn; 0x29 should vanish. Names the bad wire.

    POWER-CYCLE THE BOARD FIRST. If bring_up already ran, the sensors sit at
    0x30+ and will not answer at 0x29, and this test tells you nothing.
    """
    n = n or P.N_SENSORS
    print("\n--- XSHUT diagnostic --------------------------------------")
    print("  power-cycle the board before trusting this result\n")

    i2c = make_i2c(100_000)
    pins = [Pin(p, Pin.OUT, value=1) for p in P.XSHUT[:n]]     # all awake
    time.sleep_ms(50)

    base = i2c.scan()
    print("  all awake: %s" % [hex(a) for a in base])
    if 0x29 not in base:
        print("  0x29 absent with everything awake.")
        print("  -> no sensor is powered, or none is on this bus at all.")
        print("     Check VIN/GND before chasing XSHUT.")
        return

    bad = []
    for i, pin in enumerate(pins):
        pin.value(0)
        time.sleep_ms(P.BOOT_MS)
        gone = 0x29 not in i2c.scan()
        pin.value(1)
        time.sleep_ms(P.BOOT_MS)
        # 0x29 only disappears when this was the ONLY sensor answering, i.e.
        # with a single sensor wired. With several, use the per-sensor form
        # below instead: sleep all, wake one.
        print("  %-6s XSHUT GPIO%-3d -> %s"
              % (P.NAMES[i], P.XSHUT[i], "responds" if not gone else "silenced"))

    print("\n  per-sensor wake test (the definitive one):")
    for i in range(n):
        for p in pins:
            p.value(0)
        time.sleep_ms(P.BOOT_MS)
        pins[i].value(1)
        time.sleep_ms(P.BOOT_MS)
        ok = 0x29 in i2c.scan()
        if not ok:
            bad.append(P.NAMES[i])
        print("    %-6s alone -> %s" % (P.NAMES[i], "OK" if ok else "SILENT"))

    for p in pins:
        p.value(1)

    if bad:
        print("\n  BAD: %s" % ", ".join(bad))
        print("  Each of those is one of: XSHUT wire on the wrong GPIO,")
        print("  VIN/GND not connected, or a dead board. Check in that order.")
    else:
        print("\n  all %d sensors wake individually -- wiring is good" % n)


def characterise(sensors, seconds=10):
    """Noise and drift per sensor. Point them at a fixed surface and wait."""
    n = len(sensors)
    print("\n--- characterise ------------------------------------------")
    print("  hold everything still, aimed at a fixed surface. %d s.\n" % seconds)

    samples = [[] for _ in range(n)]
    t0 = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), t0) < seconds * 1000:
        for i, mm in enumerate(read_all(sensors)):
            if mm is not None:
                samples[i].append(mm)
        time.sleep_ms(P.LOOP_MS)

    print("  %-6s %6s %6s %6s %6s %6s" % ("sensor", "n", "min", "max", "mean", "sd"))
    print("  " + "-" * 42)
    for i in range(n):
        v = samples[i]
        if not v:
            print("  %-6s %6s  no samples" % (P.NAMES[i], 0))
            continue
        mean = sum(v) / len(v)
        var = sum((x - mean) ** 2 for x in v) / len(v)
        sd = var ** 0.5
        print("  %-6s %6d %6d %6d %6.0f %6.1f"
              % (P.NAMES[i], len(v), min(v), max(v), mean, sd))
    print("\n  sd under ~10 mm indoors is healthy. Much more than that means")
    print("  ambient light, a dark/angled target, or a loose connection.")
    return samples


def manifest():
    """Boot manifest. Half-uploading a module set cost a debugging session on
    Node 1 -- ImportError on a name that existed perfectly well in the folder."""
    import vl53l1x
    import zones
    mods = [("pins3", P.VERSION), ("tof3", VERSION)]
    for name, mod in (("vl53l1x", vl53l1x), ("zones", zones)):
        mods.append((name, getattr(mod, "VERSION", "(no VERSION)")))
    try:
        import r307
        mods.append(("r307", getattr(r307, "VERSION", "(no VERSION)")))
    except ImportError:
        # Expected. The R307S was condemned on 24-08-2026 and r307.py is
        # deliberately absent from this bundle. NOT UPLOADED here is the
        # correct reading, not a missing file to go and find.
        mods.append(("r307", "NOT UPLOADED (fingerprint dropped, expected)"))
    print("  modules on board:")
    for name, ver in mods:
        print("    %-10s %s" % (name, ver))
    return mods
