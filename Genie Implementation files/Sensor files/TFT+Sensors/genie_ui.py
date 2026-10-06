"""
genie_ui.py - GENIE's face and one-button sensor browser.  THE main file.

    import genie_ui
    genie_ui.run()

THE EYES ARE eyes.py (RoboEyes).  There is no face.py any more.
    Two eye modules on one device is how you end up debugging code that is
    not running. face.py is deleted; eyes.py is the only face GENIE has, and
    it is untouched - same brows, pupils, tweens and moods as the video.
    Everything that used to live in face.py (text, colours) is now at the
    top of THIS file, so the whole UI is one file plus the eyes.

SCREEN ORIENTATION
    ROTATION = 3 below. 1 and 3 are the landscape pair, 180 degrees apart;
    0 and 2 are the portrait pair. This ONE NUMBER is the entire fix for an
    upside-down panel. Never move coordinates to compensate - that flips the
    layout and leaves every glyph standing on its head.

    THE TEST THAT SETTLES IT with no text on screen: the BROWS must sit
    ABOVE the eyes. RoboEyes always draws them above - nothing in the code
    can put a brow underneath an eye. If they appear below, the panel is
    180 degrees out; swap 1 <-> 3.

    Or stop guessing:   import genie_menu; genie_menu.rotate()
    draws an arrow and the word TOP in each rotation and hands you the
    number to write here. Live flip from the REPL:  genie_ui.flip()

FOUR PAGES, one button

    EYES      just the eyes, centred, nothing else
    VALUE     one sensor, as large as the panel will carry, no eyes
    BOTH      eyes on top, that sensor underneath
    ALL       every sensor at once

    single click     next sensor  (and leaves EYES for VALUE)
    double click     previous sensor
    triple click     next page:  EYES -> VALUE -> BOTH -> EYES
    hold 3 seconds   ALL page
    any click        wakes it, and leaves the ALL page

EXPRESSIONS COME FROM THE SENSORS, NOT FROM A TIMER

    alarm  (flame, smoke past 1.50x, hard knock)   ANGRY      red
    warn   (smoke past 1.20x, odd tilt)            SURPRISED  bright cyan
    knock  (something tapped it in the last 2.5s)  SURPRISED
    calm   (everything nominal)                    HAPPY      amber
    quiet minute AND nothing wrong                 SLEEPY     dim slate

    The IMU also drives eyes.look() continuously, so the pupils track the
    board as you tilt it. That is the demo moment - pick GENIE up and it
    watches you.

UPLOAD SEVEN FILES, FLAT, to the device root:
    pins.py  ili9341_par.py  eyes.py  sensors.py  genie_ui.py  main.py
    genie_menu.py
"""

from machine import Pin
import time
import gc
import framebuf

from ili9341_par import ILI9341Par, color565, fbcolor, Canvas
import eyes as eyemod
import sensors

# The laptop link: newline-JSON out over USB serial, {"emotion":...} back.
# Guarded so a board that has not had node1_report.py uploaded yet still
# boots and shows a face -- which is how this firmware behaved for months
# and must keep behaving. See 03_PROTOCOL/PROTOCOL.md.
try:
    import node1_report
except ImportError:
    node1_report = None

# ---------------------------------------------------------------------------
ROTATION = 3                 # <-- flip the display here. 1 or 3 (180 apart).
BTN = 39

DEBUG_BUTTON = True          # prints every decoded press to the REPL and
                             # lights a dot on screen while the pin is low.
                             # Leave it on until the button is trusted - it
                             # separates "the button is not wired" from "the
                             # code ignored it" at a glance, which is the
                             # difference between a two minute fix and an
                             # hour of reading code that was always correct.

DEBOUNCE_MS = 30
MULTI_MS = 420               # window to collect a 2nd or 3rd click
LONG_MS = 3000               # hold for the ALL page

IDLE_MS = 30_000             # drop back to the EYES page
ALL_TIMEOUT_MS = 20_000      # ALL page falls back on its own

