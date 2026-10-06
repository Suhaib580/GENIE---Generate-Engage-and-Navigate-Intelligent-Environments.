"""
ILI9341 over an 8-bit 8080 parallel bus, for MicroPython on ESP32-S3.

Stock MicroPython has no driver for this combination, so this one talks to the
GPIO registers directly. The hot loops use @micropython.viper, which compiles to
native code and is roughly 30-50x faster than plain Python bit-banging.

Wiring (GENIE display node):
    LCD_D0..D7 -> GPIO 4, 5, 6, 7, 15, 16, 17, 18
    LCD_WR     -> GPIO 8
    LCD_RD     -> GPIO 9
    LCD_RS/DC  -> GPIO 10
    LCD_CS     -> GPIO 11
    LCD_RST    -> GPIO 12

Colour convention:
    fill_rect() / fill_screen() take a normal RGB565 int -> use color565()
    framebuf drawing needs byte-swapped values           -> use fbcolor()

CLIPPING (added 05-08-2026)
    The old _window() packed raw coordinates with bytes(). A negative x or y
    made bytes() raise "ValueError: bytes value out of range" from deep inside
    the driver, which pointed at the wrong file entirely - the real bug was
    always in the caller's geometry.

    fill_rect() and blit() now clip to the panel and draw the visible part.
    Anything fully off-screen is skipped. _window() itself still refuses
    out-of-range input, but with a message that names the numbers.

    Set DEBUG_CLIP = True to print every rect that had to be clipped. That
    print IS the bug report - it names the caller and the overshoot in pixels.
    tft.clipped counts them, so a soak test can assert it stayed at zero.
"""

import machine
import time
import framebuf

DEBUG_CLIP = True          # set False once nothing is drawing off-panel

# --- ESP32-S3 GPIO registers (all our pins are below GPIO32) ----------------
_GPIO_OUT_W1TS = const(0x60004008)
_GPIO_OUT_W1TC = const(0x6000400C)

# --- pin assignment ---------------------------------------------------------
_D_PINS = (4, 5, 6, 7, 15, 16, 17, 18)
_PIN_WR = 8
_PIN_RD = 9
_PIN_DC = 10
_PIN_CS = 11
_PIN_RST = 12

# D0..D3 sit on GPIO4..7, D4..D7 on GPIO15..18, so a byte scatters as:
#     ((b & 0x0F) << 4) | ((b & 0xF0) << 11)
_DATA_MASK = const(0x000780F0)
_WR_BIT = const(0x00000100)   # 1 << 8


# ---------------------------------------------------------------------------
# viper hot loops
# ---------------------------------------------------------------------------
@micropython.viper
def _blast(n: int, hi: int, lo: int):
    """Push the same 16-bit colour n times. Used by every solid fill."""
    ts = ptr32(_GPIO_OUT_W1TS)
    tc = ptr32(_GPIO_OUT_W1TC)
    clr = int(_DATA_MASK | _WR_BIT)
    wr = int(_WR_BIT)

    h = ((hi & 0x0F) << 4) | ((hi & 0xF0) << 11)
    l = ((lo & 0x0F) << 4) | ((lo & 0xF0) << 11)

    i = 0
    while i < n:
        tc[0] = clr
        ts[0] = h
        ts[0] = wr
        tc[0] = clr
        ts[0] = l
        ts[0] = wr
        i += 1


@micropython.viper
def _blast_bytes(buf: ptr8, n: int):
    """Push n raw bytes from a buffer. Used to blit framebuf contents."""
    ts = ptr32(_GPIO_OUT_W1TS)
    tc = ptr32(_GPIO_OUT_W1TC)
    clr = int(_DATA_MASK | _WR_BIT)
    wr = int(_WR_BIT)

    i = 0
    while i < n:
        b = int(buf[i])
        d = ((b & 0x0F) << 4) | ((b & 0xF0) << 11)
        tc[0] = clr
        ts[0] = d
        ts[0] = wr
        i += 1


