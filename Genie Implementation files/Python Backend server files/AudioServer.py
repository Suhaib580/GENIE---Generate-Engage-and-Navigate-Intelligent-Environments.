import socket
import wave
import time
import os
import math
import array
import queue
import threading
import traceback
from collections import deque

from config import (
    HOST,
    PORT,
    SAMPLE_RATE,
    CHANNELS,
    SAMPLE_WIDTH,
    STEREO_CHUNK_SIZE,
    OUTPUT_DIR,
    PREROLL_CHUNKS,
    DISCOVERY_PORT,
    MIC_GAIN,
    HPF_CUTOFF_HZ,
    DETECT_SOURCE,
    MAX_COMMAND_SECONDS,
    DEBUG_RMS,
    CONTROL_PORT,
    FOLLOWUP_PORT,
    FOLLOWUP_WINDOW,
    FOLLOWUP_MAX_CHAIN,
    FOLLOWUP_ARM_DELAY,
    OP_FOLLOWUP_START,
    OP_FOLLOWUP_END,
    OP_LISTEN_START,
    OP_SPEAK_START,
    OP_SPEAK_END,
    MIC_MUTE_MAX_SECONDS,
    OP_LISTEN_END,
)

from Services.rms_detector import RMSDetector
from Services.vad_detector import VADetector
from Services.wake_word import WakeWordDetector


# ============================================================
# LISTENING-CUE SENDER (orange LED + beeps on the speaker board)
# ============================================================
#
# The "mic is listening now / stopped listening" cues belong to THIS
# process -- AudioServer owns the wake + VAD state machine. But the board
# that lights up and beeps (the speaker ESP) is connected to
# whisper_service.py, not here. So we relay the cue as a single opcode
# byte over a localhost control link (CONTROL_PORT); whisper_service
# re-broadcasts it to the speaker board as an opcode.
#
# Sends are fire-and-forget through a queue + background thread, so a
# slow or absent whisper_service can NEVER stall the real-time audio
# loop. If nothing is listening on CONTROL_PORT, the cue is silently
# dropped and detection carries on exactly as before.

class CueSender:

    def __init__(self, host, port):
        self._host = host
        self._port = port
        self._q = queue.Queue(maxsize=8)
        self._sock = None
        threading.Thread(target=self._run, daemon=True).start()

    def send(self, opcode):
        # Never blocks the caller (the audio thread). Drops the cue if the
        # queue is somehow backed up -- a missed beep is harmless.
        try:
            self._q.put_nowait(opcode)
        except queue.Full:
            pass

    def _run(self):
        while True:
            opcode = self._q.get()
            self._deliver(opcode)

    def _deliver(self, opcode):
        # Try once on the existing connection; if that fails, reconnect
        # once and retry. If whisper_service isn't up, give up quietly --
        # cues are optional feedback, not part of the audio path.
        for _ in range(2):
            if self._sock is None:
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(0.5)
                    s.connect((self._host, self._port))
                    s.settimeout(None)
                    self._sock = s
                except OSError:
                    self._sock = None
                    return
            try:
                self._sock.sendall(bytes([opcode]))
                return
            except OSError:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None


# ============================================================
# SETUP
# ============================================================

os.makedirs(OUTPUT_DIR, exist_ok=True)

# Sends listening cues (orange LED + beeps) to the speaker board via
# whisper_service's control link. Best-effort; a no-op if whisper_service
# isn't running.
cue = CueSender("127.0.0.1", CONTROL_PORT)

# One stereo frame:
#
# Mic A / LEFT  = 2 bytes
# Mic B / RIGHT = 2 bytes
#
# Total = 4 bytes
FRAME_BYTES = 4


