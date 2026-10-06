from machine import I2S, Pin
import network
import socket
import select
import time
import neopixel

# ============================================================
# GENIE - DUAL INMP441 STEREO MICROPHONE
# ============================================================
#
# MIC A (LEFT):
#   VDD -> 3.3V
#   GND -> GND
#   SCK -> GPIO4
#   WS  -> GPIO5
#   SD  -> GPIO6
#   L/R -> GND
#
# MIC B (RIGHT):
#   VDD -> 3.3V
#   GND -> GND
#   SCK -> GPIO4
#   WS  -> GPIO5
#   SD  -> GPIO6
#   L/R -> 3.3V
#
# Both microphones share ONE I2S bus.
#
# Audio:
#   16-bit
#   16 kHz
#   Stereo
#
# TCP:
#   One connection
#   Stereo PCM data
#
# ============================================================


# ============================================================
# CONFIGURATION
# ============================================================

SSID = "GENIE1"
PASSWORD = "12345678"

AUDIO_PORT = 9000
DISCOVERY_PORT = 9999

SAMPLE_RATE = 16000
BITS = 16

# Control opcodes the laptop (AudioServer.py) sends BACK down this same
# audio socket to drive this board's LED. Must match config.py.
OP_LISTEN_START = 5   # wake word fired -> mic is listening -> orange
OP_LISTEN_END = 6     # command captured -> back to streaming idle -> green

# Audio buffer used for each read.
# Start conservatively because Wi-Fi + I2S consume RAM.
BUFFER_SIZE = 2048


# ============================================================
# NEOPIXEL
# ============================================================

np = neopixel.NeoPixel(Pin(48), 1)


def off():
    np[0] = (0, 0, 0)
    np.write()


def blue():
    np[0] = (0, 0, 40)
    np.write()


def green():
    np[0] = (0, 40, 0)
    np.write()


def red():
    np[0] = (40, 0, 0)
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
    time.sleep(0.1)
    off()
    time.sleep(0.1)


# ============================================================
# WIFI
# ============================================================

print()
print("========================================")
print(" GENIE DUAL MIC STEREO")
print("========================================")
print()

print("Connecting to Wi-Fi...")

wlan = network.WLAN(network.STA_IF)
wlan.active(True)
wlan.connect(SSID, PASSWORD)

while not wlan.isconnected():
    blink_blue()

print()
print("Wi-Fi Connected!")
print("ESP32 IP:", wlan.ifconfig()[0])
print()

blue()


# ============================================================
# SERVER DISCOVERY
# ============================================================

def discover_server_ip():

    udp_sock = socket.socket(
        socket.AF_INET,
        socket.SOCK_DGRAM
    )

    udp_sock.setsockopt(
        socket.SOL_SOCKET,
        socket.SO_REUSEADDR,
        1
    )

    udp_sock.bind(
        ("0.0.0.0", DISCOVERY_PORT)
    )

    # NOTE: this 1s timeout is ONLY so the LED can blink blue while we
    # wait for the server's broadcast. It is NOT a connection timeout and
    # has nothing to do with the audio stream -- it is dropped the moment
    # the server is found.
    udp_sock.settimeout(1.0)

    print("Waiting for Python server...")
    print("Listening on UDP port:", DISCOVERY_PORT)

    while True:

        try:

            data, addr = udp_sock.recvfrom(1024)

            if data == b"GENIE_SERVER_HELLO":

                server_ip = addr[0]

                udp_sock.close()

                print("Python server discovered!")
                print("Server IP:", server_ip)
                print()

                return server_ip

        except OSError:
            blink_blue()


SERVER_IP = discover_server_ip()


# ============================================================
# I2S STEREO MICROPHONES
# ============================================================
#
# IMPORTANT:
#
# Both microphones use the SAME:
#
#   SCK -> GPIO4
#   WS  -> GPIO5
#   SD  -> GPIO6
#
# L/R determines the channel:
#
#   MIC A L/R -> GND   = LEFT
#   MIC B L/R -> 3.3V  = RIGHT
#
# ============================================================

print("Initializing stereo I2S microphone...")

