"""
genie_menu.py - one menu for everything on Node 1.

    import genie_menu
    genie_menu.run()

Or call any entry directly:

    genie_menu.rotate()      the rotation wizard - ends the guessing
    genie_menu.button()      is GPIO39 actually reaching the ESP32?
    genie_menu.live()        every sensor, in the REPL
    genie_menu.tft()         colour test
    genie_menu.i2c()         bus scan

WHY THE FIRST TWO EXIST

    rotate() draws an arrow and the word TOP in all four rotations and asks
    which one read correctly. No more inferring orientation from upside-down
    letters or from which side the eyebrows landed on.

    button() shows the raw pin AND the decoded press side by side. When the
    face stops responding, that one screen tells you whether the button ever
    reached the chip - which is the difference between a loose wire and a
    software bug, and you cannot tell them apart by staring at the code.
"""

from machine import Pin
import time

from ili9341_par import ILI9341Par, color565
import genie_ui as G

BG = G.BG
CYAN = G.CYAN
OK = G.OK
BAD = G.BAD
LABEL = G.LABEL

_tft = None


def _display():
    global _tft
    if _tft is None:
        _tft = ILI9341Par(rotation=G.ROTATION)
    return _tft


def _ctr(tft, s, y, colour, scale=2):
    w = min(len(s) * 8 * scale, tft.width)
    G.text(tft, s, (tft.width - w) // 2, y, colour, BG, scale, tft.width)


# ---------------------------------------------------------------------------
def rotate():
    """Show an unambiguous UP marker in every rotation. Pick the right one.

    An arrow plus the word TOP, drawn at the top of the panel. Exactly one
    rotation puts them where they belong. That number goes into ROTATION at
    the top of genie_ui.py - and it is the ONLY thing you should ever change
    to fix orientation.
    """
    tft = _display()
    print("\n  Four rotations. Watch the panel.")
    print("  You are looking for: the word TOP at the TOP, arrow pointing")
    print("  UP, and the text the right way round.\n")

    for r in (0, 1, 2, 3):
        tft.set_rotation(r)
        tft.fill_screen(BG)
        w, h = tft.width, tft.height

        _ctr(tft, "TOP", 8, CYAN, 3)
        # arrow, pointing up: a stack of widening bars plus a shaft
        cx = w // 2
        for i in range(14):
            bw = 4 + i * 4
            tft.fill_rect(cx - bw // 2, 46 + i * 3, bw, 3, OK)
        tft.fill_rect(cx - 8, 88, 16, 46, OK)

        _ctr(tft, "rotation %d" % r, h - 60, LABEL, 2)
        _ctr(tft, "%dx%d" % (w, h), h - 34, LABEL, 1)
        print("    rotation %d   panel reads %dx%d" % (r, w, h))
        time.sleep(3)

    tft.set_rotation(G.ROTATION)
    tft.fill_screen(BG)
    print("\n  Put the number that looked right into genie_ui.py:")
    print("      ROTATION = <that number>")
    print("  1 and 3 are the landscape pair. 0 and 2 are portrait.\n")


# ---------------------------------------------------------------------------
def button(seconds=30):
    """Raw pin and decoded press, side by side, on screen and in the REPL.

    RAW is the pin itself. EVENT is what the decoder made of it.

        RAW never changes  -> the button is not reaching GPIO39. Wiring.
        RAW moves, no EVENT -> presses shorter than 30 ms, or a bouncing
                               switch. Raise DEBOUNCE_MS.
        Both move           -> the button is fine; the fault is elsewhere.
    """
    tft = _display()
    tft.fill_screen(BG)
    _ctr(tft, "BUTTON GPIO39", 10, CYAN, 2)
    _ctr(tft, "press it", 40, LABEL, 1)

    b = G.Button()
    last_raw = None
    last_ev = ""
    counts = {"single": 0, "double": 0, "triple": 0, "long": 0}
    t0 = time.time()
    print("\n  Press: once, twice quickly, three times, then hold 3 s.")
    print("  %d s, or Ctrl-C.\n" % seconds)

    try:
        while time.time() - t0 < seconds:
            raw = b.pin.value()
            if raw != last_raw:
                last_raw = raw
                tft.fill_rect(0, 70, tft.width, 44, BG)
                _ctr(tft, "RAW  %d" % raw, 74, BAD if raw else OK, 4)
                print("    raw -> %d" % raw)

            ev = b.poll()
            if ev:
                counts[ev] = counts.get(ev, 0) + 1
                last_ev = ev
                tft.fill_rect(0, 128, tft.width, 44, BG)
                _ctr(tft, ev.upper(), 132, CYAN, 4)
                tft.fill_rect(0, 186, tft.width, 22, BG)
                _ctr(tft, "s%d d%d t%d L%d"
                     % (counts["single"], counts["double"],
                        counts["triple"], counts["long"]), 188, LABEL, 2)
                print("    EVENT -> %s   %s" % (ev, counts))
            time.sleep_ms(5)
    except KeyboardInterrupt:
        pass

    print("\n  " + "-" * 52)
    if last_raw is None or all(v == 0 for v in counts.values()):
        if last_raw == 1:
            print("  RAW stayed at 1. The pin never went low.")
            print("  The button is NOT reaching GPIO39 - check the wire and")
            print("  that the other side really goes to GND. Nothing in")
            print("  genie_ui.py can fix this.")
        elif last_raw == 0:
            print("  RAW stuck at 0. Button held down, wired to 3V3 instead")
            print("  of GND, or shorted.")
    else:
        print("  Button works. %s" % counts)
        print("  If the face still ignores it, the fault is in the UI, not")
        print("  the hardware - and that is now a two-line search.")
    return counts


# ---------------------------------------------------------------------------
def live(seconds=40):
    """Every sensor, in the REPL, no display involved."""
    import sensors
    s = sensors.Sensors()
    print("\n  %d s, or Ctrl-C.\n" % seconds)
    t0 = time.time()
    try:
        while time.time() - t0 < seconds:
            s.tick()
            row = []
            for key, lab, every in sensors.CHANNELS:
                if every:
                    row.append("%s %s" % (lab.split()[0][:5], s.text(key)))
            print("  " + " | ".join(row))
            print("     mood=%s  tilt=%s" % (s.mood(), s.tilt()))
            time.sleep_ms(700)
    except KeyboardInterrupt:
        pass
    s.stop()
    print("\n  stopped")


def board():
    """Every sensor on the TFT at once, held until Ctrl-C."""
    import sensors
    tft = _display()
    tft.set_rotation(G.ROTATION)
    s = sensors.Sensors()
    print("\n  Ctrl-C to stop.")
    tft.fill_screen(BG)
    _ctr(tft, "ALL SENSORS", 4, CYAN, 2)
    first = True
    try:
        while True:
            # Paint on the FIRST pass, then settle into the refresh rhythm.
            # The old version polled for 600 ms before drawing anything, so
            # the page looked dead exactly long enough to make you hit Ctrl-C.
            for _ in range(1 if first else 30):
                s.tick()
                time.sleep_ms(20)
            first = False
            for i, key in enumerate(G.ORDER):
                x = (8, 168)[i % 2]
                y = 34 + (i // 2) * 34
                if y > 206:
                    break
                tft.fill_rect(x, y, 150, 30, BG)
                G.text(tft, s.label(key)[:11], x, y, LABEL, BG, 1)
                G.text(tft, s.text(key)[:11], x, y + 12,
                       G.STATUS_COLOUR[s.status(key)], BG, 2)
    except KeyboardInterrupt:
        s.stop()
        print("\n  stopped")


def tft():
    """Colour test. Wrong colour = that colour's data lines."""
    t = _display()
    t.set_rotation(G.ROTATION)
    for c, name in ((color565(255, 0, 0), "RED"),
                    (color565(0, 255, 0), "GREEN"),
                    (color565(0, 0, 255), "BLUE"),
                    (color565(255, 255, 255), "WHITE")):
        print("    %s" % name)
        t.fill_screen(c)
        time.sleep(1)
    t.fill_screen(BG)
    print("    all four correct = the bus is good")


def i2c():
    from machine import I2C
    import sensors as S
    found = I2C(0, scl=Pin(S.I2C_SCL), sda=Pin(S.I2C_SDA),
                freq=S.I2C_FREQ).scan()
    print("    %s" % ([hex(a) for a in found] if found else "nothing"))
    if S.IMU_A in found and S.IMU_B not in found:
        print("    only 0x68 - the second module's AD0 is not on 3V3")
    return found


def files():
    try:
        import cleanup
        return cleanup.audit()
    except ImportError:
        print("    cleanup.py is not on the device (it lives in Tools/)")


def calibrate():
    """One-time setup. Do this ONCE on the bench, not before every demo.

    Two things, both saved to genie_cal.json so a reboot keeps them:

      LEVEL      teaches GENIE which way is flat, and corrects each IMU's
                 scale so both read 1.00 g sitting still.
      BASELINE   teaches GENIE what clean air smells like, so smoke can be
                 reported as a RATIO instead of a meaningless raw number.

    Leave the board alone and the air clean while this runs.
    """
    import sensors
    s = sensors.Sensors()

    print("\n  1/3  holding still for 3 s to level the IMUs...")
    t0 = time.time()
    while time.time() - t0 < 3:
        s.tick()
        time.sleep_ms(20)
    s.level()

    print("\n  2/3  MQ-2 heater warm-up, 90 s. Readings drift the whole")
    print("       time - that is the heater, not a fault. Clean air please.")
    for left in range(90, 0, -10):
        print("       %3d s left   #1 %s   #2 %s"
              % (left, s.text("smoke1"), s.text("smoke2")))
        t0 = time.time()
        while time.time() - t0 < 10:
            s.tick()
            time.sleep_ms(20)

    print("\n  3/3  clean-air baseline...")
    s.calibrate()
    s.stop()
    print("\n  Done. Reboot and it is still there - genie_cal.json.")
    print("  If a channel said TOO NOISY, fit the 100 nF caps and re-run.")


def face():
    G.run()


# ---------------------------------------------------------------------------
ITEMS = (
    ("1", "GENIE face + browser", face),
    ("2", "BUTTON test - raw pin vs decoded press", button),
    ("3", "ROTATION wizard - which number is correct", rotate),
    ("4", "sensors live, in the REPL", live),
    ("5", "sensors live, on the TFT", board),
    ("6", "TFT colour test", tft),
    ("7", "I2C scan", i2c),
    ("8", "file audit", files),
    ("9", "CALIBRATE - level + clean-air baseline (once, ~2 min)", calibrate),
)


def run():
    while True:
        print("\n" + "=" * 50)
        print("  GENIE NODE 1")
        print("=" * 50)
        for k, label, _ in ITEMS:
            print("  %s  %s" % (k, label))
        print("  0  quit")
        try:
            sel = input("  select> ").strip()
        except KeyboardInterrupt:
            return
        if sel == "0":
            return
        for k, _, fn in ITEMS:
            if sel == k:
                try:
                    fn()
                except KeyboardInterrupt:
                    print("\n  interrupted")
                except Exception as e:
                    print("  FAILED: %r" % e)
                break
        else:
            print("  ?")


if __name__ == "__main__":
    run()
