"""
Board-side settings for the MODEM ESP32-S3. Deploy this next to modem_main.py.

This deliberately duplicates a handful of values from the PC's config.py rather
than importing it. config.py is ~700 lines of Whisper thresholds, Piper paths
and VAD tuning that mean nothing on a microcontroller, and half of it imports
modules MicroPython does not have.

The duplication is real, so it is worth saying plainly: if you change any of
the EMERGENCY_*, MODEM_* or I2S_* values in config.py, change them here too.
The docs list them in one table for exactly this reason - see
docs/emergency_call_audio.md.
"""

# ------------------------------------------------------------------ network
WIFI_SSID = "GENIE1"
WIFI_PASSWORD = "12345678"

# The PC running whisper_service.py.
#
# THIS ADDRESS KEEPS MOVING AND THAT IS A REAL PROBLEM. Observed in one
# session: 10.159.205.191, 10.155.201.101, 10.187.53.191, 10.134.103.191 -
# FOUR different /16s in a couple of hours, because GENIE1 is a phone hotspot
# handing out a fresh subnet on every restart. This will happen again.
#
# When it moves, the board sits retrying forever and NO ALERT EVER FIRES. It
# fails silently: Wi-Fi connects, the modem registers, everything looks healthy,
# and the trigger simply never arrives.
#
# The durable fix is to let the LAPTOP host the hotspot instead of joining one.
# Windows Mobile Hotspot always gives the host 192.168.137.1, permanently. Name
# it GENIE1 with the same password and nothing else here needs to change:
#
#     Settings -> Network & Internet -> Mobile hotspot
#     SERVER_HOST = "192.168.137.1"
#
# Until then, check `ipconfig` before every demo and update this line.
SERVER_HOST = "10.70.109.191"
EMERGENCY_PORT = 9002

# Fall back to UDP discovery when SERVER_HOST does not answer.
#
# AudioServer.py broadcasts "GENIE_SERVER_HELLO" on this port every two
# seconds and the mic and speaker boards have always listened for it. This
# board did not, so every laptop IP change meant editing the line above and
# re-flashing - five times in one session, each presenting as "the board will
# not connect".
#
# Kept as a fallback rather than the primary: a hotspot with AP/client
# isolation drops device-to-device broadcasts silently, which is why the IoT
# relay still uses a hand-set address. SERVER_HOST is tried first and is still
# the fast path when it is right.
SERVER_DISCOVERY = True
DISCOVERY_PORT = 9999

WIFI_TIMEOUT = 20           # seconds to wait for an IP before retrying
RECONNECT_DELAY = 5         # seconds between attempts to reach the server

# How long the board's socket read waits for a server frame before giving up
# and looping back around - which is also how often it checks the modem for
# new inbound SMS (see poll_inbound_sms() in modem_main.py). Shorter means
# a texted command is noticed sooner; it also means one more AT+CMGL round
# trip to the modem every cycle when nothing is happening. 3 s keeps that
# chatter modest without making someone wait long for a reply.
SMS_POLL_INTERVAL = 3.0

# ------------------------------------------------------------------ contacts
# Mirrors EMERGENCY_CONTACTS in config.py. Tried in this order.
EMERGENCY_CONTACTS = [
    "+923001234567",      # primary. PLACEHOLDER: set your own
]

# Used when the server sends a trigger with no message of its own (a spoken
# "this is an emergency" has nothing more specific to say). A sensor alert
# overrides this with wording naming the sensor.
DEFAULT_MESSAGE = "Emergency alert from Genie: please call or check immediately."

# ------------------------------------------------------------------ escalation
ACK_WINDOW = 45.0           # seconds to wait for a reply text before dialling
ACK_POLL = 5.0
CALL_TIMEOUT = 45           # seconds to let each number ring
# Seconds to hold the line open once somebody answers.
#
# Was 300, from when the plan was for the responder to hear the room through
# the modem's electret. That capsule is gone - the MIC pads tore off the
# carrier - so the call now carries silence in both directions. Holding a
# silent line for five minutes only burns airtime and keeps the responder
# listening to nothing.
#
# 20 s is long enough for them to register that it is a real, answered call
# from GENIE's number rather than a misdial, which is the entire signal a
# blank call can carry.
HOLD_SECONDS = 20

DTMF_ACK = "1"
DTMF_ESCALATE = "2"
DTMF_ASK = "3"

# ------------------------------------------------------------------ radio
# LTE. Outgoing alerts are SMS only now, so nothing here needs 2G any more.
#
# This was 13 (GSM only) for exactly one reason: calls. The SIM has no VoLTE,
# so on LTE every voice call had to fall back to 2G mid-dial, and that handover
# failed every single time - the module either rebooted about six seconds after
# ATD or the network refused with "emergency calls only". Pinned to GSM, the
# first call rang for 27 seconds. Measured 2026-08-26.
#
# What GSM cost was the SMS path. A 2G transmit burst pulls around 2 A and this
# supply cannot deliver it. On 2026-09-01 the module reset during the AT+CMGS
# prompt phase on six consecutive attempts - null bytes on the line, then
# *ATREADY: 1 - which killed the one path that had worked all evening. The same
# module on LTE had not dropped a message.
#
# Calls are out of scope, so that trade has no downside left. LTE transmit is a
# fraction of the 2G burst, registration is faster, and SMS gets through.
#
# 2 = automatic, 13 = GSM only, 38 = LTE only.
NETWORK_MODE = 2

