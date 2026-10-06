# ============================
# Network
# ============================

import os

HOST = "0.0.0.0"
PORT = 9000         # Single connection, carries BOTH mics interleaved
                    # (stereo: Mic A = left channel, Mic B = right
                    # channel). Mic A still drives VAD timing, same as
                    # before -- see AudioServer.py's handle_mic().
                    #
                    # PORT_B (was 9003) is REMOVED: the old dual-mono
                    # design used two independent I2S peripherals on the
                    # ESP, each with its own TCP connection. mic_esp.py
                    # now uses ONE shared I2S bus in STEREO mode, so
                    # there's only one connection to accept.

# ============================
# Audio
# ============================

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2          # 16-bit PCM

# Old mono chunk size -- no longer used for recv() (AudioServer.py now
# reads STEREO_CHUNK_SIZE bytes at a time from the single stereo
# connection). Left here only as the reference point for the comment
# below; nothing imports this anymore.
CHUNK_SIZE = 2048

# Bytes per recv() call from the single stereo mic connection. 4096 =
# 1024 stereo frames (2 bytes L + 2 bytes R per frame) = 1024 samples
# PER CHANNEL. Detection no longer cares about chunk size -- webrtcvad
# re-slices the stream into fixed VAD_FRAME_MS frames internally (see
# vad_detector.py), so recv chunk size and VAD framing are decoupled.
STEREO_CHUNK_SIZE = 4096

# ============================
# Speech detection (webrtcvad)
# ============================
# START/STOP are decided by webrtcvad -- Google's speech detector -- NOT
# by loudness. This is the whole point: room-noise transients reach the
# SAME RMS as speech (confirmed empirically -- a silent room threw spikes
# to 2000+ RMS), so no amplitude threshold can separate voice from noise.
# webrtcvad classifies each frame by acoustic signature instead, so a loud
# bang / fan burst / desk knock at speech volume no longer false-triggers.
#
# CORRECTION (this comment used to say RMS was display-only -- it is not):
# there are TWO different RMS numbers in this system and only one of them
# is cosmetic.
#
#   1. The [Mic A] / [Mic B] / [Beam ] console lines are computed over a
#      whole ~64 ms chunk by rms_detector.py. Those ARE display only --
#      handy for setting MIC_GAIN, they decide nothing.
#
#   2. VAD_MIN_RMS below is computed per 20 ms frame inside
#      vad_detector.py and is a REAL GATE: a frame counts as speech only
#      if webrtcvad says speech AND that frame clears VAD_MIN_RMS. It
#      decides when recording starts and stops.
#
# The two are not interchangeable -- a 20 ms frame RMS swings much wider
# than the 64 ms chunk average printed on screen, so a chunk reading 400
# contains frames well below it.

DETECT_SOURCE = "BEAM"# What the wake word AND the VAD listen to:
                             #   "A"    -> Mic A only (left)
                             #   "B"    -> Mic B only (right)
                             #   "BEAM" -> delay-and-sum of both mics
                             #
                             # "BEAM" averages the two channels sample-for-
                             # sample. Both mics sit on ONE I2S bus, so they
                             # are already sample-synchronous -- no resampling
                             # or clock alignment needed, which is what makes
                             # naive averaging valid here at all.
                             #
                             # Why it should help: the speech in both channels
                             # is CORRELATED (same voice, same room) and sums
                             # coherently, while each mic's own electrical /
                             # thermal noise is UNCORRELATED and partially
                             # cancels. Two mics = up to ~3 dB SNR gain, i.e.
                             # the wake word should hold at greater range.
                             #
                             # Caveat: with zero delay compensation this steers
                             # the array straight ahead (broadside). Sit
                             # centred in front of the box and the two paths
                             # are equal, delay is genuinely zero, and the sum
                             # is exact. Step off-axis and the far mic hears
                             # you late, so the sum partially cancels the top
                             # end (comb filtering).
                             #
                             # MEASURED SPACING: 10 cm (mics are 10 cm apart).
                             #   worst-case delay  = 0.10 m / 343 m/s
                             #                     = 292 us = 4.7 samples @16k
                             #   first cancellation notch at angle th:
                             #                       f = c / (2 * d * sin th)
                             #     th = 15 deg  ->  6.6 kHz   (harmless)
                             #     th = 30 deg  ->  3.4 kHz   (edge of speech)
                             #     th = 90 deg  ->  1.7 kHz   (hurts)
                             #
                             # So 10 cm is fine for a talker roughly in front
                             # (within ~+/-20 deg) and degrades from the side.
                             # 10 cm is WIDER than the 4-8 cm usually quoted
                             # for delay-and-sum, which cuts both ways: it
                             # decorrelates diffuse room noise down to a lower
                             # frequency (better cancellation), and it makes
                             # the true delay 4.7 samples instead of ~2, which
                             # is large enough for GCC-PHAT to actually
                             # estimate reliably. Wide spacing makes the
                             # ADAPTIVE version work better, not worse -- so if
                             # off-axis catch rate suffers, the fix is GCC-PHAT
                             # delay estimation then align-and-sum, never a
                             # bigger WAKE_THRESHOLD.
                             #
                             # Set "A"/"B" to A/B test against a single mic.
                             # Both mics are recorded to separate WAVs either
                             # way -- this only changes what DETECTION hears.

VAD_AGGRESSIVENESS = 3       # 0-3. Higher = rejects more non-speech (better
                             # in noise) but can miss soft/distant speech.
                             # At 3 because residual room noise was still
                             # reading as speech at 2 and blocking STOP.
                             # Missing your voice at range? drop to 2.

VAD_FRAME_MS = 20            # webrtcvad frame size: must be 10, 20, or 30.

VAD_MIN_RMS = 275            # A frame only counts as speech if webrtcvad says
                             # speech AND its RMS clears this. This is the
                             # floor that rejects the quiet room-noise webrtcvad
                             # misclassifies as speech -- set it ABOVE your
                             # silent-room RMS (~120-350 observed) and BELOW
                             # your speaking RMS. NOTE: this is a per-20ms-frame
                             # RMS, so it runs a touch higher than the ~64ms
                             # chunk RMS printed in the console. False-starting
                             # on noise? raise it. Missing soft speech? lower.
                             #
                             # WAS 450, tuned against a SINGLE mic. With
                             # DETECT_SOURCE = "BEAM" the VAD listens to the
                             # averaged channel, which measures ~0.8-0.85x of
                             # Mic A (observed: A 426/Beam 334, A 945/Beam 794,
                             # A 767/Beam 704). Averaging is deliberate -- see
                             # beamform() -- but it meant the effective floor
                             # rose ~2 dB when BEAM was selected, and nobody
                             # re-tuned this. Measured consequence: during
                             # CAPTURE the speech ratio sat at 0.07-0.47 with
                             # the talker clearly audible (Beam RMS 400-700,
                             # peaks 5-6% FS), i.e. BELOW VAD_CONTINUE_RATIO,
                             # so the silence timer kept restarting mid-command.
                             # 275 sits above the quiet-room Beam floor
                             # (85-150) and below far-field speech (200-800).

VAD_WINDOW_MS = 300          # Sliding window used for BOTH decisions below.

VAD_TRIGGER_RATIO = 0.6      # START: fraction of the window that must be speech
                             # to begin recording. Lower than a lone-spike-proof
                             # 0.9 because the RMS floor already blocks quiet
                             # noise, so distant/softer speech can still trip it.

VAD_CONTINUE_RATIO = 0.5     # Raised 0.3 -> 0.5. At 0.3 a ceiling fan plus two
                             # people talking across the room scored high enough
                             # to keep RESETTING the end-of-speech timer, so a
                             # finished command kept recording for several extra
                             # seconds. Ambient noise rarely sustains half a
                             # 300 ms window as speech; a person mid-sentence
                             # easily does.
                             #
                             # Pauses between words still dip below this, which
                             # is why SILENCE_DURATION exists -- it takes a full
                             # 2 s under the ratio to actually stop. Getting cut
                             # off mid-sentence? lower toward 0.4. Still hanging
                             # on after you stop? raise toward 0.6.
                             # STOP: while recording, hold open while at least
                             # this fraction of the window is speech; below it,
                             # the silence timer runs. Using the ratio (not one
                             # frame) is the fix for record-forever. Hysteresis
                             # gap to START (0.6) avoids flapping. Cutting off
                             # mid-sentence? lower it; not stopping? raise it.

