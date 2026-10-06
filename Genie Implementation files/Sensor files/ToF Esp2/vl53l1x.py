"""VL53L1X driver — MicroPython, compact port of ST's ultra-lite driver.

MUST be saved onto the BOARD.  Thonny: File > Save as... > MicroPython device

Implements what this project needs and nothing else:
    init, model ID check, address reassignment, short/long distance mode,
    start/stop ranging, data-ready poll, distance in mm, range status.

NOT implemented: calibration, ROI, timing budget, interrupt thresholds.
Timing budget stays at the ~100 ms default. If you later need faster than
10 Hz, that is the thing to add.

All registers are 16-bit addressed. That is the main difference from the
VL53L0X and the usual reason L0X code fails silently on an L1X.
"""

import time

VERSION = "vl53l1x 1.0  2026-08-15"

_SOFT_RESET = 0x0000
_I2C_SLAVE_DEVICE_ADDRESS = 0x0001
_GPIO_HV_MUX_CTRL = 0x0030
_GPIO_TIO_HV_STATUS = 0x0031
_PHASECAL_TIMEOUT_MACROP = 0x004B
_RANGE_CONFIG_VCSEL_PERIOD_A = 0x0060
_RANGE_CONFIG_VCSEL_PERIOD_B = 0x0063
_RANGE_CONFIG_VALID_PHASE_HIGH = 0x0069
_SD_CONFIG_WOI_SD0 = 0x0078
_SD_CONFIG_INITIAL_PHASE_SD0 = 0x007A
_SYSTEM_INTERRUPT_CLEAR = 0x0086
_SYSTEM_MODE_START = 0x0087
_RESULT_RANGE_STATUS = 0x0089
_RESULT_DISTANCE_MM = 0x0096
_FIRMWARE_SYSTEM_STATUS = 0x00E5
_IDENTIFICATION_MODEL_ID = 0x010F

MODEL_ID = 0xEACC

SHORT, LONG = 1, 2

# 91 bytes written to 0x2D..0x87 at boot. From ST's ULD default config.
_DEFAULT_CONFIG = bytes((
    0x00, 0x00, 0x00, 0x01, 0x02, 0x00, 0x02, 0x08,   # 0x2D-0x34
    0x00, 0x08, 0x10, 0x01, 0x01, 0x00, 0x00, 0x00,   # 0x35-0x3C
    0x00, 0xFF, 0x00, 0x0F, 0x00, 0x00, 0x00, 0x00,   # 0x3D-0x44
    0x00, 0x20, 0x0B, 0x00, 0x00, 0x02, 0x0A, 0x21,   # 0x45-0x4C
    0x00, 0x00, 0x05, 0x00, 0x00, 0x00, 0x00, 0xC8,   # 0x4D-0x54
    0x00, 0x00, 0x38, 0xFF, 0x01, 0x00, 0x08, 0x00,   # 0x55-0x5C
    0x00, 0x01, 0xDB, 0x0F, 0x01, 0xF1, 0x0D, 0x01,   # 0x5D-0x64
    0x68, 0x00, 0x80, 0x08, 0xB8, 0x00, 0x00, 0x00,   # 0x65-0x6C
    0x00, 0x0F, 0x89, 0x00, 0x00, 0x00, 0x00, 0x00,   # 0x6D-0x74
    0x00, 0x00, 0x01, 0x0F, 0x0D, 0x0E, 0x0E, 0x00,   # 0x75-0x7C
    0x00, 0x02, 0xC7, 0xFF, 0x9B, 0x00, 0x00, 0x00,   # 0x7D-0x84
    0x01, 0x00, 0x00,                                  # 0x85-0x87
))
assert len(_DEFAULT_CONFIG) == 91, "config block must be 91 bytes"

# raw status -> (code, meaning). code 0 is the only trustworthy reading.
_STATUS = {
    9:  (0, "valid"),
    6:  (1, "sigma too high - noisy, target edge or low reflectivity"),
    4:  (2, "signal too weak - target too far or too dark"),
    8:  (3, "below minimum range"),
    5:  (4, "out of bounds - phase wrapped, target beyond max"),
    3:  (5, "hardware fail"),
    19: (6, "no update"),
    7:  (7, "wrapped target - reading is ambiguous"),
    12: (9, "crosstalk fail"),
    18: (10, "sync int"),
    22: (11, "merged target"),
    23: (12, "signal too low"),
}


