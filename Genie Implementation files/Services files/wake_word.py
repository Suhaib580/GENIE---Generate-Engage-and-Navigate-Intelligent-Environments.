import time

import numpy as np
from openwakeword.model import Model

from config import (
    WAKE_MODEL,
    WAKE_THRESHOLD,
    WAKE_COOLDOWN,
    WAKE_CONSECUTIVE,
)


# openWakeWord expects 16 kHz mono 16-bit audio. 80 ms (1280 samples) per
# predict() call is the framework's recommended block size.
_BLOCK_SAMPLES = 1280
_BLOCK_BYTES = _BLOCK_SAMPLES * 2


class WakeWordDetector:
    """Local wake-word gate built on openWakeWord (ONNX backend).

    Feed it mono 16-bit PCM with .process(pcm); it returns True the moment
    the wake word is detected. This is what opens a recording now -- raw
    VAD no longer does, so fan/noise bursts can't false-start.

    WAKE_MODEL is either a pretrained name ("hey_jarvis", ...) or a path to
    a custom .onnx ("hey_genie.onnx"). Only that one model is loaded, so we
    just take the max score it reports each block.
    """

    def __init__(self):
        self.model = Model(
            wakeword_models=[WAKE_MODEL],
            inference_framework="onnx",
        )

        self._buf = b""
        self.last_score = 0.0
        self.detect_score = 0.0   # the score that actually fired the last wake
        self._mute_until = 0.0
        # How many back-to-back blocks have cleared WAKE_THRESHOLD so far.
        # Any block that falls below resets it to zero -- that is the whole
        # point: we are looking for a SUSTAINED score, not a spike.
        self._run = 0

    def process(self, pcm):
        """Returns True once when the wake word is detected (honouring a
        short cooldown so one utterance can't fire it repeatedly)."""

        self._buf += pcm
        detected = False

        while len(self._buf) >= _BLOCK_BYTES:

            block = self._buf[:_BLOCK_BYTES]
            self._buf = self._buf[_BLOCK_BYTES:]

            samples = np.frombuffer(block, dtype=np.int16)

            scores = self.model.predict(samples)
            score = max(scores.values()) if scores else 0.0
            self.last_score = score

            now = time.time()

            if score >= WAKE_THRESHOLD:
                self._run += 1
            else:
                # One block below threshold breaks the run. A real wake word
                # holds its score across several consecutive blocks; an
                # impulsive false accept does not.
                self._run = 0

            if self._run >= WAKE_CONSECUTIVE and now >= self._mute_until:
                detected = True
                self.detect_score = score   # capture BEFORE reset() zeroes last_score
                self._mute_until = now + WAKE_COOLDOWN
                self.reset()
                break

        return detected

    def reset(self):
        """Clear buffered audio + prediction history so stale audio (e.g.
        the just-detected wake word) can't immediately re-trigger."""
        try:
            self.model.reset()
        except Exception:
            pass
        self._buf = b""
        self.last_score = 0.0
        self._run = 0