SILENCE_DURATION = 2.0       # Seconds below VAD_CONTINUE_RATIO before STOP.
                             # Inter-word gaps stay above the ratio so they
                             # don't cut you off; only a real pause ends it.
                             # Lower (1.0-1.5) for snappier end-of-turn.

# How many chunks to keep buffered at all times so that when START fires,
# we can back-fill the audio that happened just before detection.
PREROLL_CHUNKS = 8            # ~512ms of pre-roll

# Cooldown after a STOP before the detector will listen for a new START.
# Prevents mic-pop/settling transients right after closing a WAV from
# immediately re-triggering a new recording.
POST_STOP_COOLDOWN = 0.4     # seconds

DEBUG_RMS = True             # Print RMS values

# ============================
# Wake word (openWakeWord)
# ============================
# The recording is now GATED by a wake word ("Hey Genie") instead of raw
# VAD. This is what finally kills the false-starts: fan bursts, desk
# knocks and random room noise can't open a recording -- only the wake
# word can. The webrtcvad settings above are now used ONLY to detect the
# END of your command once the wake word has already started the capture.
#
# openWakeWord runs fully locally (ONNX), no account, no expiry, keeping
# the whole GENIE stack self-hosted. (We tried Picovoice Porcupine first
# but its free tier is now a 7-day trial -- unusable for a project.)

# Either a pretrained name ("hey_jarvis", "alexa", "hey_mycroft",
# "hey_rhasspy") OR a path to a custom .onnx model ("hey_genie.onnx").
# Set to "hey_jarvis" for now so the whole pipeline works end-to-end
# TODAY; train a custom "Hey Genie" model free via openWakeWord's Colab
# and point this at the resulting hey_genie.onnx when ready.
WAKE_MODEL = "hey_genie.onnx"

WAKE_THRESHOLD = 0.65        # 0-1. Score above this = wake word fired.
                             # Raised 0.15 -> 0.5 -> 0.6 -> 0.65 across tuning.
                             # Live test: full "Hey Genie" fires 0.77-0.99;
                             # PARTIALS ("hey"/"genie"/"hello" alone) top out at
                             # 0.59. 0.65 sits in the gap -- clears every partial
                             # (0.59<0.65) with a 0.12 margin below the 0.77
                             # full-phrase floor. Fan noise never crossed ~0.35.
                             # A partial still sneaks through? go 0.7. Missing
                             # your wake word from across the room? back to 0.6/0.5.

WAKE_CONSECUTIVE = 3         # Raised 2 -> 3 after false accepts persisted at 2
                             # on BOTH channels (~0.6-1.0 per minute in a room
                             # with conversation). A real 'Hey Genie' spans ~6
                             # blocks, so 3 is still comfortably inside a
                             # genuine utterance; 4+ would start cutting into
                             # it. If far-field catch rate drops noticeably at
                             # 1-2 m, go back to 2 and raise WAKE_THRESHOLD
                             # instead -- but measure, do not guess.
                             #
                             # How many BACK-TO-BACK 80 ms openWakeWord blocks
                             # must clear WAKE_THRESHOLD before we call it a
                             # wake word. 1 = old behaviour (fire on any single
                             # block).
                             #
                             # Why this exists: raising WAKE_THRESHOLD is the
                             # obvious way to kill false accepts, but it costs
                             # range directly -- measured far-field hits land at
                             # 0.66-0.87, so 0.65 -> 0.75 would delete most of
                             # the 2-3 m detections. This knob attacks a
                             # different axis instead: DURATION.
                             #
                             # A real "Hey Genie" is ~500 ms, so it spans ~6
                             # blocks and the score stays elevated across
                             # several of them. Impulsive false accepts (a
                             # knock, a clatter, one syllable of unrelated
                             # speech) spike for a single block and collapse.
                             # Requiring 2 in a row removes the impulsive class
                             # WITHOUT touching the threshold, so far-field
                             # sensitivity is preserved.
                             #
                             # Cost: adds 80 ms of latency to every detection,
                             # and if a marginal far-field utterance only ever
                             # pokes above 0.65 for one block, it is now a miss.
                             # If 2 costs noticeable range, go back to 1 and
                             # raise WAKE_THRESHOLD instead -- but measure both.
                             # If false accepts survive at 2, try 3 before
                             # touching the threshold.

WAKE_COOLDOWN = 1.0          # Seconds to ignore further wake-word hits right
                             # after one fires (stops a double-trigger).

MAX_COMMAND_SECONDS = 15     # Hard cap on one command recording after the
                             # wake word -- a noisy tail can't record forever
                             # even if the VAD never sees clean silence.

# ============================
# Digital mic gain (post I2S, pre-everything)
# ============================
# Applied uniformly to both mics' raw PCM the instant AudioServer.py
# receives it -- before RMS detection AND before it's written to the
# WAV file, so "does this trigger START" and "how audible is the saved
# recording" move together, not independently.
#
# 1.0 = off. This does NOT fix a genuine signal-below-noise-floor
# problem at range -- multiplying speech and room noise by the same
# factor doesn't change their ratio. It only helps if there's real
# UNUSED headroom in the raw samples at 1-2m (peak well under +-32767).
# Check the new Peak/%FS printout in rms_detector.py's DEBUG_RMS output
# before picking a value here -- don't guess one.
#
# Dropped from 8.0 -> 4.0 alongside the new high-pass filter: with DC and
# low-frequency rumble now removed before gain (see HPF_CUTOFF_HZ below),
# the RMS reading is much closer to real voice energy, so less brute
# gain is needed and there's less risk of amplifying residual noise into
# clipping. Re-measure the silent-room floor and tune from there.
MIC_GAIN = 6.0

# ============================
# High-pass filter (removes DC + low-frequency rumble)
# ============================
# Everything below this frequency is filtered out of BOTH mics in
# AudioServer.py BEFORE gain, RMS detection, and WAV writing. The
# INMP441's DC bias and sub-~120Hz rumble (HVAC, fans, desk vibration,
# handling) carry a lot of RMS energy but ZERO speech, so counting them
# was making "silence" look as loud as speech once MIC_GAIN was applied.
# Speech intelligibility lives ~300-3400Hz, so 120Hz is safely below the
# voice band -- raise toward 150-200 if low rumble still leaks through,
# lower toward 80 if voices sound thin.
HPF_CUTOFF_HZ = 120.0

# ============================
# Whisper transcription quality gates
# ============================
# Without a forced language, Whisper auto-detects per clip -- on short,
# noisy, non-speech audio (background chatter, a door, ambient
# conversation the mic happened to catch) it still has to guess SOME
# language, and it frequently guesses wrong: Arabic, Urdu, Farsi, French,
# Spanish have all been observed on what was actually just noise. Genie
# is English-only end to end (LLM prompt, Piper voice), so there's no
# reason to leave this on auto-detect.
WHISPER_LANGUAGE = "en"

WHISPER_REJECT_OTHER_LANGUAGES = False
                             # 28-08-2026: set False. Every clip is now
                             # decoded straight as WHISPER_LANGUAGE in ONE
                             # pass -- no detection pass, no "language
                             # uncertain" line, and no second transcription
                             # when the guess comes back low-confidence
                             # (which was costing a full extra decode on
                             # clips it was going to read as English
                             # anyway).
                             #
                             # WHAT THIS GIVES UP, and it is not nothing:
                             # the paragraph below is a real observation,
                             # not a hypothetical. Set this back to True if
                             # Urdu room conversation starts reaching the
                             # LLM as English commands.
                             #
                             # Discard a clip when Whisper detects it is
                             # not English, BEFORE it reaches the LLM.
                             #
                             # Why this is needed: WHISPER_LANGUAGE above
                             # does not mean "expect English". It means
                             # "decode as English, whatever you hear".
                             # Given Urdu speech it will happily map the
                             # sounds onto fluent English words -- and be
                             # CONFIDENT about it, because it did find a
                             # plausible English sentence.
                             #
                             # That defeats both existing gates by design:
                             # no_speech_prob is low (it IS speech) and
                             # avg_logprob is healthy (the sentence is
                             # fluent). Confidence cannot catch a
                             # confident mistake. Only the LANGUAGE check
                             # can. Observed live: Urdu conversation in
                             # the room transcribed as "I dont know how
                             # you do it" and was sent to Mistral.