# ============================================================
# HIGH-PASS FILTER
# ============================================================
#
# The RMS meter squares raw samples WITHOUT removing DC or low-frequency
# content first, so the INMP441's DC bias + sub-cutoff rumble (HVAC, fans,
# desk vibration, mic handling) get counted as "loudness" -- and MIC_GAIN
# multiplies all of it. None of it is speech. A one-pole high-pass run
# BEFORE gain/RMS strips it, so the RMS reading reflects actual voice-band
# energy, and the recorded WAV is cleaner for Whisper too.
#
# One-pole coefficient from the cutoff (config: HPF_CUTOFF_HZ).

_HPF_DT = 1.0 / SAMPLE_RATE
_HPF_RC = 1.0 / (2.0 * math.pi * HPF_CUTOFF_HZ)
HPF_ALPHA = _HPF_RC / (_HPF_RC + _HPF_DT)


class HighPass:
    """Stateful one-pole high-pass filter for a single 16-bit PCM channel.

    State (x_prev / y_prev) persists across recv() chunks so there is no
    discontinuity at chunk boundaries. One instance PER channel per
    connection -- Mic A and Mic B each need their own.
    """

    def __init__(self, alpha):
        self.alpha = alpha
        self.x_prev = 0.0
        self.y_prev = 0.0

    def process(self, pcm):

        samples = array.array("h")
        samples.frombytes(pcm)

        a = self.alpha
        xp = self.x_prev
        yp = self.y_prev

        for i in range(len(samples)):

            x = samples[i]

            # y[n] = a * (y[n-1] + x[n] - x[n-1])
            yp = a * (yp + x - xp)
            xp = x

            v = int(yp)

            if v > 32767:
                v = 32767
            elif v < -32768:
                v = -32768

            samples[i] = v

        self.x_prev = xp
        self.y_prev = yp

        return samples.tobytes()


# ============================================================
# DISCOVERY BROADCAST
# ============================================================

# ============================================================
# REVERSE CUE LINK  (whisper_service -> here)
# ============================================================
# Holds the time.time() at which Genie last FINISHED speaking, or 0.0 if
# it never has. handle_mic() reads this to decide whether it is inside a
# follow-up window.
#
# A bare float assigned under the GIL needs no lock: the listener thread
# only ever overwrites it, handle_mic() only ever reads it, and a torn
# read is impossible for a single float rebind in CPython. Keeping it
# lock-free matters because handle_mic() is the real-time audio path and
# must never block on whisper_service.
_last_speech_done = 0.0

# True from the moment whisper_service starts streaming a reply until it
# reports that reply has finished playing.
_speaking = False
_speaking_since = 0.0


def is_speaking():
    """Is Genie talking right now?

    While this is True the microphone path ignores audio completely.
    Without it, GENIE hears its own reply through its own microphone:
    the wake word can fire on the assistant's voice, opening a capture
    mid-sentence, which is then transcribed and answered -- a feedback
    loop the follow-up window makes considerably easier to enter.

    MIC_MUTE_MAX_SECONDS is the failsafe. If whisper_service dies
    between SPEAK_START and SPEAK_END the release byte never arrives,
    and a permanently deaf microphone would be a worse failure than the
    echo this prevents."""
    if not _speaking:
        return False

    if time.time() - _speaking_since > MIC_MUTE_MAX_SECONDS:
        return False

    return True


def speech_done_at():
    """When did Genie last stop talking? 0.0 = never this session."""
    return _last_speech_done


