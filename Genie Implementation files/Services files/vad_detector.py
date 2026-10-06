import time
import math
import struct
import collections

import webrtcvad

from config import (
    SAMPLE_RATE,
    VAD_AGGRESSIVENESS,
    VAD_FRAME_MS,
    VAD_WINDOW_MS,
    VAD_TRIGGER_RATIO,
    VAD_CONTINUE_RATIO,
    VAD_MIN_RMS,
    SILENCE_DURATION,
    POST_STOP_COOLDOWN,
)


def _frame_rms(frame):
    """RMS of one 16-bit PCM frame."""
    n = len(frame) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack("<{}h".format(n), frame)
    return math.sqrt(sum(s * s for s in samples) / n)


class VADetector:
    """Speech-activity detector: webrtcvad AND an RMS floor.

    Drop-in replacement for the old RMS-threshold VAD: feed it a mono
    16-bit PCM chunk with .process(pcm) and it returns exactly one of
    "START" / "CONTINUE" / "STOP" / "IDLE" for that chunk.

    A frame counts as speech ONLY if webrtcvad calls it speech AND its RMS
    clears VAD_MIN_RMS. That combination is deliberate:

      * webrtcvad alone misclassifies the quiet room-noise floor as speech
        (verified) -> the RMS floor rejects it.
      * an RMS floor alone can't tell a loud bang from a loud word (both
        cross it) -> webrtcvad rejects the non-speech transient.

    Decisions run on a sliding VAD_WINDOW_MS window, never a single frame:

      * START when >= VAD_TRIGGER_RATIO of the window is speech.
      * While recording, hold open while >= VAD_CONTINUE_RATIO is speech;
        once it drops below, run a silence timer and STOP after
        SILENCE_DURATION. Using the ratio (not one frame) is what stops a
        stray misclassified frame from resetting the timer forever.
    """

    IDLE = "IDLE"
    RECORDING = "RECORDING"

    def __init__(self):

        self.vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)

        # Bytes per webrtcvad frame: samples * 2 (16-bit).
        self.frame_bytes = int(
            SAMPLE_RATE * (VAD_FRAME_MS / 1000.0)
        ) * 2

        # Incomplete-frame carryover between chunks.
        self._buf = b""

        self.state = self.IDLE

        window_frames = max(1, int(VAD_WINDOW_MS / VAD_FRAME_MS))
        self._ring = collections.deque(maxlen=window_frames)

        self._silence_start = None
        self._cooldown_until = 0.0

        # Last rolling speech ratio -- exposed purely for the console
        # display so the operator can see how close things are to the
        # trigger/continue thresholds.
        self.speech_ratio = 0.0

    def begin_capture(self):
        """Force the detector straight into RECORDING.

        Used when a wake word has already decided START -- from here the
        VAD's only job is to find the END of the command. The silence
        timer won't fire for SILENCE_DURATION, which is the grace period
        for the user to actually start their command after "Hey Genie".
        """
        self.state = self.RECORDING
        self._silence_start = None
        self._ring.clear()
        self._buf = b""
        self.speech_ratio = 0.0

    def process(self, pcm):

        self._buf += pcm

        now = time.time()
        result = None

        while len(self._buf) >= self.frame_bytes:

            frame = self._buf[:self.frame_bytes]
            self._buf = self._buf[self.frame_bytes:]

            try:
                voiced = self.vad.is_speech(frame, SAMPLE_RATE)
            except Exception:
                voiced = False

            # Combined gate: webrtcvad AND loud enough.
            is_speech = voiced and (_frame_rms(frame) >= VAD_MIN_RMS)

            self._ring.append(is_speech)

            ratio = sum(1 for s in self._ring if s) / len(self._ring)
            self.speech_ratio = ratio

            # ------------------------------------------------
            # IDLE -> wait for sustained speech
            # ------------------------------------------------
            if self.state == self.IDLE:

                if now < self._cooldown_until:
                    continue

                if (
                    len(self._ring) == self._ring.maxlen
                    and ratio >= VAD_TRIGGER_RATIO
                ):
                    self.state = self.RECORDING
                    self._silence_start = None
                    result = "START"

            # ------------------------------------------------
            # RECORDING -> stop after sustained silence
            # ------------------------------------------------
            else:

                if ratio >= VAD_CONTINUE_RATIO:
                    self._silence_start = None
                else:
                    if self._silence_start is None:
                        self._silence_start = now
                    elif now - self._silence_start >= SILENCE_DURATION:

                        self.state = self.IDLE
                        self._cooldown_until = now + POST_STOP_COOLDOWN
                        self._silence_start = None
                        result = "STOP"

        if result is not None:
            return result

        return "CONTINUE" if self.state == self.RECORDING else "IDLE"
