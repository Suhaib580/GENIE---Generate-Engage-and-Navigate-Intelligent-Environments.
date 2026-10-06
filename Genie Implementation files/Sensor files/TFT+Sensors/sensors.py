"""
sensors.py - one reader for every GENIE Node-1 sensor.

Everything is polled on its OWN schedule and cached, so read() never blocks.
The DHT11 in particular refuses to answer faster than once per 2 s, and a
blocking read there would freeze the eyes and the button.

    import sensors
    s = sensors.Sensors()
    while True:
        s.tick()                      # non-blocking, self-paced
        print(s.text("flame"))

Node 2 (ToF x4) is a SEPARATE ESP32. Its values arrive
over ESP-NOW, so they live here as remote slots that read "--" until
something calls set_remote(). Nothing else in the UI needs to know the
difference - a remote channel looks exactly like a local one.

THE MQ-2 GROUND REFERENCE - solved 21-08-2026, keep it that way
    The MQ-2's AO is a VOLTAGE, and a voltage only means something relative
    to a reference. That reference is the ESP32's own GND pin, because that
    is what the ADC measures against.

    While the sensor ground reached the ESP32 only the long way round -
    through the LED strip's return path and the supply - every milliamp the
    strip drew shifted the sensor's ground relative to the chip's, and the
    ADC faithfully reported the difference. Readings swung 1713..2972 in
    clean air, plus and minus 25%.

    Running ESP32 GND directly into the sensor/power junction collapsed that
    to about plus and minus 0.5%. A 40x improvement from one wire, and it
    also dropped the absolute reading from ~2300 to ~840 - the old number
    was mostly ground offset, not gas.

    KEEP THAT WIRE. It is the single most important connection on the
    analog side, and it is invisible on a schematic that only shows "GND".

    If the occasional 3-5% single-sample spike ever matters, 100 nF from
    each AO pin to GND at the ESP32 end will take it out - the AO divider is
    tens of kilohms and the ADC's sampling capacitor cannot refill from that
    in the microseconds it has. Optional now, not required.
"""

from machine import Pin, ADC, I2C
import json
import time

# ---- pins (P2 header, matches pins.py) -----------------------------------
MQ2_1, MQ2_2 = 1, 2
FLAME = 42
SW_TOP, SW_BOT = 41, 40
DHT_PIN = 21
I2C_SCL, I2C_SDA = 38, 47
IMU_A, IMU_B = 0x68, 0x69

I2C_FREQ = 100_000      # 100k, not 400k. Dupont wire + two modules is not a
                        # 400 kHz bus, and the failure mode is silent byte
                        # corruption, not an error you would notice.

KNOCK_DEBOUNCE_MS = 120
MQ2_ALERT = 1.20
MQ2_ALARM = 1.50

# ---- noise rejection ------------------------------------------------------
# A bench run in clean air swung 1713..2972 around a 2363 baseline - plus and
# minus 25%, sample to sample, on a sensor whose real signal moves over
# SECONDS. A 1.20x threshold sitting in that much noise fires on nothing, and
# it did: eight false alerts in sixty readings with no smoke in the room.
#
# Three layers, cheapest first:
#   MEDIAN   throws away spikes outright. A mean lets one bad sample drag
#            the result; a median ignores it completely.
#   EMA      gas events take seconds, so a ~1 s time constant costs nothing
#            real and removes what is left.
#   CONFIRM  an alarm must survive N consecutive polls. One sample over the
#            line is noise; three in a row is a measurement. Exactly the
#            same reasoning as the SW-420 knock debounce.
#
# None of this substitutes for the hardware fix - see the header note.

# ---- slow baseline tracking -----------------------------------------------
# Slow baseline tracking. An MQ-2's resistance creeps for HOURS after power
# up - a bench run drifted 836 -> 867, about 4%, in ninety seconds, and it
# was still climbing. Left alone that creep eventually walks a healthy
# sensor past the 1.20x line and GENIE cries smoke at 3 a.m. with nothing
# burning.
#
# So the baseline follows the drift, very slowly, and ONLY while the channel
# is calm and close to where it already was. A real gas event moves in
# seconds and lands outside that window, so it can never be absorbed - which
# is the whole trick. Adapt too fast and the sensor learns to ignore smoke.
DRIFT_ALPHA = 0.0006    # ~5 min time constant at a 200 ms poll
DRIFT_BAND = 0.15       # only track while within +-15% of baseline

