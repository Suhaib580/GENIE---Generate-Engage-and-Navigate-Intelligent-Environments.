"""
Standalone transcription + LLM + TTS orchestrator, and playback server.

Lives in Services/ alongside the other service modules, but imports
config.py from the project root -- the sys.path line below handles that.

Run this in its own terminal, separate from AudioServer.py, from the
GENIE project root:

    python Services/whisper_service.py

Responsibilities:
  1. Watches recordings/ for new .wav files, transcribes with Whisper.
  2. Sends the transcript to the local LLM (or AIML/IoT, or a status
     query, or the emergency trigger -- see process_transcript below).
  3. Runs a small TCP server (PLAYBACK_PORT) that the ESP32 connects to
     separately from the mic-streaming connection. Over that connection
     it sends a simple opcode protocol: THINKING (blink purple),
     SPEAK_START (solid purple), AUDIO_CHUNK (streamed TTS audio),
     SPEAK_END (back to idle). See config.py for the opcode values --
     the ESP MicroPython script must match them exactly.
"""

import sys
import os

# Add project root (one level up from Services/) to sys.path so
# "from config import ..." works no matter where this is run from.
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
import re
import shutil
import socket
import threading
import requests
from faster_whisper import WhisperModel

from config import (
    HOST,
    OUTPUT_DIR,
    PLAYBACK_PORT,
    TTS_TAIL_MARGIN,
    TTS_NETWORK_CHUNK_SIZE,
    OP_THINKING,
    OP_SPEAK_START,
    OP_AUDIO_CHUNK,
    OP_SPEAK_END,
    IOT_API_URL,
    EMERGENCY_PORT,
    EMERGENCY_VOICE_MESSAGE,
    EV_ANSWERED,
    SMS_IN,
    SMS_OUT,
    SMS_COMMAND_ALLOWED,
    SMS_REPLY_MAX_CHARS,
    EMERGENCY_ANNOUNCE_DELAY,
    EMERGENCY_ANNOUNCEMENT,
    EMERGENCY_ANNOUNCE_REPEAT,
    CONTROL_PORT,
    FOLLOWUP_PORT,
    FOLLOWUP_WINDOW,
    REACTION_MUTE_MAX_MS,
    REACTION_SETTLE_MS,
    WHISPER_LANGUAGE,
    WHISPER_REJECT_OTHER_LANGUAGES,
    WHISPER_LANG_CONFIDENCE,
    NO_SPEECH_PROB_MAX,
    AVG_LOGPROB_MIN,
    UNCLEAR_SPEECH_REPLY,
    FACE_THINKING,
    FACE_SPEAKING,
    FACE_ALARM,
)
from llm_service import LLMService
from tts_service import TTSService
from ai import IntentClassifier
from genie_serial import GenieNode
from modem import numbers_match
import sensor_watch
import reaction_watch
import climate_watch
import node_bridge

POLL_INTERVAL = 0.5      # seconds between folder scans
STABLE_CHECKS = 2        # file size must be unchanged this many polls before reading
PROCESSED_DIR = os.path.join(OUTPUT_DIR, "processed")

# Matches AudioServer.py's dual-mic filenames: recording_<ts>_A.wav /
# recording_<ts>_B.wav, sharing the same <ts> per utterance.
RECORDING_RE = re.compile(r"^recording_(\d+)_([AB])\.wav$")

# How long to wait (real seconds, not poll counts) for a matching "_B"
# file after "_A" shows up stable, before giving up and processing Mic
# A alone. Mic B's file should appear within roughly the same recv()
# cycle if it's connected at all -- this just covers minor scheduling
# jitter between the two AudioServer.py threads, not a real wait for a
# slow connection. If Mic B was simply never wired up, this is the
# grace period before falling back to single-mic behavior each time.
PAIR_GRACE_SECONDS = 2.0

# Holds ALL currently connected ESP playback boards, so audio and LED
# commands can be broadcast to more than one speaker at once.
playback_conns = []
playback_lock = threading.Lock()


def playback_server():
    """
    Runs in a background thread. Accepts playback connections from any
    number of ESP speaker boards and keeps them all in playback_conns.
    Each one independently receives every broadcasted command.
    """
    global playback_conns

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PLAYBACK_PORT))
    server.listen(4)

    print(f"Playback server listening on {PLAYBACK_PORT}")

    while True:
        conn, addr = server.accept()
        print("ESP playback connection established:", addr)

        # A NEW CONNECTION FROM A HOST RETIRES ITS OLD ONES.
        #
        # A speaker board holds exactly one playback session. If the same
        # address connects again it has reset, and the previous socket is
        # dead -- but dead in the worst way: Windows accepts writes to a
        # half-closed socket for some time without raising, so sendall()
        # succeeds, "Sent 22 audio chunks" prints, delivery counts as
        # successful, and the audio goes into a buffer nobody is reading.
        #
        # Observed: three connections from 10.187.53.222 accumulating over
        # one session, then replies that reported delivery and produced
        # silence. Nothing in the logs said anything was wrong, because
        # from the laptop's side nothing was.
        #
        # Matched on HOST, not host:port -- the port changes on every
        # reconnect, which is precisely what makes the stale ones
        # invisible. One speaker per address is the assumption, and it is
        # the actual deployment; two speakers on one host would need a
        # board identifier in the protocol instead.
        stale = []
        with playback_lock:
            for old in list(playback_conns):
                try:
                    if old.getpeername()[0] == addr[0]:
                        stale.append(old)
                        playback_conns.remove(old)
                except OSError:
                    # Already dead enough to have no peer. Drop it too.
                    stale.append(old)
                    if old in playback_conns:
                        playback_conns.remove(old)
            playback_conns.append(conn)

        for old in stale:
            try:
                old.close()
            except OSError:
                pass
        if stale:
            print("  retired %d stale playback connection(s) from %s"
                  % (len(stale), addr[0]))


def _broadcast(send_fn):
    """Runs send_fn(conn) against every connected board. Any board that
    fails (disconnected, reset, etc.) is dropped from the list rather
    than taking the others down with it. Returns how many boards the
    send actually succeeded on -- callers need this to tell real
    delivery apart from a broadcast that quietly reached nobody because
    playback_conns had gone empty (every tracked connection was already
    dead, e.g. from a WinError 10053 mid-stream) or had never been
    populated (no speaker has connected yet)."""
    global playback_conns

    with playback_lock:
        conns = list(playback_conns)

    still_alive = []
    for conn in conns:
        try:
            send_fn(conn)
            still_alive.append(conn)
        except OSError as e:
            print(f"Dropping a playback connection (send failed): {e}")

    with playback_lock:
        playback_conns = still_alive

    return len(still_alive)


