"""GENIE Node 2 -- pin map for the 4x ToF + fingerprint bench.

MODULE. Upload this to the board; run_tof_test.py and tof3.py both import it.

One place for every pin number. Change it here, nowhere else.

N_SENSORS drives everything: bring-up, the live view, the diagnostics and the
menu header all use only the first N entries of the tables below. Drop a
sensor for debugging by setting it to 3 and re-uploading this one file.

==========================================================================
RESTORED 26-08-2026 -- and read the two notes below before trusting it
==========================================================================
This file and tof3.py were imported by `bench/run_tof_test.py` and
`bench/tof_bench.py` but were missing from the bundle entirely, so both
of those scripts died on ImportError before reaching a single sensor.
Restored from Arman's working copy.

NOTE 1 -- THIS IS THE SECOND COPY OF THE PIN MAP.
`node2_main.py` declares SDA_PIN/SCL_PIN, XSHUT_PINS, ADDRESSES and
VIB_PINS as its own module-level constants and does NOT import this file.
They agree today (8/9, 10-13, 0x30-0x33) -- checked 26-08-2026. Nothing
enforces that they keep agreeing, and the failure mode is the bench tool
proving a wiring that production does not use. If you move a wire, move it
in BOTH files, or make node2_main.py import this one.

NOTE 2 -- ROOM_ZONES AT THE BOTTOM IS NOT WHAT PRODUCTION USES.
See the comment there. The bands the laptop actually receives come from
Node 1, not from here.
"""

VERSION = "pins3 1.3  2026-08-15"

# ------------------------------------------------------------------- ToF
#
# THIS IS THE BISECT KNOB. Set it to how many sensors are PHYSICALLY wired,
# counting from index 0. Everything downstream reads only the first N entries
# of the three tables below, so:
#
#   1 -> front only          (GPIO10)          minimum load, proves the bus
#   2 -> front, left         (GPIO10,11)
#   3 -> front, left, right  (GPIO10,11,12)
#   4 -> all four            (GPIO10,11,12,13)
#
# Re-upload this one file after changing it. Nothing else needs touching.
N_SENSORS = 4                       # all four wired: front/left/right/rear

SDA, SCL = 8, 9                     # shared by every sensor
I2C_FREQ = 100_000                  # 100k, matching node2_main.py and the
                                    # face board. Four modules on dupont wire
                                    # is not a 400 kHz bus -- 400k produced
                                    # ENODEV on all four, 27-08-2026.

# One GPIO per sensor. The ORDER here is the order XSHUT is raised and the
# order addresses are handed out, so index 0 MUST physically be the front
# sensor on GPIO10. Every board is identical -- the code cannot detect that
# you swapped two, it will just confidently report the wrong direction.
XSHUT     = [10, 11, 12, 13]
ADDRESSES = [0x30, 0x31, 0x32, 0x33]
NAMES     = ["front", "left", "right", "rear"]

BOOT_MS   = 15                      # settle after raising XSHUT. Datasheet says
                                    # ~1.2 ms; being stingy here is exactly where
                                    # flaky ENODEV failures come from.

MIN_VALID_MM = 40                   # below this the chip saturates and lies
MAX_TRUST_MM = 4000                 # LONG mode ceiling, good conditions only

# ----------------------------------------------------------- fingerprint
#
# DEAD SECTION, kept only so this file matches Arman's copy byte for byte.
# The R307S was diagnosed and condemned on 24-08-2026 -- controller healthy,
# capture illumination lamp dead -- and the decision was taken not to buy a
# replacement. r307.py is deliberately NOT in this bundle (WHAT_IS_MISSING.md
# section 8) and tof3.manifest() already expects it to be absent.
#
# Do not wire anything to these pins expecting it to work. If fingerprint
# ever comes back, the proven driver is at 09_GENIE_Node/Sensors_Node2_ESP32S3/
# R307_FINAL_TRY/ in the main tree.
FP_UART   = 1                       # UART0 is the Thonny REPL, UART2 is the SIM
FP_TX     = 17                      # ESP TX  -> module RXD
FP_RX     = 18                      # ESP RX  <- module TXD
FP_TOUCH  = 16                      # module WAKEUP out; None if not wired
FP_BAUD   = 57600

# ----------------------------------------------------------------- files
USERS_FILE  = "/users.json"
EVENTS_FILE = "/events.log"
MIN_SCORE   = 50                    # below this a match is not trusted

# ----------------------------------------------------------------- timing
LOOP_MS        = 20                 # poll rate
PRINT_MS       = 250                # status-line rate; serial is the bottleneck
FINGER_POLL_MS = 300                # how often to ask the R307 for a finger
DURATION_S     = 20                 # default length of an individual test

# ------------------------------------------------------- room-scale zones
# Wider than the desk-scale defaults in zones.py because this array watches a
# room, not a hand. NOTE the 100 mm TOUCH band with HYST 40: hysteresis must
# stay below half the narrowest band or that band can only ever be entered
# via the saturation clamp.
#
# ------------------------------------------------------------------------
# WARNING, 26-08-2026: THREE SETS OF BANDS EXIST AND THESE ARE NOT THE ONES
# THE LAPTOP SEES.
#
#   pins3.ROOM_ZONES (here)   100 / 500 / 1500 / 3000   room scale
#   zones.DEFAULT_ZONES        40 / 300 /  800 / 1500   desk scale
#   node1_link._BANDS          40 / 300 /  800 / 1500   desk scale
#
# These bands are used by the BENCH TOOLS ONLY. In production, node2_main.py
# builds its trackers with `zones.ZoneTracker(link.SIDE[i])` and no `zones=`
# argument -- so it gets the DESK-scale defaults -- and then never sends the
# zone anyway: the ESP-NOW frame carries a `tof_hit` bitmask derived from the
# calibrated baseline, not a zone name. Node 1 re-derives `tof_zone` from its
# own _BANDS, which are desk scale too.
#
# So a person standing 2 m away calibrates and triggers correctly, but is
# reported to the laptop as "CLEAR" -- because 2000 mm is past the desk-scale
# 1500 mm FAR band. The trigger is right and the WORD is wrong.
#
# Fix by making node1_link._BANDS match these numbers, or by passing
# ROOM_ZONES into node2_main's trackers and sending the zone in the frame.
# Do not fix it by widening one and not the others.
# ------------------------------------------------------------------------
ROOM_ZONES = (
    (100,   "TOUCH"),       # 0 - 10 cm    object against the lens
    (500,   "NEAR"),        # 10 - 50 cm   hand / gesture range
    (1500,  "PRESENT"),     # 0.5 - 1.5 m  person at the bed or chair
    (3000,  "FAR"),         # 1.5 - 3 m    in the room, doorway crossing
    (99999, "CLEAR"),       # beyond that nothing meaningful
)
ROOM_HYST_MM  = 40
ROOM_STABLE_N = 3