ADC_SAMPLES = 15        # odd, so the median is a real sample
SMOKE_ALPHA = 0.15      # EMA weight for each new reading
CONFIRM_N = 3           # consecutive polls before a status level changes

PWR_MGMT_1 = 0x6B
ACCEL_XOUT_H = 0x3B

# key, label shown on screen, poll interval ms
CHANNELS = (
    ("smoke1",  "SMOKE 1",   200),
    ("smoke2",  "SMOKE 2",   200),
    ("flame",   "FLAME",      50),
    ("temp",    "TEMP",     2200),
    ("hum",     "HUMIDITY", 2200),
    ("knock1",  "KNOCK TOP",  50),
    ("knock2",  "KNOCK BOT",  50),
    ("imu1",    "TILT TOP",  120),
    ("imu2",    "TILT BOT",  120),
    ("tof",     "DISTANCE",    0),     # remote, Node 2
)

REMOTE = ("tof",)

# Calibration survives a reboot. Without this every power cycle threw away
# the clean-air baseline and the IMU scale, so a demo meant a 90 second
# warm-up in front of whoever was watching. Calibrate once, on the bench.
CAL_FILE = "genie_cal.json"


def _make_adc(pin):
    a = ADC(Pin(pin))
    try:
        a.atten(ADC.ATTN_11DB)
    except AttributeError:
        pass
    if hasattr(a, "read"):
        try:
            a.width(ADC.WIDTH_12BIT)
        except AttributeError:
            pass
        return a.read
    return lambda: a.read_u16() >> 4


class _Knock:
    """Edge counter with an ISR-side debounce.

    The SW-420 is a rolling-ball switch. One tap is a BURST of edges, not one
    edge, so a raw counter over-reads by 5-20x. The window below is what makes
    the number mean 'knocks'. It also means an idle-LOW module still works -
    edges are what count, not level.
    """

    def __init__(self, pin):
        self.count = 0
        self._last = 0
        self.pin = Pin(pin, Pin.IN)
        self.pin.irq(trigger=Pin.IRQ_FALLING | Pin.IRQ_RISING,
                     handler=self._isr)

    def _isr(self, _):
        now = time.ticks_ms()
        if time.ticks_diff(now, self._last) < KNOCK_DEBOUNCE_MS:
            return
        self._last = now
        self.count += 1

    def stop(self):
        self.pin.irq(handler=None)


