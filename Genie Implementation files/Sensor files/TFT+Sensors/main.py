"""
main.py - face board autostart.  (Suhaib's "ESP 2". The docs call it Node 1.)

FLASH THIS AS main.py ON THE FACE BOARD.

Upload alongside it, all to the device ROOT (flat, no folders -- the
imports are flat and a folder breaks them):

    ili9341_par.py   eyes.py          pins.py         sensors.py
    genie_ui.py      genie_link.py    node1_link.py   fall_fsm.py
    node1_report.py  wifi_cfg.py      net_uplink.py   main.py  <- this

--------------------------------------------------------------------------
WHAT THIS BOARD DOES
--------------------------------------------------------------------------
Draws the face, reads its own sensors (2x MQ-2, flame, DHT11, 2x MPU-6050,
2x SW-420), receives the ToF array over ESP-NOW from the ToF board, and
sends the lot to the laptop as newline-JSON -- over the USB serial port AND
over WiFi, both at once.

    genie_ui.run()
      -> UI.loop(), one cooperative pass every ~33 ms
         -> sensors.tick()          poll whatever channel is due
         -> node1_report.tick()     ESP-NOW poll, fall FSM at 50 Hz,
                                    stdin for {"emotion":...},
                                    one telemetry line every 2 s

From the laptop's node_bridge those lines reach api.py, and from there
sensor.aiml answers "is there smoke", "did you move", "how far away is it".

Serial and WiFi both run because they fail differently: serial survives the
router being down, WiFi survives the cable being unplugged. For a device
whose job is calling for help, keep the cable attached during the demo.

--------------------------------------------------------------------------
THE RULE: main.py must never retry in a loop.

The old one did. When line 11 threw, it caught the error, printed
"Initialization failed. Bailing out." and went round again - forever, faster
than you can type. That is what filled your console and locked you out of the
REPL. A boot loop on an autostarting board is the single worst failure mode
in MicroPython, because the only way out is a hard erase.

Three rules are baked in below:

    1  A 3 second grace period BEFORE anything is imported. Ctrl-C in that
       window drops you straight to the REPL with nothing running.
    2  ONE attempt. If it throws, it prints the real traceback and STOPS.
       No retry, ever.
    3  The traceback is printed in full, not swallowed into a one-line
       message that tells you nothing.

If you are ever locked out again:
    Ctrl-C repeatedly while pressing reset. If that fails, erase and
    reflash - or hold BOOT while plugging in to reach the bootloader.
"""

import sys
import time

GRACE_S = 3

print()
print("=" * 46)
print("  GENIE starting in %d s" % GRACE_S)
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
    import genie_ui
    genie_ui.run()
except KeyboardInterrupt:
    print("\n  stopped by Ctrl-C\n")
except ImportError as e:
    # An ImportError here is never a wiring fault. Its own branch so
    # the hardware advice below does not send you to check a ribbon
    # cable when the real problem is a file that never got uploaded.
    print()
    print("!" * 46)
    print("  A MODULE IS MISSING FROM THE BOARD.")
    print("!" * 46)
    sys.print_exception(e)
    print()
    print("  Not a wiring problem. A file is not on the board.")
    print()
    for _m in ("ili9341_par", "eyes", "pins", "sensors", "genie_ui",
               "genie_link", "node1_link", "fall_fsm", "node1_report",
               "wifi_cfg", "net_uplink"):
        try:
            __import__(_m)
            print("      ok        %s.py" % _m)
        except ImportError:
            print("      MISSING   %s.py   <-- upload this" % _m)
        except Exception:
            print("      present   %s.py" % _m)
    print()
    print("  Thonny: File > Save as... > MicroPython device, same name.")
    print()

except Exception as e:
    print("\n" + "!" * 46)
    print("  GENIE failed to start. Full traceback:")
    print("!" * 46)
    sys.print_exception(e)
    print()
    print("  Most common causes, in order:")
    print("    1. A module missing from the upload list at the top of")
    print("       this file. Half an upload gives an ImportError on a")
    print("       name sitting right there in the folder on your PC.")
    print("    2. TFT ribbon. LCD_D0/D1 sit in the SD-pin group,")
    print("       separated from D2-D7, and both blocks run backwards.")
    print("       Counting positions instead of reading labels puts")
    print("       SD_SS where LCD_D0 belongs - the code runs perfectly")
    print("       and the screen stays white. Two days went to this.")
    print("    3. I2C: both MPUs on 38/47. i2c_doctor.py names it.")
    print()
    print("  The face runs without the laptop, without WiFi and")
    print("  without the ToF board. If it will not start, the fault")
    print("  is on this board.")
    print()
    print("  NOT retrying. Fix it, then:  import machine; machine.reset()")
