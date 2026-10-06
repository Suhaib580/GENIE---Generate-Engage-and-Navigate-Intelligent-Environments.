"""
GENIE expressive eyes.

Geometry model adapted from the FluxGarage RoboEyes design: an eye is a rounded
rectangle, and every mood is a preset of four numbers -- width, height, corner
radius, eyelid coverage -- plus a colour. Everything tweens, so mood changes
glide instead of snapping.

Extends the original with colour (it targeted monochrome OLEDs) and per-eye
offscreen canvases so nothing flickers.

Usage:
    from eyes import RoboEyes, NEUTRAL, HAPPY, SAD, ANGRY, SURPRISED, SLEEPY

    eyes = RoboEyes(tft)
    eyes.set_mood(HAPPY)
    while True:
        eyes.update()        # non-blocking, call as often as you like
"""

import time
import random
from array import array
from ili9341_par import Canvas, color565, fbcolor

VERSION = "2026-08-13"

NEUTRAL = 0
HAPPY = 1
SAD = 2
ANGRY = 3
SURPRISED = 4
SLEEPY = 5

MOOD_NAMES = ("neutral", "happy", "sad", "angry", "surprised", "sleepy")

# per mood: (height scale, radius scale, eyelid scale, (r, g, b))
_MOODS = (
    (1.00, 0.28, 0.00, (0, 210, 235)),     # neutral  - cyan
    (0.78, 0.34, 0.00, (255, 190, 40)),    # happy    - warm amber
    (0.85, 0.26, 0.34, (60, 110, 210)),    # sad      - deep blue
    (0.80, 0.14, 0.40, (240, 70, 40)),     # angry    - red
    (1.22, 0.40, 0.00, (140, 245, 255)),   # surprised- bright cyan
    (0.26, 0.13, 0.00, (70, 90, 130)),     # sleepy   - dim slate
)

# per mood: (brow tilt, brow lift, pupil scale)
#
#   tilt   +ve drops the INNER end of the brow  -> angry, focused
#          -ve drops the OUTER end              -> sad, worried
#   lift   fraction of eye height above the eye. Bigger = more awake.
#   pupil  radius as a fraction of the eye's short side. A small pupil in a
#          wide eye reads as alarm; a large one reads as calm and friendly.
#          This is real: pupils constrict under adrenaline, and faces are read
#          by exactly these ratios, which is why it works on a rounded rect.
_FACE = (
    (0.00, 0.30, 0.30),                    # neutral  - level, open, calm
    (-0.18, 0.40, 0.34),                   # happy    - raised and arched
    (-0.34, 0.22, 0.30),                   # sad      - outer ends drooping
    (0.42, 0.16, 0.20),                    # angry    - inner ends driven down
    (0.00, 0.52, 0.16),                    # surprised- flung up, pupil tiny
    (-0.10, 0.06, 0.26),                   # sleepy   - low and flat
)

BROW_T = 0.13            # brow thickness as a fraction of eye height
BROW_W = 0.92            # brow width as a fraction of eye width
PUPIL_PARALLAX = 1.9     # pupil travels this much further than the eye body,
                         # which is what sells the eye as looking AT something
                         # rather than the whole face sliding sideways.
GLINT = 0.30             # highlight radius as a fraction of the pupil

_BG = color565(0, 0, 0)


def _round_rect(fb, x, y, w, h, r, c):
    if w <= 0 or h <= 0:
        return
    if r * 2 > h:
        r = h // 2
    if r * 2 > w:
        r = w // 2
    if r < 1:
        fb.fill_rect(x, y, w, h, c)
        return

    fb.fill_rect(x + r, y, w - 2 * r, h, c)
    fb.fill_rect(x, y + r, r, h - 2 * r, c)
    fb.fill_rect(x + w - r, y + r, r, h - 2 * r, c)

    x1 = x + r
    x2 = x + w - r - 1
    y1 = y + r
    y2 = y + h - r - 1
    fb.ellipse(x2, y1, r, r, c, True, 1)   # Q1 top right
    fb.ellipse(x1, y1, r, r, c, True, 2)   # Q2 top left
    fb.ellipse(x1, y2, r, r, c, True, 4)   # Q3 bottom left
    fb.ellipse(x2, y2, r, r, c, True, 8)   # Q4 bottom right


