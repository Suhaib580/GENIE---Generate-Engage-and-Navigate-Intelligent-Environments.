"""
node2_main.py — GENIE Node 2. Four VL53L1X + vibration switches -> ESP-NOW.

Save as main.py on Node 2 when you are happy with it. Run with F5 first.

UPLOAD TO THE BOARD (modules):   vl53l1x.py   zones.py   genie_link.py
RUN WITH F5 (this file):         node2_main.py

--------------------------------------------------------------------------
WHAT THIS NODE OWNS
--------------------------------------------------------------------------
Four ToF sensors and the vibration switches. Nothing else. DHT11, MQ-2,
flame and MPU-6050 live on Node 1 and are reported by Node 1 in its JSON
telemetry. Do not read the same sensor on two boards.

--------------------------------------------------------------------------
THE ADDRESS PROBLEM - READ BEFORE YOU BLAME THE CODE
--------------------------------------------------------------------------
Every VL53L1X ships at I2C address 0x29. Four on one bus all answer at once
and the bus returns garbage. bring_up() walks the XSHUT lines to give each
one its own address.

Those addresses live in sensor RAM, NOT flash. Every reset and every power
cycle throws them away. bring_up() therefore runs at the top of EVERY boot.
There is no way around this and it is the single most common way this build
appears to be broken when it is not.
"""
from machine import Pin, I2C
import network
import espnow
import time

import vl53l1x
import zones
import genie_link as link

try:
    import ujson
except ImportError:
    import json as ujson

# ---------------------------------------------------------------- config
NODE1_MAC = b'\xff\xff\xff\xff\xff\xff'   # <-- PUT NODE 1'S STA MAC HERE
# Get it by running this on Node 1:
#     import network; print(network.WLAN(network.STA_IF).config('mac'))
# Broadcast (all ff) works for a first test but every ESP-NOW device in the
# room receives it. Set the real MAC before the demo.

SDA_PIN, SCL_PIN = 8, 9
XSHUT_PINS = (10, 11, 12, 13)             # front, left, right, rear
ADDRESSES  = (0x30, 0x31, 0x32, 0x33)
N_SENSORS  = 4

VIB_PINS   = (14, 15, 21, 47)             # per 00_COMMON/PIN_MAP.md
N_VIB      = 2                            # you have two fitted; set 4 if more

# 100 kHz, NOT 400 kHz. Changed 27-08-2026 after all four sensors started
# failing bring-up with ENODEV on a bus that had worked minutes earlier.
#
# The face board already learned this and wrote it down -- sensors.py:
#
#     I2C_FREQ = 100_000   # 100k, not 400k. Dupont wire + two modules is
#                          # not a 400 kHz bus, and the failure mode is
#                          # silent byte corruption, not an error.
#
# That was TWO modules. This bus has FOUR, on the same dupont wire, sharing
# one pull-up pair. 400 kHz was never going to be reliable here; it was
# marginal, and marginal means "works until the room warms up or the radio
# starts transmitting".
#
# Raise it back only if you move to short soldered leads, and only after
# tof3.characterise() shows a clean yield at the higher speed.
I2C_FREQ   = 100_000
DIST_MODE  = vl53l1x.SHORT                # ~1.3 m, far better in room light

TICK_MS       = 100                       # sensor loop period
HEARTBEAT_MS  = 1000                      # send at least this often
BASELINE_N    = 40                        # samples per sensor at startup

# Settle after raising an XSHUT line before talking to the chip. Matches
# pins3.BOOT_MS. See the note in bring_up() -- this is the ENODEV knob.
BOOT_SETTLE_MS = 15
MIN_MARGIN_MM = 80                        # never trigger on less than this
K_SIGMA       = 6.0
N_CONSEC      = 2                         # frames before a state change sticks
OPEN_TRIGGER  = 1200                      # mm, when a sensor sees nothing at rest

# ---------------------------------------------------------------- helpers
def median(xs):
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def mad_sigma(xs, med):
    """Median absolute deviation -> robust sigma. A plain stdev gets
    destroyed by the outliers a ToF produces; this does not."""
    return median([abs(x - med) for x in xs]) * 1.4826