@micropython.viper
def _blast_row(buf: ptr8, off: int, n: int):
    """Push n bytes starting at off. Used by the clipped blit path only.

    Taking an offset instead of slicing the buffer keeps this allocation-free,
    which matters because the clipped path runs once per visible row.
    """
    ts = ptr32(_GPIO_OUT_W1TS)
    tc = ptr32(_GPIO_OUT_W1TC)
    clr = int(_DATA_MASK | _WR_BIT)
    wr = int(_WR_BIT)

    i = off
    end = off + n
    while i < end:
        b = int(buf[i])
        d = ((b & 0x0F) << 4) | ((b & 0xF0) << 11)
        tc[0] = clr
        ts[0] = d
        ts[0] = wr
        i += 1


# ---------------------------------------------------------------------------
def color565(r, g, b):
    """Normal RGB565, for fill_rect / fill_screen."""
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)


def fbcolor(c):
    """Byte-swapped RGB565, for drawing into a framebuf."""
    return ((c & 0xFF) << 8) | (c >> 8)


class ILI9341Par:

    WIDTH = 240
    HEIGHT = 320

    _MADCTL = (0x48, 0x28, 0x88, 0xE8)   # rotation 0..3

    def __init__(self, rotation=1):
        self._dpins = [machine.Pin(p, machine.Pin.OUT) for p in _D_PINS]
        self._wr = machine.Pin(_PIN_WR, machine.Pin.OUT, value=1)
        self._rd = machine.Pin(_PIN_RD, machine.Pin.OUT, value=1)
        self._dc = machine.Pin(_PIN_DC, machine.Pin.OUT, value=1)
        self._cs = machine.Pin(_PIN_CS, machine.Pin.OUT, value=1)
        self._rst = machine.Pin(_PIN_RST, machine.Pin.OUT, value=1)

        self.rotation = rotation
        self.width = self.HEIGHT if rotation & 1 else self.WIDTH
        self.height = self.WIDTH if rotation & 1 else self.HEIGHT

        self.clipped = 0        # running count of off-panel draws

        self._reset()
        self._init_panel()
        self.set_rotation(rotation)

    # -- low level ----------------------------------------------------------
    def _reset(self):
        self._rst.value(1); time.sleep_ms(10)
        self._rst.value(0); time.sleep_ms(20)
        self._rst.value(1); time.sleep_ms(150)

    def _write_byte(self, b):
        machine.mem32[_GPIO_OUT_W1TC] = _DATA_MASK | _WR_BIT
        machine.mem32[_GPIO_OUT_W1TS] = ((b & 0x0F) << 4) | ((b & 0xF0) << 11)
        machine.mem32[_GPIO_OUT_W1TS] = _WR_BIT

    def cmd(self, c, data=None):
        self._cs.value(0)
        self._dc.value(0)
        self._write_byte(c)
        if data:
            self._dc.value(1)
            for b in data:
                self._write_byte(b)
        self._cs.value(1)

    def _init_panel(self):
        seq = (
            (0xEF, b'\x03\x80\x02'),
            (0xCF, b'\x00\xC1\x30'),
            (0xED, b'\x64\x03\x12\x81'),
            (0xE8, b'\x85\x00\x78'),
            (0xCB, b'\x39\x2C\x00\x34\x02'),
            (0xF7, b'\x20'),
            (0xEA, b'\x00\x00'),
            (0xC0, b'\x23'),
            (0xC1, b'\x10'),
            (0xC5, b'\x3E\x28'),
            (0xC7, b'\x86'),
            (0x36, b'\x48'),
            (0x3A, b'\x55'),
            (0xB1, b'\x00\x18'),
            (0xB6, b'\x08\x82\x27'),
            (0xF2, b'\x00'),
            (0x26, b'\x01'),
            (0xE0, b'\x0F\x31\x2B\x0C\x0E\x08\x4E\xF1\x37\x07\x10\x03\x0E\x09\x00'),
            (0xE1, b'\x00\x0E\x14\x03\x11\x07\x31\xC1\x48\x08\x0F\x0C\x31\x36\x0F'),
        )
        for c, d in seq:
            self.cmd(c, d)

        self.cmd(0x11)            # sleep out
        time.sleep_ms(120)
        self.cmd(0x29)            # display on
        time.sleep_ms(20)

    def set_rotation(self, r):
        r &= 3
        self.rotation = r
        self.width = self.HEIGHT if r & 1 else self.WIDTH
        self.height = self.WIDTH if r & 1 else self.HEIGHT
        self.cmd(0x36, bytes([self._MADCTL[r]]))

    # -- geometry -----------------------------------------------------------
    def _clip(self, x, y, w, h, who):
        """Clip a rect to the panel.

        Returns (x, y, w, h, sx, sy) where sx and sy are how many source
        columns and rows were cut off the left and top edges, or None if the
        rect is entirely off-panel.
        """
        x = int(x); y = int(y); w = int(w); h = int(h)
        if w <= 0 or h <= 0:
            return None

        ox, oy, ow, oh = x, y, w, h
        sx = 0
        sy = 0

        if x < 0:
            sx = -x
            w -= sx
            x = 0
        if y < 0:
            sy = -y
            h -= sy
            y = 0
        if x + w > self.width:
            w = self.width - x
        if y + h > self.height:
            h = self.height - y

        if w <= 0 or h <= 0:
            self.clipped += 1
            if DEBUG_CLIP:
                print("CLIP %s: (%d,%d %dx%d) is fully off the %dx%d panel"
                      % (who, ox, oy, ow, oh, self.width, self.height))
            return None

        if w != ow or h != oh:
            self.clipped += 1
            if DEBUG_CLIP:
                print("CLIP %s: asked (%d,%d %dx%d) -> drew (%d,%d %dx%d)"
                      % (who, ox, oy, ow, oh, x, y, w, h))

        return (x, y, w, h, sx, sy)

    def _window(self, x, y, w, h):
        """Set the RAM window. Coordinates must already be on the panel."""
        x1 = x + w - 1
        y1 = y + h - 1
        if w <= 0 or h <= 0 or x < 0 or y < 0 \
                or x1 >= self.width or y1 >= self.height:
            # This used to surface as "bytes value out of range", which named
            # the driver instead of the caller. Say what actually happened.
            raise ValueError(
                "window off panel: x=%d y=%d w=%d h=%d -> x1=%d y1=%d "
                "(panel is %dx%d)"
                % (x, y, w, h, x1, y1, self.width, self.height))
        self.cmd(0x2A, bytes((x >> 8, x & 0xFF, x1 >> 8, x1 & 0xFF)))
        self.cmd(0x2B, bytes((y >> 8, y & 0xFF, y1 >> 8, y1 & 0xFF)))

    # -- drawing ------------------------------------------------------------
    def fill_rect(self, x, y, w, h, color):
        c = self._clip(x, y, w, h, "fill_rect")
        if c is None:
            return
        cx, cy, cw, ch, _, _ = c

        self._window(cx, cy, cw, ch)
        self._cs.value(0)
        self._dc.value(0)
        self._write_byte(0x2C)          # RAMWR
        self._dc.value(1)
        _blast(cw * ch, color >> 8, color & 0xFF)
        self._cs.value(1)

    def fill_screen(self, color):
        self.fill_rect(0, 0, self.width, self.height, color)

    def blit(self, x, y, w, h, buf):
        """Push a framebuf's RGB565 buffer to the panel, clipping if needed."""
        c = self._clip(x, y, w, h, "blit")
        if c is None:
            return
        cx, cy, cw, ch, sx, sy = c

        self._window(cx, cy, cw, ch)
        self._cs.value(0)
        self._dc.value(0)
        self._write_byte(0x2C)
        self._dc.value(1)

        if cw == w and ch == h:
            # fast path, byte for byte what the old driver did
            _blast_bytes(buf, cw * ch * 2)
        else:
            # clipped: walk the visible rows out of the source buffer
            stride = w * 2
            n = cw * 2
            base = sy * stride + sx * 2
            for _ in range(ch):
                _blast_row(buf, base, n)
                base += stride

        self._cs.value(1)


class Canvas:
    """A framebuf you draw into, then blit in one shot. The sprite equivalent."""

    def __init__(self, w, h):
        self.w = w
        self.h = h
        self.buf = bytearray(w * h * 2)
        self.fb = framebuf.FrameBuffer(self.buf, w, h, framebuf.RGB565)

    def push(self, tft, x, y):
        tft.blit(x, y, self.w, self.h, self.buf)
