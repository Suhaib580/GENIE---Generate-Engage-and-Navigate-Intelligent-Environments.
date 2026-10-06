import network
import socket
import time
import math
import array
import neopixel
from machine import I2S, Pin

# ==========================================
# Configuration
# ==========================================
SSID = "GENIE1"
PASSWORD = "12345678"
PLAYBACK_PORT = 9001
DISCOVERY_PORT = 9999  # must match config.py's DISCOVERY_PORT on the laptop

# Protocol opcodes -- MUST match config.py on the server exactly
OP_THINKING = 1
OP_SPEAK_START = 2
OP_AUDIO_CHUNK = 3
OP_SPEAK_END = 4
OP_LISTEN_START = 5   # mic started capturing -> orange + rising beep
OP_LISTEN_END = 6     # mic stopped capturing -> falling beep + idle

# ==========================================
# NeoPixel
# ==========================================
np = neopixel.NeoPixel(Pin(48), 1)

def off():
    np[0] = (0, 0, 0)
    np.write()

def blue():
    np[0] = (0, 0, 40)
    np.write()

def red():
    np[0] = (40, 0, 0)
    np.write()

def purple():
    np[0] = (30, 0, 40)
    np.write()

def orange():
    np[0] = (55, 18, 0)
    np.write()

def blink_blue():
    blue()
    time.sleep(0.25)
    off()
    time.sleep(0.25)

def blink_red():
    red()
    time.sleep(0.25)
    off()
    time.sleep(0.25)

# ==========================================
# Wi-Fi
# ==========================================
print("Connecting to Wi-Fi...")
wlan = network.WLAN(network.STA_IF)
wlan.active(True)
wlan.connect(SSID, PASSWORD)

while not wlan.isconnected():
    blink_blue()

print("Wi-Fi Connected!")
print("Speaker ESP IP:", wlan.ifconfig()[0])
blue()


def discover_server_ip():
    """Listens for the laptop's periodic UDP broadcast and returns
    whichever IP it actually came from -- no more hardcoded address
    that goes stale every time the network reassigns IPs."""
    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    udp_sock.bind(("0.0.0.0", DISCOVERY_PORT))
    udp_sock.settimeout(1.0)

    print("Listening for server announcement...")

    while True:
        try:
            data, addr = udp_sock.recvfrom(1024)
            if data == b"GENIE_SERVER_HELLO":
                udp_sock.close()
                print("Discovered server at:", addr[0])
                return addr[0]
        except OSError:
            # timeout -- no broadcast yet, blink while waiting
            blink_blue()


SERVER_IP = discover_server_ip()
print("Will try to reach server at:", SERVER_IP, PLAYBACK_PORT)

# ==========================================
# I2S Speaker (TX) -- MAX98357A
# IMPORTANT: rate must match the Piper voice's sample rate exactly.
# en_US-amy-medium reports 22050 Hz -- confirm with:
#   python Services/tts_service.py
# and update this if it prints something different.
# ==========================================
audio_tx = I2S(
    0,
    sck=Pin(15),
    ws=Pin(16),
    sd=Pin(17),
    mode=I2S.TX,
    bits=16,
    format=I2S.MONO,
    rate=22050,
    ibuf=32768
)


# ==========================================
# Listening-cue beeps (built ONCE at boot)
# Short tones, same 22050 Hz / 16-bit / mono format audio_tx expects, so
# they can be written straight to it like any TTS chunk. Rising pitch =
# "I'm listening"; falling pitch = "stopped" -- the Siri/Alexa convention.
# Phase is accumulated (not recomputed per sample) so the pitch glide has
# no clicks, and a short linear fade in/out kills the on/off pop.
# ==========================================
BEEP_RATE = 22050