# ---- how GENIE behaves when nobody is touching it -------------------------
# A ladder, not a switch. Walk up to GENIE and it is SMILING; leave it and it
# settles, then eventually dozes. Every rung is still a CALM face - none of
# them is unhappy, because nothing bad has happened. An unhappy face stays
# reserved for a sensor measuring something, or the face is lying, and a
# device that cries wolf gets ignored on the day it matters.
#
#   quiet for        eyes                colour
#   0 - 20 s         HAPPY               amber, arched, smiling
#   20 s - 2 min     NEUTRAL             cyan, open, calm
#   2 min +          SLEEPY              dim slate, half closed
#
# Change the numbers, or swap which mood sits on which rung, right here.
# An alarm still beats every rung of it instantly.
IDLE_LADDER = (
    (20_000,  "calm"),        # 0-20 s      HAPPY
    (120_000, "rest"),        # 20 s - 2 m  NEUTRAL
    (None,    "sleep"),       # after that  SLEEPY
)

# Below this much deviation from the resting angle, hand the eyes back to
# RoboEyes' own idle wander instead of pinning them with look(). Without a
# deadzone the pupils sit locked at whatever angle the board is bolted at,
# and a fixed stare is exactly what reads as "annoyed".
TILT_DEADZONE = 0.22

# eye geometry. RoboEyes needs 1.8 * eye_h + 16 px of canvas height, so 80
# gives 160 - which leaves room for a readout underneath on a 240 px panel.
EYE_W, EYE_H, EYE_GAP = 92, 80, 40
CY_EYES = 120                # centred, EYES page
CY_BOTH = 80                 # pushed up, BOTH page
TEXT_Y = 166                 # readout top on the BOTH page

# ---- palette --------------------------------------------------------------
BG = color565(0, 0, 0)
CYAN = color565(0, 210, 235)
DIM = color565(70, 90, 130)
LABEL = color565(120, 130, 145)
OK = color565(60, 220, 120)
WARN = color565(255, 190, 40)
BAD = color565(255, 70, 70)
STATUS_COLOUR = (OK, WARN, BAD)

PAGES = ("eyes", "value", "both")

ORDER = ("flame", "smoke1", "smoke2", "temp", "hum",
         "knock1", "knock2", "imu1", "imu2", "tof", "finger")

# Every unhappy face here is earned by a measurement. "calm" is the only
# resting state, and it is a smile.
MOOD = {
    "alarm":   eyemod.ANGRY,      # red      - flame, smoke 1.50x, impact
    "warn":    eyemod.SURPRISED,  # br cyan  - smoke 1.20x, odd tilt
    "knock":   eyemod.SURPRISED,  # br cyan  - something tapped it
    "offline": eyemod.SAD,        # blue     - a module stopped answering
    "calm":    eyemod.HAPPY,      # amber    - walk up to it and it smiles
    "rest":    eyemod.NEUTRAL,    # cyan     - settled, still awake
    "sleep":   eyemod.SLEEPY,     # dim      - dozing
}


# ---------------------------------------------------------------------------
# text  (was face.py - folded in here so there is only one UI file)
# ---------------------------------------------------------------------------
# This board has NO PSRAM - the boot log says so plainly:
#     PSRAM chip is not connected, or wrong PSRAM line mode
#     Failed to init external RAM; continuing without it
# so everything lives in ~150 kB of internal heap, and RoboEyes already
# holds 34 kB of that permanently.
#
# The old text() allocated a fresh canvas on EVERY call - up to 40 kB at
# scale 8. Do that a few hundred times on a small heap and it does not run
# out, it FRAGMENTS: plenty free, no single block big enough. So these two
# buffers are allocated ONCE, at import, and reused forever. Peak memory
# becomes a constant you can reason about instead of a slow leak you cannot.
MAX_SCALE = 6                       # 48 px glyphs, readable across a room
_DST = bytearray(320 * 8 * MAX_SCALE * 2)
_SRC = bytearray(320 * 8 * 2)