def _broadcast_to(conns, send_fn):
    """Like _broadcast, but against an explicit, already-decided list of
    connections instead of re-reading the live global playback_conns.

    This exists specifically for speak(): playback_server()'s accept
    loop appends new ESP connections to playback_conns from a SEPARATE
    thread, with no coordination against an in-progress utterance. If
    speak() kept reading the live global list on every chunk (as
    _broadcast does), a connection that reconnects mid-utterance --
    exactly what a dropped WinError 10053/10054 followed by a fresh
    accept looks like -- would start receiving OP_AUDIO_CHUNK bytes it
    was never sent OP_SPEAK_START for. The ESP has no header telling it
    what those orphaned bytes are; speaker_esp.py's opcode loop silently
    ignores anything it doesn't recognize. Net effect: chunks genuinely
    reach a live socket (so delivery reports succeed) but never produce
    sound, because the receiving connection was never told a speech
    sequence had started.

    Any connection that fails here is pruned from the GLOBAL
    playback_conns too, not just the local snapshot -- a dead connection
    found mid-utterance should stay forgotten for the next one as well.

    Returns the still-alive subset of `conns`, for the caller to keep
    using for the rest of this same utterance."""
    global playback_conns

    still_alive = []
    failed = []
    for conn in conns:
        try:
            send_fn(conn)
            still_alive.append(conn)
        except OSError as e:
            print(f"Dropping a playback connection (send failed): {e}")
            failed.append(conn)

    if failed:
        with playback_lock:
            failed_set = set(id(c) for c in failed)
            playback_conns = [c for c in playback_conns if id(c) not in failed_set]

    return still_alive


def send_opcode(op):
    """Broadcasts a single-byte opcode with no payload to every
    connected speaker board. Returns how many boards actually received
    it -- 0 means nothing is connected right now, which callers should
    treat as a real failure, not silently proceed past."""
    return _broadcast(lambda conn: conn.sendall(bytes([op])))


def send_audio_chunk(data):
    """Broadcasts OP_AUDIO_CHUNK + 4-byte big-endian length + raw PCM
    bytes to every connected speaker board. Returns how many boards
    actually received this specific chunk."""
    header = bytes([OP_AUDIO_CHUNK]) + len(data).to_bytes(4, "big")
    return _broadcast(lambda conn: conn.sendall(header + data))