def _lerp(a, b, k):
    return a + (b - a) * k


class RoboEyes:

    def __init__(self, tft, eye_w=88, eye_h=112, gap=44, cy=None, fps=40):
        self.tft = tft
        self.base_w = eye_w
        self.base_h = eye_h

        self.cw = eye_w + 14
        # Brows live ABOVE the eye, so the canvas needs headroom the eye itself
        # never uses. Rather than centre the eye and let the brow fall off the
        # top, reserve the space explicitly and push the eye down into what is
        # left. framebuf clips silently, so an un-reserved brow does not crash -
        # it just quietly goes missing, which is worse.
        self.brow_room = int(eye_h * 0.45)
        self.ch = int(eye_h * 1.35) + 16 + self.brow_room

        cy = tft.height // 2 if cy is None else cy
        cx = tft.width // 2
        half = (eye_w * 2 + gap) // 2
        self.lx = cx - half
        self.rx = cx + half - self.cw
        self.ty = cy - self.ch // 2

        # The canvas is TALLER than eye_h - 1.35x for the surprised stretch,
        # plus 16 px of padding. So cy needs ch // 2 of room above and below,
        # not eye_h // 2. Getting this wrong pushes ty negative, and a negative
        # coordinate is what used to blow up the driver with the useless
        # "bytes value out of range". Caught here instead, where the numbers
        # still mean something.
        #     eye_h=100, cy=70  ->  ch=151, ty=-5   CRASH
        #     eye_h=88,  cy=78  ->  ch=134, ty=11   fine, bottom at 145
        if self.ty < 0 or self.ty + self.ch > tft.height:
            safe = max(0, min(tft.height - self.ch, self.ty))
            print("eyes: cy=%d puts the %dx%d canvas at y=%d..%d on a %d px "
                  "panel. Clamped to y=%d. Lower eye_h or raise cy."
                  % (cy, self.cw, self.ch, self.ty, self.ty + self.ch,
                     tft.height, safe))
            self.ty = safe

        if self.lx < 0 or self.rx + self.cw > tft.width:
            print("eyes: eye_w=%d gap=%d needs %d px, panel is %d px wide. "
                  "Eyes will be clipped at the sides."
                  % (eye_w, gap, eye_w * 2 + gap, tft.width))
            self.lx = max(0, self.lx)
            self.rx = min(self.rx, tft.width - self.cw)

        self.canvas = Canvas(self.cw, self.ch)

        self.mood = NEUTRAL
        self._h, self._r, self._lid, rgb = _MOODS[NEUTRAL]
        self._cr, self._cg, self._cb = rgb
        self._th, self._tr, self._tlid = self._h, self._r, self._lid
        self._tcr, self._tcg, self._tcb = rgb

        self._tilt, self._lift, self._pup = _FACE[NEUTRAL]
        self._ttilt, self._tlift, self._tpup = _FACE[NEUTRAL]
        self.brows = True
        self.pupils = True

        self.blink_state = 0.0
        self._blinking = False
        self._blink_dir = -1
        self._next_blink = time.ticks_add(time.ticks_ms(), 3000)

        self.idle = True
        self._ox = 0.0
        self._oy = 0.0
        self._tox = 0.0
        self._toy = 0.0
        self._next_idle = time.ticks_add(time.ticks_ms(), 2500)

        self._frame_ms = 1000 // fps
        self._last = time.ticks_ms()
        self._dirty = True

    # -- public -------------------------------------------------------------
    def set_mood(self, m):
        m %= len(_MOODS)
        self.mood = m
        h, r, lid, rgb = _MOODS[m]
        self._th, self._tr, self._tlid = h, r, lid
        self._tcr, self._tcg, self._tcb = rgb
        self._ttilt, self._tlift, self._tpup = _FACE[m]
        self._dirty = True

    def set_face(self, brows=None, pupils=None):
        """Turn the brows or the pupils off. Both cost about 3 draw ops each,
        so this is for taste, not for frame rate."""
        if brows is not None:
            self.brows = brows
        if pupils is not None:
            self.pupils = pupils
        self._dirty = True

    def blink(self):
        if not self._blinking:
            self._blinking = True
            self._blink_dir = -1

    def look(self, dx, dy):
        """dx, dy in -1.0 .. 1.0"""
        self._tox = max(-1.0, min(1.0, dx)) * 10
        self._toy = max(-1.0, min(1.0, dy)) * 8

    def set_idle(self, on):
        self.idle = on
        if not on:
            self._tox = 0.0
            self._toy = 0.0

    def update(self):
        now = time.ticks_ms()
        if time.ticks_diff(now, self._last) < self._frame_ms:
            return False
        self._last = now

        self._tick_blink(now)
        self._tick_idle(now)
        self._tick_tween()

        if self._dirty:
            self._render()
            self._dirty = False
        return True

    # -- internals ----------------------------------------------------------
    def _tick_blink(self, now):
        if not self._blinking and time.ticks_diff(now, self._next_blink) >= 0:
            self._blinking = True
            self._blink_dir = -1

        if self._blinking:
            self.blink_state += self._blink_dir * 0.34
            if self.blink_state <= -1.0:
                self.blink_state = -1.0
                self._blink_dir = 1
            elif self.blink_state >= 0.0 and self._blink_dir > 0:
                self.blink_state = 0.0
                self._blinking = False
                self._next_blink = time.ticks_add(now, random.randint(2200, 6500))
            self._dirty = True

    def _tick_idle(self, now):
        if not self.idle:
            return
        if time.ticks_diff(now, self._next_idle) >= 0:
            self._tox = random.uniform(-1.0, 1.0) * 10
            self._toy = random.uniform(-1.0, 1.0) * 7
            self._next_idle = time.ticks_add(now, random.randint(1800, 5000))

    def _tick_tween(self):
        k = 0.22
        nh = _lerp(self._h, self._th, k)
        nr = _lerp(self._r, self._tr, k)
        nl = _lerp(self._lid, self._tlid, k)
        ncr = _lerp(self._cr, self._tcr, k)
        ncg = _lerp(self._cg, self._tcg, k)
        ncb = _lerp(self._cb, self._tcb, k)
        nox = _lerp(self._ox, self._tox, 0.14)
        noy = _lerp(self._oy, self._toy, 0.14)
        nti = _lerp(self._tilt, self._ttilt, k)
        nli = _lerp(self._lift, self._tlift, k)
        npu = _lerp(self._pup, self._tpup, k)

        if (abs(nh - self._h) > 0.002 or abs(nr - self._r) > 0.002 or
                abs(nl - self._lid) > 0.002 or abs(ncr - self._cr) > 1.0 or
                abs(ncg - self._cg) > 1.0 or abs(ncb - self._cb) > 1.0 or
                abs(nox - self._ox) > 0.3 or abs(noy - self._oy) > 0.3 or
                abs(nti - self._tilt) > 0.002 or abs(nli - self._lift) > 0.002
                or abs(npu - self._pup) > 0.002):
            self._dirty = True

        self._h, self._r, self._lid = nh, nr, nl
        self._cr, self._cg, self._cb = ncr, ncg, ncb
        self._ox, self._oy = nox, noy
        self._tilt, self._lift, self._pup = nti, nli, npu

    def _render(self):
        col = fbcolor(color565(int(self._cr), int(self._cg), int(self._cb)))
        bg = fbcolor(_BG)

        blink_k = 1.0 + self.blink_state * 0.92
        h = int(self.base_h * self._h * blink_k)
        if h < 4:
            h = 4
        w = self.base_w
        r = int(min(w, h) * self._r)

        # Keep the eye inside its own canvas. look()/idle drift can reach +-10
        # px, but the canvas only has (cw - w) // 2 = 7 px of slack either side,
        # so an untrimmed offset slices 3 px off the eye at full drift. framebuf
        # clips silently, so this never crashed - it just looked wrong.
        mx = (self.cw - w) // 2
        my = (self.ch - self.brow_room - h) // 2
        ox = int(self._ox)
        oy = int(self._oy)
        if ox > mx:
            ox = mx
        elif ox < -mx:
            ox = -mx
        if oy > my:
            oy = my
        elif oy < -my:
            oy = -my

        self._draw_one(True, w, h, r, col, bg, ox, oy)
        self._draw_one(False, w, h, r, col, bg, ox, oy)

    def _draw_one(self, is_left, w, h, r, col, bg, ox, oy):
        fb = self.canvas.fb
        fb.fill(bg)

        x = (self.cw - w) // 2 + ox
        y = self.brow_room + (self.ch - self.brow_room - h) // 2 + oy

        _round_rect(fb, x, y, w, h, r, col)

        open_eye = self.blink_state > -0.5

        # -- pupil ("retina") -------------------------------------------------
        # Drawn as a hole in the eye, not a shape on top of it, so it reads as
        # depth. It travels FURTHER than the eye body does, which is the whole
        # trick: the face looking at something, rather than sliding sideways.
        if self.pupils and open_eye:
            pr = int(min(w, h) * self._pup)
            if pr >= 2 and h > pr * 2 + 4:
                cx = x + w // 2 + int(self._ox * (PUPIL_PARALLAX - 1.0))
                cy = y + h // 2 + int(self._oy * (PUPIL_PARALLAX - 1.0))
                # keep the pupil fully inside the eye, or it smears the edge
                lim_x = w // 2 - pr - max(1, r // 2)
                lim_y = h // 2 - pr - 1
                if lim_x < 0:
                    lim_x = 0
                if lim_y < 0:
                    lim_y = 0
                ex, ey = x + w // 2, y + h // 2
                cx = ex + max(-lim_x, min(lim_x, cx - ex))
                cy = ey + max(-lim_y, min(lim_y, cy - ey))
                fb.ellipse(cx, cy, pr, pr, bg, True)
                gr = int(pr * GLINT)
                if gr >= 1:
                    fb.ellipse(cx - pr // 3, cy - pr // 3, gr, gr, col, True)

        if self.mood == HAPPY and open_eye:
            fb.ellipse(self.cw // 2 + ox, y + h + int(h * 0.42),
                       int(w * 0.78), int(h * 0.72), bg, True)

        lid = int(h * self._lid)
        if lid > 1 and open_eye:
            thick_right = ((self.mood == ANGRY and is_left) or
                           (self.mood == SAD and not is_left))
            if thick_right:
                pts = array('h', [0, 0, w, 0, w, lid])
            else:
                pts = array('h', [0, 0, w, 0, 0, lid])
            fb.poly(x, y, pts, bg, True)

        # -- brow -------------------------------------------------------------
        # A quad, not a rectangle, so the two ends sit at different heights and
        # the brow reads as tilted rather than merely moved. Mirrored between
        # the eyes: "inner" is the right end of the left eye and vice versa,
        # which is what makes an angry face angry instead of just lopsided.
        if self.brows and open_eye:
            bt = max(2, int(h * BROW_T))
            bw = int(w * BROW_W)
            bx = x + (w - bw) // 2
            by = y - int(h * self._lift) - bt
            drop = int(h * self._tilt * 0.55)
            inner_left = not is_left          # inner end of the RIGHT eye is
                                              # on its left-hand side
            d0 = drop if inner_left else 0
            d1 = 0 if inner_left else drop
            top = by + min(d0, d1)
            if top < 0:                        # never let it leave the canvas
                by -= top
            pts = array('h', [0, d0,
                              bw, d1,
                              bw, d1 + bt,
                              0, d0 + bt])
            fb.poly(bx, by, pts, col, True)

        self.canvas.push(self.tft, self.lx if is_left else self.rx, self.ty)

    # -- repositioning without reallocating ----------------------------------
    def move_to(self, cy):
        """Slide the whole face up or down. The canvas is NOT rebuilt, so this
        is free - which is the only reason the eyes can be full-screen when
        idle and tucked at the top when a reading is on screen. Rebuilding
        RoboEyes to do that would churn ~20 kB of heap every button press.

        Returns the rows that need clearing, or None if nothing moved.
        """
        ty = int(cy) - self.ch // 2
        if ty < 0:
            ty = 0
        elif ty + self.ch > self.tft.height:
            ty = self.tft.height - self.ch
        if ty == self.ty:
            return None
        old = self.ty
        self.ty = ty
        self._dirty = True
        return (min(old, ty), max(old, ty) + self.ch)
