"""
pins.py — single source of truth for the GENIE node pin map.

Every test file imports from here. Change a pin once, here, and nothing else
goes stale. This exists because the old PIN_MAP.md and display_cfg.h disagreed
on five pins and cost two days.

Upload FLAT to the device root along with everything else — the imports are
flat, a folder breaks them.

===========================================================================
THE RULE:  P1 = display.  P2 = sensors.  Nothing crosses.
===========================================================================

P1 is full: 13 TFT signals + 3V3 + GND, 15 wires, frozen and working. The only
thing still free on P1 is a second 3V3 pin, which is where the sensor rail is
fed from.

EVERY sensor now lives on P2. Revised 12-08-2026 — flame and SW-420-top used
to be on GPIO13/GPIO14, which are P1 pins buried under the display bundle.
They moved to GPIO42/GPIO41.

P2 header, counting from the G end (the one near TX/RX):

    pos  label   used for
     1   G       ground rail        <- rail feed
     2   TX      leave open (serial console)
     3   RX      leave open
     4   1       MQ-2 sensor 1  AO      ADC1
     5   2       MQ-2 sensor 2  AO      ADC1
     6   42      flame DO
     7   41      SW-420 top
     8   40      SW-420 bottom
     9   39      THE BUTTON
    10   38      I2C SCL
    11   37      PSRAM  - never use
    12   36      PSRAM  - never use
    13   35      PSRAM  - never use
    14   0       free - onboard BOOT button lives here, no wiring needed
    15   45      strapping - leave open
    16   48      WS2812 strip data (also the onboard RGB LED)
    17   47      I2C SDA
    18   21      DHT11 DATA
    19   20      native USB - leave open
    20   19      native USB - leave open
    21   G       ground rail
    22   G       ground rail

Sensor signals occupy positions 4-10 contiguously, then 17 and 18. Wire them
in header order and a misplaced jumper is obvious at a glance.
"""

# ---- display, frozen. Do not reuse any of these. ----------------------
TFT_PINS = (4, 5, 6, 7, 15, 16, 17, 18, 8, 9, 10, 11, 12)

# ---- sensors — ALL on the P2 header ------------------------------------
MQ2_1 = 1            # P2-4   MQ-2 sensor 1, AO   (ADC1)
MQ2_2 = 2            # P2-5   MQ-2 sensor 2, AO   (ADC1)
FLAME = 42           # P2-6   flame module DO     (was 13 - that is a P1 pin)
SW420_TOP = 41       # P2-7   vibration/knock, top    (was 14 - P1 pin)
SW420_BOT = 40       # P2-8   vibration/knock, bottom (was 39)
I2C_SCL = 38         # P2-10
I2C_SDA = 47         # P2-17
DHT = 21             # P2-18  DHT11 DATA
BTN_A = 39           # P2-9   the ONE button. Moved off GPIO0 13-08-2026.
                     #        GPIO0 is a strapping pin: any wiring fault that
                     #        holds it low means the board boots into the
                     #        bootloader and Thonny cannot connect at all. On
                     #        GPIO39 the same fault is merely visible, not
                     #        fatal - you can still boot and debug it.
BTN_B = None         # second button not fitted - one button does all four jobs
BUTTON = BTN_A       # backwards-compatible alias for older files

LED_STRIP = 48       # P2-16  WS2812 data. Also the onboard RGB LED pin, so
                     # the board's own LED becomes part of the chain.

SPARE = (0,)         # P2-14. GPIO0 still has the ONBOARD BOOT button on it,
                     # so it is a free extra input in software with no wiring.

# GPIO 39-42 double as JTAG (MTCK/MTDO/MTDI/MTMS). Fine as plain GPIO unless
# you ever want hardware JTAG debugging, which this project does not.

# ---- I2C addresses ----------------------------------------------------
MPU_TOP = 0x68       # AD0 -> GND
MPU_BOT = 0x69       # AD0 -> 3V3   <- forget this and both answer at 0x68

# ---- analog ------------------------------------------------------------
DIVIDER = 1.0        # 1.0 = AO straight to the pin
                     # 1.5 = 10k/20k divider fitted  <- set this once they are in
ADC_VREF = 3.3
ADC_MAX = 4095
ADC_LINEAR_MAX = 3040

VCC_V = 5.0          # what the bench supply actually reads