class Sensors:

    def __init__(self):
        self.v = {}                     # key -> raw value or None
        self._next = {}                 # key -> ticks_ms of next poll
        self.errors = {}

        self._a1 = _make_adc(MQ2_1)
        self._a2 = _make_adc(MQ2_2)
        self.tilt_zero = None           # resting angle, set on first reading
        self.imu_gain = {}              # per-module scale correction
        self._levelled = False
        self._ema = {}                  # smoothed smoke values
        self._st = {}                   # confirmed status per channel
        self._conf = {}                 # consecutive polls disagreeing with it
        self.base1 = None               # clean-air baselines
        self.base2 = None

        self._flame = Pin(FLAME, Pin.IN)
        self._k1 = _Knock(SW_TOP)
        self._k2 = _Knock(SW_BOT)

        self._dht = None
        try:
            import dht
            self._dht = dht.DHT11(Pin(DHT_PIN))
        except Exception as e:
            self.errors["temp"] = repr(e)

        self._i2c = None
        self.imus = []
        try:
            self._i2c = I2C(0, scl=Pin(I2C_SCL), sda=Pin(I2C_SDA),
                            freq=I2C_FREQ)
            for a in (IMU_A, IMU_B):
                if a in self._i2c.scan():
                    self._i2c.writeto_mem(a, PWR_MGMT_1, b'\x00')
                    self.imus.append(a)
            time.sleep_ms(60)
        except Exception as e:
            self.errors["imu1"] = repr(e)

        now = time.ticks_ms()
        for key, _, every in CHANNELS:
            self.v[key] = None
            self._next[key] = now
            if every:
                self._next[key] = time.ticks_add(now, 10)

        self.load_cal()

    # -- calibration that survives a reboot ---------------------------------
    def save_cal(self):
        d = {"base1": self.base1, "base2": self.base2,
             "tilt_zero": list(self.tilt_zero) if self.tilt_zero else None,
             "imu_gain": self.imu_gain}
        try:
            with open(CAL_FILE, "w") as f:
                json.dump(d, f)
            print("  saved to %s - survives a reboot" % CAL_FILE)
            return True
        except Exception as e:
            print("  could not save %s: %r" % (CAL_FILE, e))
            return False

    def load_cal(self):
        """Restore baseline, tilt zero and IMU gains from the last run."""
        try:
            with open(CAL_FILE) as f:
                d = json.load(f)
        except Exception:
            return False
        self.base1 = d.get("base1")
        self.base2 = d.get("base2")
        tz = d.get("tilt_zero")
        self.tilt_zero = tuple(tz) if tz else None
        self.imu_gain = d.get("imu_gain") or {}
        self._levelled = bool(self.imu_gain) and self.tilt_zero is not None
        if self.base1:
            self._ema["smoke1"] = self.base1
        if self.base2:
            self._ema["smoke2"] = self.base2
        print("  calibration loaded: base %s/%s  gains %s"
              % (self.base1, self.base2, self.imu_gain))
        if not (self.base1 and self.base2):
            print("  NO SMOKE BASELINE - smoke reads raw counts and can")
            print("  never alarm. Run: genie_menu.run() -> 9")
        return True

    def calibrated(self):
        return bool(self.base1 and self.base2 and self.imu_gain)

    # -- baselines ----------------------------------------------------------
    def calibrate(self, samples=40):
        """Clean-air reference for the MQ-2 ratio. Call AFTER the warm-up.

        Uses a median of many samples, not a mean. A baseline is the one
        number every later reading is divided by, so a single spike caught
        during calibration biases every ratio for the rest of the run - and
        it looks like a real drift, not like a bad baseline, which is what
        makes it expensive to find.

        Also reports the spread. If max/min is worse than about 1.15 the
        channel is too noisy to threshold at 1.20x, and no amount of
        filtering will make that safe - fit the 100 nF cap first.
        """
        out = []
        for a in (self._a1, self._a2):
            s = sorted(a() for _ in range(samples))
            lo, hi, mid = s[0], s[-1], s[len(s) // 2]
            spread = hi / max(1, lo)
            out.append((max(1, mid), lo, hi, spread))
        self.base1 = out[0][0]
        self.base2 = out[1][0]
        self._ema["smoke1"] = self.base1
        self._ema["smoke2"] = self.base2
        for i, (mid, lo, hi, spread) in enumerate(out):
            note = "OK" if spread < 1.15 else \
                   "TOO NOISY to threshold at %.2fx - fit the cap" % MQ2_ALERT
            print("  MQ-2 #%d  baseline %d   spread %d..%d = %.2fx   %s"
                  % (i + 1, mid, lo, hi, spread, note))
        self.save_cal()
        return self.base1, self.base2

    # -- polling ------------------------------------------------------------
    def tick(self):
        """Poll whatever is due. At most one slow device per call."""
        now = time.ticks_ms()
        if not self._levelled and self.v.get("imu1") is not None:
            # One automatic level(), as soon as an IMU has answered once.
            # save=False: this runs on EVERY boot, and writing flash every
            # boot wears it out for no gain. Only an explicit level() - or
            # calibrate() - is worth committing to storage.
            self._levelled = True
            self.level(save=False)
        slow_done = False
        for key, _, every in CHANNELS:
            if not every or time.ticks_diff(now, self._next[key]) < 0:
                continue
            if every >= 1000:
                if slow_done:
                    continue
                slow_done = True
            self._poll(key)
            self._next[key] = time.ticks_add(time.ticks_ms(), every)

    def _poll(self, key):
        try:
            if key == "smoke1":
                self.v[key] = self._smooth(key, self._median(self._a1))
                self._track(key)
            elif key == "smoke2":
                self.v[key] = self._smooth(key, self._median(self._a2))
                self._track(key)
            elif key == "flame":
                self.v[key] = self._flame.value()
            elif key == "knock1":
                self.v[key] = self._k1.count
            elif key == "knock2":
                self.v[key] = self._k2.count
            elif key in ("temp", "hum"):
                if self._dht:
                    self._dht.measure()
                    self.v["temp"] = self._dht.temperature()
                    self.v["hum"] = self._dht.humidity()
            elif key in ("imu1", "imu2"):
                idx = 0 if key == "imu1" else 1
                if len(self.imus) > idx:
                    self.v[key] = self._accel(self.imus[idx])
            self.errors.pop(key, None)
        except Exception as e:
            self.v[key] = None
            self.errors[key] = repr(e)
        self._confirm(key)
        if key == "temp":
            self._confirm("hum")

    def _median(self, rd, n=ADC_SAMPLES):
        """Median, not mean. One bad conversion drags a mean; a median
        ignores it entirely, which is the whole point on a noisy ADC."""
        s = sorted(rd() for _ in range(n))
        return s[n // 2]

    def _track(self, key):
        """Let the clean-air baseline follow slow sensor drift.

        Three gates, all of which must pass:
          - a baseline exists (nothing to track otherwise)
          - the channel is currently calm
          - the reading is within DRIFT_BAND of the baseline

        Anything that looks like gas fails the last two and is left alone.
        """
        base = self.base1 if key == "smoke1" else self.base2
        v = self.v.get(key)
        if not base or v is None or self._st.get(key, 0) != 0:
            return
        if abs(v - base) > base * DRIFT_BAND:
            return
        base = base + (v - base) * DRIFT_ALPHA
        if key == "smoke1":
            self.base1 = base
        else:
            self.base2 = base

    def _smooth(self, key, raw):
        """Exponential moving average. Gas moves over seconds; this does
        not slow down anything real, and it removes what the median left."""
        p = self._ema.get(key)
        v = raw if p is None else int(p + (raw - p) * SMOKE_ALPHA)
        self._ema[key] = v
        return v

    def _accel(self, addr):
        d = self._i2c.readfrom_mem(addr, ACCEL_XOUT_H, 6)
        out = []
        for i in range(0, 6, 2):
            r = (d[i] << 8) | d[i + 1]
            out.append((r - 65536 if r > 32767 else r) / 16384.0)
        return out

    # -- remote (Node 2 over ESP-NOW) ---------------------------------------
    def set_remote(self, key, value):
        if key in REMOTE:
            self.v[key] = value

    # -- presentation -------------------------------------------------------
    def label(self, key):
        for k, lab, _ in CHANNELS:
            if k == key:
                return lab
        return key.upper()

    def text(self, key):
        """One short string for the big readout. Never longer than 12 chars."""
        v = self.v.get(key)
        if key in self.errors:
            return "ERR"
        if v is None:
            return "--"
        if key == "smoke1":
            return self._ratio(v, self.base1)
        if key == "smoke2":
            return self._ratio(v, self.base2)
        if key == "flame":
            return "CLEAR" if v else "FLAME!"
        if key == "temp":
            return "%d C" % v
        if key == "hum":
            return "%d %%" % v
        if key in ("knock1", "knock2"):
            return "%d taps" % v
        if key in ("imu1", "imu2"):
            return "%.2f g" % self._mag(key)
        return str(v)

    def _ratio(self, raw, base):
        if not base:
            return "%d raw" % raw
        return "%.2f x" % (raw / base)

    def _rate(self, key):
        """Raw, instantaneous status. Never call this directly - it is one
        sample's opinion, and one sample is not a measurement."""
        v = self.v.get(key)
        if key in self.errors:
            return 1
        if v is None:
            return 0
        if key == "flame":
            return 2 if v == 0 else 0
        if key in ("smoke1", "smoke2"):
            base = self.base1 if key == "smoke1" else self.base2
            if not base:
                return 0
            q = v / base
            return 2 if q > MQ2_ALARM else (1 if q > MQ2_ALERT else 0)
        if key in ("imu1", "imu2"):
            m = self._mag(key)
            return 2 if m > 1.8 else (1 if m < 0.7 else 0)
        return 0

    def _confirm(self, key):
        """Advance the confirmed status. Called ONCE per poll.

        A level only changes after CONFIRM_N consecutive polls agree on the
        new one. Escalation and de-escalation both need it, so a single
        spike cannot raise an alarm and a single good sample cannot clear
        one. This is what stops a dangling IMU on a ribbon cable from
        throwing an ANGRY face every time it swings.

        Deliberately NOT inside status(): status() is called several times
        per frame by the UI, and counting there would confirm an alarm in
        one pass of the render loop.
        """
        raw = self._rate(key)
        held = self._st.get(key, 0)
        if raw == held:
            self._conf[key] = 0
            return
        c = self._conf.get(key, 0) + 1
        if c >= CONFIRM_N:
            self._st[key] = raw
            c = 0
        self._conf[key] = c

    def status(self, key):
        """0 ok, 1 warn, 2 alarm. Confirmed, not instantaneous.

        NOT-YET-READ IS NOT A WARNING. Every channel starts as None, so an
        earlier version put the whole board in warning for the first two
        seconds of every boot, and GENIE's first expression was always a
        worried one. A channel that has genuinely FAILED lands in
        self.errors, and offline() reports those.
        """
        if key in REMOTE:
            return self._rate(key)
        return self._st.get(key, 0)

    def worst(self):
        """Highest status across everything. The face reacts to this."""
        w = 0
        for key, _, every in CHANNELS:
            if every:
                w = max(w, self.status(key))
        return w

    def offline(self):
        """Local channels that are actually FAILING, not merely unread.

        Remote channels are excluded - tof reads None until Node 2
        sends something, and that is normal, not a fault.
        """
        return [k for k, _, every in CHANNELS
                if every and k in self.errors]

    def mood(self):
        """The expression, chosen ONLY from what the sensors say.

        Returns one of: "alarm", "offline", "knock", "warn", "calm".
        genie_ui maps those onto RoboEyes moods - this file deliberately
        knows nothing about eyes, so the two can change independently.

        "calm" is the resting state and it is a POSITIVE one. Nothing here
        returns an unhappy face because time passed; only because a sensor
        measured something. That is the whole design rule.

        Priority order matters, and an alarm has to come first - otherwise
        a knock two seconds ago keeps the face surprised while the room
        fills with smoke.
        """
        if self.v.get("flame") == 0:
            return "alarm"

        worst = self.worst()
        if worst >= 2:
            return "alarm"

        if self.offline():
            return "offline"       # a module stopped answering: concern,
                                   # not alarm, and not annoyance

        now = time.ticks_ms()
        for k in (self._k1, self._k2):
            if k._last and time.ticks_diff(now, k._last) < 2500:
                return "knock"

        if worst == 1:
            return "warn"

        return "calm"

    # ---- tilt --------------------------------------------------------------
    TILT_GAIN = 2.4

    def level(self, save=True):
        """Zero the tilt, and correct each IMU's scale. Board must be STILL.

        Two jobs:

        ZERO - raw accelerometer X on a board mounted at an angle reads a
        constant -0.8, and feeding that to eyes.look() pins the pupils hard
        to one side forever. A fixed stare is what reads as annoyed, not the
        mood. Zeroing makes "level" wherever GENIE actually lives.

        GAIN - at rest, |a| MUST be 1.00 g on every module, because the only
        force acting is gravity. A bench run had 0x68 reading a steady 0.83
        and 0x69 reading 1.03: the first module is 17% low. That is not
        noise (it never varies), it is a scale error on a cheap part, and it
        would put the same 17% error straight into any fall-detection
        threshold. One number per module fixes it.

        Called automatically once at startup. Re-run it by hand after
        re-mounting anything.
        """
        a = self.v.get("imu1") or self.v.get("imu2")
        if a:
            self.tilt_zero = (a[0], a[1])
        for key in ("imu1", "imu2"):
            v = self.v.get(key)
            if not v:
                continue
            m = (v[0] ** 2 + v[1] ** 2 + v[2] ** 2) ** 0.5
            if 0.55 < m < 1.6:          # plausible for a stationary board
                self.imu_gain[key] = 1.0 / m
            else:
                print("  %s reads |a|=%.2f - too far off to calibrate. Hold"
                      " the board still and call level() again." % (key, m))
        if self.imu_gain:
            print("  levelled. gains %s" % self.imu_gain)
            if save:
                self.save_cal()
        return self.tilt_zero, dict(self.imu_gain)

    def _mag(self, key):
        """Corrected |a| in g. 1.00 at rest on a module that has been
        levelled, whatever that particular chip's scale error is."""
        v = self.v.get(key)
        if not v:
            return None
        m = (v[0] ** 2 + v[1] ** 2 + v[2] ** 2) ** 0.5
        return m * self.imu_gain.get(key, 1.0)

    def tilt(self):
        """Deviation from the resting angle, as (dx, dy) in -1.0 .. 1.0.

        Relative, not absolute. (None, None) when no IMU is answering, which
        tells the UI to fall back on RoboEyes' own idle wander.
        """
        a = self.v.get("imu1") or self.v.get("imu2")
        if not a:
            return (None, None)
        if self.tilt_zero is None:
            self.tilt_zero = (a[0], a[1])
            return (0.0, 0.0)
        zx, zy = self.tilt_zero
        g = self.TILT_GAIN
        return (max(-1.0, min(1.0, (a[0] - zx) * g)),
                max(-1.0, min(1.0, (a[1] - zy) * g)))

    def stop(self):
        self._k1.stop()
        self._k2.stop()