WHISPER_LANG_CONFIDENCE = 0.5
                             # Only reject on language when the detector
                             # is at least this sure. Below it we assume
                             # accented English rather than a foreign
                             # language and decode as English anyway.
                             #
                             # This is the safety valve: an English
                             # command in a strong accent can be
                             # misdetected, and silently dropping real
                             # commands would be a worse failure than the
                             # one being fixed. Raise toward 0.8 if real
                             # commands get dropped; lower toward 0.3 if
                             # foreign speech still gets through.

# Segment-level confidence gates, applied in whisper_service.py AFTER
# transcription. Whisper computes these per segment regardless -- these
# thresholds decide whether whisper_service.py trusts a segment enough
# to forward it to the classifier/LLM, or discards it as probably not
# real speech. Both starting values match faster-whisper's OWN internal
# defaults for "this segment is probably not real speech" (its
# no_speech_threshold/log_prob_threshold), not arbitrary numbers -- just
# made explicit and tunable here instead of only affecting Whisper's
# internal retry logic. Tune based on real false-trigger frequency:
# lower NO_SPEECH_PROB_MAX / raise AVG_LOGPROB_MIN to reject more.
NO_SPEECH_PROB_MAX = 0.6     # segment probably isn't speech above this
AVG_LOGPROB_MIN = -1.0       # segment is low-confidence below this

# Spoken when a segment sounds like real speech (no_speech_prob is fine)
# but Whisper wasn't confident about the actual words (avg_logprob too
# low) -- distinct from pure background noise, which stays silent
# instead of prompting, so Genie doesn't start interjecting into
# unrelated nearby conversation just because it crossed the RMS gate.
UNCLEAR_SPEECH_REPLY = "Sorry, I didn't catch that. Could you repeat that?"


# ============================
# Recording
# ============================

OUTPUT_DIR = "recordings"

# ============================
# LLM (local Mistral-7B via Ollama)
# ============================

LLM_API_URL = "http://localhost:11434/api/generate"
LLM_MODEL_NAME = "mistral"

LLM_SYSTEM_PROMPT = (
    "You are Genie, a voice assistant with a female persona, styled after "
    "Alexa and Siri. The user is speaking to you out loud and your reply "
    "is read aloud back to them immediately. "
    "Answer in ONE short sentence whenever possible. Two sentences only "
    "if genuinely necessary. Never more than that unless the user "
    "explicitly asks for more detail. "
    "Give the direct answer first, with no preamble, no restating the "
    "question, no filler like 'I'd be happy to help'. "
    "Plain language, no markdown, no bullet points, no headers. "
    "If you respond in a language other than English that has grammatical "
    "gender (e.g. Hindi, Urdu, Arabic, Spanish, French), speak using "
    "feminine forms, consistent with your persona as Genie. "
    "You are only reached when the user's request did NOT match a direct "
    "sensor check or device command -- you have NO live access to smoke, "
    "flame, motion, fall, temperature, or humidity readings, and no access "
    "to the light or fan state, in this conversation. NEVER state a "
    "specific sensor reading or device state, and NEVER claim a hazard "
    "(fire, smoke, gas, a fall) is happening or not happening -- both a "
    "false alarm and a false all-clear can cause real harm, and you have "
    "no data to base either on. You also DO have real sensors installed, "
    "so never claim you lack them either. If asked about a sensor, a "
    "hazard, or a device's on/off state, say only that you didn't catch "
    "that and ask them to ask again directly, e.g. 'try asking is there "
    "smoke or check all sensors' -- do not attempt to answer it yourself. "
    # The face node renders whatever expression it is told. Asking the
    # model that just wrote the sentence to label its own sentiment costs
    # nothing -- it already knows. A second classifier pass over the same
    # text would re-derive it for 100-300ms on a pipeline that is already
    # the bottleneck, and could only ever return positive/negative, which
    # cannot separate "thinking" from "concerned".
    #
    # The tag is stripped before TTS by whisper_service.speak(), so a
    # model that ignores this instruction costs a neutral face, never a
    # spoken "[EMOTION:happy]". Do not make the tag mandatory-sounding
    # enough that the model starts explaining it out loud.
    "Finally, end every reply with a sentiment tag on its own final line, "
    "in exactly this form and nothing else: "
    "[EMOTION:neutral] or [EMOTION:happy] or [EMOTION:sad] or "
    "[EMOTION:thinking] or [EMOTION:concerned]. "
    "Never speak the tag aloud, never mention it, never explain it."
)

LLM_TIMEOUT_SECONDS = 300

# Hard cap on generated tokens. This keeps response latency predictable
# on CPU-only hardware -- without this, a broad question can make the
# model generate hundreds of tokens even if the system prompt asks for
# brevity, since that's a request, not a guarantee.
LLM_MAX_TOKENS = 60

# ============================
# TTS (Piper)
# ============================

PIPER_MODEL_PATH = "piper_voices/en_US-amy-medium.onnx"

# Synthesis tuning. Defaults in Piper are volume=1.0, noise_scale=0.667,
# noise_w_scale=0.8. Raising noise_scale/noise_w_scale adds more natural
# pitch/timing variation (less flat/robotic); too high starts sounding
# unstable, so nudge gradually if you tweak further.
# WAS 1.4, AND THAT IS WHY IT WAS QUIET. Turning this DOWN made GENIE
# louder, which is worth understanding before anyone turns it back up.
#
# At 1.4 Piper saturates internally: measured on a real utterance, peaks
# sat at 32767 of 32767 with 98 hard-clipped samples while the average
# was 14.8 dB below that. A handful of flattened transients held the
# ceiling and nothing else could rise. Raising the number further could
# only add distortion; the ceiling was already the ceiling.
#
# Backing off to 0.9 leaves Piper clean, and the soft limiter below then
# lifts the BODY of the speech instead of the peaks. Measured on the same
# sentence:
#
#     volume  drive    average    clipped
#      1.4     off    -14.8 dBFS     98      <- the "too quiet" version
#      1.4     2.0    -10.4 dBFS     98      louder, still distorted
#      1.0     2.0    -12.6 dBFS      1
#      0.9     3.0    -10.9 dBFS      0      clean, +3.9 dB
#      0.9     5.0     -7.9 dBFS      0
#      0.8     6.0     -7.6 dBFS      0      <- shipped, +7.2 dB total
#      0.7     8.0     -6.9 dBFS      0      audibly squashed
#      0.6    12.0     -6.0 dBFS      0      squashed further, no clipping
#
# Pushed to 0.8/6.0 because audibility in a room matters more here than
# fidelity, and compressed speech is what every public-address system in
# the world does for the same reason. Past drive 8 it stops sounding like
# a voice and starts sounding like a megaphone -- still measurably louder,
# but not better.
TTS_VOLUME = 1.1

# Soft limiter drive. 0 disables it; see tts_service.soft_limit for the
# waveshaping and why it is safe to apply per streaming chunk.
#
# STILL TOO QUIET? The next gain is in HARDWARE, not here. There is no
# software headroom left at all now -- the signal already reaches full
# scale without clipping, which is the best a digital path can do. The
# MAX98357A's GAIN pin selects 9, 12 or 15 dB, far more than any
# remaining software trick, and unlike software gain it costs nothing in
# signal-to-noise.
TTS_SOFT_LIMIT = 1.5
TTS_NOISE_SCALE = 0.8
TTS_NOISE_W_SCALE = 1.0

# ============================
# Intent classification (AIML) -- IoT command routing
# ============================
# If a transcript matches a pattern in relay.aiml, it's treated as a
# direct device-control command: skip the LLM entirely, speak the AIML
# confirmation line, and POST the command to api.py, which pushes it on
# to the relay ESP32. Anything that doesn't match falls through to the
# normal LLM pipeline unchanged.