def control_server():
    """Accepts the AudioServer.py control link and relays its bytes to the
    speaker board(s) as opcodes.

    AudioServer owns the wake/VAD state machine, so it's the only process
    that knows the instant the mic starts and stops capturing a command.
    But the speaker board -- the thing that lights up and beeps -- is
    connected HERE, to the playback server. So the "listening" cues
    (OP_LISTEN_START / OP_LISTEN_END = orange LED + beeps) are relayed:
    AudioServer sends the raw opcode byte over CONTROL_PORT, and each byte
    is forwarded as-is via send_opcode() to every connected speaker.

    One client (AudioServer) at a time; a reconnect just replaces the old
    link. If AudioServer isn't connected, nothing happens -- the cues are
    best-effort and never affect anything else here."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, CONTROL_PORT))
    server.listen(1)

    print(f"Control server listening on {CONTROL_PORT} (AudioServer cue link)")

    while True:
        conn, addr = server.accept()
        print("AudioServer control link connected:", addr)
        try:
            while True:
                b = conn.recv(1)
                if not b:
                    break
                # Relay the raw opcode straight through to the speaker(s).
                send_opcode(b[0])
        except OSError as e:
            print(f"Control link dropped: {e}")
        finally:
            try:
                conn.close()
            except OSError:
                pass
            print("AudioServer control link closed.")


# Holds the mic ESP's persistent emergency-trigger connection. Single
# board, single connection -- much simpler than the speaker's
# broadcast-to-many design.
emergency_conn = None
emergency_lock = threading.Lock()


# What most recently triggered an alert, so the spoken announcement can
# name it. Set when the trigger goes out, read when the board reports
# that somebody answered -- which may be a minute later, after the SMS
# and the acknowledgement window.
last_alert_text = ""


def emergency_server():
    """
    Runs in a background thread. Accepts the modem ESP's persistent
    connection and spawns a reader for it. If it reconnects (reset,
    Wi-Fi blip), the new connection simply replaces the old one.
    """
    global emergency_conn

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, EMERGENCY_PORT))
    server.listen(1)

    print(f"Emergency server listening on {EMERGENCY_PORT}")

    while True:
        conn, addr = server.accept()
        print("Modem ESP connection established:", addr)

        with emergency_lock:
            emergency_conn = conn

        threading.Thread(target=emergency_reader, args=(conn,),
                         daemon=True).start()


def _read_exactly(conn, count):
    """Read exactly count bytes from a blocking socket, or None if the
    peer went away. Mirrors modem_main.py's helper of the same purpose --
    this side can afford to block, since emergency_reader() runs in its
    own daemon thread per connection rather than sharing a single-threaded
    loop with everything else the board does."""
    buf = b""
    while len(buf) < count:
        chunk = conn.recv(count - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _read_sms_in_frame(conn):
    """Body of an SMS_IN frame, after its opcode byte has already been
    read. Wire format matches modem_main.py's FrameReader:

        <sender_len:1> <sender>  <text_len:1> <text utf-8>

    Returns (sender, text), or None if the connection dropped mid-frame.
    """
    sender_len = _read_exactly(conn, 1)
    if sender_len is None:
        return None
    sender = _read_exactly(conn, sender_len[0])
    if sender is None:
        return None
    text_len = _read_exactly(conn, 1)
    if text_len is None:
        return None
    text = _read_exactly(conn, text_len[0])
    if text is None:
        return None
    try:
        return sender.decode("utf-8"), text.decode("utf-8")
    except Exception:
        return None


def emergency_reader(conn):
    """
    Listens for events coming back FROM the modem board.

    This link used to be one-way and carry a single opcode. It now carries
    two: EV_ANSWERED, because the board is the only thing that knows the
    instant a call is answered, and SMS_IN, because the board is also the
    only thing that knows an SMS just arrived on the SIM that was NOT a
    reply inside an emergency ack window.

    SMS_IN is handed to its own thread rather than handled inline, because
    building its reply can mean a Mistral call taking several seconds (see
    llm_service.py), and this loop must not be blocked from noticing the
    NEXT byte on this connection -- in particular EV_ANSWERED, which can
    legitimately arrive while an unrelated text is still being answered.

    Anything else is logged and ignored rather than treated as an error --
    an unrecognised byte from the board must never take down the link that
    carries emergency triggers.
    """
    global emergency_conn

    try:
        while True:
            data = conn.recv(1)
            if not data:
                print("Modem ESP closed the connection.")
                break

            opcode = data[0]

            if opcode == EV_ANSWERED:
                announce_on_call()

            elif opcode == SMS_IN:
                frame = _read_sms_in_frame(conn)
                if frame is None:
                    print("Modem ESP: incomplete SMS_IN frame - link is "
                          "probably gone, closing it.")
                    break
                sender, text = frame
                threading.Thread(target=handle_incoming_sms,
                                 args=(sender, text), daemon=True).start()

            else:
                print(f"Modem ESP sent unknown opcode {opcode:#04x} - ignoring.")

    except OSError as e:
        print(f"Modem ESP link dropped: {e}")

    finally:
        with emergency_lock:
            if emergency_conn is conn:
                emergency_conn = None
        try:
            conn.close()
        except OSError:
            pass


def announce_on_call():
    """Announce IN THE ROOM that a call has been answered.

    Not into the call. The modem's MIC pads tore off the carrier, so it
    has no microphone and the call carries silence -- the person who
    answered hears nothing. This is for whoever is physically present:
    during a fire, "help has been called" is worth saying out loud.

    If the pads are ever repaired, putting GENIE's speaker beside a
    re-soldered electret makes this reach the call too, with no code
    change. That was the design; it just never got tested in time.
    """
    alert = last_alert_text or "An emergency was detected."
    text = EMERGENCY_ANNOUNCEMENT.format(alert=alert)

    print(f"\nCALL ANSWERED -- announcing in {EMERGENCY_ANNOUNCE_DELAY:.1f}s\n")
    time.sleep(EMERGENCY_ANNOUNCE_DELAY)

    for i in range(EMERGENCY_ANNOUNCE_REPEAT):
        print(f"  announcing ({i + 1}/{EMERGENCY_ANNOUNCE_REPEAT})")
        speak(text)


def send_emergency_trigger(message=None):
    """Tells the modem ESP32 to run the SMS-then-call escalation.

    Wire format, deliberately fixed-length-prefixed rather than
    newline-delimited so the board never has to guess with a timeout:

        0x01  <len:1>  <message utf-8, len bytes>

    len 0 means "use your built-in default message", which is what a
    spoken trigger sends -- the operator said "this is an emergency"
    and there is nothing more specific to add. A sensor alert passes
    the sensor-specific wording instead, so the SMS says *smoke* rather
    than a generic plea.

    The message is capped at 255 bytes because that is all one length
    byte holds, and an SMS is 160 characters anyway.
    """
    global emergency_conn

    with emergency_lock:
        conn = emergency_conn

    if conn is None:
        print("EMERGENCY TRIGGER FAILED: modem ESP not connected!")
        return False

    # Remember what this alert is about. The board reports back when
    # somebody answers -- possibly a minute later, after the SMS and the
    # acknowledgement window -- and announce_on_call() needs the wording
    # then, to say "smoke detected" rather than something generic.
    global last_alert_text
    last_alert_text = message or "An emergency was detected."

    body = (message or "").encode("utf-8")[:255]
    packet = b"\x01" + bytes([len(body)]) + body

    try:
        conn.sendall(packet)
        print(f"Emergency trigger sent to modem ESP ({len(body)} byte message).")
        return True
    except OSError as e:
        print(f"EMERGENCY TRIGGER FAILED: {e}")
        with emergency_lock:
            emergency_conn = None
        return False


def send_sms_reply(number, text):
    """Tells the modem ESP32 to send exactly this text to exactly this
    number. No escalation, no ack window, no call -- that machinery is
    send_emergency_trigger()'s alone. This is the plain reply to a command
    someone texted in: they asked a question, this is the answer.

        0x04  <number_len:1> <number>  <text_len:1> <text utf-8>

    Same 255-byte-per-field cap as the trigger, and for the same reason --
    one length byte, and an SMS is 160 characters anyway. SMS_REPLY_MAX_CHARS
    trims the text well before that limit so a long sensor summary is
    shortened on purpose rather than silently truncated mid-word at 255.
    """
    global emergency_conn

    with emergency_lock:
        conn = emergency_conn

    if conn is None:
        print(f"SMS REPLY FAILED: modem ESP not connected (reply to {number}).")
        return False

    number_b = (number or "").encode("utf-8")[:255]

    # Trim by CHARACTERS first, then guard the byte cap on a character
    # boundary. Slicing encoded bytes at 255 can land mid-sequence and hand
    # the board something it cannot decode; MicroPython's decode would either
    # raise or drop the frame, and the reply would vanish with no error here.
    text_b = (text or "")[:SMS_REPLY_MAX_CHARS].encode("utf-8")
    while len(text_b) > 255:
        text = text[:len(text) - 1]
        text_b = text.encode("utf-8")
    packet = (bytes([SMS_OUT])
              + bytes([len(number_b)]) + number_b
              + bytes([len(text_b)]) + text_b)

    try:
        conn.sendall(packet)
        print(f"SMS reply sent to modem ESP for {number} ({len(text_b)} bytes).")
        return True
    except OSError as e:
        print(f"SMS REPLY FAILED: {e}")
        with emergency_lock:
            emergency_conn = None
        return False


def on_sensor_alert(sensor, message):
    """Called by the sensor watcher when a reading crosses into alarm.

    Speaks first, then triggers. The order matters: anyone in the room
    should hear what is happening before the message goes out, and if the
    modem board is unreachable that announcement is the only warning they
    get.

    SMS ONLY. esp32/settings.py has CALLS_ENABLED = False, so
    emergency_alert() texts every contact and stops -- voice calls need
    the 2G fallback that browns the module out. The spoken line said
    "calling for help" regardless, which promised something the hardware
    was never going to do.
    """
    print(f"\nSENSOR EMERGENCY: {sensor} -- texting the emergency contact.\n")

    # Node 1 forces the alarm face for its own local alarms regardless of
    # what the laptop asks for. This is for the cases it cannot see: a
    # fall relayed from Node 2's ToF array, or a reading that came from
    # the other sensor ESP32 entirely.
    node_bridge.set_emotion(FACE_ALARM)

    speak(f"{sensor.capitalize()} detected. Texting for help now.",
          emotion=FACE_ALARM)

    if not send_emergency_trigger(message):
        speak("I could not reach the emergency module. Please call for help yourself.")


climate_watcher = None          # set at startup; None until then


def on_climate_change(device, action, words):
    """The room's readings want something. Act, or just say so.

    Acting speaks AFTER the relay is commanded, unlike the emergency path.
    The order is reversed deliberately: an emergency announcement matters
    even when the modem is unreachable, but announcing a fan that failed
    to switch would simply be false.

    A SUGGESTION TOUCHES NOTHING. GENIE says the room is warm and stops.
    An earlier build asked "shall I turn the fan on?" and held a pending
    yes/no to catch the answer -- a whole state machine, and one that
    could go stale between the question and the reply. Handing the
    decision back entirely is better on every count: "turn on the fan"
    arrives through the command path that already exists and already
    works, nothing is held open, and a suggestion nobody acts on costs
    exactly nothing.
    """
    if action == "suggest":
        print("  [climate] suggesting: %s" % words)
        speak(words)
        return

    want_on = bool(action)
    send_iot_command("ON" if want_on else "OFF", device)
    print("  [climate] %s %s -- %s"
          % (device, "ON" if want_on else "OFF", words))
    speak(words)


def on_handling_reaction(event, entry):
    """The board says it was shoved or picked up. Remark on it, or don't.

    Returns False when GENIE is mid-utterance so reaction_watch drops the
    event rather than queueing it. Returns False too when sensor.aiml has
    no line for this event name -- a board emitting something this build
    does not know about must produce silence, not an empty utterance.
    """
    line = intent_classifier.build_reaction_reply(event)
    if not line:
        return False

    # "concerned" for being handled, back to neutral once it is down. The
    # node forgets a laptop-set expression after 20 s and an alarming
    # sensor overrides it outright, so this can never leave a worried face
    # sitting there during a gas leak.
    face = FACE_SPEAKING if event == "set_down" else "concerned"

    if not speak_if_free(line, emotion=face):
        return False

    print("  [reaction] %s -> %s" % (event, line))
    return True


def send_iot_command(command, device):
    """POSTs the matched command to api.py, which tracks state and
    pushes it on to the relay ESP32. Timeout is intentionally longer
    than api.py's own inner timeout to the relay ESP (5s) -- otherwise
    this request can give up before api.py even finishes its attempt,
    even on a call that would have succeeded a moment later."""
    try:
        response = requests.post(
            f"{IOT_API_URL}/relay",
            json={"command": command, "device": device},
            timeout=10,
        )
        print(f"IoT API response: {response.json()}")
    except Exception as e:
        print(f"IoT API request failed: {e}")


def get_iot_status():
    """GETs the real current relay states from api.py. Returns
    {"light": 0/1, "fan": 0/1} or None on failure -- including if the
    response is missing the expected keys (e.g. api.py running an old
    version without the /status endpoint would return a 404 body like
    {"detail": "Not Found"}, which is still valid JSON but has neither
    key -- checked explicitly so that fails loudly instead of silently
    reporting "off" for everything)."""
    try:
        response = requests.get(f"{IOT_API_URL}/status", timeout=5)
        data = response.json()

        if "light" not in data or "fan" not in data:
            print(f"IoT status response missing expected keys: {data}")
            return None

        return data
    except Exception as e:
        print(f"IoT status request failed: {e}")
        return None


# Fetched for a "check all sensors" request. DISTANCE is in; KNOCK is
# deliberately not -- "a tap happened three seconds ago" is not part of a
# room status report, and sensor.aiml has no category for it.
ALL_SENSOR_NAMES = (
    "SMOKE", "FLAME", "IR", "MOTION", "PRESENCE", "FALL",
    "TEMPERATURE", "HUMIDITY", "DISTANCE", "TILT", "ACCEL",
)


def get_sensor_data(sensor):
    """POSTs to api.py's /sensor endpoint for ONE sensor's latest stored
    reading (api.py doesn't contact the sensor ESP32 live -- it just
    returns whatever that board most recently pushed to /update_sensor).
    Returns the data dict (e.g. {"status": "true"}) or None on failure."""
    try:
        response = requests.post(
            f"{IOT_API_URL}/sensor",
            json={"sensor": sensor},
            timeout=5,
        )
        data = response.json()

        if "error" in data:
            print(f"Sensor request for {sensor} failed: {data}")
            return None

        return data
    except Exception as e:
        print(f"Sensor request for {sensor} failed: {e}")
        return None


def get_all_sensor_data():
    """Fetches every sensor's latest reading for a 'check all sensors'
    request. Returns a dict of SENSOR_NAME -> data, skipping (not
    crashing on) any single sensor whose fetch fails -- so one flaky
    reading doesn't block the other six from being reported."""
    all_data = {}
    for sensor in ALL_SENSOR_NAMES:
        data = get_sensor_data(sensor)
        if data is not None:
            all_data[sensor] = data
    return all_data