def followup_listener():
    """Accept connections from whisper_service and record the moment each
    reply finished playing.

    Best-effort by design. whisper_service may be down, may restart, may
    never connect at all -- in every one of those cases GENIE must keep
    working exactly as it does today, just without follow-up. So every
    failure here is swallowed and the loop continues; nothing in this
    thread can stall or kill the audio path."""
    global _last_speech_done, _speaking, _speaking_since

    try:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((HOST, FOLLOWUP_PORT))
        server.listen(5)
    except OSError as e:
        print(f"[followup] could NOT listen on {FOLLOWUP_PORT}: {e}")
        print("[followup] follow-up window disabled; wake word still works.")
        return

    print(f"Follow-up link listening on {FOLLOWUP_PORT} "
          f"(whisper_service speech-done signal)")

    while True:
        try:
            conn, _ = server.accept()
        except OSError:
            continue

        # One connection may carry many replies -- whisper_service keeps
        # it open across utterances rather than reconnecting each time.
        try:
            while True:
                b = conn.recv(1)
                if not b:
                    break
                op = b[0]

                if op == OP_SPEAK_START:
                    _speaking = True
                    _speaking_since = time.time()
                    print("[followup] Genie is speaking -- mic muted")

                elif op == OP_SPEAK_END:
                    _speaking = False
                    _last_speech_done = time.time()
                    print("[followup] reply finished -- mic live, follow-up window may open")

                else:
                    # Unknown opcode: treat as end-of-reply rather than
                    # ignoring it, so an unrecognised byte can never
                    # leave the mic muted.
                    _speaking = False
                    _last_speech_done = time.time()
                    print(f"[followup] unknown op {op} -- treating as speech-done")
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


def discovery_broadcaster():
    """
    Periodically broadcasts a UDP discovery message so the ESP32
    can automatically discover the laptop's current IP address.
    """

    sock = socket.socket(
        socket.AF_INET,
        socket.SOCK_DGRAM
    )

    sock.setsockopt(
        socket.SOL_SOCKET,
        socket.SO_BROADCAST,
        1
    )

    while True:

        try:

            sock.sendto(
                b"GENIE_SERVER_HELLO",
                ("255.255.255.255", DISCOVERY_PORT)
            )

        except OSError as e:

            print(
                f"Discovery broadcast failed: {e}"
            )

        time.sleep(2)


# ============================================================
# APPLY MICROPHONE GAIN
# ============================================================

def apply_gain(data, gain):
    """
    Apply gain to 16-bit PCM audio.

    Values are clamped to the valid int16 range.
    """

    if gain == 1.0:
        return data

    samples = array.array("h")

    samples.frombytes(data)

    for i in range(len(samples)):

        value = int(
            samples[i] * gain
        )

        if value > 32767:
            value = 32767

        elif value < -32768:
            value = -32768

        samples[i] = value

    return samples.tobytes()


# ============================================================
# DEINTERLEAVE STEREO
# ============================================================

def deinterleave(chunk):
    """
    Convert:

        LEFT RIGHT LEFT RIGHT LEFT RIGHT ...

    into:

        left bytes
        right bytes

    Mic A = LEFT
    Mic B = RIGHT
    """

    stereo = array.array("h")

    stereo.frombytes(chunk)

    left = stereo[0::2]

    right = stereo[1::2]

    return (
        left.tobytes(),
        right.tobytes()
    )


# ============================================================
# BEAMFORMER (delay-and-sum, broadside)
# ============================================================

def beamform(left, right):
    """
    Combine both microphones into one detection channel.

    combined[n] = (left[n] + right[n]) / 2

    This is a delay-and-sum beamformer with the delay set to ZERO, i.e.
    steered broadside (straight out from the mic pair). Zero delay is
    legitimate here rather than lazy: both INMP441s hang off the SAME I2S
    bus and are clocked by the same WS/SCK, so sample n on the left is
    physically simultaneous with sample n on the right. There is no clock
    drift to correct -- only the acoustic path difference, which at the
    measured 10 cm spacing is at most 4.7 samples at 16 kHz, and is exactly
    ZERO for a talker centred in front of the box (equal path lengths).
    See DETECT_SOURCE in config.py for the off-axis geometry.

    Why average instead of add:

        Speech is CORRELATED across the two mics, so it survives the
        averaging at full amplitude. Each mic's self-noise (electrical,
        thermal, ADC) is UNCORRELATED, so averaging drops it by ~3 dB.
        Net: better SNR into the wake word at the same signal level.

        Adding instead of averaging would double speech amplitude and
        clip -- MIC_GAIN (6x) has already pushed levels up, so headroom
        is not there to spend. Averaging keeps the level identical to a
        single mic, which matters because WAKE_THRESHOLD and VAD_MIN_RMS
        are both tuned against single-mic levels and would otherwise need
        re-tuning.

    Returns 16-bit PCM bytes, same length as either input.
    """

    a = array.array("h")
    a.frombytes(left)

    b = array.array("h")
    b.frombytes(right)

    # Defensive: deinterleave always yields equal halves, but a truncated
    # frame upstream would silently zip short and quietly shorten the
    # detection chunk. Cut to the common length instead.
    n = min(len(a), len(b))

    out = array.array("h", bytes(2 * n))

    for i in range(n):

        # Sum in Python ints (arbitrary precision) BEFORE halving, so the
        # intermediate can't wrap the int16 range mid-calculation.
        out[i] = (a[i] + b[i]) // 2

    return out.tobytes()