def text(tft, s, x, y, fg, bg=BG, scale=2, maxw=320):
    """8x8 system font, scaled by pixel replication. Zero allocation."""
    scale = max(1, min(scale, MAX_SCALE))
    n = len(s)
    if n * 8 * scale > maxw:
        n = maxw // (8 * scale)
        s = s[:n]
    if n <= 0:
        return (0, 0)
    w, h = n * 8, 8
    cw, chh = w * scale, h * scale

    src = framebuf.FrameBuffer(memoryview(_SRC)[:w * h * 2], w, h,
                               framebuf.RGB565)
    src.fill(0)
    src.text(s, 0, 0, 0xFFFF)

    dst_mv = memoryview(_DST)[:cw * chh * 2]
    dst = framebuf.FrameBuffer(dst_mv, cw, chh, framebuf.RGB565)
    dst.fill(fbcolor(bg))
    f = fbcolor(fg)
    for yy in range(h):
        for xx in range(w):
            if src.pixel(xx, yy):
                dst.fill_rect(xx * scale, yy * scale, scale, scale, f)
    tft.blit(x, y, cw, chh, dst_mv)
    return cw, chh


def centre_text(tft, s, y, fg, bg=BG, scale=2):
    w = min(len(s) * 8 * scale, 320)
    return text(tft, s, (320 - w) // 2, y, fg, bg, scale)


def big_text(tft, s, y, fg, bg=BG, maxscale=MAX_SCALE, maxw=312):
    """As large as the string will go without running off the panel."""
    n = max(1, len(s))
    scale = max(1, min(maxscale, maxw // (n * 8)))
    return centre_text(tft, s, y, fg, bg, scale)


# ---------------------------------------------------------------------------
# PANEL WATCHDOG
#
# Symptom: the screen goes white mid-run while the REPL keeps printing button
# events. White is an ILI9341's POWER-ON state - it means the controller lost
# its initialisation and the ESP32 is happily writing pixels into a panel
# that is no longer listening. The loop never notices, because writing to a
# dead panel raises nothing.
#
# Usual cause on this bench: the 5 V rail dipping far enough for the shield's
# regulator to brown out, with MQ-2 heaters, an LED strip and the TFT all on
# one supply. But the fix should not depend on knowing that.
#
# So: ask the panel. Register 0x0A (RDDPM) reports its own power state.
# Bit 4 = sleep-out, bit 2 = display-on. A panel that reset on its own comes
# back with both clear, and that is unambiguous - a live panel cannot report
# it is asleep. Catch it, re-init, redraw, and print a line so you know it
# happened and how often.
#
# If that line NEVER prints, the white screen is not a panel reset and we
# look elsewhere. If it prints repeatedly, it is power, and no software can
# do better than recover quickly.
PANEL_CHECK_MS = 2000


def panel_alive(tft):
    """Read RDDPM. Returns (ok, raw). Never raises - a watchdog that can
    break the thing it guards is worse than no watchdog."""
    try:
        tft._cs.value(0)
        tft._dc.value(0)
        tft._write_byte(0x0A)
        tft._dc.value(1)
        for p in tft._dpins:
            p.init(Pin.IN)
        got = []
        for _ in range(2):                 # first byte is a dummy
            tft._rd.value(0)
            time.sleep_us(2)
            v = 0
            for i, p in enumerate(tft._dpins):
                v |= p.value() << i
            tft._rd.value(1)
            time.sleep_us(1)
            got.append(v)
        for p in tft._dpins:
            p.init(Pin.OUT)
        tft._cs.value(1)
        return ((got[1] & 0x14) == 0x14), got[1]
    except Exception:
        return True, -1


# ---------------------------------------------------------------------------
class Button:
    """Non-blocking single / double / triple / long decoder.

    Clicks are COUNTED into a burst and reported only when the burst ends -
    the only way three clicks can be told apart from one. Costs MULTI_MS of
    latency on a single click; 420 ms is unnoticeable.

    Long press fires on RELEASE, not at the 3 second mark, so letting go
    early aborts it and one hold can never fire twice.
    """

    def __init__(self, pin=BTN):
        self.pin = Pin(pin, Pin.IN, Pin.PULL_UP)
        self._down_at = 0
        self._was_down = False
        self._clicks = 0
        self._last_up = 0

    def poll(self):
        now = time.ticks_ms()
        down = self.pin.value() == 0

        if down and not self._was_down:
            self._was_down = True
            self._down_at = now
            return None

        if not down and self._was_down:
            held = time.ticks_diff(now, self._down_at)
            self._was_down = False
            if held < DEBOUNCE_MS:
                return None
            if held >= LONG_MS:
                self._clicks = 0
                return "long"
            self._clicks += 1
            self._last_up = now
            return None

        if self._clicks and time.ticks_diff(now, self._last_up) >= MULTI_MS:
            n = min(self._clicks, 3)
            self._clicks = 0
            return ("single", "double", "triple")[n - 1]
        return None

    def held_ms(self):
        if not self._was_down:
            return 0
        return time.ticks_diff(time.ticks_ms(), self._down_at)


# ---------------------------------------------------------------------------
class UI:

    def __init__(self):
        self.tft = ILI9341Par(rotation=ROTATION)
        self.tft.fill_screen(BG)
        centre_text(self.tft, "GENIE", 108, CYAN, scale=4)
        time.sleep_ms(600)
        self.tft.fill_screen(BG)

        self.eyes = eyemod.RoboEyes(self.tft, eye_w=EYE_W, eye_h=EYE_H,
                                    gap=EYE_GAP, cy=CY_EYES)
        self.sen = sensors.Sensors()
        self.btn = Button()

        self.idx = 0
        self.page = "eyes"
        self.prev_page = "both"
        self.last_input = time.ticks_ms()
        self._shown = None
        self._hold_bar = -1
        self._all_at = 0
        self._btn_lit = None
        self._panel_at = time.ticks_ms()
        self._revives = 0

        # Reporter owns the serial protocol, the fall FSM's fast IMU poll
        # and the ESP-NOW link to Node 2. Constructed last: everything it
        # reads has to exist first.
        self.rep = None
        if node1_report is not None:
            try:
                self.rep = node1_report.Reporter(self.sen)
            except Exception as e:
                print("node1_report failed to start (face runs anyway):", e)

    # -- helpers ------------------------------------------------------------
    def key(self):
        return ORDER[self.idx % len(ORDER)]

    # -- REPL controls: drive the whole UI with the button unplugged --------
    def next(self):
        self.handle("single")

    def prev(self):
        self.handle("double")

    def page_next(self):
        self.handle("triple")

    def all(self):
        self.handle("long")

    def rotate(self, r=None):
        """Flip the panel live. No edit, no reboot."""
        global ROTATION
        ROTATION = (ROTATION ^ 2) if r is None else r
        self.tft.set_rotation(ROTATION)
        self.tft.fill_screen(BG)
        self.eyes._dirty = True
        self._shown = None
        self._hold_bar = -1
        print("rotation %d - brows ABOVE the eyes means this is the right "
              "one. Write it into ROTATION at the top of genie_ui.py."
              % ROTATION)
        return ROTATION

    def _set_page(self, page):
        if page == self.page:
            return
        self.page = page
        self.tft.fill_screen(BG)
        self._shown = None
        self._hold_bar = -1
        if page in ("eyes", "both"):
            self.eyes.move_to(CY_EYES if page == "eyes" else CY_BOTH)
            self.eyes._dirty = True

    # -- input --------------------------------------------------------------
    def handle(self, ev):
        if ev is None:
            return
        if DEBUG_BUTTON:
            print("btn: %-7s  page=%s -> sensor %s"
                  % (ev, self.page, self.key()))
        self.last_input = time.ticks_ms()

        if self.page == "all" and ev != "long":
            self._set_page(self.prev_page)
            return

        if ev in ("single", "double"):
            self.idx = (self.idx + (1 if ev == "single" else -1)) % len(ORDER)
            if self.page == "eyes":
                self._set_page("value")
            else:
                self._shown = None
        elif ev == "triple":
            here = self.page if self.page in PAGES else "eyes"
            self._set_page(PAGES[(PAGES.index(here) + 1) % len(PAGES)])
        elif ev == "long":
            self.prev_page = self.page if self.page in PAGES else "both"
            self.page = "all"
            self.tft.fill_screen(BG)
            self._hold_bar = -1
            self._all_at = time.ticks_ms()
            self.draw_all()

    # -- drawing ------------------------------------------------------------
    def draw_btn_dot(self):
        """A dot in the corner, lit while GPIO39 is low.

        This answers the only question that matters when nothing responds:
        is the PIN moving? Dot lights on press = wiring fine, look at the
        code. Dot never lights = the button never reaches the ESP32, and no
        amount of reading genie_ui.py will help.
        """
        lit = self.btn.pin.value() == 0
        if lit == self._btn_lit:
            return
        self._btn_lit = lit
        self.tft.fill_rect(304, 2, 12, 12, OK if lit else color565(30, 34, 40))

    def draw_hold_hint(self):
        h = self.btn.held_ms()
        step = -1 if h < 400 else min(10, (h - 400) * 10 // (LONG_MS - 400))
        if step == self._hold_bar:
            return
        self._hold_bar = step
        self.tft.fill_rect(0, 234, 320, 6, BG)
        if step > 0:
            self.tft.fill_rect(0, 234, step * 32, 6, DIM)

    def draw_dots(self, y):
        n = len(ORDER)
        w = 320 // n
        self.tft.fill_rect(0, y, 320, 4, BG)
        for i in range(n):
            c = CYAN if i == self.idx % n else color565(38, 42, 50)
            self.tft.fill_rect(i * w + w // 2 - 3, y, 6, 4, c)

    def draw_value_page(self):
        """VALUE: no eyes at all. The reading owns the whole panel."""
        key = self.key()
        val = self.sen.text(key)
        st = self.sen.status(key)
        stamp = ("value", key, val, st)
        if stamp == self._shown:
            return
        self._shown = stamp
        self.tft.fill_rect(0, 36, 320, 156, BG)
        centre_text(self.tft, self.sen.label(key), 44, LABEL, scale=3)
        big_text(self.tft, val, 102, STATUS_COLOUR[st])
        self.draw_dots(222)

    def draw_both_page(self):
        """BOTH: eyes on top, the reading underneath."""
        key = self.key()
        val = self.sen.text(key)
        st = self.sen.status(key)
        stamp = ("both", key, val, st)
        if stamp == self._shown:
            return
        self._shown = stamp
        self.tft.fill_rect(0, TEXT_Y, 320, 62, BG)
        centre_text(self.tft, self.sen.label(key), TEXT_Y, LABEL, scale=2)
        centre_text(self.tft, val, TEXT_Y + 22, STATUS_COLOUR[st], scale=4)
        self.draw_dots(230)

    def draw_all(self):
        centre_text(self.tft, "ALL SENSORS", 4, CYAN, scale=2)
        col_x = (8, 168)
        for i, key in enumerate(ORDER):
            x = col_x[i % 2]
            y = 34 + (i // 2) * 34
            if y > 206:
                break
            text(self.tft, self.sen.label(key)[:11], x, y, LABEL, scale=1)
            text(self.tft, self.sen.text(key)[:11], x, y + 12,
                 STATUS_COLOUR[self.sen.status(key)], scale=2)
        text(self.tft, "click to go back", 8, 224, DIM, scale=1)

    # -- panel recovery -----------------------------------------------------
    def check_panel(self):
        now = time.ticks_ms()
        if time.ticks_diff(now, self._panel_at) < PANEL_CHECK_MS:
            return
        self._panel_at = now
        ok, raw = panel_alive(self.tft)
        if ok:
            return
        self._revives += 1
        print("PANEL RESET (RDDPM=0x%02X) - re-initialising, #%d"
              % (raw & 0xFF, self._revives))
        try:
            self.tft._reset()
            self.tft._init_panel()
            self.tft.set_rotation(ROTATION)
        except Exception as e:
            print("  re-init failed: %r" % e)
            return
        self.tft.fill_screen(BG)
        self.eyes._dirty = True
        self._shown = None
        self._hold_bar = -1
        self._btn_lit = None

    # -- expression ---------------------------------------------------------
    def update_eyes(self):
        state = self.sen.mood()

        # The laptop can ask for an expression while it is thinking or
        # speaking. mood_override() returns None once the request expires
        # (20 s, per PROTOCOL.md) AND whenever a sensor is alarming -- so
        # a requested smile can never sit on the face during a gas leak.
        asked = self.rep.mood_override() if self.rep is not None else None

        # The idle ladder only ever applies to "calm". Anything a sensor
        # actually measured skips it entirely, so an alarm is instant no
        # matter how long the board has been sitting there.
        #
        # A laptop request skips it too, and must be applied AFTER it, not
        # before: GENIE talking to you is not GENIE being idle. Asking for
        # "happy" and then letting the ladder walk it down to "sleepy" mid
        # reply is the exact bug this ordering avoids.
        if state == "calm" and asked is None:
            quiet = time.ticks_diff(time.ticks_ms(), self.last_input)
            for limit, rung in IDLE_LADDER:
                if limit is None or quiet < limit:
                    state = rung
                    break

        if asked is not None:
            state = asked

        self.eyes.set_mood(MOOD[state])

        # The pupils track the board - but only once it has actually MOVED.
        # sensors.tilt() is relative to the resting angle, and anything
        # inside the deadzone hands the eyes back to RoboEyes so they wander
        # and blink on their own. A face that never moves its eyes looks
        # cross no matter which mood it is wearing.
        dx, dy = self.sen.tilt()
        if dx is None or (abs(dx) < TILT_DEADZONE and abs(dy) < TILT_DEADZONE):
            self.eyes.set_idle(True)
        else:
            self.eyes.set_idle(False)
            self.eyes.look(dx, dy)

        self.eyes.update()

    # -- main ---------------------------------------------------------------
    def loop(self):
        gc_at = time.ticks_ms()
        while True:
            self.handle(self.btn.poll())
            self.sen.tick()

            # Costs microseconds when there is nothing due. Placed with
            # sen.tick() and not inside a draw branch on purpose: the "all"
            # page below `continue`s, and telemetry must not stop just
            # because somebody is holding a menu open.
            if self.rep is not None:
                self.rep.tick()

            if self.page == "all":
                if time.ticks_diff(time.ticks_ms(), self._all_at) \
                        > ALL_TIMEOUT_MS:
                    self._set_page(self.prev_page)
                else:
                    time.sleep_ms(10)
                    continue

            if time.ticks_diff(time.ticks_ms(), self.last_input) >= IDLE_MS \
                    and self.page != "eyes":
                self._set_page("eyes")

            if self.page == "value":
                self.draw_value_page()
            else:
                self.update_eyes()
                if self.page == "both":
                    self.draw_both_page()

            self.draw_hold_hint()
            if DEBUG_BUTTON:
                self.draw_btn_dot()
            self.check_panel()

            if time.ticks_diff(time.ticks_ms(), gc_at) > 5000:
                gc_at = time.ticks_ms()
                gc.collect()

            time.sleep_ms(5)


# The live instance, so the REPL can drive it with the button unplugged:
#     import genie_ui
#     genie_ui.run()          # Ctrl-C
#     genie_ui.ui.next()
#     genie_ui.flip()
ui = None


def flip(r=None):
    """Rotate the panel 180 degrees, live. Brows above the eyes = correct."""
    if ui is None:
        print("nothing running. genie_ui.run() first, then Ctrl-C.")
        return None
    return ui.rotate(r)


def run():
    global ui
    try:
        ui = UI()
        ui.loop()
    except KeyboardInterrupt:
        if ui:
            ui.sen.stop()
            ui.tft.fill_screen(BG)
            centre_text(ui.tft, "stopped", 110, DIM, scale=2)
        print("stopped. ui.next() / ui.page_next() / genie_ui.flip() still "
              "work from here.")


if __name__ == "__main__":
    run()