AIML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "relay.aiml")

# Loaded into the SAME kernel as AIML_FILE (see ai.py) -- one shared
# pattern graph and predicate space for relay + sensor + emergency
# intents, rather than a separate kernel per topic. Keeping topics in
# separate files is just for readability; aiml.py's Kernel.learn() can
# be called more than once on one kernel, each call adds categories to
# the same graph.
SENSOR_AIML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sensor.aiml")

# api.py's own address -- whisper_service.py POSTs here when a command matches
IOT_API_HOST = "127.0.0.1"
IOT_API_PORT = 8000
IOT_API_URL = f"http://{IOT_API_HOST}:{IOT_API_PORT}"

# The relay ESP32's address -- api.py pushes commands here.
# Updated from IoT_esp.py's own boot console log (Aug 8, 2026). This
# board is on a hotspot with no DHCP reservation, so it WILL drift again
# on the next reboot/reconnect -- if /relay stops reaching the board,
# re-check IoT_esp.py's serial output for its current "ESP32 IP:" line
# and update this value again.
#
# This is now the ONLY place this IP is defined -- api.py imports it
# instead of keeping its own copy. (The sensor-integration prototype
# had a second hardcoded RELAY_ESP32_IP constant that had drifted out
# of sync with this one -- two copies of the same fact is exactly how
# that kind of drift happens.)
IOT_ESP_IP = "10.150.15.63"



# ============================
# Playback (ESP audio-out connection)
# ============================
# Separate TCP connection from the mic-streaming one. ESP connects to this
# port specifically to receive NeoPixel state + streamed TTS audio.

MIC_MUTE_MAX_SECONDS = 90.0  # Failsafe on the speaking mute below.
                             #
                             # While Genie is talking, AudioServer stops
                             # listening entirely (see is_speaking() in
                             # AudioServer.py) so its own reply cannot
                             # trigger the wake word or open a capture.
                             # That mute is released by a SPEAK_END byte
                             # from whisper_service.
                             #
                             # If whisper_service crashes mid-reply that
                             # byte never arrives, and without this cap
                             # the microphone would stay deaf forever --
                             # a far worse failure than the one being
                             # prevented. After this many seconds the
                             # mute is released regardless. No real reply
                             # comes close to 90 s.

TTS_TAIL_MARGIN = 0.5        # Extra seconds to wait after the LAST audio chunk
                             # has been streamed before declaring the reply
                             # finished.
                             #
                             # Sending bytes down a TCP socket is not the same
                             # event as sound leaving the speaker. The ESP
                             # buffers what it receives and plays it out at its
                             # own rate, so the last chunk is still audible for
                             # some time after send() returns. whisper_service
                             # therefore computes the true playback duration
                             # from the PCM byte count and waits for it, plus
                             # this margin, before opening the follow-up window.
                             #
                             # Without that wait the mic goes live while Genie
                             # is still talking, hears its own reply, and
                             # answers itself. Covers ESP-side buffering and
                             # I2S latency. If the window still opens over the
                             # tail of a reply, raise this.

PLAYBACK_PORT = 9001

# AudioServer.py -> whisper_service.py control link (localhost only). The
# "mic is listening" cues (OP_LISTEN_START/END -> orange LED + beeps) are
# decided by AudioServer's wake/VAD state machine, but only whisper_service
# holds the speaker board's connection. AudioServer connects here as a client
# and sends the raw opcode byte; whisper_service relays it to the speaker
# ESP(s). Kept separate from PLAYBACK_PORT so that if whisper_service isn't
# running, the cue is simply skipped and AudioServer keeps working normally.
CONTROL_PORT = 9003

# Max bytes per single network audio message sent to the ESP. Piper can
# emit chunks of varying size (sometimes tens of KB for a sentence);
# splitting those further before sending keeps each allocation on the
# ESP small, which matters a lot given its limited RAM.
TTS_NETWORK_CHUNK_SIZE = 4096

# Protocol opcodes (must match the ESP MicroPython script exactly)
OP_THINKING = 1      # no payload -> blink purple
OP_SPEAK_START = 2   # no payload -> solid purple
OP_AUDIO_CHUNK = 3   # + 4-byte big-endian length + raw PCM16 bytes
OP_SPEAK_END = 4     # no payload -> back to idle
OP_LISTEN_START = 5  # no payload -> the mic just STARTED capturing your command
                     # (wake word fired, WAIT_WAKE -> CAPTURE). Speaker ESP:
                     # orange LED + a short rising "I'm listening" beep.
OP_FOLLOWUP_START = 7  # no payload -> follow-up window OPEN: the mic is
                     # live and no wake word is needed. Must look
                     # DIFFERENT from OP_LISTEN_START -- suggest a dim
                     # or breathing blue against CAPTURE's solid orange.
                     # Alexa and Google both show this state; without a
                     # cue the mic is open for 8 s with no visible sign.
OP_FOLLOWUP_END = 8  # no payload -> window closed, wake word required
OP_LISTEN_END = 6    # no payload -> the mic STOPPED capturing (end of command,
                     # CAPTURE -> WAIT_WAKE). Speaker ESP: short falling beep +
                     # back to idle blue.
                     #
                     # NOTE: these two originate in AudioServer.py (it owns the
                     # wake/VAD state machine) but the speaker board is held by
                     # whisper_service.py. AudioServer relays them over
                     # CONTROL_PORT and whisper_service re-broadcasts them.

# ============================
# Auto-discovery (UDP broadcast)
# ============================
# Solves the "laptop's IP changed, ESP can't find it" problem for the
# mic and speaker ESPs -- the laptop broadcasts a UDP "hello" that they
# listen for and use the sender's IP automatically.
#
# NOT used for the IoT relay ESP: that board is on a mobile hotspot
# with AP/client isolation enabled, which silently blocks device-to-
# device broadcasts regardless of firewall settings -- confirmed by
# testing with Windows Firewall fully disabled and still no packets
# arriving. IOT_ESP_IP below stays a manually-set value for that board;
# update it if that connection ever needs to change.

DISCOVERY_PORT = 9999

# ============================
# Emergency (SIM900A on mic ESP)
# ============================
# The mic ESP holds a persistent connection to this port on the laptop
# (like the speaker's playback connection). When the AIML emergency
# intent matches, whisper_service.py sends a single trigger byte down
# it, and the mic ESP runs its own AT-command sequence over its
# SIM900A UART to place a call and send an SMS.

FOLLOWUP_MAX_CHAIN = 3       # How many follow-ups may run back-to-back
                             # before the wake word is required again.
                             #
                             # This is the termination guarantee. Every
                             # other defence against Genie answering
                             # itself is probabilistic -- a margin, a
                             # threshold, an estimate of when the speaker
                             # went quiet. Any of them can be wrong on a
                             # given turn. This one cannot: a feedback
                             # loop needs an unbounded chain of
                             # wake-word-free captures, and after this
                             # many the chain simply ends.
                             #
                             # A real conversation rarely runs more than
                             # three turns without a pause long enough to
                             # close the window anyway, so the ceiling is
                             # almost never reached by a human.

FOLLOWUP_ARM_DELAY = 0.8     # Dead time between the reply finishing and
                             # the window opening.
                             #
                             # TTS_TAIL_MARGIN estimates when the SPEAKER
                             # stops. This covers what happens after: room
                             # reverb, a slow ESP buffer draining, the
                             # physical decay of the last syllable. During
                             # this gap the wake word still works -- only
                             # the wake-word-FREE path is held back, which
                             # is the one that can loop.