# ---------------------------------------------------------------- bring-up
def bring_up(i2c):
    """XSHUT walk. Returns (sensors, fault) — sensors may contain None."""
    xs = [Pin(p, Pin.OUT, value=0) for p in XSHUT_PINS]
    time.sleep_ms(20)

    # With every sensor held in reset the bus must be silent. If anything
    # answers, an XSHUT wire is not connected and every address below is a lie.
    if i2c.scan():
        print("!! bus not silent with all XSHUT low - an XSHUT wire is wrong")

    sensors, fault = [], False
    for i in range(N_SENSORS):
        xs[i].value(1)

        # 15 ms, not 5. pins3.py has said this all along:
        #
        #   BOOT_MS = 15   # Datasheet says ~1.2 ms; being stingy here is
        #                  # exactly where flaky ENODEV failures come from.
        #
        # And that is exactly what happened -- all four sensors ENODEV on a
        # bus that had worked minutes before. A chip that has not finished
        # booting does not ACK, and "does not ACK" is ENODEV.
        time.sleep_ms(BOOT_SETTLE_MS)

        # Two attempts. The first one costs nothing when it succeeds, and
        # when it does not, a second try after a further settle recovers a
        # sensor that was merely slow rather than absent. Bring-up runs on
        # every single boot, so making it robust is worth more here than
        # anywhere else in this file.
        err = None
        for attempt in (1, 2):
            try:
                s = vl53l1x.VL53L1X(i2c, 0x29)
                s.set_address(ADDRESSES[i])
                s.init(DIST_MODE)
                s.start()
                sensors.append(s)
                print("  ToF %d (%s) -> 0x%02X  ok%s"
                      % (i, link.SIDE[i], ADDRESSES[i],
                         "" if attempt == 1 else "  (on retry)"))
                err = None
                break
            except Exception as e:
                err = e
                time.sleep_ms(BOOT_SETTLE_MS * 2)

        if err is not None:
            sensors.append(None)
            fault = True
            print("  ToF %d (%s) FAILED: %s" % (i, link.SIDE[i], err))
    return sensors, fault


def read_all(sensors):
    out = []
    for s in sensors:
        if s is None:
            out.append(None)
            continue
        try:
            out.append(s.distance())      # mm, or None on a bad range status
        except OSError:
            out.append(None)              # a dropped read is not a crash
    return out


def calibrate(sensors):
    """Learn the empty environment. Returns per-sensor (mode, baseline, margin).

    WALL  - something is in view at rest (a real wall, GENIE's own body).
            Trigger when a reading comes in CLOSER than baseline - margin.
    OPEN  - nothing in range at rest. Trigger on any valid reading closer
            than OPEN_TRIGGER.
    """
    print("Calibrating - keep the area clear for %d samples..." % BASELINE_N)
    buckets = [[] for _ in range(N_SENSORS)]
    for _ in range(BASELINE_N):
        for i, v in enumerate(read_all(sensors)):
            if v is not None:
                buckets[i].append(v)
        time.sleep_ms(60)

    cal = []
    for i, b in enumerate(buckets):
        yield_pct = 100 * len(b) // BASELINE_N
        if len(b) < BASELINE_N // 2:
            cal.append(("OPEN", OPEN_TRIGGER, 0))
            print("  %-5s OPEN   yield %d%%" % (link.SIDE[i], yield_pct))
            continue
        med = median(b)
        sigma = mad_sigma(b, med)
        margin = max(K_SIGMA * sigma, MIN_MARGIN_MM)
        cal.append(("WALL", med, margin))
        print("  %-5s WALL   base %4d mm  sigma %5.1f  margin %4.0f  yield %d%%"
              % (link.SIDE[i], med, sigma, margin, yield_pct))
        if med < 150 and sigma < 3:
            print("     !! lens is almost certainly obstructed. A ToF cannot")
            print("        see through acrylic, tape or a window. Cut the hole.")
        if yield_pct < 70:
            print("     !! %d%% dropped reads - sunlight, a black target, or a"
                  % (100 - yield_pct))
            print("        marginal I2C bus. Recalibrate in the demo lighting.")
    return cal


def triggered(mm, mode, base, margin, currently):
    """Hysteresis: entering needs the full margin, leaving needs 60% of it.
    Without this the state strobes at the boundary."""
    if mm is None:
        return False
    if mode == "OPEN":
        thr = OPEN_TRIGGER if not currently else OPEN_TRIGGER * 1.15
        return mm < thr
    thr = base - (margin if not currently else margin * 0.6)
    return mm < thr