# ============================================================
# OPEN WAV FILE
# ============================================================

def _open_wav(ts, suffix):
    """
    Create a mono WAV file for one microphone.
    """

    filename = os.path.join(
        OUTPUT_DIR,
        f"recording_{ts}_{suffix}.wav"
    )

    wf = wave.open(
        filename,
        "wb"
    )

    wf.setnchannels(
        CHANNELS
    )

    wf.setsampwidth(
        SAMPLE_WIDTH
    )

    wf.setframerate(
        SAMPLE_RATE
    )

    return wf


# ============================================================
# MICROPHONE TCP HANDLER
# ============================================================

def handle_mic(conn, addr):
    """
    Handle one ESP32 stereo microphone connection.

    Incoming TCP stream:

        LEFT RIGHT LEFT RIGHT LEFT RIGHT ...

    LEFT:
        Mic A

    RIGHT:
        Mic B

    Mic A controls VAD.

    Mic B is monitored independently and is also recorded
    whenever Mic A triggers a recording.

    Both microphones are time-aligned because they arrive
    in the same stereo stream.
    """

    print()
    print("========================================")
    print(" STEREO MICROPHONE CONNECTION")
    print("========================================")
    print("Client:", addr)
    print("LEFT  = Mic A")
    print("RIGHT = Mic B")
    print()

    # --------------------------------------------------------
    # TCP timeout
    # --------------------------------------------------------

    # No timeout: the connection is only ever closed when the ESP itself
    # closes it or the TCP link genuinely errors. It will NEVER be dropped
    # just because audio went quiet for a few seconds. (Was settimeout(5.0),
    # which is what used to kill the stream after 5s.)
    conn.settimeout(None)

    # Diagnostics: so every disconnect can report how much actually
    # arrived before it died (0 bytes = ESP never sent; lots = it streamed
    # then stopped/crashed on one side).
    total_bytes = 0
    total_chunks = 0
    conn_start = time.time()

    # --------------------------------------------------------
    # RMS/VAD detector
    # --------------------------------------------------------

    # Wake word gates the START ("Hey Genie"); the VAD then only finds the
    # END of the command. mode is either "WAIT_WAKE" or "CAPTURE".
    wake = WakeWordDetector()
    vad = VADetector()

    # mode is "WAIT_WAKE", "FOLLOW_UP" or "CAPTURE".
    #
    #   WAIT_WAKE  idle. ONLY "Hey Genie" can open a recording.
    #   FOLLOW_UP  the FOLLOWUP_WINDOW seconds after Genie finished
    #              speaking. Wake word still works, but sustained speech
    #              alone is enough -- this is what removes the second and
    #              third "Hey Genie" from a conversation.
    #   CAPTURE    recording the command; the VAD now only finds its END.
    mode = "WAIT_WAKE"
    capture_start = 0.0

    # When the last capture finished. The follow-up window may only open
    # on a reply that arrived AFTER this moment -- otherwise the stale
    # speech-done timestamp from the PREVIOUS answer would reopen the
    # window the instant a new command stopped recording, while Whisper
    # and Mistral are still working on it. The mic would be live, and
    # Genie's own reply would land in an open follow-up capture.
    last_capture_end = 0.0

    # Follow-ups taken since the last real wake word. Reset to zero the
    # moment someone actually says 'Hey Genie'; a runaway chain of
    # wake-word-free captures cannot exceed FOLLOWUP_MAX_CHAIN.
    followup_chain = 0

    # Display meters only (RMS/Peak console lines). Mic A no longer decides
    # anything by RMS; both of these exist purely so signal levels stay
    # visible for setting MIC_GAIN.
    meter_a = RMSDetector()
    meter_b = RMSDetector()

    # Third meter for the beamformed channel. Its whole job is the
    # comparison that tells you whether beamforming is actually working:
    # in a quiet room [Beam] RMS should sit BELOW both [Mic A] and [Mic B]
    # (uncorrelated self-noise partially cancelling), while during speech it
    # should track them (correlated signal surviving the average). If [Beam]
    # is not below both in silence, the mics are not contributing
    # independent noise and beamforming is buying nothing.
    meter_beam = RMSDetector()

    # --------------------------------------------------------
    # Per-channel high-pass filters (one each, stateful across chunks)
    # --------------------------------------------------------

    hpf_a = HighPass(HPF_ALPHA)
    hpf_b = HighPass(HPF_ALPHA)

    # --------------------------------------------------------
    # Preroll
    #
    # Each entry contains:
    #
    #     (Mic A audio, Mic B audio)
    # --------------------------------------------------------

    preroll = deque(
        maxlen=PREROLL_CHUNKS
    )

    # --------------------------------------------------------
    # WAV handles
    # --------------------------------------------------------

    wf_a = None
    wf_b = None

    # --------------------------------------------------------
    # TCP leftover bytes
    #
    # TCP does NOT guarantee that recv() returns complete
    # stereo frames.
    #
    # We therefore keep incomplete bytes here.
    # --------------------------------------------------------

    leftover = b""

    # --------------------------------------------------------
    # Main receive loop
    # --------------------------------------------------------

    while True:

        try:

            data = conn.recv(
                STEREO_CHUNK_SIZE
            )

        except socket.timeout:

            print()
            print(
                "Mic board: no data received for 20 seconds "
                f"(got {total_bytes} bytes / {total_chunks} chunks "
                f"over {time.time() - conn_start:.1f}s before going quiet)."
            )

            break

        except (
            ConnectionResetError,
            ConnectionAbortedError,
            BrokenPipeError,
            OSError
        ) as e:

            print()
            print(
                "Mic board: connection lost:"
            )

            print(e)

            break

        # ----------------------------------------------------
        # ESP32 closed connection
        # ----------------------------------------------------

        if not data:

            print()
            print(
                "Mic board: ESP disconnected (clean close) "
                f"after {total_bytes} bytes / {total_chunks} chunks."
            )

            break

        total_bytes += len(data)
        total_chunks += 1

        # ----------------------------------------------------
        # Combine previous incomplete bytes with new data
        # ----------------------------------------------------

        buf = (
            leftover +
            data
        )

        # ----------------------------------------------------
        # Find complete stereo frames
        # ----------------------------------------------------

        usable_len = (
            len(buf) -
            (len(buf) % FRAME_BYTES)
        )

        # Keep incomplete bytes for next recv()
        leftover = buf[
            usable_len:
        ]

        # Nothing complete yet
        if usable_len == 0:
            continue

        # ----------------------------------------------------
        # Extract complete stereo data
        # ----------------------------------------------------

        stereo_chunk = buf[
            :usable_len
        ]

        # ----------------------------------------------------
        # Separate Mic A and Mic B
        # ----------------------------------------------------

        left, right = deinterleave(
            stereo_chunk
        )

        # ----------------------------------------------------
        # High-pass filter (BEFORE gain)
        #
        # Strips DC + sub-cutoff rumble so gain doesn't amplify it and
        # the RMS reading reflects actual voice-band energy. Runs before
        # gain so DC can't be inflated into clipping first.
        # ----------------------------------------------------

        left = hpf_a.process(left)

        right = hpf_b.process(right)

        # ----------------------------------------------------
        # Apply gain
        # ----------------------------------------------------

        left = apply_gain(
            left,
            MIC_GAIN
        )

        right = apply_gain(
            right,
            MIC_GAIN
        )

        # ----------------------------------------------------
        # What the wake word and the VAD actually listen to (DETECT_SOURCE):
        # a single mic, or both mics beamformed into one channel. Mic A used
        # to be ~2x noisier than Mic B, which is why detection was pinned to
        # a single clean channel; A's baseline now matches B, so both can be
        # combined. Both mics are still recorded to their own WAVs either
        # way -- this only changes what DETECTION hears, never what Whisper
        # transcribes.
        # ----------------------------------------------------

        if DETECT_SOURCE == "BEAM":
            vad_input = beamform(left, right)
        elif DETECT_SOURCE == "B":
            vad_input = right
        else:
            vad_input = left

        # ----------------------------------------------------
        # Mic A RMS / Peak -- DISPLAY ONLY. Shows the current mode plus the
        # live wake-word score (while waiting) and the VAD speech ratio
        # (while capturing), so both gates are visible for tuning.
        # ----------------------------------------------------

        mic_a_rms, mic_a_peak = meter_a.calculate_rms(
            left
        )

        if DEBUG_RMS and meter_a.should_print(mic_a_rms, mic_a_peak):

            mic_a_pct = mic_a_peak / 327.67

            print(
                f"[Mic A] RMS: {int(mic_a_rms):5d} | Peak: {mic_a_peak:5d} "
                f"({mic_a_pct:4.1f}% FS) | {mode} "
                f"| wake {wake.last_score:.2f} | speech {vad.speech_ratio:.2f}"
            )

        # ----------------------------------------------------
        # Mic B RMS / Peak (diagnostic only, mirrors Mic A's
        # line exactly -- same fields, same heartbeat
        # suppression -- minus "State:", since Mic B has no
        # real VAD state of its own)
        # ----------------------------------------------------

        mic_b_rms, mic_b_peak = meter_b.calculate_rms(
            right
        )

        if DEBUG_RMS and meter_b.should_print(mic_b_rms, mic_b_peak):

            mic_b_pct = mic_b_peak / 327.67

            mic_b_heartbeat = (
                " (heartbeat, still silent)"
                if meter_b._silent_run > 1
                else ""
            )

            print(
                f"[Mic B] RMS: {int(mic_b_rms):5d} | Peak: {mic_b_peak:5d} "
                f"({mic_b_pct:4.1f}% FS){mic_b_heartbeat}"
            )

        # ----------------------------------------------------
        # Beam RMS / Peak -- only when beamforming is actually driving
        # detection, so the console doesn't grow a third line for nothing.
        # ----------------------------------------------------

        if DETECT_SOURCE == "BEAM":

            beam_rms, beam_peak = meter_beam.calculate_rms(
                vad_input
            )

            if DEBUG_RMS and meter_beam.should_print(beam_rms, beam_peak):

                beam_pct = beam_peak / 327.67

                print(
                    f"[Beam ] RMS: {int(beam_rms):5d} | Peak: {beam_peak:5d} "
                    f"({beam_pct:4.1f}% FS) | {mode} "
                    f"| wake {wake.last_score:.2f} | speech {vad.speech_ratio:.2f}"
                )

        # ----------------------------------------------------
        # Store both channels in preroll
        # ----------------------------------------------------

        # ----------------------------------------------------
        # Muted while Genie talks
        # ----------------------------------------------------
        # Everything below this point -- wake word, VAD, preroll -- is
        # skipped while the reply is playing. The pre-roll buffer is
        # cleared rather than merely frozen: it is back-filled into the
        # next recording, so leaving the tail of Genie's own reply in it
        # would prepend the assistant's voice to the user's next command.

        if is_speaking():
            preroll.clear()
            wake.reset()
            continue

        preroll.append(
            (
                left,
                right
            )
        )

        # ====================================================
        # WAIT_WAKE -- nothing is recorded until "Hey Genie" fires
        # ====================================================

        # ----------------------------------------------------
        # Follow-up window bookkeeping
        # ----------------------------------------------------
        # Purely derived from "how long since Genie stopped talking", so
        # it self-heals: a missed speech-done byte just means no window,
        # never a stuck-open mic. Only WAIT_WAKE <-> FOLLOW_UP flip here;
        # CAPTURE is never interrupted by the window expiring under it.

        if FOLLOWUP_WINDOW > 0 and mode in ("WAIT_WAKE", "FOLLOW_UP"):

            since_reply = time.time() - speech_done_at()
            open_now = (
                speech_done_at() > last_capture_end
                and FOLLOWUP_ARM_DELAY <= since_reply <= FOLLOWUP_WINDOW
                and followup_chain < FOLLOWUP_MAX_CHAIN
            )

            if open_now and mode == "WAIT_WAKE":
                mode = "FOLLOW_UP"
                print()
                print(f">>> FOLLOW-UP WINDOW OPEN ({FOLLOWUP_WINDOW:.0f}s) "
                      f"-- just talk, no wake word needed "
                      f"[{followup_chain}/{FOLLOWUP_MAX_CHAIN} used]")
                print()

                cue.send(OP_FOLLOWUP_START)
                try:
                    conn.sendall(bytes([OP_FOLLOWUP_START]))
                except OSError:
                    pass

            elif not open_now and mode == "FOLLOW_UP":
                mode = "WAIT_WAKE"
                # Drop the VAD back to a clean IDLE so a half-finished
                # speech run inside the window can't leak into the next
                # one and open a capture the moment it reopens.
                vad.begin_capture()
                vad.state = vad.IDLE
                print()
                print(">>> follow-up window closed -- wake word required again")
                print()

                cue.send(OP_FOLLOWUP_END)
                try:
                    conn.sendall(bytes([OP_FOLLOWUP_END]))
                except OSError:
                    pass

        # ====================================================
        # WAIT_WAKE -- nothing is recorded until "Hey Genie" fires
        # FOLLOW_UP -- ...or, briefly, until you simply speak
        # ====================================================

        if mode in ("WAIT_WAKE", "FOLLOW_UP"):

            trigger = None

            if wake.process(vad_input):
                trigger = f"WAKE WORD DETECTED (score {wake.detect_score:.2f})"
                # A deliberate wake word ends any runaway chain.
                followup_chain = 0

            elif mode == "FOLLOW_UP" and vad.process(vad_input) == "START":
                # No wake word required here -- this is the whole point of
                # the window. VAD_TRIGGER_RATIO still demands sustained
                # speech across a 300 ms window, so a single knock or
                # click cannot open a recording; and if what follows is
                # not real speech, Whisper's confidence gate discards it
                # without ever waking Mistral.
                trigger = "FOLLOW-UP SPEECH (no wake word)"
                followup_chain += 1

            if trigger:

                print()
                print(f">>> {trigger}")
                print("Listening for your command...")
                print()

                ts = int(time.time() * 1000)

                wf_a = _open_wav(ts, "A")
                wf_b = _open_wav(ts, "B")

                # Back-fill preroll so the first word of the command isn't
                # clipped. (The wake word itself is in here too; Whisper
                # just ignores it.)
                for pl, pr in preroll:
                    wf_a.writeframes(pl)
                    wf_b.writeframes(pr)

                vad.begin_capture()
                mode = "CAPTURE"
                capture_start = time.time()

                # Cue both boards the instant we enter CAPTURE: the speaker
                # board beeps (relayed via whisper_service), and the mic
                # board lights orange straight down its own stream socket.
                cue.send(OP_LISTEN_START)
                try:
                    conn.sendall(bytes([OP_LISTEN_START]))
                    print("[cue] mic board <- OP_LISTEN_START (orange)")
                except OSError as e:
                    print(f"[cue] mic board OP_LISTEN_START FAILED: {e}")

        # ====================================================
        # CAPTURE -- record until the VAD sees end-of-speech, or the cap
        # ====================================================

        else:

            result = vad.process(vad_input)

            if wf_a is not None:
                wf_a.writeframes(left)
                wf_b.writeframes(right)

            hit_cap = (time.time() - capture_start) >= MAX_COMMAND_SECONDS

            if result == "STOP" or hit_cap:

                if wf_a is not None:
                    wf_a.close()
                    wf_b.close()
                    wf_a = None
                    wf_b = None

                print()
                print(
                    ">>> COMMAND CAPTURED "
                    f"({'max length' if hit_cap else 'end of speech'})"
                )
                print("Recording saved: Mic A + Mic B")
                print()

                wake.reset()
                mode = "WAIT_WAKE"
                last_capture_end = time.time()

                # Falling "done" beep on the speaker board + the mic board's
                # LED back to green (streaming idle).
                cue.send(OP_LISTEN_END)
                try:
                    conn.sendall(bytes([OP_LISTEN_END]))
                    print("[cue] mic board <- OP_LISTEN_END (green)")
                except OSError as e:
                    print(f"[cue] mic board OP_LISTEN_END FAILED: {e}")


    # ========================================================
    # CONNECTION CLEANUP
    # ========================================================

    if wf_a is not None:

        wf_a.close()

        wf_a = None

    if wf_b is not None:

        wf_b.close()

        wf_b = None

    print()

    print(
        "Stereo microphone handler finished."
    )

    print()