FOLLOWUP_WINDOW = 8.0        # Seconds after Genie STOPS speaking during which
                             # a follow-up needs no wake word -- just talk.
                             #
                             # "Hey Genie, turn on the light." ... "What is the
                             # temperature?" ... "Who founded Pakistan?" costs
                             # one wake word instead of three.
                             #
                             # The window is ROLLING: every answered follow-up
                             # re-arms it, so a real back-and-forth never needs
                             # a second "Hey Genie". It only closes when you
                             # actually stop talking.
                             #
                             # Reference points: Alexa Follow-Up Mode is ~5 s,
                             # Google Continued Conversation ~8 s. 8 leaves
                             # thinking room without the mic feeling stuck open.
                             #
                             # THE TRADE, stated plainly: inside this window
                             # there is NO wake-word gate -- the VAD alone can
                             # open a recording. That is exactly the raw-VAD
                             # false-start behaviour the wake word was added to
                             # kill (see DETECT_SOURCE notes). It is acceptable
                             # ONLY because the window is short, only ever opens
                             # after a real answered interaction, and Whisper's
                             # confidence gate discards junk before it can reach
                             # Mistral. Raise this and you are trading false
                             # triggers for convenience -- 15 s would be an open
                             # mic in the room for a quarter of a minute.
                             # Set to 0.0 to disable follow-up entirely.

# ------------------------------------------------------------------
# Reverse cue link: whisper_service -> AudioServer
# ------------------------------------------------------------------
# CONTROL_PORT carries cues ONE WAY (AudioServer -> whisper_service ->
# speaker ESP). Nothing has ever travelled the other direction, which is
# why AudioServer currently has no idea when Genie has finished
# speaking -- it hands off a WAV and immediately returns to WAIT_WAKE.
#
# The follow-up window needs exactly that missing fact. The window MUST
# open when the reply finishes playing, never when the recording ends:
# open it at capture-end and the mic is live while the speaker is still
# talking, so Genie transcribes its own voice and answers itself. With a
# 7B model each of those loops costs a full generate cycle.
#
# So whisper_service connects here and sends one byte at the end of
# speak(). Deliberately a SEPARATE port rather than making CONTROL_PORT
# bidirectional: CueSender is fire-and-forget with its own reconnect
# logic, and threading replies back through it would couple the
# real-time audio path to whisper_service's liveness. A separate
# best-effort listener can be ignored entirely when it is not there.
FOLLOWUP_PORT = 9004

EMERGENCY_PORT = 9002

# ------------------------------------------------------------------
# Emergency alert: contacts and escalation timing
# ------------------------------------------------------------------
# A LIST, in the order they get tried. emergency_alert() texts every
# contact first, waits for one of them to reply, and only then starts
# dialling -- so the order here decides who gets rung first, not who
# gets texted first (everyone gets texted).
#
# !!! UPDATE THESE with the real numbers before relying on the feature !!!
EMERGENCY_CONTACTS = [
    "+923001234567"     # primary - rung first. PLACEHOLDER: set your own
]

# Kept so anything still importing the old singular name keeps working.
EMERGENCY_PHONE_NUMBER = EMERGENCY_CONTACTS[0]

EMERGENCY_SMS_MESSAGE = "Emergency alert from Genie: please call or check immediately."

# What the SMS says when a PERSON asked for help out loud, rather than a
# sensor tripping. Worth distinguishing: "someone asked Genie for help" tells
# a responder there is a conscious person speaking in that room, which is a
# different situation from "smoke detected" and calls for a different response.
EMERGENCY_VOICE_MESSAGE = (
    "GENIE EMERGENCY: someone asked for help out loud at the registered "
    "address. Please call or check immediately."
)


# ------------------------------------------------------------------
# Emergency trigger link: opcodes in BOTH directions
# ------------------------------------------------------------------
# The socket on EMERGENCY_PORT used to be one-way - the server told the
# modem board to alert, and the board never said anything back. It is
# now bidirectional, because the board is the only thing that knows the
# moment a call is actually answered, and that is when the announcement
# has to be spoken.
#
#   server -> board
#     0x01 <len:1> <message utf-8>            run the escalation, using
#                                              this wording for the SMS.
#                                              len 0 = the board's default.
#     0x04 <num_len:1> <number> <text_len:1> <text utf-8>
#                                              send exactly this SMS. No
#                                              escalation, no ack window,
#                                              no call - the reply to a
#                                              command someone texted in.
#
#   board -> server
#     0x02                                    somebody answered - SPEAK NOW
#     0x03 <sender_len:1> <sender> <text_len:1> <text utf-8>
#                                              an SMS arrived that was NOT
#                                              a reply inside an ack window
#                                              - run it through AI inference
#                                              and answer with 0x04.
#
# Why the board cannot just play the audio itself: it can, and it did,
# through a PCM5102A wired into the modem's mic pins. That produced only
# 217 Hz GSM buzz - see docs/Audio Injection Investigation.docx. The
# working arrangement is acoustic: GENIE's ordinary speaker says the
# message out loud, and the modem's own electret capsule, sitting a few
# centimetres away, picks it up and sends it down the call.
#
# So the board no longer needs a DAC, an I2S bus, or its own copy of the
# audio. It needs to say "now".
EV_ANSWERED = 0x02

# board -> server: an inbound SMS to run through AI inference. See
# handle_incoming_sms() in whisper_service.py - it is process_transcript()'s
# sibling, minus Whisper and AudioServer on the way in and minus Piper on
# the way out. The board only ever sends this from its idle loop, never
# while an emergency_alert() ack window is open on the same modem -
# see modem_main.py's poll_inbound_sms() for why that matters.
SMS_IN = 0x03

# server -> board: send exactly this text to exactly this number. Used for
# the reply to a command SMS. Never used for the emergency path - that
# stays 0x01 end to end, so a command SMS can never accidentally trigger
# the call escalation and an emergency trigger can never be mistaken for
# an ordinary reply.
SMS_OUT = 0x04

# Seconds to wait after the board reports an answer before speaking.
#
# The call has only just connected and its audio path takes a moment to
# settle; speaking immediately clips the first word, so the responder
# hears "...mergency alert from Genie" and misses what it is. It also
# covers the latency of streaming the audio out to the speaker board.
EMERGENCY_ANNOUNCE_DELAY = 2.0

# What GENIE says out loud once the call is answered, so the modem's
# electret can pick it up. "{alert}" is replaced with whatever triggered
# this - the sensor-specific wording where there is one.
EMERGENCY_ANNOUNCEMENT = (
    "Emergency. {alert} "
    "This is an automated call from Genie at the registered address. "
    "Please respond."
)

# How many times to repeat it. Somebody who has just picked up misses
# the first second or two of the first pass.
EMERGENCY_ANNOUNCE_REPEAT = 2
# Seconds to wait after the last SMS goes out before dialling anyone.
#
# The whole point of texting first is that it is instant and survives a
# missed call -- but a text is useless if we ring three seconds later
# anyway. This window gives a contact time to read it and reply "OK",
# which cancels the calls entirely.
#
# Too short and the phone rings while they are still reading. Too long
# and a real fire burns for a minute before anyone is called. 45s is a
# deliberate compromise; drop it to ~20 for the demo so the marker does
# not sit watching a silent board.
EMERGENCY_ACK_WINDOW = 45.0

# How often to check for an inbound reply during that window.
EMERGENCY_ACK_POLL = 5.0

# Any inbound SMS from a contact counts as acknowledgement, whatever it
# says -- someone who texts back has clearly seen the alert. Set this to
# a word (e.g. "OK") to require that word instead. None = any reply.
EMERGENCY_ACK_KEYWORD = None

# Seconds to let each number ring before giving up and trying the next.
EMERGENCY_CALL_TIMEOUT = 45

# Seconds to hold the line open AFTER the recorded message has played.
# The DAC is muted for this whole stretch, so the modem's own electret
# capsule is live and the responder hears the actual room. This is the
# most useful part of the call and it should be generous.
EMERGENCY_HOLD_SECONDS = 300

# The pre-rendered clip, as it is stored on the MODEM ESP32's flash.
# Built on the PC by make_emergency_clip.py -- see that file. Played
# twice because someone who has just picked up misses the first second
# or two of the first pass.
EMERGENCY_CLIP_PATH = "/emergency_mic.wav"
EMERGENCY_CLIP_REPEAT = 2

# ------------------------------------------------------------------
# DTMF: what the responder can press during the call
# ------------------------------------------------------------------
# Speech in the other direction was considered and rejected -- a live
# mic input and a live speaker output in one loop with no echo canceller
# means GSM feeds Genie's own TTS back into Whisper. Keypad digits
# arrive as clean URCs instead, and survive a bad voice channel.
DTMF_ACK      = "1"   # acknowledged: hang up, stop escalating
DTMF_ESCALATE = "2"   # not me: hang up, try the next contact
DTMF_ASK      = "3"   # open a question window and answer over the call