class VL53L1X:
    def __init__(self, i2c, address=0x29):
        self.i2c = i2c
        self.address = address

    # ------------------------------------------------------------ registers

    def _w(self, reg, data):
        self.i2c.writeto(self.address, bytes((reg >> 8, reg & 0xFF)) + data)

    def _w8(self, reg, val):
        self._w(reg, bytes((val,)))

    def _w16(self, reg, val):
        self._w(reg, bytes((val >> 8, val & 0xFF)))

    def _r(self, reg, n):
        self.i2c.writeto(self.address, bytes((reg >> 8, reg & 0xFF)), False)
        return self.i2c.readfrom(self.address, n)

    def _r8(self, reg):
        return self._r(reg, 1)[0]

    def _r16(self, reg):
        d = self._r(reg, 2)
        return (d[0] << 8) | d[1]

    # ----------------------------------------------------------------- init

    def model_id(self):
        return self._r16(_IDENTIFICATION_MODEL_ID)

    def init(self, mode=LONG, timeout_ms=1000):
        mid = self.model_id()
        if mid != MODEL_ID:
            raise OSError("not a VL53L1X at 0x%02X (model ID 0x%04X)" %
                          (self.address, mid))

        # wait for firmware boot
        t0 = time.ticks_ms()
        while not (self._r8(_FIRMWARE_SYSTEM_STATUS) & 0x01):
            if time.ticks_diff(time.ticks_ms(), t0) > timeout_ms:
                raise OSError("VL53L1X at 0x%02X never booted" % self.address)
            time.sleep_ms(10)

        self._w(0x002D, _DEFAULT_CONFIG)
        self.start()
        self.wait_ready()
        self.clear_interrupt()
        self.stop()
        self._w8(0x0008, 0x09)      # VHV config, per ULD init
        self._w8(0x000B, 0x00)
        self.set_distance_mode(mode)

    def set_address(self, new_address):
        """Reassign the I2C address. VOLATILE — lost on every power cycle."""
        self._w8(_I2C_SLAVE_DEVICE_ADDRESS, new_address & 0x7F)
        self.address = new_address & 0x7F
        time.sleep_ms(2)

    def set_distance_mode(self, mode):
        if mode == SHORT:               # up to ~1.3 m, better ambient immunity
            self._w8(_PHASECAL_TIMEOUT_MACROP, 0x14)
            self._w8(_RANGE_CONFIG_VCSEL_PERIOD_A, 0x07)
            self._w8(_RANGE_CONFIG_VCSEL_PERIOD_B, 0x05)
            self._w8(_RANGE_CONFIG_VALID_PHASE_HIGH, 0x38)
            self._w16(_SD_CONFIG_WOI_SD0, 0x0705)
            self._w16(_SD_CONFIG_INITIAL_PHASE_SD0, 0x0606)
        else:                           # LONG, up to ~4 m
            self._w8(_PHASECAL_TIMEOUT_MACROP, 0x0A)
            self._w8(_RANGE_CONFIG_VCSEL_PERIOD_A, 0x0F)
            self._w8(_RANGE_CONFIG_VCSEL_PERIOD_B, 0x0D)
            self._w8(_RANGE_CONFIG_VALID_PHASE_HIGH, 0xB8)
            self._w16(_SD_CONFIG_WOI_SD0, 0x0F0D)
            self._w16(_SD_CONFIG_INITIAL_PHASE_SD0, 0x0E0E)
        self.mode = mode

    # -------------------------------------------------------------- ranging

    def start(self):
        self._w8(_SYSTEM_MODE_START, 0x40)

    def stop(self):
        self._w8(_SYSTEM_MODE_START, 0x00)

    def data_ready(self):
        pol = 0 if (self._r8(_GPIO_HV_MUX_CTRL) & 0x10) else 1
        return (self._r8(_GPIO_TIO_HV_STATUS) & 0x01) == pol

    def wait_ready(self, timeout_ms=1000):
        t0 = time.ticks_ms()
        while not self.data_ready():
            if time.ticks_diff(time.ticks_ms(), t0) > timeout_ms:
                raise OSError("VL53L1X at 0x%02X: no data ready" % self.address)
            time.sleep_ms(2)

    def clear_interrupt(self):
        self._w8(_SYSTEM_INTERRUPT_CLEAR, 0x01)

    def status(self):
        raw = self._r8(_RESULT_RANGE_STATUS) & 0x1F
        return _STATUS.get(raw, (255, "unknown raw status %d" % raw))

    def distance(self):
        """mm, or None if the reading is not trustworthy. Blocks ~<100 ms."""
        self.wait_ready()
        code, _ = self.status()
        mm = self._r16(_RESULT_DISTANCE_MM)
        self.clear_interrupt()
        return mm if code == 0 else None

    def distance_verbose(self):
        """(mm, status_code, status_text) — mm is returned even when invalid."""
        self.wait_ready()
        code, text = self.status()
        mm = self._r16(_RESULT_DISTANCE_MM)
        self.clear_interrupt()
        return mm, code, text
