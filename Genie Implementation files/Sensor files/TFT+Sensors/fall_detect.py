"""
Fall detection on the MPU-6050 — ESP32-S3 + MicroPython

    VCC -> 3V3 | GND -> GND | SCL -> GPIO38 | SDA -> GPIO47

THE ALGORITHM
    A real fall has a three-stage signature that ordinary movement does not:

      1. FREE-FALL   total acceleration collapses toward 0 g as the body
                     stops being supported (0.3-0.6 g, 100-400 ms)
      2. IMPACT      a sharp spike, 2-6 g, as the body hits the floor
      3. STILLNESS   they do not get up, and their orientation has changed

    Requiring all three in sequence is what separates a fall from setting the
    sensor down hard or dropping a phone.

STILLNESS IS MEASURED AGAINST GRAVITY, NOT A SNAPSHOT
    An object at rest reads exactly 1.0 g, because gravity is the only force
    on it. So "still" means |magnitude - 1.0| stays small. Comparing against
    a sample grabbed at the end of the impact instead is wrong - that sample
    is taken mid-bounce and is not 1 g, so everything afterwards looks like
    movement. That was the original bug.

    Stillness is also scored over the whole window rather than aborted on the
    first outlier. Real floors bounce, and wires wobble.

WHAT THIS IS NOT
    A credible demonstrator, not a medical device. Tuned on one person, one
    surface, one mounting position. Say so plainly in the report.
"""
from machine import I2C, Pin
import time
import math

PIN_SCL = 38
PIN_SDA = 47

REG_PWR_MGMT_1   = 0x6B
REG_SMPLRT_DIV   = 0x19
REG_CONFIG       = 0x1A
REG_ACCEL_XOUT_H = 0x3B
ACCEL_LSB_PER_G  = 16384.0

# --- thresholds - retune on your own hardware ---------------------------
FREEFALL_G       = 0.55     # below this = unsupported
IMPACT_G         = 2.00     # above this = hit something
FREEFALL_MIN_MS  = 80       # ignore blips shorter than this
FREEFALL_MAX_MS  = 1200     # longer than this is waving, not falling
SETTLE_MS        = 600      # let the bounce die down before judging stillness
STILL_CONFIRM_MS = 2000     # how long they must stay down
STILL_TOL_G      = 0.25     # |magnitude - 1.0| under this counts as still
STILL_FRACTION   = 0.75     # this share of the window must be still
MOVE_ABORT_N     = 15       # consecutive moving samples = they got up (~300 ms)
TILT_CHANGE_DEG  = 30.0     # orientation must have changed by this much

SAMPLE_MS = 20              # 50 Hz

i2c = I2C(0, scl=Pin(PIN_SCL), sda=Pin(PIN_SDA), freq=400000)
addr = 0x68


def setup() -> None:
    global addr
    found = i2c.scan()
    if 0x68 in found:
        addr = 0x68
    elif 0x69 in found:
        addr = 0x69
    else:
        raise OSError("MPU-6050 not found. Bus shows: {}".format([hex(a) for a in found]))
    i2c.writeto_mem(addr, REG_PWR_MGMT_1, b'\x00')
    time.sleep_ms(100)
    i2c.writeto_mem(addr, REG_SMPLRT_DIV, b'\x07')
    i2c.writeto_mem(addr, REG_CONFIG, b'\x03')
    print("MPU-6050 ready at {}\n".format(hex(addr)))


def _s16(hi: int, lo: int) -> int:
    v = (hi << 8) | lo
    return v - 65536 if v & 0x8000 else v


def read_accel():
    d = i2c.readfrom_mem(addr, REG_ACCEL_XOUT_H, 6)
    return (_s16(d[0], d[1]) / ACCEL_LSB_PER_G,
            _s16(d[2], d[3]) / ACCEL_LSB_PER_G,
            _s16(d[4], d[5]) / ACCEL_LSB_PER_G)


def magnitude(a) -> float:
    return math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])


def tilt_deg(a) -> float:
    """Angle between the sensor's Z axis and gravity. 0 = flat, 90 = on edge."""
    m = magnitude(a)
    if m < 0.01:
        return 0.0
    z = max(-1.0, min(1.0, a[2] / m))
    return math.degrees(math.acos(z))