def build_status_reply(device, status):
    """Builds a short spoken status line from api.py's real state,
    for whichever device (or ALL) was asked about."""
    light_word = "on" if status.get("light") else "off"
    fan_word = "on" if status.get("fan") else "off"

    if device == "LIGHT":
        return f"The light is {light_word}."
    elif device == "FAN":
        return f"The fan is {fan_word}."
    else:
        return f"The light is {light_word} and the fan is {fan_word}."


# ------------------------------------------------------------------
# Reverse cue link -> AudioServer
# ------------------------------------------------------------------
# AudioServer needs to know the instant a reply STOPS playing so it can
# open its follow-up window then, and not a moment earlier. Opening it
# when the recording ended would leave the mic live while the speaker is
# still talking, and Genie would transcribe its own reply and answer
# itself -- one wasted Mistral cycle per loop.
#
# One long-lived connection is reused across replies and lazily
# reopened if it drops. Everything here is best-effort: if AudioServer
# is down or was started without the listener, speak() must behave
# exactly as it always has.
_followup_conn = None

# When Genie last finished speaking. Used for two things: telling
# AudioServer to open its follow-up window, and -- locally -- knowing
# whether the clip we are about to judge arrived INSIDE that window.
_last_spoke_at = 0.0


# ------------------------------------------------------------------
# Sessions
# ------------------------------------------------------------------
# A session is one wake word plus every follow-up that came after it --
# the same unit the user experiences as "a conversation".
#
# Nothing has to be sent from AudioServer to define it. A recording that
# arrives INSIDE a follow-up window continues the current session; one
# that arrives outside it can only have come from a fresh wake word, and
# so starts a new session. Both processes already apply the same
# FOLLOWUP_WINDOW to the same event, so they agree without a protocol.
_session_id = None
_session_seq = 0

# Session ids are second-resolution timestamps, so two sessions starting
# inside the same second would collide and merge their audio and
# transcripts into one folder. Unlikely in use -- it needs a wake word
# within a second of the previous conversation ending -- but silent data
# loss is not worth leaving to luck, so a counter makes it impossible.
_session_count = 0


def session_dir():
    """Folder holding this session's audio and its transcript."""
    if _session_id is None:
        return PROCESSED_DIR
    return os.path.join(PROCESSED_DIR, f"session_{_session_id}")


def begin_or_continue_session():
    """Decide whether the recording about to be processed continues the
    current conversation or starts a new one."""
    global _session_id, _session_seq, _session_count

    if _session_id is not None and in_followup_window():
        _session_seq += 1
        print(f"[session {_session_id}] turn {_session_seq} (follow-up)")
        return

    _session_count += 1
    _session_id = f"{time.strftime('%Y%m%d_%H%M%S')}_{_session_count:03d}"
    _session_seq = 1
    os.makedirs(session_dir(), exist_ok=True)
    print(f"[session {_session_id}] started")


def session_log(role, text):
    """Append one line to the current session's transcript.

    Best-effort: a logging failure must never stop GENIE answering, so
    every error here is swallowed."""
    if _session_id is None:
        return
    try:
        os.makedirs(session_dir(), exist_ok=True)
        with open(os.path.join(session_dir(), "transcript.md"),
                     "a", encoding="utf-8") as fh:
            fh.write(f"**{role}** ({time.strftime('%H:%M:%S')}) {text}\n\n")
    except OSError:
        pass


def in_followup_window():
    """Did this recording arrive while the follow-up window was open?

    whisper_service is the process that ends every reply, so it can
    answer this itself -- no extra flag has to travel with the WAV and
    no filename convention has to change. Both processes apply the same
    FOLLOWUP_WINDOW to the same event, so they agree by construction."""
    return _last_spoke_at > 0 and (time.time() - _last_spoke_at) <= FOLLOWUP_WINDOW


def notify_speaking_start():
    """Tell AudioServer to stop listening -- a reply is about to play.

    Sent BEFORE the first audio chunk. The mic must be deaf for the
    whole utterance, not merely after it: the wake word is live the
    entire time Genie is talking, and it can fire on Genie."""
    _send_followup(OP_SPEAK_START)


def notify_speech_done():
    """Tell AudioServer that Genie has just finished speaking."""
    global _last_spoke_at

    # Shorten the board's handling mute from the generous ceiling set at
    # speech start down to just the settle. The enclosure is still ringing
    # for a moment after the sound stops, and the feature window is 2.5 s
    # of real time, so a short tail is needed -- but only a short one, or
    # putting the unit down right after a reply goes unacknowledged.
    node_bridge.mute_handling(REACTION_SETTLE_MS)

    _last_spoke_at = time.time()
    _send_followup(OP_SPEAK_END)


def _send_followup(op):
    global _followup_conn

    for attempt in (1, 2):
        try:
            if _followup_conn is None:
                _followup_conn = socket.create_connection(
                    ("127.0.0.1", FOLLOWUP_PORT), timeout=0.5
                )
            _followup_conn.sendall(bytes([op]))
            return
        except OSError:
            # Stale socket from a previous AudioServer run: drop it and
            # retry once with a fresh connection.
            try:
                if _followup_conn is not None:
                    _followup_conn.close()
            except OSError:
                pass
            _followup_conn = None
            if attempt == 2:
                print("[followup] AudioServer not reachable -- "
                      "no follow-up window for this reply.")