# ---- MQ-2 thresholds (from your bench runs) ----------------------------
ALERT_RATIO = 1.20
ALARM_RATIO = 1.50

# ---- power -------------------------------------------------------------
# 3V3 rail  <- ESP32 3V3 (the one pin still free on P1)
#              flame, both SW-420, DHT11, both MPU-6050   ~90 mA total
# GND rail  <- ESP32 G (P2-1/21/22) AND the bench supply GND. Common ground
#              is not optional: without it the ADC has no reference and the
#              readings wander between states. That was the "bimodal" fault.
# 5 V       <- bench supply only, MQ-2 heaters. ~300 mA for the pair.

# ---- hardware facts that bite -----------------------------------------
# GPIO 26-37  flash + octal PSRAM. Touching them crashes the board.
# GPIO 43,44  UART0, the serial console.
# GPIO 0,3,45,46  strapping pins.
# GPIO 19,20  native USB.
# ADC2 (GPIO 11-20) returns garbage whenever WiFi/ESP-NOW is active, which is
# why MQ2_1 and MQ2_2 are on GPIO1/GPIO2 - the only two usable ADC1 pins.

# header position lookup, for report() and for wiring by eye
P2_POS = {1: 4, 2: 5, 42: 6, 41: 7, 40: 8, 39: 9, 38: 10,
          0: 14, 48: 16, 47: 17, 21: 18}


def make_adc(pin):
    """ADC reader that works across MicroPython versions."""
    from machine import Pin, ADC
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


def volts(raw):
    return raw * ADC_VREF / ADC_MAX * DIVIDER


def bar(value, lo=0.5, hi=2.0, width=20, mark=1.0):
    """ASCII bar with a marked reference point. Bench readability only."""
    span = hi - lo
    pos = max(0, min(width, int((value - lo) / span * width)))
    mk = max(0, min(width - 1, int((mark - lo) / span * width)))
    return "".join("#" if i < pos else ("|" if i == mk else ".")
                   for i in range(width))


def report():
    print("GENIE pin map - EVERY sensor is on the P2 header")
    print("  P2 pos  GPIO  signal")
    rows = [(MQ2_1, "MQ-2 sensor 1 AO", "5 V module"),
            (MQ2_2, "MQ-2 sensor 2 AO", "5 V module"),
            (FLAME, "flame DO", "3V3"),
            (SW420_TOP, "SW-420 top", "3V3"),
            (SW420_BOT, "SW-420 bottom", "3V3"),
            (I2C_SCL, "I2C SCL", "3V3"),
            (I2C_SDA, "I2C SDA", "3V3"),
            (DHT, "DHT11 DATA", "3V3"),
            (BTN_A, "button", "-"),
            (LED_STRIP, "WS2812 strip", "5 V bench")]
    for gpio, name, rail in sorted(rows, key=lambda r: P2_POS.get(r[0], 99)):
        print("   %2d     %-4d  %-18s %s"
              % (P2_POS.get(gpio, 0), gpio, name, rail))
    print("     MPU top    0x%02X   AD0 -> GND" % MPU_TOP)
    print("     MPU bottom 0x%02X   AD0 -> 3V3" % MPU_BOT)
    print("  spare  %s" % (", ".join(str(p) for p in SPARE) if SPARE
                             else "NONE - the P2 header is now full"))
    print("  supply %.1f V, divider %.1f" % (VCC_V, DIVIDER))


def check():
    """Sanity-check the map. Catches a pin moved onto the TFT or into PSRAM."""
    bad = []
    used = {}
    for name in ("MQ2_1", "MQ2_2", "FLAME", "SW420_TOP", "SW420_BOT",
                 "I2C_SCL", "I2C_SDA", "DHT", "BTN_A", "LED_STRIP"):
        p = globals()[name]
        if p in used:
            bad.append("%s and %s both on GPIO%d" % (name, used[p], p))
        used[p] = name
        if p in TFT_PINS:
            bad.append("%s GPIO%d collides with the TFT" % (name, p))
        if 26 <= p <= 37:
            bad.append("%s GPIO%d is flash/PSRAM" % (name, p))
        if p not in P2_POS:
            bad.append("%s GPIO%d is not on the P2 header" % (name, p))
    if bad:
        for b in bad:
            print("  FAIL " + b)
    else:
        print("  pin map OK - no collisions, everything on P2")
    return not bad