# Outgoing voice calls. Off: the escalation ends after the SMS.
#
# The call could never carry information anyway - the MIC pads tore off the
# carrier, so an answered call is silent in both directions - and dialling
# needs the 2G fallback that browns the module out. With this False,
# emergency_alert() texts every contact and stops there. Inbound SMS is
# untouched: replies still arrive and still run as commands.
CALLS_ENABLED = False

# ------------------------------------------------------------------ SMS centre
# The service centre that relays outgoing texts.
#
# None means "use whatever the SIM carries", which is right almost always -
# the operator provisions it and it is correct out of the box.
#
# Set it only if the one reported at bring-up is genuinely not your operator's.
# A wrong SMSC fails in a way that looks like a radio fault and is not one:
# the send times out, or comes back "+CMS ERROR: unknown error", while
# registration and signal both look perfectly healthy.
#
# To find the right value, ask the operator or read it off another phone on
# the same network (Android: Messages -> Settings -> Advanced -> SMSC). Do NOT
# guess it from a list on the internet - they differ per region and per SIM
# batch, and a plausible-looking wrong number is worse than leaving this None,
# because it overrides a value that may have been correct.
#
# Include the leading + ; the driver derives the 145/129 type from it.
SMSC = None

# ------------------------------------------------------------------ modem UART
MODEM_UART_ID = 1
MODEM_TX = 17               # ESP32 TX -> modem RXD
MODEM_RX = 18               # ESP32 RX <- modem TXD
MODEM_RST = 16
MODEM_BAUD = 115200

# ------------------------------------------------------------------ status LED
# The onboard NeoPixel, used as a link indicator.
#
# WHY THIS EXISTS. Every network fault this board has had looked identical
# from the outside: the board sat there, powered, doing nothing visible, and
# the only way to tell a healthy link from a dead one was to plug into USB and
# read the console. That is fine on a bench and useless in the enclosure,
# which is where this board actually lives - and an emergency alerter that
# cannot say "I am not connected" without a laptop is not much of an alerter.
#
# GPIO 48 is the onboard RGB LED on most ESP32-S3 devkits; some use 38. If the
# light never comes on, try 38 before assuming the code is wrong.
#
# Colours, dimmest to most alarming:
#   white    booting
#   red      no wifi
#   yellow   wifi up, cannot reach the server
#   green    connected, idle - the normal resting state
#   blue     an alert is running, SMS going out
#   purple   wifi and server fine, but the modem is not answering
STATUS_PIN = 48
STATUS_ENABLED = True

# 0-255. Deliberately low: these LEDs are painfully bright at full scale and
# this one sits in a room someone may be sleeping in.
STATUS_BRIGHTNESS = 24

# ------------------------------------------------------------------ audio: none
# THE CALL IS SILENT. There is no audio path in either direction.
#
# The MIC+ / MIC- pads on the BK-7670 carrier tore off, taking the electret
# capsule with them. Without a microphone the modem has nothing to encode, so
# the far end hears silence for the whole call.
#
# What the alert still delivers, and it is not nothing:
#
#   SMS      says WHAT happened - "smoke detected", "a fall was detected" -
#            and lands even when the phone is on silent. This carries the
#            actual information and it works reliably.
#   CALL     a ringing phone from a known number, at 3am, gets attention that
#            a text does not. It says SOMETHING IS WRONG, GO AND LOOK. That
#            it is silent when answered does not undo the alert - the SMS
#            arrived first and already said why.
#   ACK      a reply to the SMS during the window cancels the call entirely.
#
# Three ways of getting speech into the call were built and measured before
# the pads broke, and all three are dead ends on this hardware:
#
#   ANALOG    PCM5102A into MIC+/MIC-. The far end heard only harmonics of
#             216.7 Hz - the GSM TDMA frame rate - because the modem's own 2G
#             transmit bursts contaminate the shared supply and the DAC's
#             regulator does not reject them. Needs a separate regulator and
#             a ground plane, i.e. a PCB.
#   DIGITAL   AT+CCMXPLAY from module flash. The file uploads byte-perfect and
#             the module reports playback starting, but it decodes to noise -
#             and play_path 1 and 2 (remote, both) return ERROR anyway, so
#             this firmware has no route from stored audio to the uplink.
#   ACOUSTIC  GENIE's speaker next to the modem's electret. This one would
#             have worked. The pads broke before it could be tested.
#
# All three are written up in docs/Audio Injection Investigation.docx so
# nobody rebuilds them.
#
# IF THE PADS ARE EVER REPAIRED, or the carrier replaced: solder an electret
# back onto MIC+/MIC-, put GENIE's speaker a few centimetres from it, and set
# HOLD_SECONDS back up. Nothing else needs changing - the server already knows
# how to speak when a call is answered.