# ------------------------------------------------------------------
# SMS as a command channel
# ------------------------------------------------------------------
# Anyone can text the SIM in the modem module. Anyone who does gets read
# and, if they are on this list, gets an answer routed through exactly the
# intent classifier and LLM the voice pipeline uses - "turn on the fan",
# "is there smoke", "what's the capital of France" all work by SMS the same
# way they work spoken, minus Whisper and AudioServer on the way in and
# minus Piper on the way out (see handle_incoming_sms() in
# whisper_service.py).
#
# Defaults to EMERGENCY_CONTACTS[0] only - the primary contact, not
# everyone who gets called during an emergency. A text from anyone else,
# including the secondary emergency contacts, is logged and silently
# dropped - no reply at all, on purpose. Answering a stranger's text (even
# with "sorry, not authorised") confirms that texting this number does
# something, which an unlisted device answering nothing does not. Add more
# numbers here directly if more than one person should be able to control
# GENIE by text.
SMS_COMMAND_ALLOWED = [EMERGENCY_CONTACTS[0]]

# An SMS reply is cut to this length before it ever reaches the 255-byte
# per-field cap in the wire protocol above.
#
# MUST STAY BELOW 255. It was 300, which is above the cap, so the trim did
# nothing and the wire silently cut the text mid-word at 255 - precisely what
# this setting exists to prevent. 240 leaves headroom for a few multi-byte
# characters (an LLM will happily emit a curly quote) without crossing it. LLM_MAX_TOKENS already keeps a
# Mistral answer short, but a "check all sensors" reply or a status line is
# built independently of that and could still run past what is comfortable
# as one text message.
SMS_REPLY_MAX_CHARS = 240

# ------------------------------------------------------------------
# Sensor-driven alerts
# ------------------------------------------------------------------
# Which stored sensor states count as an emergency. The watcher polls
# api.py's /sensors and fires when a sensor ENTERS one of these states
# -- edge triggered, not level triggered, so a sensor that stays latched
# on does not re-alert every poll.
#
# FALL reports "idle"/"detected" rather than "false"/"true", which is
# why this is a per-sensor set and not a blanket `== "true"` check.
# Node 1 (GENIE FINAL face node) reports GRADED levels, not booleans:
# sensors.py's status() runs median -> EMA -> 3-consecutive-confirm and
# emits 0/1/2, which node_bridge.py maps to clear/elevated/alarm. Only
# the top level is an emergency -- "elevated" is what the MQ-2 does when
# someone lights a cigarette two rooms away, and phoning an ambulance
# for that is how a safety system gets unplugged. The old boolean
# "true" stays accepted so the original sensor ESP32 (which pushes to
# /update_sensor directly) keeps working unchanged.
SENSOR_ALERT_STATES = {
    "SMOKE":  {"true", "alarm"},
    "FLAME":  {"true", "alarm"},
    "MOTION": {"true"},

    # FALL IS DISABLED, 02-09-2026, and this is a real capability being
    # switched off rather than a tidy-up. Read before re-enabling.
    #
    # Turned off because the device is being handled constantly during
    # development and every drop onto a desk placed a phone call. That is
    # the right call for a bench, and the WRONG one for a demonstration --
    # "calls for help autonomously when it detects a fall" is one of the
    # project's headline claims, and with this commented out it is not
    # true.
    #
    # Two things to know before turning it back on:
    #   * fall_rate_ok has been false throughout. The FSM wants a sample
    #     every 20 ms and the TFT loop delivers about 50 ms, so it is
    #     sampling through impact spikes.
    #   * the detector sees the DEVICE falling, not a person. On a desk-
    #     mounted unit those are different events, and the report should
    #     say which one it means.
    #
    # Uncomment for the demo:
    # "FALL":   {"detected", "true", "fallen"},
}

# Per-sensor override of SENSOR_ALERT_DEBOUNCE below.
#
# The blanket 4s debounce exists for ONE reason: a cheap MQ-2 twitches.
# It is the wrong tool for the other three. Node 1 already confirms
# flame across 3 consecutive 50ms polls before it reports at all, and
# its fall FSM needs a freefall AND an impact AND a settled tilt -- so a
# second debounce here just adds four seconds to the two events where
# seconds are the whole point. MOTION keeps a debounce because ToF
# presence flickers at a zone boundary.
#
# Anything not listed here uses SENSOR_ALERT_DEBOUNCE.
SENSOR_ALERT_DEBOUNCE_OVERRIDE = {
    "FLAME": 0.0,
    "FALL":  0.0,
}

# IR is deliberately absent above: it fires on anything walking past and
# would alert constantly. Add it only if the deployment needs it.

SENSOR_WATCH_INTERVAL = 2.0    # seconds between polls of /sensors

# A sensor must hold the alerting state for this long before it counts.
# Cheap MQ-2 smoke sensors twitch; one bad reading should not phone
# somebody at 3am.
SENSOR_ALERT_DEBOUNCE = 4.0

# After an alert fires, ignore ALL sensor triggers for this long. Stops
# a real fire (which keeps the sensor high) from queueing a second call
# the moment the first one hangs up.
SENSOR_ALERT_COOLDOWN = 600.0

# ============================
# Handling reactions
# ============================
# The face board's IMU can tell it was shoved or picked up
# (handling_fsm.py). reaction_watch.py drains api.py's /events log and
# speaks a line from sensor.aiml's REACTION EVENT category.
#
# This is presentation, not assistance -- "why are you pushing me" helps
# nobody do anything. It makes GENIE read as aware of its own situation.
# Say that plainly in the report rather than dressing it up as a feature.
REACTIONS_ENABLED = True

# Faster than the sensor poll on purpose. An alert 2 s late is fine; a
# device that objects to being shoved 2 s after the shove is not
# reacting, it is commenting.
REACTION_POLL_SECONDS = 0.75

# Floor between two spoken reactions, whatever the board sends. The FSM
# already rate-limits at source (one push per 4 s, lifts repeat at 10 s
# and stop after 6), so this is the second line of defence: a board with
# badly tuned thresholds, or a future event type, cannot turn GENIE into
# something that talks continuously.
REACTION_MIN_GAP_SECONDS = 3.0

# GENIE MUST BE DEAF TO ITS OWN VOICE. Not a nicety -- without it the
# feature is unusable, and it was.
#
# The speaker is mounted in the same enclosure as the MPU-6050. When GENIE
# talks, the case vibrates, the classifier reads that as being handled, and
# it reacts -- which makes it talk again. Observed live: an untouched board
# cycling lifted -> set_down -> lifted -> set_down continuously, talking
# over every attempt to use it.
#
# Same failure the wake word already guards against. AudioServer is told to
# stop listening for the whole utterance because, in
# notify_speaking_start()'s own words, "the wake word is live the entire
# time Genie is talking, and it can fire on Genie."
#
# WHERE THE MUTE LIVES MATTERS, and the first attempt got it wrong. A
# laptop-side "ignore reactions for N seconds after any speech" did stop
# the loop -- and also threw away the REAL reactions, because GENIE's own
# reply sat inside its own dead window. Put the unit down right after being
# told "put me down" and the acknowledgement never came.
#
# So the board is muted instead, for exactly as long as the speaker runs.
# speak() sends the ceiling below at the start; notify_speech_done() cuts
# it to the settle the instant playback ends. The classifier keeps running
# and its state keeps tracking throughout -- only the announcement is
# withheld, so the unit never comes out of a mute believing it is still
# where it was before.
REACTION_MUTE_MAX_MS = 25000    # ceiling while Piper is still generating
REACTION_SETTLE_MS = 1200       # enclosure ring-down after the sound stops