# ============================================================
# THREAD WRAPPER
# ============================================================

def _run_handler(conn, addr):
    """
    Run one ESP32 connection in its own thread.
    """

    try:

        handle_mic(
            conn,
            addr
        )

    except Exception as e:

        print()
        print(
            "Unexpected error in mic handler:"
        )

        print(e)

        # Full traceback -> tells us the EXACT file+line that threw,
        # instead of just the exception message. This is what will
        # reveal whether the disconnect is a processing bug.
        traceback.print_exc()

    finally:

        try:
            conn.close()

        except:
            pass

        print(
            "Mic board connection closed."
        )

        print()


# ============================================================
# ACCEPT LOOP
# ============================================================

def _accept_loop(server):

    while True:

        try:

            conn, addr = server.accept()

            print()
            print(
                "Mic board connected:",
                addr
            )

            threading.Thread(
                target=_run_handler,
                args=(conn, addr),
                daemon=True
            ).start()

        except Exception as e:

            print(
                "Accept loop error:",
                e
            )

            time.sleep(1)


# ============================================================
# MAIN
# ============================================================

def main():

    print()
    print("========================================")
    print(" GENIE AUDIO SERVER")
    print("========================================")
    print()

    # --------------------------------------------------------
    # TCP server
    # --------------------------------------------------------

    server = socket.socket(
        socket.AF_INET,
        socket.SOCK_STREAM
    )

    server.setsockopt(
        socket.SOL_SOCKET,
        socket.SO_REUSEADDR,
        1
    )

    server.bind(
        (
            HOST,
            PORT
        )
    )

    server.listen(1)

    print(
        f"Audio Server listening on TCP port {PORT}"
    )

    print(
        "Stereo input:"
    )

    print(
        "  LEFT  = Mic A"
    )

    print(
        "  RIGHT = Mic B"
    )

    print()

    # --------------------------------------------------------
    # UDP discovery
    # --------------------------------------------------------

    threading.Thread(
        target=discovery_broadcaster,
        daemon=True
    ).start()

    threading.Thread(
        target=followup_listener,
        daemon=True
    ).start()

    print(
        f"Broadcasting discovery announcements "
        f"on UDP port {DISCOVERY_PORT}"
    )

    print()

    # --------------------------------------------------------
    # Accept ESP32 connection
    # --------------------------------------------------------

    _accept_loop(
        server
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    main()