# One utterance at a time, whoever is asking.
#
# speak() is already called from three threads -- the transcript path, the
# sensor_watch alert thread, and now reaction_watch -- and before this lock
# two of them could interleave: two OP_SPEAK_STARTs, two streams of chunks
# into the same socket, one garbled reply. It also gives reactions a way to
# ASK whether GENIE is free without waiting, which is what makes a refused
# reaction a drop instead of a queue.
#
# RLock, not Lock: speak() must stay safe to call from inside a callback
# that already holds it.
_speech_lock = threading.RLock()


def speak(text, emotion=None):
    """Speak, waiting for any utterance already in progress to finish."""
    with _speech_lock:
        return _speak_now(text, emotion)


def speak_if_free(text, emotion=None):
    """Speak only if GENIE is not already talking. Returns False if busy.

    For unprompted remarks. A reaction that waits its turn is no longer a
    reaction -- by the time the queue clears, the shove it was about is
    eight seconds in the past and the room has moved on. Dropping is the
    honest behaviour.
    """
    if not _speech_lock.acquire(blocking=False):
        return False
    try:
        _speak_now(text, emotion)
        return True
    finally:
        _speech_lock.release()


def _speak_now(text, emotion=None):
    """Synthesizes text with Piper and streams it to the ESP chunk by
    chunk as it's generated, rather than waiting for the whole utterance.
    Each Piper chunk is further split into TTS_NETWORK_CHUNK_SIZE-sized
    pieces so the ESP never has to allocate one big buffer at once.

    Delivery is tracked, not assumed. Previously chunk_count just counted
    how many times send_audio_chunk() was CALLED, regardless of whether
    _broadcast() reached zero connections -- so "Sent 115 audio chunks to
    ESP." could print even when every single one silently went nowhere.
    That's fixed. But a second, subtler bug remained even after that fix:
    _broadcast() always re-reads the LIVE playback_conns, and
    playback_server()'s accept loop appends new connections to that same
    list from a different thread with no coordination against an
    in-progress utterance. If the ESP dropped and reconnected mid-reply
    (a WinError 10053/10054 followed by a fresh accept -- which happens),
    the new connection would start receiving OP_AUDIO_CHUNK bytes it was
    never sent OP_SPEAK_START for. speaker_esp.py's opcode loop silently
    ignores bytes it doesn't recognize as a valid opcode, so those chunks
    would reach a live socket (delivery reports "success") and still
    produce no sound.

    Fixed by snapshotting the connection list ONCE, right when
    OP_SPEAK_START goes out, and using only that snapshot for the rest of
    this utterance. A connection that joins mid-utterance is simply not
    part of it -- it'll be included starting from the next speak() call,
    with its own fresh OP_SPEAK_START."""

    session_log("GENIE", text)

    # Set the face BEFORE the first audio chunk goes out, so the
    # expression and the voice arrive together. Node 1 forgets a
    # laptop-set emotion after 20 s and falls back to sensor-driven --
    # that expiry is deliberate (if this process dies, GENIE must not sit
    # there with a happy face during a gas leak), so this re-sends every
    # turn rather than trying to hold one. Costs one JSON line over
    # serial; returns False and moves on if Node 1 is not connected.
    # PERCEIVED vs EXHIBITED. set_emotion returns whether the board
    # actually took it, and that answer used to be discarded -- so a face
    # that never changed looked exactly like one that did. The two failures
    # behind it are different and need telling apart: the model not tagging
    # the reply (nothing to show), versus the expression being read
    # correctly and never reaching the eyes (no board, or the command
    # channel down).
    face = emotion or FACE_SPEAKING
    shown = node_bridge.set_emotion(face)
    print("  [face] %s%s -> %s"
          % (face,
             "" if emotion else " (default, reply was not tagged)",
             "shown on the eyes" if shown
             else "NOT DELIVERED - no board on the command channel"))

    # Deafen the board's handling classifier for the whole utterance. The
    # speaker shares an enclosure with the IMU, so GENIE's own voice reads
    # as being handled -- it reacted, which made it talk, which shook it
    # again, and an untouched unit talked continuously.
    #
    # A generous ceiling here because the true length is not known until
    # Piper has finished generating; notify_speech_done() shortens it to
    # the settle time the moment playback actually ends, so the mute is
    # never longer than it needs to be.
    node_bridge.mute_handling(REACTION_MUTE_MAX_MS)

    notify_speaking_start()

    # EVERYTHING from here to notify_speech_done() runs inside try/finally.
    #
    # notify_speaking_start() latches _speaking in AudioServer, and that flag
    # gates the ENTIRE mic path - wake word, VAD and pre-roll are all skipped
    # while it is set. If anything between here and the end raises (Piper
    # failing on an odd string, a socket error that is not OSError), the
    # "done" signal is never sent and the microphone stays deaf until
    # AudioServer's 90-second failsafe releases it.
    #
    # A silent wake word is a much worse failure than a lost reply, so the
    # release is guaranteed rather than merely likely.
    try:
        _stream_utterance(text)
    finally:
        notify_speech_done()


def _stream_utterance(text):
    """Synthesize and stream one utterance. Caller owns the speaking flag."""
    with playback_lock:
        utterance_conns = list(playback_conns)

    utterance_conns = _broadcast_to(
        utterance_conns, lambda conn: conn.sendall(bytes([OP_SPEAK_START]))
    )
    delivered_any = len(utterance_conns) > 0

    chunk_count = 0
    delivered_count = 0
    warned_zero = False

    # Bytes of PCM actually handed to the speaker, and when streaming
    # began. Together these give the real playback duration -- see the
    # wait below OP_SPEAK_END.
    pcm_bytes = 0
    stream_start = time.time()

    for piper_chunk in tts.synthesize_chunks(text):
        for i in range(0, len(piper_chunk), TTS_NETWORK_CHUNK_SIZE):
            piece = piper_chunk[i:i + TTS_NETWORK_CHUNK_SIZE]
            header = bytes([OP_AUDIO_CHUNK]) + len(piece).to_bytes(4, "big")
            utterance_conns = _broadcast_to(
                utterance_conns, lambda conn, d=header + piece: conn.sendall(d)
            )
            chunk_count += 1
            pcm_bytes += len(piece)
            if utterance_conns:
                delivered_count += 1
                delivered_any = True
            elif not warned_zero:
                print("WARNING: this utterance's speaker connection(s) "
                      "dropped mid-stream -- remaining audio is not "
                      "reaching any ESP (a NEW connection, if one forms, "
                      "will be used starting from the next reply, not "
                      "this one).")
                warned_zero = True

    _broadcast_to(utterance_conns, lambda conn: conn.sendall(bytes([OP_SPEAK_END])))

    if delivered_count == 0:
        print(f"Sent {chunk_count} audio chunks, but NONE reached a "
              f"connected speaker -- nothing was actually played.")
    elif delivered_count < chunk_count:
        print(f"Sent {chunk_count} audio chunks, but only "
              f"{delivered_count} reached a speaker -- audio likely "
              f"glitched or cut out mid-reply.")
    else:
        print(f"Sent {chunk_count} audio chunks to ESP.")

    # ----------------------------------------------------------------
    # Wait for the audio to actually FINISH PLAYING.
    # ----------------------------------------------------------------
    # send() returning means the bytes are in the network stack, not
    # that they have come out of the speaker. The ESP buffers what it
    # receives and plays it at its own rate, so the tail of a reply is
    # still audible well after the last chunk was streamed.
    #
    # Signalling "done" here -- as this code used to -- opens the
    # follow-up window while Genie is still talking. The mic goes live,
    # hears the rest of the reply, transcribes it as a new command and
    # answers it: a conversation with itself, one full Mistral
    # generation per turn.
    #
    # Piper emits 16-bit mono PCM, so the true duration is simply
    # bytes / (sample_rate * 2). Subtract the time streaming already
    # took, add a margin for ESP-side buffering, and wait out whatever
    # is left.
    if delivered_count > 0 and pcm_bytes > 0:
        play_seconds = pcm_bytes / float(tts.sample_rate * 2)
        already_elapsed = time.time() - stream_start
        remaining = play_seconds - already_elapsed + TTS_TAIL_MARGIN

        if remaining > 0:
            print(f"Waiting {remaining:.1f}s for playback to finish "
                  f"({play_seconds:.1f}s of audio) before reopening the mic.")
            time.sleep(remaining)

    # Reply is done playing. notify_speech_done() is NOT called here - the
    # caller's finally: owns it, so it fires on the exception paths too.