# ============================
# Climate control
# ============================
# The sensors act on the room instead of only describing it: warm or humid
# turns the air relay on, and GENIE says why.
#
# ONE RELAY, SEVERAL NAMES. Relay 1 has a fan wired to it. It answers to
# "fan", "exhaust", "AC", "cooling" and "dehumidifier" because those are
# what people say -- but GENIE reports back what is ACTUALLY plugged in.
# Generous in, honest out: claiming to have started a dehumidifier that
# does not exist is a false statement about the world, and an examiner
# will ask. The scalability is real all the same -- swap the appliance and
# only the spoken name changes.
CLIMATE_ENABLED = True
CLIMATE_POLL_SECONDS = 10.0

# TWO THRESHOLDS PER CONDITION, not one. A single 28.0 cutoff flaps: a
# room sitting at 27.9-28.1 would switch the fan on and off every poll,
# announcing it each time. It must get properly cool before the rule lets
# go.
# TWO TIERS, because acting without being asked is only welcome when the
# situation warrants it.
#
#   ASK   warm. GENIE offers -- "it is 29 degrees, shall I turn the fan
#         on?" -- and does nothing unless told yes. Asked ONCE per
#         situation; an assistant that repeats the same offer every ten
#         seconds is worse than one that never offers.
#   ACT   genuinely hot. It switches the fan on and says so. At this point
#         waiting for permission is the wrong behaviour, particularly for
#         a user who may not be able to reach a switch themselves.
CLIMATE_TEMP_ASK = 28.0
CLIMATE_TEMP_ACT = 32.0
CLIMATE_TEMP_OFF = 26.5

# Humidity ACTS rather than asks. Unlike heat, high humidity has no
# comfortable middle where an offer makes sense -- and moving the air is a
# mild, easily reversed intervention.
CLIMATE_HUMID_ON = 70.0
CLIMATE_HUMID_OFF = 67.0

# MANUAL BEATS AUTOMATIC. Say "turn off the fan" while it is 30 degrees
# and it stays off -- otherwise the rule switches it back on within ten
# seconds and the voice command is a lie.
#
# The override lifts when the SITUATION changes, not on a timer. Overriding
# while merely warm should not also decide what happens when it later goes
# humid as well: that is a different condition and the rule deserves
# another say. A plain timeout would either expire while you still want it
# off, or leave the automation dead for the rest of the day.
CLIMATE_OVERRIDE_UNTIL_BAND_CHANGES = True

# What GENIE CALLS relay 1, depending on which reading triggered it. One
# relay, two roles: heat gets the fan, humidity gets the dehumidifier.
#
# Naming it for the JOB rather than the part is what makes the deployment
# story real -- plug an actual dehumidifier into relay 1 and only this
# string changes; nothing in the decision logic knows or cares. The
# aliases in api.py's DEVICE_ALIASES already accept every one of these
# names as a spoken command, so "turn off the dehumidifier" works whether
# or not that is literally what is wired up.
#
# Worth one line in the report: relay 1 currently has a FAN on it, and it
# is performing the dehumidifying role by moving air.
CLIMATE_COOLING_NAME = "fan"
CLIMATE_DEHUMIDIFIER_NAME = "dehumidifier"

# Per-sensor wording for the SMS. Falls back to EMERGENCY_SMS_MESSAGE.
#
# KEEP THESE ASCII. The trigger socket carries UTF-8 happily, but the modem is
# put into AT+CSCS="GSM" by begin(), which cannot represent Urdu or any other
# non-Latin script - the text would arrive mangled. Sending those needs UCS2
# encoding and PDU mode, which nothing here implements yet.
SENSOR_ALERT_MESSAGES = {
    "SMOKE":  "GENIE ALERT: smoke detected. Please check immediately.",
    "FLAME":  "GENIE ALERT: flame detected. Please check immediately.",
    "MOTION": "GENIE ALERT: unexpected movement detected.",
    "FALL":   "GENIE ALERT: a fall was detected. Please check immediately.",
}

# ------------------------------------------------------------------
# Node 1 (GENIE FINAL face node) serial bridge
# ------------------------------------------------------------------
# Node 1 is the ESP32-S3 carrying the TFT face, 2x MQ-2, the flame
# module, DHT11 and 2x MPU-6050, and it relays the 4x VL53L1X ToF array
# that Node 2 sends it over ESP-NOW. It speaks newline-delimited JSON
# over its USB serial port -- the contract is GENIE FINAL's
# 03_LAPTOP/PROTOCOL.md plus PROTOCOL_ADDENDUM.md, and Services/
# genie_serial.py is that contract's reference reader, vendored here
# unchanged so both sides parse with the same code.
#
# WHY SERIAL AND NOT WIFI. Node 1 could POST to api.py directly over
# WiFi and skip this whole file. It must not: Node 2 reaches Node 1 by
# ESP-NOW, and an ESP-NOW peer whose partner has associated with an AP
# on a different channel fails silently -- frames simply stop arriving,
# with no error on either side. The classic symptom is "it worked on
# the bench and not in the lab". Serial keeps Node 1 off the air, and
# keeps the sensor path alive with the router unplugged, which for a
# safety system is the point.
#
# None of the audio path depends on this. If NODE1_SERIAL_PORT is None,
# or the port is missing, or the board is unplugged, the bridge logs
# once and everything else runs exactly as it did before.
# 28-08-2026: SET TO "off", and the reason is not tidiness.
#
# With WIFI_UPLINK = True on the board there are now TWO writers into
# api.py's sensor_state, and they do not know about each other:
#
#   api.py board_listener  <- WiFi telemetry, stores real values, node1 True
#   node_bridge            <- serial. When it cannot open the port it calls
#                             _drop(), which POSTs blanked() with node1 False
#
# node_bridge has no idea another transport is feeding the same store, so a
# serial port it cannot open (Thonny holding it, or no cable at all) makes
# it declare the node dead every few seconds and wipe the readings the WiFi
# path just wrote. Observed live: node1 flipping True/False and every
# channel alternating between a real value and `unknown` on a ~2 s cycle,
# with age_seconds staying low the whole time because BOTH writers keep
# refreshing updated_at. The answer GENIE gave depended on which writer won
# the race, which reads exactly like a board that keeps dropping off.
#
# WHAT THIS COSTS, so nobody rediscovers it as a bug:
#   * the ~150 ms fast alert path. node_bridge called sensor_watch.poll_once()
#     the moment an urgent reading landed; alerts now wait for the 2 s poll.
#   * the emotion channel. whisper_service -> node_bridge -> the board's face
#     ran over serial only, so a laptop-set expression no longer reaches it.
#
# Set this back to a port name if you return to a serial-primary setup --
# but then set WIFI_UPLINK = False on the board too, so only one writer owns
# the state.
NODE1_SERIAL_PORT = "off"     # e.g. "COM5" on Windows, "/dev/ttyUSB0" on Linux.
                              # None = auto-detect (see node_bridge._autodetect),
                              # "off" = disable the bridge entirely.
NODE1_SERIAL_BAUD = 115200

# Serial is a shared resource and Thonny grabs it. If the bridge cannot
# open the port it retries at this interval forever rather than giving
# up, so closing Thonny is enough to bring the sensors back -- no
# restart of whisper_service.
NODE1_RECONNECT_SECONDS = 5.0

# No telemetry line for this long means Node 1 is gone, not merely
# quiet (it ticks every 2s). Every sensor it owns then reports as
# unavailable rather than holding its last reading: a frozen value that
# looks live is worse than a visibly dead sensor, because it reads as a
# confident all-clear.
NODE1_STALE_SECONDS = 8.0

# How long api.py keeps believing "node1: true" after the last push.
#
# WHY THIS EXISTS. link_state used to be whatever was last POSTed, with no
# expiry, so an api.py that had not been written to for fourteen minutes
# still cheerfully reported both boards LIVE -- caught 27-08-2026 with a
# stale test fixture. That is the exact "frozen value that looks live"
# failure every sensor channel here is built to avoid, sitting in the one
# field whose job is to say whether the others can be trusted.
#
# Must stay comfortably above NODE_REFRESH_SECONDS below, or a healthy but
# quiet system flickers between live and stale.
LINK_STALE_SECONDS = 30.0

# The bridge re-POSTs even when nothing has changed, at this interval, so
# LINK_STALE_SECONDS has something to measure. One POST every few seconds
# is nothing; the alternative is a 30-second window where a dead bridge
# still reads as live.
NODE_REFRESH_SECONDS = 10.0