# ---------------------------------------------------------------- main
def main(uplink=None):
    """Range, and send.

    `uplink`, if given, is a net_uplink.Uplink and gets the same readings
    as a JSON line straight to the laptop -- IN ADDITION to the ESP-NOW
    frame, never instead of it. Node 1 still needs the frame to draw
    distances on the TFT, and the direct line is what keeps the ToF array
    reporting when Node 1 is off or has crashed. main.py sets it up.
    """
    print("GENIE Node 2 - ToF array")
    i2c = I2C(0, sda=Pin(SDA_PIN), scl=Pin(SCL_PIN), freq=I2C_FREQ)

    sensors, fault = bring_up(i2c)
    if all(s is None for s in sensors):
        # Return, do not raise SystemExit. SystemExit does not inherit from
        # Exception, so main.py's handler could not catch it -- the board
        # simply soft-rebooted and the advice below never appeared.
        print()
        print("  NO ToF SENSOR CAME UP. In order of likelihood:")
        print()
        print("    1. POWER. Four ToF boards draw ~40 mA each while ranging,")
        print("       and 5 V is wrong -- VIN must be 3V3, the S3 is not 5 V")
        print("       tolerant. USB alone often will not carry this.")
        print("    2. A BROKEN WIRE. Dupont wires fail INSIDE the insulation")
        print("       and test dead while looking perfect. Replace it, do")
        print("       not re-seat it. This project has hit that twice.")
        print("    3. PULL-UPS. ONE 4.7k pair for the whole bus, SDA->3V3")
        print("       and SCL->3V3. Four boards each with their own makes")
        print("       the bus too stiff to drive.")
        print()
        print("  Power-cycle (not Ctrl-D), then run the diagnostic that")
        print("  names the bad wire:")
        print("      import tof3; tof3.xshut_diagnostic()")
        print()
        return

    cal = calibrate(sensors)
    trackers = [zones.ZoneTracker(link.SIDE[i]) for i in range(N_SENSORS)]
    vib = [Pin(VIB_PINS[i], Pin.IN) for i in range(N_VIB)]

    # ------------------------------------------------------------------
    # JOIN THE SAME AP AS NODE 1. Read this before changing it back.
    # ------------------------------------------------------------------
    # This used to be sta.disconnect(), with the comment "ESP-NOW needs no
    # AP association". That was true right up until 27-08-2026, when Node 1
    # started joining GENIE1 to reach the laptop over WiFi.
    #
    # ESP-NOW and WiFi share ONE radio. An associated board is locked to
    # its AP's channel; an unassociated one is on whatever channel it was
    # left on. Mismatch and every frame is dropped SILENTLY -- this board
    # sends happily, Node 1 receives nothing, and neither prints an error.
    # That is the classic "worked on the bench, not in the lab".
    #
    # Joining the same AP puts both boards on whatever channel it chose,
    # and keeps them together when the router reboots onto a different one.
    # Pinning a channel number by hand does not survive that.
    #
    # If wifi_cfg.py is not uploaded, or the AP is unreachable, this falls
    # back to the old unassociated behaviour -- which still works fine as
    # long as Node 1 is not on WiFi either.
    sta = network.WLAN(network.STA_IF)
    sta.active(True)
    try:
        import wifi_cfg
        wifi_cfg.connect(log=print)
        if not sta.isconnected():
            print("!! not associated - ESP-NOW will only reach Node 1 if it")
            print("   is also unassociated. Check the AP before the demo.")
    except ImportError:
        sta.disconnect()
        print("!! wifi_cfg.py not uploaded. If Node 1 is on WiFi, this link")
        print("   will drop every frame with no error. Upload wifi_cfg.py.")

    e = espnow.ESPNow()
    e.active(True)
    e.add_peer(NODE1_MAC)
    print("ESP-NOW up. My MAC:", sta.config('mac'),
          " channel:", sta.config('channel'))
    print("   Node 1 must report the SAME channel number.")
    if NODE1_MAC == b'\xff\xff\xff\xff\xff\xff':
        print("!! still broadcasting - set NODE1_MAC before the demo")

    seq = 0
    state = [False] * N_SENSORS
    streak = [0] * N_SENSORS
    last_send = -HEARTBEAT_MS

    while True:
        now = time.ticks_ms()
        mm = read_all(sensors)

        hit = 0
        changed = False
        for i in range(N_SENSORS):
            trackers[i].update(mm[i])
            want = triggered(mm[i], cal[i][0], cal[i][1], cal[i][2], state[i])
            if want != state[i]:
                streak[i] += 1
                if streak[i] >= N_CONSEC:
                    state[i] = want
                    streak[i] = 0
                    changed = True
            else:
                streak[i] = 0
            if state[i]:
                hit |= 1 << i

        vib_mask = 0
        for i, p in enumerate(vib):
            if p.value():
                vib_mask |= 1 << i

        flags = link.F_CALIBRATED
        if fault:
            flags |= link.F_TOF_FAULT
        if vib_mask:
            flags |= link.F_KNOCK

        # A state change is an EVENT and goes out now. Presence and knock are
        # on the rule-based path; they must not wait for the next heartbeat.
        due = time.ticks_diff(now, last_send) >= HEARTBEAT_MS
        if changed or vib_mask or due:
            frame = link.pack(seq, mm, hit, vib_mask, flags,
                              link.MSG_EVENT if (changed or vib_mask)
                              else link.MSG_TELEMETRY)
            try:
                e.send(NODE1_MAC, frame, False)   # False = do not block on ack
            except OSError as err:
                print("ESP-NOW send failed:", err)

            # Direct line to the laptop, when one is configured. Marked
            # "tof" rather than "telemetry" so the laptop updates ONLY the
            # three ToF channels and leaves the face node's smoke, flame
            # and DHT readings alone -- this board knows nothing about
            # those, and a message that looked like full telemetry would
            # blank them.
            if uplink is not None:
                try:
                    uplink.send(ujson.dumps({
                        "t": "tof",
                        "seq": seq,
                        "tof_mm": mm,
                        "present": bool(hit),
                        "knock": bool(vib_mask),
                        "tof_fault": bool(fault),
                        "node2_link": True,
                    }))
                except Exception as err:
                    print("uplink send failed:", err)

            last_send = now
            seq = (seq + 1) & 0xFF
            print("%s  %s" % (
                " ".join("%-5s %s" % (link.SIDE[i],
                         "----" if mm[i] is None else "%4d" % mm[i])
                         for i in range(N_SENSORS)),
                "".join("#" if state[i] else "." for i in range(N_SENSORS))))

        time.sleep_ms(TICK_MS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("stopped")