# Shared decode settings whose only job is to stop Whisper inventing
# fluent sentences out of noise.
#
#   vad_filter                 strips non-speech BEFORE decoding. Whisper
#                              hallucinates most when handed near-silence
#                              or steady noise; the surest fix is to not
#                              hand it any.
#   condition_on_previous_text stops each segment being primed by the
#                              last one. Without it a single hallucinated
#                              line seeds the next, which is exactly the
#                              observed "confident multi-line English from
#                              garbage" failure.
#   compression_ratio_threshold rejects degenerate output -- repetitive
#                              text compresses far better than real
#                              speech, so a high ratio means looping.
#   no_speech_threshold        aligned with the per-segment gate below.
_DECODE = dict(
    vad_filter=True,
    condition_on_previous_text=False,
    compression_ratio_threshold=2.4,
    no_speech_threshold=NO_SPEECH_PROB_MAX,
)


def _transcribe_with_confidence(path):
    """Transcribes one wav file with the same language-forcing and
    per-segment confidence filtering as before. Returns (text,
    mean_confidence, heard_but_unclear, pure_noise). mean_confidence is
    the average avg_logprob across KEPT segments only (None if none were
    kept) -- this is what decides which mic 'heard better' when there
    are two files to compare, per the project's own earlier finding
    that raw volume isn't a reliable stand-in for clarity."""
    # Language gate. Passing language="en" does not ask Whisper to
    # CHECK for English -- it orders it to decode as English no matter
    # what it hears, which is how Urdu conversation became a confident
    # English sentence. So we let it detect first, and only then decide.
    if WHISPER_REJECT_OTHER_LANGUAGES:
        segments, info = model.transcribe(path, language=None, **_DECODE)
        detected = getattr(info, "language", WHISPER_LANGUAGE)
        lang_prob = getattr(info, "language_probability", 0.0) or 0.0

        if detected != WHISPER_LANGUAGE and lang_prob >= WHISPER_LANG_CONFIDENCE:
            # Confidently not English -- discard without decoding.
            print(f"--- {os.path.basename(path)} ---")
            print(f"  [discarded, wrong language -- detected {detected!r} "
                  f"p={lang_prob:.2f}]")
            return "", None, False, True

        if detected != WHISPER_LANGUAGE:
            # Detected something else but is not sure. Far more likely to
            # be accented English than a foreign language, and dropping a
            # real command is the worse error -- so decode as English.
            print(f"  [language uncertain: {detected!r} p={lang_prob:.2f} "
                  f"-- decoding as {WHISPER_LANGUAGE} anyway]")
            segments, info = model.transcribe(path, language=WHISPER_LANGUAGE, **_DECODE)
    else:
        segments, info = model.transcribe(path, language=WHISPER_LANGUAGE, **_DECODE)

    text = ""
    heard_but_unclear = False
    pure_noise = False
    logprobs = []

    print(f"--- {os.path.basename(path)} ---")
    for seg in segments:
        if seg.no_speech_prob > NO_SPEECH_PROB_MAX:
            print(f"  [discarded, not speech -- no_speech_prob={seg.no_speech_prob:.2f}]: {seg.text!r}")
            pure_noise = True
            continue

        if seg.avg_logprob < AVG_LOGPROB_MIN:
            print(f"  [discarded, unclear words -- avg_logprob={seg.avg_logprob:.2f}]: {seg.text!r}")
            heard_but_unclear = True
            continue

        print(" ", seg.text)
        text += seg.text
        logprobs.append(seg.avg_logprob)

    mean_confidence = sum(logprobs) / len(logprobs) if logprobs else None
    return text, mean_confidence, heard_but_unclear, pure_noise


def transcribe_pair_and_print(path_a, path_b=None):
    """path_b is None when Mic B never produced a matching file (not
    connected, or this session is running Mic A alone) -- behaves
    identically to the original single-mic transcribe_and_print in that
    case. When both exist, transcribes both and keeps whichever
    produced usable text with the higher mean confidence."""
    try:
        print("\n========== YOU SAID ==========")

        text_a, conf_a, unclear_a, noise_a = _transcribe_with_confidence(path_a)

        if path_b is None:
            text = text_a
            heard_but_unclear, pure_noise = unclear_a, noise_a
            winner_label = "Mic A (Mic B not connected)"

        else:
            text_b, conf_b, unclear_b, noise_b = _transcribe_with_confidence(path_b)

            if text_a.strip() and text_b.strip():
                if conf_a >= conf_b:
                    text = text_a
                    winner_label = f"Mic A (avg_logprob {conf_a:.2f} vs Mic B {conf_b:.2f})"
                else:
                    text = text_b
                    winner_label = f"Mic B (avg_logprob {conf_b:.2f} vs Mic A {conf_a:.2f})"
                heard_but_unclear, pure_noise = False, False
            elif text_a.strip():
                text = text_a
                winner_label = "Mic A (Mic B produced no usable text)"
                heard_but_unclear, pure_noise = False, False
            elif text_b.strip():
                text = text_b
                winner_label = "Mic B (Mic A produced no usable text)"
                heard_but_unclear, pure_noise = False, False
            else:
                text = ""
                winner_label = "neither mic"
                heard_but_unclear = unclear_a or unclear_b
                pure_noise = noise_a or noise_b

        print("==============================")
        names = os.path.basename(path_a) + (f" + {os.path.basename(path_b)}" if path_b else "")
        print(f"(from {names}, winner: {winner_label})\n")

        if not text.strip():
            if heard_but_unclear and in_followup_window():
                # Inside a follow-up window there was no wake word --
                # the VAD opened this recording on its own. "Speech-like
                # but unintelligible" here is far more likely to be room
                # noise than a person being misheard.
                #
                # Prompting "sorry, say again?" would be worse than
                # useless: speaking re-arms the window, which reopens
                # the mic, which lets the same noise prompt again.
                # Silence lets the window expire and the wake word take
                # over -- the loop cannot start.
                session_log("--", "_discarded: unclear audio inside follow-up window_")
                print("Unclear audio inside the follow-up window -- "
                      "staying silent so the window can close.")

            elif heard_but_unclear:
                print("Sounded like speech but wasn't clear enough -- asking to repeat.\n")
                speak(UNCLEAR_SPEECH_REPLY)
            elif pure_noise:
                session_log("--", "_discarded: no confident speech_")
                print("No confident speech in this clip -- discarded, nothing sent onward.\n")
            return

        session_log("YOU", text.strip())

        process_transcript(text)

    except Exception as e:
        print("Transcription error:", e)