# Node 1 -> api.py sensor-name mapping is in node_bridge.py, not here.
# It is code, not configuration: two of the seven entries need a max()
# across a pair of physical sensors and one needs a level lookup.

# ------------------------------------------------------------------
# Sensor nodes over WiFi
# ------------------------------------------------------------------
# The same network the mic ESP already uses. Mirrored here so the laptop
# and the boards have one place to disagree from, rather than two.
WIFI_SSID = "GENIE1"
WIFI_PASSWORD = "12345678"

# Node 1 streams the SAME newline-JSON it puts on the serial port to this
# TCP port instead. One long-lived socket, one write() per tick.
#
# WHY A RAW SOCKET AND NOT AN HTTP POST TO api.py.
# Node 1 is one cooperative loop with a 33.3 ms budget at 30 fps, and two
# eyes already cost 15.6 ms of it. MicroPython's urequests.post() is
# blocking: DNS, connect, send, wait for the response headers. On a
# healthy LAN that is 40-150 ms and on a busy one it is seconds. Every
# one of those milliseconds is a frozen face -- a POST every 2 s would
# put a visible hitch in the eyes twice a minute, and a router hiccup
# would look like a crash.
#
# A socket that is already open costs a write() -- microseconds -- which
# is the same thing the serial path costs. So the wire format stays
# newline-JSON and the transport becomes a detail.
NODE1_TCP_PORT = 9005

# Serial and WiFi are not exclusive. Node 1 can run both, and the bridge
# accepts whichever arrives:
#
#   SERIAL  survives the router being down, which for a safety device is
#           the point. Costs a cable and blocks Thonny.
#   WIFI    no cable, board can sit anywhere, Thonny stays free. Dies
#           with the network.
#
# Running both is the safe default -- the bridge de-duplicates by taking
# whatever arrives most recently, and a value pushed twice is harmless.
# WHO LISTENS ON NODE1_TCP_PORT.
#
# api.py does, and it starts that listener itself. It is the store the
# boards are trying to reach, and putting the socket there means `python
# api.py` plus two boards is a complete sensor rig -- no whisper_service,
# no forty-second Whisper load before a distance reading appears.
#
# node_bridge therefore does NOT bind it. Two processes cannot hold one
# port: whichever started second would log a bind error and silently do
# nothing, which is a confusing way to lose half the sensors.
#
# Set True only if you are running node_bridge WITHOUT api.py, which is an
# odd thing to want -- the bridge's whole job is feeding api.py.
NODE1_ACCEPT_TCP = False

# One consequence worth knowing. The serial path pokes sensor_watch the
# instant an urgent reading lands, so a fall or a flame alerts in ~150 ms.
# The WiFi path cannot: api.py and whisper_service are separate processes,
# so sensor_watch finds it on its next poll instead -- up to
# SENSOR_WATCH_INTERVAL, currently 2 s.
#
# 2 s to raise a fall alarm is fine and is worth saying out loud rather
# than discovering. If you want the fast path, keep the USB cable plugged
# in; the board sends over both at once.

# ------------------------------------------------------------------
# ToF array (Node 2, 4x VL53L1X, relayed by Node 1)
# ------------------------------------------------------------------
# Four sensors, and the order is the XSHUT bring-up order on Node 2
# (GPIO 10, 11, 12, 13), which is also the order of the four apertures in
# the enclosure. Nothing can detect a swapped pair -- the code will just
# confidently report the wrong direction -- so this order is a wiring
# fact, not a preference.
TOF_SIDES = ("front", "left", "right", "rear")

# Distance bands, for naming a reading rather than reciting millimetres.
#
# The project has THREE copies of these and they do not agree:
# pins3.ROOM_ZONES is room scale (100/500/1500/3000) and is used only by
# the bench tools; zones.DEFAULT_ZONES and node1_link._BANDS are desk
# scale, and desk scale is what production actually applies -- so a person
# standing 2 m away triggers correctly and is reported CLEAR.
#
# This table is the LAPTOP's, applied to readings that arrive as raw
# millimetres (the ToF board's direct line). Readings relayed through
# Node 1 arrive pre-named by node1_link._BANDS and are passed through as
# they are. Aligning those two is a board-side change; until then, expect
# the direct path to be the more accurate of the two.
TOF_BANDS = ((40, "TOUCH"), (300, "NEAR"), (800, "PRESENT"),
             (1500, "FAR"), (99999, "CLEAR"))

# Drive api.py's IR channel from the ToF FRONT sensor.
#
# sensor.aiml already carries a complete IR category -- "is someone in
# front of you", "is your path blocked", "do you detect anyone", plus a
# dozen more phrasings -- and until now NOTHING fed ir_status, so every
# one of those questions answered "I cannot read the presence sensor
# right now." Node 1 has no IR sensor at all: the pin-map docs list one
# on GPIO 13, and the firmware never reads that pin.
#
# The ToF front sensor answers exactly that question and answers it
# better -- it has a distance and it cannot be fooled by a dark surface
# the way a reflective IR gate can.
#
# Set False if the original sensor ESP32 (the board that POSTs to
# /update_sensor directly) turns out to be pushing real IR readings --
# then this would fight it. Nothing on this machine writes IR today.
TOF_DRIVES_IR = True

# MOTION stays "any of the four triggered" and remains the channel that
# can raise an alert. IR is front-only and alerts on nothing. Keeping
# them separate is deliberate: "something is in front of me" is a
# conversational answer, "something is in the room" is a safety signal,
# and collapsing the two would either alert on a person walking past or
# stop answering the question.

# Nearest-object reporting. api.py's DISTANCE channel carries the nearest
# valid reading in mm plus which side saw it, so "how far away is it" has
# a real answer instead of an invented one.
TOF_DISTANCE_ENABLED = True

# ------------------------------------------------------------------
# Face / emotion channel (laptop -> Node 1)
# ------------------------------------------------------------------
# Node 1 renders whatever expression it is told, and FORGETS it after
# 20 seconds, falling back to sensor-driven. That expiry is deliberate
# and must not be "fixed": if whisper_service dies mid-conversation,
# GENIE must not sit there with a happy face during a gas leak. So the
# emotion is re-sent on every turn rather than held.
#
# Alarms always win on the node side -- a gas, flame or fall alarm
# forces the alarm face regardless of what was asked for here.
FACE_ENABLED = True
FACE_THINKING = "thinking"    # sent when the LLM is working
FACE_SPEAKING = "happy"       # default while speaking, if the reply carries no tag
FACE_ALARM = "alarm"          # sent by the sensor-alert path

# Mistral is asked to tag its own reply's sentiment (see the tag lines
# appended to LLM_SYSTEM_PROMPT). Zero extra latency, zero extra model:
# it already knows the sentiment of what it just wrote. A separate
# classifier pass would re-derive that for 100-300ms on a pipeline that
# is already the bottleneck.
FACE_EMOTIONS = ("neutral", "happy", "sad", "thinking", "concerned", "alarm")

# ------------------------------------------------------------------
# Modem ESP32-S3 wiring (used by the MicroPython firmware, not the PC)
# ------------------------------------------------------------------
# Mirrored here so the wiring documented in docs/emergency_call_audio.md
# has exactly one source of truth that both sides can read.
MODEM_UART_ID   = 1
MODEM_UART_TX   = 17     # ESP32 TX -> modem RXD
MODEM_UART_RX   = 18     # ESP32 RX <- modem TXD
MODEM_UART_RST  = 16
MODEM_UART_BAUD = 115200

# I2S out to the PCM5102A. SCK on the DAC is tied to GND in hardware and
# has no GPIO here on purpose -- a floating SCK is the most common cause
# of that board producing nothing at all.
I2S_BCLK = 5
I2S_LRCK = 6
I2S_DIN  = 7
I2S_XSMT = 15            # LOW = DAC muted, HIGH = playing

# MUST equal the sample rate of emergency_mic.wav, which is whatever
# Piper's voice produces (22050 for en_US-amy-medium). Mismatch here
# plays the emergency message pitch-shifted.
I2S_RATE = 22050