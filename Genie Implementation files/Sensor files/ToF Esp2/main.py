"""
main.py - ToF board autostart.  (Suhaib's "ESP 1". The docs call it Node 2.)

FLASH THIS AS main.py ON THE ToF BOARD.

Upload alongside it, all to the device ROOT (flat, no folders -- the
imports are flat and a folder breaks them):

    vl53l1x.py      zones.py        genie_link.py
    pins3.py        tof3.py         wifi_cfg.py
    net_uplink.py   node2_main.py   main.py     <- this file

`net_uplink.py` comes from `01_NODE1/UPLOAD/`. It is not Node-1-specific;
it just lives there because Node 1 needed it first.

--------------------------------------------------------------------------
WHAT THIS BOARD DOES
--------------------------------------------------------------------------
Reads four VL53L1X ToF sensors and the vibration switches at 10 Hz, and
sends them TWO ways:

  ESP-NOW  ->  the face board, so it can draw distances on the TFT and fold
               them into its own telemetry line
  TCP      ->  the laptop directly, so the ToF array keeps reporting even
               when the face board is off, crashed, or being reflashed

Both, not either. The ESP-NOW frame is the documented contract and the face
board needs it; the direct line is what stops one dead board taking two
sensors' worth of data with it.

It reads no other sensor. Gas, flame, temperature and the IMU belong to the
face board. Two boards reading the same sensor is how two different
temperatures end up on screen during a viva.

--------------------------------------------------------------------------
THE RULE THIS FILE EXISTS TO ENFORCE: NEVER RETRY IN A LOOP
--------------------------------------------------------------------------
An autostarting board that catches its own startup error and tries again
is the worst failure mode in MicroPython. It fills the console faster than
you can type and locks you out of the REPL, and the only way back is a
full erase and reflash.

So: a 3 second grace period BEFORE anything is imported, then ONE attempt,
then stop with the real traceback printed. Node 1's main.py has said this
since the beginning and it is right.

If you are ever locked out: Ctrl-C repeatedly while pressing reset. If that
fails, hold BOOT while plugging in to reach the bootloader.

--------------------------------------------------------------------------
BEFORE THE DEMO
--------------------------------------------------------------------------
Set NODE1_MAC at the top of node2_main.py to the face board's real STA MAC.
Broadcast works for a first test and every ESP-NOW device in the room
receives it.
"""

import sys
import time

GRACE_S = 3

# Send readings straight to the laptop as well as to the face board.
#
# Costs one TCP socket and a JSON line every time a frame goes out. Turn it
# off only if you want to prove the ESP-NOW path in isolation -- the face
# board still relays ToF either way, so nothing is lost from the demo, only
# the redundancy.
DIRECT_UPLINK = True

print()
print("=" * 46)
print("  GENIE ToF board starting in %d s" % GRACE_S)
print("  press Ctrl-C NOW for a REPL")
print("=" * 46)

try:
    for i in range(GRACE_S, 0, -1):
        print("  %d ..." % i)
        time.sleep(1)
except KeyboardInterrupt:
    print("\n  cancelled. REPL is yours.\n")
    raise SystemExit

try:
    import node2_main

    uplink = None
    if DIRECT_UPLINK:
        try:
            import net_uplink
            uplink = net_uplink.Uplink(log=print)
            print("  direct uplink to the laptop: enabled")
        except ImportError:
            # Not fatal, and worth being loud about rather than silently
            # running with half the reporting paths.
            print("  !! net_uplink.py / wifi_cfg.py not uploaded.")
            print("     ESP-NOW to the face board still works, but this")
            print("     board will go dark if that board does.")

    node2_main.main(uplink=uplink)

except KeyboardInterrupt:
    print("\n  stopped by Ctrl-C\n")
except ImportError as e:
    # Its own branch on purpose. An ImportError is never a wiring fault,
    # and an earlier version of this file printed the XSHUT advice for it
    # -- sending you to check pull-ups when the real problem was a file
    # that never made it onto the board.
    print()
    print("!" * 46)
    print("  A MODULE IS MISSING FROM THE BOARD.")
    print("!" * 46)
    sys.print_exception(e)
    print()
    print("  This is NOT a wiring problem. Nothing is wrong with the")
    print("  sensors. A file simply is not on the board.")
    print()
    print("  The ToF board needs ALL of these at the device ROOT,")
    print("  flat, no folders:")
    for _m in ("vl53l1x", "zones", "genie_link", "pins3", "tof3",
               "wifi_cfg", "net_uplink", "node2_main"):
        try:
            __import__(_m)
            print("      ok        %s.py" % _m)
        except ImportError:
            print("      MISSING   %s.py   <-- upload this" % _m)
        except Exception:
            print("      present   %s.py" % _m)
    print()
    print("  In Thonny: open the file, then")
    print("      File > Save as... > MicroPython device")
    print("  keeping the same name. Then reset the board.")
    print()

except Exception as e:
    print("\n" + "!" * 46)
    print("  ToF board failed to start. Full traceback:")
    print("!" * 46)
    sys.print_exception(e)
    print()
    print("  Most common causes, in order:")
    print("    1. XSHUT bring-up failed - a sensor did not wake at 0x29.")
    print("       Power-cycle, then run tof3.xshut_diagnostic() to find")
    print("       the bad wire. Addresses live in RAM, so bring-up runs")
    print("       on EVERY boot - this is not a one-off setup step.")
    print("    2. VIN on 5 V. It must be 3V3 - the S3 is not 5 V tolerant.")
    print("    3. Pull-ups: ONE 4.7k pair for the whole bus, not one per")
    print("       board. Four sets in parallel is too stiff to drive.")
    print()
    print("  NOT retrying. Fix it, then:  import machine; machine.reset()\n")
