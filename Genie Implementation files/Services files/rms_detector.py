import math
import struct

from config import DEBUG_RMS


HEARTBEAT_EVERY = 50   # ~3.2s of continuous silence between heartbeat lines


class RMSDetector:
    """Signal-level meter for console display ONLY.

    NOTE: this used to run the VAD state machine (START/STOP by RMS
    threshold). That job now belongs to VADetector (webrtcvad) -- see
    vad_detector.py -- because room-noise transients hit the same RMS as
    speech, so loudness can't separate them. What survives here is purely
    the RMS/Peak measurement + print-suppression used to show Mic A / Mic B
    signal levels in the console (handy for setting MIC_GAIN).
    """

    def __init__(self):
        self._silent_run = 0   # print-suppression only

    def calculate_rms(self, pcm):
        """Returns (rms, peak) for one chunk. peak is the largest absolute
        sample value seen, same raw int16 units as rms. peak/32767 is how
        much of the ADC's dynamic range this chunk actually used -- if peak
        stays low even up close, MIC_GAIN has real room to work with; if
        peak is already near 32767, more gain will just clip."""

        sample_count = len(pcm) // 2

        if sample_count == 0:
            return 0, 0

        samples = struct.unpack("<{}h".format(sample_count), pcm)

        square_sum = 0
        peak = 0

        for sample in samples:
            square_sum += sample * sample
            abs_sample = sample if sample >= 0 else -sample
            if abs_sample > peak:
                peak = abs_sample

        return math.sqrt(square_sum / sample_count), peak

    def should_print(self, rms, peak):
        """True if this reading is worth printing to console. Any
        non-zero reading always prints -- that's the one thing that
        actually matters. A run of exact zero/zero readings only prints
        the first one, then a heartbeat every HEARTBEAT_EVERY chunks, so
        "is anything reaching the mic at all" stays scannable at a glance
        instead of drowning in thousands of identical silent lines."""
        if rms != 0 or peak != 0:
            self._silent_run = 0
            return True
        self._silent_run += 1
        return self._silent_run == 1 or self._silent_run % HEARTBEAT_EVERY == 0
