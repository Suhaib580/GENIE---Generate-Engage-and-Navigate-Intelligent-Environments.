"""
Build the emergency clip that GENIE plays down a phone call.

Run this ONCE, on the PC. It produces a WAV already scaled to microphone
level, which you then copy to the modem ESP's flash. At emergency time the ESP
reads that file straight off flash - no server, no WiFi, no Piper, no Whisper.

    python make_emergency_clip.py
    python make_emergency_clip.py --gain 0.02 --text "Custom message here."
    python make_emergency_clip.py --from-recording my_voice.wav

Then check the level landed where you expect:

    ffmpeg -i emergency_mic.wav -af volumedetect -f null -
    # max_volume should read about -38 dB at the default gain
"""

import argparse
import wave

import numpy as np

from Services.tts_service import TTSService, GAIN_SPEAKER, scale_pcm16

DEFAULT_TEXT = (
    "This is an automated emergency alert from GENIE. "
    "Assistance is needed at the registered address. "
    "This message will repeat."
)

OUTPUT = "emergency_speech.wav"


def from_piper(text, gain):
    """Synthesize with Piper and scale to mic level. Returns (pcm_bytes, rate)."""
    tts = TTSService()
    pcm = b"".join(tts.synthesize_chunks(text, gain=gain))
    return pcm, tts.sample_rate


def from_recording(path, gain):
    """Take an existing 16-bit WAV (your own voice) and scale it to mic level."""
    with wave.open(path, "rb") as w:
        if w.getsampwidth() != 2:
            raise SystemExit("%s must be 16-bit PCM" % path)
        rate, channels = w.getframerate(), w.getnchannels()
        pcm = w.readframes(w.getnframes())

    if channels > 1:
        a = np.frombuffer(pcm, dtype="<i2").reshape(-1, channels)
        pcm = a.mean(axis=1).astype("<i2").tobytes()

    return scale_pcm16(pcm, gain), rate


def write_wav(path, pcm, rate):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def peak_dbfs(pcm):
    a = np.frombuffer(pcm, dtype="<i2")
    peak = int(np.abs(a).max()) if a.size else 0
    return -np.inf if peak == 0 else 20 * np.log10(peak / 32768.0)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--text", default=DEFAULT_TEXT)
    ap.add_argument("--from-recording", metavar="WAV",
                    help="use an existing 16-bit WAV instead of Piper")
    ap.add_argument("--gain", type=float, default=GAIN_SPEAKER,
                    help="linear amplitude, default %(default)s. The clip now drives a "
                         "LOUDSPEAKER next to the modem's electret, so it wants "
                         "full scale - the old 0.4 suited a resistor divider "
                         "that no longer exists.")
    ap.add_argument("-o", "--output", default=OUTPUT)
    args = ap.parse_args()

    if args.from_recording:
        pcm, rate = from_recording(args.from_recording, args.gain)
    else:
        pcm, rate = from_piper(args.text, args.gain)

    write_wav(args.output, pcm, rate)

    seconds = len(pcm) / (2.0 * rate)
    print("wrote %s" % args.output)
    print("  %d Hz mono 16-bit, %.1f s, %d bytes" % (rate, seconds, len(pcm)))
    print("  gain %.4f  ->  peak %.1f dBFS" % (args.gain, peak_dbfs(pcm)))
    print()
    print("IMPORTANT: set the ESP's I2S TX rate to %d to match this file." % rate)
    print("Copy it to the modem ESP's flash, then play it with I2SVoice.play().")


if __name__ == "__main__":
    main()