def make_beep(f_start, f_end, ms, vol=7000, fade_ms=6):
    n = BEEP_RATE * ms // 1000
    fade = max(1, BEEP_RATE * fade_ms // 1000)
    buf = array.array("h", bytes(2 * n))
    phase = 0.0
    for i in range(n):
        f = f_start + (f_end - f_start) * i / n
        phase += 2 * math.pi * f / BEEP_RATE
        s = vol * math.sin(phase)
        if i < fade:
            s = s * i / fade
        elif i >= n - fade:
            s = s * (n - i) / fade
        buf[i] = int(s)
    return bytes(buf)

START_BEEP = make_beep(880, 1320, 120)   # rising  -> mic is now listening
STOP_BEEP = make_beep(880, 620, 120)     # falling -> mic stopped recording


def recv_exact(sock, n):
    """socket.recv() can return fewer bytes than asked for -- this loops
    until exactly n bytes have been collected, or raises if the
    connection drops.

    Writes into a single pre-allocated bytearray via slice assignment
    instead of `buf += chunk`. Slice assignment into an existing
    bytearray only copies the incoming piece, not the whole buffer each
    time, so this avoids the memory fragmentation that repeated
    concatenation causes on constrained MicroPython heaps.

    (Not using recv_into() here -- not available on this build's
    socket module.)
    """
    buf = bytearray(n)
    pos = 0
    while pos < n:
        chunk = sock.recv(n - pos)
        if not chunk:
            raise OSError("Connection closed while reading")
        buf[pos:pos + len(chunk)] = chunk
        pos += len(chunk)
    return buf


def wait_for_next_opcode_with_blink(sock):
    """Called right after OP_THINKING. Blinks purple in short bursts
    while polling (with a short timeout) for the next real opcode."""
    sock.settimeout(0.3)
    try:
        while True:
            try:
                b = sock.recv(1)
                if b:
                    return b[0]
            except OSError:
                # timeout -> no data yet, animate one blink cycle
                purple()
                time.sleep(0.15)
                off()
                time.sleep(0.15)
    finally:
        sock.settimeout(None)


# ==========================================
# Playback connection + main loop
# This board only has one job, so no threading is needed -- everything
# runs in a single straightforward loop.
# ==========================================
print("Connecting to playback server...")

while True:
    try:
        sock = socket.socket()
        sock.settimeout(5)
        sock.connect((SERVER_IP, PLAYBACK_PORT))
        sock.settimeout(None)
        print("Playback connected!")
        blue()
        break
    except Exception as e:
        print("Playback connect failed:", e)
        blink_red()
        time.sleep(1)

while True:
    try:
        opcode = recv_exact(sock, 1)[0]

        if opcode == OP_THINKING:
            opcode = wait_for_next_opcode_with_blink(sock)

        if opcode == OP_SPEAK_START:
            purple()

        elif opcode == OP_AUDIO_CHUNK:
            length = int.from_bytes(recv_exact(sock, 4), "big")
            pcm_data = recv_exact(sock, length)
            audio_tx.write(pcm_data)

        elif opcode == OP_SPEAK_END:
            off()
            blue()

        elif opcode == OP_LISTEN_START:
            # Wake word fired -- mic is now capturing. Just beep; the orange
            # "listening" LED lives on the MIC board (right where you speak),
            # driven separately by AudioServer.
            audio_tx.write(START_BEEP)

        elif opcode == OP_LISTEN_END:
            # Command captured -- mic stopped listening. Falling beep.
            audio_tx.write(STOP_BEEP)

    except Exception as e:
        print("Connection lost:", e)
        off()
        red()
        time.sleep(2)

        # Attempt to reconnect
        try:
            sock.close()
        except Exception:
            pass

        while True:
            try:
                sock = socket.socket()
                sock.settimeout(5)
                sock.connect((SERVER_IP, PLAYBACK_PORT))
                sock.settimeout(None)
                print("Reconnected!")
                blue()
                break
            except Exception as e2:
                print("Reconnect failed:", e2)
                blink_red()
                time.sleep(1)