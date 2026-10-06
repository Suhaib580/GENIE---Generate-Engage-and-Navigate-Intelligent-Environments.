"""
Wraps Piper for streaming speech synthesis.

Yields raw 16-bit PCM audio chunks as they are generated, rather than
waiting for the whole utterance -- this is what lets the server start
sending audio to the ESP before the full sentence has finished synthesizing.
"""

import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from piper import PiperVoice, SynthesisConfig
from config import (
    PIPER_MODEL_PATH,
    TTS_VOLUME,
    TTS_SOFT_LIMIT,
    TTS_NOISE_SCALE,
    TTS_NOISE_W_SCALE,
)


# ------------------------------------------------------------------
# Per-destination output levels.
#
# The SAME Piper audio goes to two different DACs that want wildly
# different amplitudes, so the gain belongs to the DESTINATION, not to the
# voice and not to the stored file:
#
#   GAIN_SPEAKER  -> MAX98357A driving a real speaker. Full scale.
#   GAIN_MIC_LINE -> PCM5102A wired into the modem's MIC+/MIC- pins
#                    THROUGH the attenuator in docs/emergency_call_audio.md.
#
# Do NOT put the mic figure in TTS_VOLUME - that is global, and it would
# make GENIE's own speaker inaudible too.
#
# On GAIN_MIC_LINE's value: getting from the DAC's 2.1 Vrms full scale
# down to the ~10 mV a mic input expects is about 46 dB of attenuation,
# and WHERE that happens matters as much as the total.
#
# This used to be 0.012 (-38 dB), which did the whole job in software.
# That works in the sense that the level is right, but it wastes the
# DAC: Piper ends up using ~1% of its dynamic range while the PCM5102A's
# own analog noise floor stays exactly where it was. The far end hears a
# faint voice riding on full-strength hiss.
#
# So the split is deliberate: the R1/R2 divider carries ~40 dB in
# hardware, and this carries the remaining ~8 dB. The DAC now runs 40 dB
# above its own noise.
#
#     2.1 Vrms * 0.4 = 0.84 Vrms  ->  /101 divider  ->  ~8.3 mVrms
#
# If the far end is too quiet, turn the R2 trimpot BEFORE raising this -
# hardware trim keeps the signal-to-noise ratio, software gain does not.
# ------------------------------------------------------------------

GAIN_SPEAKER = 1.0
GAIN_MIC_LINE = 0.4


def soft_limit(pcm, drive):
    """Round the peaks off instead of chopping them, so the BODY can be louder.

    MEASURED on a real utterance before this existed:

        peak 32767 of 32767   rms 6193  (-14.5 dBFS)   120 clipped samples

    Piper's output was already pinned against the ceiling -- TTS_VOLUME is
    1.4 and there was not a single dB of headroom left. So the obvious fix
    for "too quiet", turning the volume up, could only have added
    distortion. The problem was never the peaks; it was the ~14 dB gap
    between them and the average, with a handful of transients holding the
    ceiling while the speech itself sat far below it.

    tanh waveshaping closes that gap:

        drive 1.5   rms -11.4 dBFS   +3.0 dB    0 clipped
        drive 2.0   rms -10.2 dBFS   +4.3 dB    0 clipped   <- shipped
        drive 3.0   rms  -8.2 dBFS   +6.2 dB  120 clipped
        drive 4.0   rms  -7.0 dBFS   +7.5 dB  120 clipped

    2.0 is louder AND cleaner than doing nothing: the transients that were
    being hard-clipped get curved instead, so the count goes 120 -> 0.
    Past 3.0 it starts clipping again and audibly hardens.

    SAFE TO APPLY PER CHUNK. tanh is memoryless -- every sample maps
    through the same curve regardless of neighbours -- so chunk-by-chunk
    gives bit-identical output to processing the whole utterance, with no
    discontinuity at the seams. A compressor with attack and release
    would NOT have that property and would need lookahead the streaming
    path cannot give it.
    """
    if not drive:
        return pcm
    import math
    import numpy as np
    a = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32767.0
    y = np.tanh(a * drive) / math.tanh(drive)
    return np.clip(y * 32767.0, -32768, 32767).astype("<i2").tobytes()


def scale_pcm16(pcm, gain):
    """
    Scale a chunk of little-endian int16 PCM, clipping rather than wrapping.

    numpy is imported here rather than at module scope on purpose: the speaker
    path never calls this, so an environment without numpy can still import and
    use TTSService exactly as before.
    """
    import numpy as np

    a = np.frombuffer(pcm, dtype="<i2").astype(np.float32) * gain
    return np.clip(a, -32768, 32767).astype("<i2").tobytes()


class TTSService:

    def __init__(self):
        self.voice = PiperVoice.load(PIPER_MODEL_PATH)
        self.sample_rate = self.voice.config.sample_rate
        self.sample_width = 2   # 16-bit
        self.channels = 1

        self.syn_config = SynthesisConfig(
            volume=TTS_VOLUME,
            noise_scale=TTS_NOISE_SCALE,
            noise_w_scale=TTS_NOISE_W_SCALE,
        )

    def synthesize_chunks(self, text, gain=GAIN_SPEAKER):
        """
        Generator: yields raw int16 PCM bytes, chunk by chunk, as Piper
        produces them.

        gain scales for the destination - GAIN_SPEAKER for the speaker board,
        GAIN_MIC_LINE for the PCM5102A feeding the modem's mic pins. At the
        default the bytes are passed through untouched, so the existing
        speaker path is bit-for-bit unchanged.

        No resampling happens here on purpose: the modem's mic input is
        analog, so it does not care about sample rate. Piper's native rate is
        fine as long as the ESP's I2S TX rate matches it.
        """
        if not text or not text.strip():
            return

        for chunk in self.voice.synthesize(text, syn_config=self.syn_config):
            pcm = chunk.audio_int16_bytes
            # Limit BEFORE the destination gain, not after. The speaker
            # path uses gain 1.0 so the order is moot there, but the modem
            # path attenuates by 0.4 -- limiting after that would shape the
            # curve around a signal already 8 dB down and do almost
            # nothing. Shaping at full scale first, then attenuating, keeps
            # the loudness the limiter recovered.
            pcm = soft_limit(pcm, TTS_SOFT_LIMIT)
            yield pcm if gain == 1.0 else scale_pcm16(pcm, gain)


if __name__ == "__main__":
    # Standalone test: python Services/tts_service.py
    tts = TTSService()
    print(f"Voice sample rate: {tts.sample_rate} Hz")
    print("IMPORTANT: the ESP's I2S TX 'rate' must match this exactly.")

    total_bytes = 0
    chunk_count = 0

    for chunk in tts.synthesize_chunks("Hello, this is Genie speaking."):
        total_bytes += len(chunk)
        chunk_count += 1

    print(f"Generated {chunk_count} chunks, {total_bytes} bytes total.")