IDLE, FREEFALL, IMPACTED, CONFIRMING = 0, 1, 2, 3


def main() -> None:
    setup()

    print("Baseline orientation - hold the sensor as it will be worn...")
    time.sleep(2)
    base_tilt = 0.0
    for _ in range(25):
        base_tilt += tilt_deg(read_accel())
        time.sleep_ms(20)
    base_tilt /= 25
    print("Resting tilt = {:.0f} deg\n".format(base_tilt))

    print("Monitoring. Drop it 20-30 cm onto a cushion, then LET GO and")
    print("leave it alone for 3 seconds. Ctrl+C to stop.\n")

    state = IDLE
    t_state = time.ticks_ms()
    peak_impact = 0.0
    still_n = total_n = moving_run = 0
    m_lo = m_hi = 1.0
    falls = 0

    while True:
        a = read_accel()
        m = magnitude(a)
        now = time.ticks_ms()
        elapsed = time.ticks_diff(now, t_state)

        if state == IDLE:
            if m < FREEFALL_G:
                state, t_state = FREEFALL, now
                print("  ... free-fall  {:.2f}g".format(m))

        elif state == FREEFALL:
            if m > IMPACT_G and elapsed >= FREEFALL_MIN_MS:
                state, t_state = IMPACTED, now
                peak_impact = m
                print("  ... impact     {:.2f}g".format(m))
            elif elapsed > FREEFALL_MAX_MS:
                print("  ... too long, not a fall")
                state = IDLE
            elif m > FREEFALL_G and elapsed < FREEFALL_MIN_MS:
                state = IDLE

        elif state == IMPACTED:
            if m > peak_impact:
                peak_impact = m
            if elapsed > SETTLE_MS:
                state, t_state = CONFIRMING, now
                still_n = total_n = moving_run = 0
                m_lo, m_hi = m, m
                print("  ... settled, watching {}ms for stillness".format(STILL_CONFIRM_MS))

        elif state == CONFIRMING:
            total_n += 1
            if m < m_lo:
                m_lo = m
            if m > m_hi:
                m_hi = m

            if abs(m - 1.0) <= STILL_TOL_G:
                still_n += 1
                moving_run = 0
            else:
                moving_run += 1

            if moving_run >= MOVE_ABORT_N:
                print("  ... sustained movement - they got up. Not a fall.\n")
                state = IDLE

            elif elapsed >= STILL_CONFIRM_MS:
                frac = still_n / total_n if total_n else 0.0
                new_tilt = tilt_deg(a)
                delta = abs(new_tilt - base_tilt)
                still_ok = frac >= STILL_FRACTION
                tilt_ok = delta >= TILT_CHANGE_DEG

                if still_ok and tilt_ok:
                    falls += 1
                    print("\n" + "=" * 48)
                    print("  FALL DETECTED  (#{})".format(falls))
                    print("  impact peak    {:.2f} g".format(peak_impact))
                    print("  tilt change    {:.0f} -> {:.0f} deg".format(base_tilt, new_tilt))
                    print("  stillness      {:.0f}% of window".format(frac * 100))
                    print("  ACTION: haptic alarm + TTS + SMS via SIM900A")
                    print("=" * 48 + "\n")
                else:
                    # Print the numbers either way - a rejection is tuning data.
                    print("  ... no fall logged")
                    print("      stillness {:.0f}% (need {:.0f}%)  {}".format(
                        frac * 100, STILL_FRACTION * 100, "OK" if still_ok else "<-- failed"))
                    print("      tilt {:.0f} -> {:.0f} = {:.0f} deg (need {:.0f})  {}".format(
                        base_tilt, new_tilt, delta, TILT_CHANGE_DEG,
                        "OK" if tilt_ok else "<-- failed"))
                    print("      magnitude ranged {:.2f} - {:.2f} g\n".format(m_lo, m_hi))
                state = IDLE

        time.sleep_ms(SAMPLE_MS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
    except OSError as e:
        print("\nI2C error: {}".format(e))