def process_transcript(text):
    """Everything that happens once we have a final transcript,
    regardless of which mic channel it came from: intent
    classification (status query / IoT command / neither), then LLM
    or a direct spoken reply."""

    if not text.strip():
        return

    intent = intent_classifier.classify(text)

    if intent and intent["type"] == "emergency":
        print("EMERGENCY TRIGGERED -- calling and texting now.\n")

        # Say it was a person asking, not a sensor. A responder who gets
        # "someone asked Genie for help" knows there is somebody conscious and
        # speaking in that room; "smoke detected" tells them something quite
        # different. Both are urgent, and which one it is changes what they do.
        sent = send_emergency_trigger(EMERGENCY_VOICE_MESSAGE)

        if sent:
            speak("Emergency triggered. Calling and texting for help now.")
        else:
            speak("I couldn't reach the emergency module. Please call for help yourself.")

    elif intent and intent["type"] == "sensor":
        print(f"Sensor query detected: {intent['sensor']} "
              f"-- reading live data, skipping LLM.\n")

        if intent["sensor"] == "ALL":
            data = get_all_sensor_data()
            reply = intent_classifier.build_all_sensors_reply(data)
        else:
            data = get_sensor_data(intent["sensor"])
            reply = intent_classifier.build_sensor_reply(intent["sensor"], data)

        print(f"Sensor reply: {reply}\n")
        speak(reply)

    elif intent and intent["type"] == "query":
        print(f"Status query detected: {intent['device']} "
              f"-- reading real state, skipping LLM.\n")

        status = get_iot_status()

        if status is not None:
            reply = build_status_reply(intent["device"], status)
        else:
            reply = "I couldn't reach the relay to check that."

        print(f"Status reply: {reply}\n")
        speak(reply)

    elif intent and intent["type"] == "command":
        print(f"IoT command detected: {intent['command']} "
              f"{intent['device']} -- routing directly, skipping LLM.\n")

        # Fire the relay push in the background -- the spoken
        # confirmation doesn't need to wait on that network round
        # trip, since the confirmation text is already known.
        # A person just commanded this relay, so the climate rule stops
        # touching it. Without this, "turn off the fan" on a warm day is
        # undone within ten seconds and the spoken confirmation is a lie.
        if climate_watcher is not None:
            climate_watcher.manual_override(
                intent["device"], intent["command"] == "ON")

        threading.Thread(
            target=send_iot_command,
            args=(intent["command"], intent["device"]),
            daemon=True
        ).start()

        speak(intent["confirmation"])

    else:
        print("Thinking...")
        send_opcode(OP_THINKING)
        node_bridge.set_emotion(FACE_THINKING)

        reply = llm.ask(text)

        # Mistral is asked (in LLM_SYSTEM_PROMPT) to end each reply with
        # an [EMOTION:...] line. Stripping it here is not cosmetics:
        # leave it in and Piper reads "bracket emotion happy" out loud. A
        # model that ignores the instruction just yields emotion=None and
        # the default speaking face, so nothing depends on it complying.
        reply, emotion = GenieNode.strip_emotion_tag(reply)

        print("\n========== GENIE SAYS ==========")
        print(reply)
        # PERCEIVED sentiment. The tag is stripped by this point, so
        # without saying so there is no sign it was read at all -- a
        # tagged reply and an untagged one look identical. Whether it
        # actually reached the eyes is reported separately by speak(),
        # because that is a different question with a different failure.
        print("--- sentiment: %s"
              % (emotion or "none (Mistral did not tag this reply)"))
        print("=================================\n")

        if reply.strip() and not reply.startswith("["):
            # Don't try to speak our own error messages like
            # "[LLM unavailable...]"
            speak(reply, emotion=emotion)


def handle_incoming_sms(sender, text):
    """An SMS landed on the modem's SIM that the board decided was NOT a
    reply inside an emergency ack window (see modem_main.py's
    poll_inbound_sms()). Route it the same way a spoken command is routed
    in process_transcript(), except the input skipped Whisper and
    AudioServer entirely -- it was already text -- and the output skips
    Piper: the reply goes back as an SMS to whoever sent the original
    message, not out loud through GENIE's speaker.

    Runs in its own thread per message (see emergency_reader()), so a slow
    Mistral call here can never delay this connection from noticing an
    EV_ANSWERED on an unrelated, possibly concurrent, real emergency call.
    """
    text = (text or "").strip()
    if not text:
        return

    if not any(numbers_match(sender, allowed) for allowed in SMS_COMMAND_ALLOWED):
        print(f"SMS from {sender} ignored (not an authorised number): {text!r}")
        return

    print(f"\n========== SMS FROM {sender} ==========")
    print(text)
    print("===========================================\n")

    reply = build_sms_reply(text)

    if reply:
        send_sms_reply(sender, reply)
    else:
        print(f"(no reply built for {sender} -- nothing sent)")