audio = I2S(
    0,

    sck=Pin(4),
    ws=Pin(5),
    sd=Pin(6),

    mode=I2S.RX,

    bits=BITS,

    format=I2S.STEREO,

    rate=SAMPLE_RATE,

    ibuf=4096
)

print("I2S initialized.")
print("Sample rate:", SAMPLE_RATE)
print("Bits:", BITS)
print("Format: STEREO")
print()


# ============================================================
# TCP AUDIO CONNECTION
# ============================================================

def connect_audio_server():

    while True:

        try:

            print(
                "Connecting to Python audio server..."
            )

            sock = socket.socket()

            sock.connect(
                (SERVER_IP, AUDIO_PORT)
            )

            print(
                "Connected to audio server!"
            )

            print(
                "Streaming stereo audio..."
            )

            print()

            return sock

        except Exception as e:

            print(
                "Audio server connection failed:",
                e
            )

            blink_red()

            time.sleep(2)


sock = connect_audio_server()

# Poll the audio socket for control bytes the laptop sends back (LED cues).
# poll(0) is non-blocking, so checking it never stalls the outbound audio
# stream. Registered here and re-armed on every reconnect below.
poller = select.poll()
poller.register(sock, select.POLLIN)


# ============================================================
# AUDIO BUFFER
# ============================================================

buf = bytearray(BUFFER_SIZE)

# memoryview over the SAME buffer. Slicing a memoryview (mv[:n]) does NOT
# copy or allocate -- unlike buf[:n], which builds a brand-new bytearray
# on every single read (~30x/sec) and fragments the ESP heap until it
# eventually throws MemoryError. Create it once, here.
mv = memoryview(buf)


# ============================================================
# STREAM STEREO AUDIO
# ============================================================

print("========================================")
print(" STEREO MICROPHONE STREAM ACTIVE")
print("========================================")
print()
print("LEFT  = Mic A")
print("RIGHT = Mic B")
print()
print("Listening...")
print("LED cue reader ACTIVE (orange on capture)")
print()


green()


while True:

    try:

        # Read stereo PCM from both microphones.
        #
        # The buffer contains interleaved samples:
        #
        # LEFT, RIGHT, LEFT, RIGHT, ...
        #
        n = audio.readinto(buf)

        if n > 0:

            # sock.write() sends ALL n bytes (it won't send a partial
            # buffer and silently drop the rest the way sock.send() can --
            # a partial send on an interleaved stereo stream would swap
            # L/R). mv[:n] avoids allocating a new buffer each loop.
            sock.write(mv[:n])

        # Did the laptop send a listening cue? Non-blocking check -- if a
        # byte is waiting, read it and set the LED. Orange while Genie is
        # capturing your command, back to green (streaming idle) when done.
        # Drain EVERY queued control byte, not just one per audio chunk --
        # if two cues land in the same 32 ms window the second would
        # otherwise sit in the socket buffer and the LED would lag behind
        # (or stay stuck on the older cue).
        while poller.poll(0):

            b = sock.recv(1)

            if not b:
                # Readable but zero bytes = the laptop closed the socket.
                # Bail out to the reconnect path instead of spinning here.
                raise OSError("server closed control/audio socket")

            print("[cue] opcode", b[0])

            if b[0] == OP_LISTEN_START:
                orange()
            elif b[0] == OP_LISTEN_END:
                green()

    except OSError as e:

        print()
        print("Audio connection lost:")
        print(e)
        print()

        try:
            sock.close()
        except:
            pass

        red()

        time.sleep(1)

        sock = connect_audio_server()

        # Re-arm the control-byte poller on the fresh socket (a new poll
        # object avoids any stale registration on the old, closed sock).
        poller = select.poll()
        poller.register(sock, select.POLLIN)

        green()

    except Exception as e:

        print()
        print("Unexpected error:")
        print(e)
        print()

        try:
            sock.close()
        except:
            pass

        red()

        time.sleep(1)

        sock = connect_audio_server()

        # Re-arm the control-byte poller on the fresh socket (a new poll
        # object avoids any stale registration on the old, closed sock).
        poller = select.poll()
        poller.register(sock, select.POLLIN)

        green()