def build_sms_reply(text):
    """process_transcript()'s logic, minus the audio in both directions.

    Same classifier, same four branches, same fallback to the LLM -- a
    texted "turn on the fan" and a spoken one hit identical code from here
    on. What differs is the destination: this returns the words GENIE
    would have spoken, for the caller to text back, instead of calling
    speak(). Returns None when there is nothing worth sending (an LLM
    error string, or an empty reply) -- mirroring process_transcript()'s
    own "don't speak our own error messages" guard.
    """
    intent = intent_classifier.classify(text)

    if intent and intent["type"] == "emergency":
        print("EMERGENCY TRIGGERED BY SMS -- calling and texting now.\n")

        # Same escalation a spoken "help me" runs -- SMS + calls to
        # EMERGENCY_CONTACTS. This reply is a separate, smaller text back
        # to whoever sent THIS message, confirming it went through; it is
        # not a substitute for that escalation and does not gate it.
        sent = send_emergency_trigger(EMERGENCY_VOICE_MESSAGE)

        return ("Emergency triggered. Calling and texting your contacts now."
                if sent else
                "Could not reach the emergency module. Please call for help yourself.")

    elif intent and intent["type"] == "sensor":
        print(f"SMS sensor query: {intent['sensor']} -- reading live data, skipping LLM.\n")

        if intent["sensor"] == "ALL":
            data = get_all_sensor_data()
            reply = intent_classifier.build_all_sensors_reply(data)
        else:
            data = get_sensor_data(intent["sensor"])
            reply = intent_classifier.build_sensor_reply(intent["sensor"], data)

        print(f"Sensor reply: {reply}\n")
        return reply

    elif intent and intent["type"] == "query":
        print(f"SMS status query: {intent['device']} -- reading real state, skipping LLM.\n")

        status = get_iot_status()
        reply = (build_status_reply(intent["device"], status) if status is not None
                 else "I couldn't reach the relay to check that.")

        print(f"Status reply: {reply}\n")
        return reply

    elif intent and intent["type"] == "command":
        print(f"SMS IoT command: {intent['command']} {intent['device']} "
              f"-- routing directly, skipping LLM.\n")

        # Same background-thread pattern as the spoken path: the relay
        # push doesn't need to finish before the confirmation text goes
        # out, since the confirmation wording is already decided.
        # A person just commanded this relay, so the climate rule stops
        # touching it. Without this, "turn off the fan" on a warm day is
        # undone within ten seconds and the spoken confirmation is a lie.
        if climate_watcher is not None:
            climate_watcher.manual_override(
                intent["device"], intent["command"] == "ON")

        threading.Thread(
            target=send_iot_command,
            args=(intent["command"], intent["device"]),
            daemon=True
        ).start()

        return intent["confirmation"]

    else:
        print("SMS: no intent matched -- asking the LLM.\n")

        reply = llm.ask(text)
        reply, _emotion = GenieNode.strip_emotion_tag(reply)

        print("\n========== GENIE WOULD TEXT ==========")
        print(reply)
        print("=======================================\n")

        if reply.strip() and not reply.startswith("["):
            return reply.strip()
        return None


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(PROCESSED_DIR, exist_ok=True)

    processed = set()
    pending = {}     # filename -> (last_size, stable_count)
    groups = {}      # ts -> {"A": path_or_None, "B": path_or_None, "first_seen": time.time()}

    print(f"Watching '{OUTPUT_DIR}' for new recordings...\n")

    while True:
        try:
            files = sorted(
                f for f in os.listdir(OUTPUT_DIR)
                if f.endswith(".wav") and os.path.isfile(os.path.join(OUTPUT_DIR, f))
            )
        except FileNotFoundError:
            files = []

        for fname in files:
            if fname in processed:
                continue

            full_path = os.path.join(OUTPUT_DIR, fname)

            try:
                size = os.path.getsize(full_path)
            except OSError:
                continue

            last_size, stable_count = pending.get(fname, (None, 0))

            if size == last_size and size > 0:
                stable_count += 1
            else:
                stable_count = 0

            pending[fname] = (size, stable_count)

            if stable_count < STABLE_CHECKS:
                continue

            match = RECORDING_RE.match(fname)
            if not match:
                # Doesn't match the expected pattern -- shouldn't happen
                # from AudioServer.py, but don't silently ignore it.
                print(f"New recording detected: {fname} (unrecognized name, processing alone)")
                try:
                    transcribe_pair_and_print(full_path)
                finally:
                    try:
                        shutil.move(full_path, os.path.join(PROCESSED_DIR, fname))
                    except OSError as e:
                        print(f"Could not move {fname} to processed/: {e}")
                processed.add(fname)
                pending.pop(fname, None)
                continue

            ts, suffix = match.group(1), match.group(2)
            group = groups.setdefault(ts, {"A": None, "B": None, "first_seen": time.time()})
            group[suffix] = full_path

        # Check every pending group: ready once both A and B are stable-
        # discovered, or once A alone has waited PAIR_GRACE_SECONDS for a
        # B that never showed up (Mic B not connected this session).
        for ts in list(groups.keys()):
            group = groups[ts]
            path_a, path_b = group["A"], group["B"]

            if path_a is None:
                continue  # Mic A always creates the group; nothing to do yet

            waited = time.time() - group["first_seen"]

            ready = (path_a is not None and path_b is not None) or (
                path_a is not None and waited >= PAIR_GRACE_SECONDS
            )

            if not ready:
                continue

            fname_a = os.path.basename(path_a)
            names = fname_a + (f" + {os.path.basename(path_b)}" if path_b else "")
            print(f"New recording detected: {names}")

            # Decide the session BEFORE transcribing: speak() logs into
            # whichever session is current, and the reply belongs to this
            # turn, not the next one.
            begin_or_continue_session()
            turn = _session_seq
            dest = session_dir()

            try:
                transcribe_pair_and_print(path_a, path_b)
            except Exception as e:
                print(f"Failed to process {names}: {e}")

            for p in (path_a, path_b):
                if p is None:
                    continue
                fname = os.path.basename(p)
                # Prefix with the turn number so the folder reads in
                # conversation order rather than by raw timestamp.
                mic = fname[-5]
                try:
                    os.makedirs(dest, exist_ok=True)
                    shutil.move(p, os.path.join(dest, f"turn{turn:02d}_{mic}.wav"))
                except OSError as e:
                    print(f"Could not file {fname} into {dest}: {e}")
                processed.add(fname)
                pending.pop(fname, None)

            del groups[ts]

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    print("Loading Whisper model...")
    model = WhisperModel(
        "base",
        device="cpu",
        compute_type="int8"
    )
    print("Whisper Ready!")

    llm = LLMService()

    print("Warming up local LLM (loading model into memory)...")
    warmup_start = time.time()
    warmup_reply = llm.ask("Hello")
    warmup_elapsed = time.time() - warmup_start
    print(f"LLM warm-up done in {warmup_elapsed:.1f}s. Reply: {warmup_reply}\n")

    print("Loading Piper TTS voice...")
    tts = TTSService()
    print(f"Piper Ready! Sample rate: {tts.sample_rate} Hz "
          f"(ESP I2S TX rate must match this).\n")

    print("Loading AIML intent classifier...")
    intent_classifier = IntentClassifier()
    print("Intent classifier ready.\n")

    threading.Thread(target=playback_server, daemon=True).start()
    threading.Thread(target=emergency_server, daemon=True).start()
    threading.Thread(target=control_server, daemon=True).start()

    # Autonomous alerts. Until this existed, GENIE only ever called for
    # help when somebody asked it to out loud -- api.py stored the smoke
    # and flame readings and nothing looked at them.
    watcher = sensor_watch.start(on_sensor_alert)

    # ...and until THIS existed, nothing was pushing readings into api.py
    # in the first place, so the watcher above was polling a dict of
    # startup defaults. The bridge is what makes the GENIE FINAL sensor
    # nodes real to this process: Node 1 over USB serial, and the Node 2
    # ToF array by way of Node 1's ESP-NOW link.
    #
    # The watcher is handed over so the bridge can wake it the moment a
    # bad reading lands, rather than up to SENSOR_WATCH_INTERVAL later.
    # The watcher stays the only thing that DECIDES an alert.
    #
    # Everything about the bridge is best-effort: no board, no serial
    # port, or Thonny holding the port open all log once and leave the
    # wake word, Whisper, the LLM and TTS running exactly as before.
    node_bridge.start(watcher=watcher)

    # Unprompted remarks. Separate from sensor_watch on purpose: that one
    # reads STATE and asks "is this bad", this one reads MOMENTS and asks
    # "did something just happen". A shove cannot be re-read a second
    # later the way a smoke level can, which is why api.py logs these with
    # a cursor instead of storing a flag.
    reaction_watch.start(on_handling_reaction)

    # The sensors act on the room instead of only describing it. Started
    # last because it is the only watcher that CHANGES anything -- if
    # something above it failed to come up, better to find out before a
    # relay starts moving on its own.
    # No `global` here: this block already IS module scope, and declaring
    # it produced "assigned to before global declaration" -- a SyntaxError
    # that ast.parse() does not raise, because it comes from building the
    # symbol table rather than from parsing. Check these with compile().
    climate_watcher = climate_watch.start(on_climate_change)

    main()