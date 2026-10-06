"""
Firmware for the MODEM ESP32-S3. MicroPython.

This board does one job and must do it while everything else is on fire: hold a
socket open to the PC, and when a trigger arrives, text and call for help. A
second, smaller job rides alongside it: any OTHER SMS that lands on this SIM -
someone texting "turn on the fan" or "is there smoke" instead of speaking to
GENIE - gets relayed to the server as a command, and the server's reply comes
back here to be texted, not spoken.

    PC (whisper_service)  --0x01 <len> <msg>-->  this board
                                                     |
                                          SMS -> ack window -> call
                                                     |
                          <--0x02 answered---  announce in the ROOM

    this board  --0x03 <sender> <text>-->  PC (whisper_service, AI inference)
    this board  <--0x04 <number> <text>--  PC: send exactly this SMS back

The call itself is SILENT - the modem's MIC pads tore off the carrier, so
there is no microphone and nothing to encode. The announcement is for whoever
is physically present, not for the person who answered.

THE ACK WINDOW AND THE COMMAND RELAY MUST NEVER RUN AT THE SAME TIME. Both
read the modem's "REC UNREAD" SMS list, and AT+CMGL marks whatever it lists
as read - so whichever of the two asks first steals the messages the other
needed. See poll_inbound_sms() below: it is only ever called from main()'s
idle loop, never from inside run_alert(). That is a hard rule, not a
convenience - break it and a real acknowledgement reply can be read as a
command and answered by the LLM instead of cancelling the call.

Deploy alongside Services/modem.py and esp32/settings.py:

    mpremote cp Services/modem.py   :modem.py
    mpremote cp esp32/settings.py   :settings.py
    mpremote cp esp32/modem_main.py :main.py

No audio file. This board has no DAC, no I2S and no speaker, and the modem has
no microphone either - the MIC pads broke. What ships is SMS plus a blank call:
the text says what happened, the ring says go and look.

Design notes worth knowing before changing anything here:

  THE CALL CARRIES NO AUDIO.  Three routes were built and measured - a DAC
  into the mic pins (217 Hz GSM buzz), AT+CCMXPLAY from module flash (decodes
  to noise, and will not route to the uplink), and a speaker beside the modem's
  electret. The last would have worked; the MIC pads tore off before it could
  be tested. See docs/Audio Injection Investigation.docx.

  A blank call is still a real alert. The SMS arrives first and says WHAT
  happened; the ring says GO AND LOOK. Silence when answered does not undo
  either of those.

  SMS AND THE CALL STILL WORK WITHOUT THE SERVER.  Once triggered, this board
  completes the whole escalation on its own. Only the spoken announcement needs
  the network - and the trigger itself already came over that network, so there
  is little left to lose by the time it matters.

  THE SOCKET IS THE ONLY THING THAT RECONNECTS.  If the network drops, this
  board keeps retrying forever and the modem stays registered. A failure to
  reach the PC must never leave the board unable to place a call.

  NOTHING HERE RAISES.  Every failure path logs and carries on. A board that
  reboots mid-alert is worse than one that limps.
"""

import time
import errno

import network
import socket

import settings
from modem import Modem, ModemError


TRIGGER_BYTE = 0x01      # server -> board: run the escalation
EV_ANSWERED = 0x02       # board -> server: somebody answered, speak now
SMS_IN = 0x03            # board -> server: an inbound SMS, run it through AI
SMS_OUT = 0x04           # server -> board: send this exact SMS, no escalation

# Timeouts and EAGAIN both mean "nothing arrived this tick" on the sockets
# MicroPython's port hands back here. Anything else raised out of recv() is
# a real socket failure and must NOT be swallowed - see FrameReader.feed().
_NO_DATA_ERRNOS = (errno.ETIMEDOUT, errno.EAGAIN)


def log(*args):
    print("[modem]", *args)


# ---------------------------------------------------------------- status LED
#
# One colour, one meaning, visible across a room with no laptop attached.
#
# Every network fault this board has had presented identically from the
# outside: a powered board doing nothing. Telling "connected and idle" from
# "retrying an address that no longer exists" needed a USB cable and a serial
# console every single time. In the enclosure that is not an option, and an
# alerter that cannot say "I am not connected" is not much of an alerter.
#
# Scaled by STATUS_BRIGHTNESS rather than written at full value: these LEDs
# are painful at 255, and this one lives in a room somebody may be asleep in.

STATUS_COLOURS = {
    "boot":     (255, 255, 255),   # white  - starting up
    "no_wifi":  (255, 0, 0),       # red    - no network at all
    "no_server": (255, 160, 0),    # yellow - wifi up, server unreachable
    "ready":    (0, 255, 0),       # green  - connected and idle
    "alert":    (0, 0, 255),       # blue   - an alert is going out
    "no_modem": (180, 0, 255),     # purple - link fine, modem silent
}

_pixel = None
_status = None


def status(state):
    """Show one of STATUS_COLOURS. Never raises, never blocks.

    A board with no NeoPixel, the wrong pin, or no `neopixel` module must
    behave exactly as it did before this existed - the LED is a convenience,
    and an alerter that dies because an indicator failed would be a poor
    trade. Every failure here is swallowed on purpose.
    """
    global _pixel, _status

    if state == _status:
        return                      # already showing it; skip the bus traffic
    _status = state

    if not getattr(settings, "STATUS_ENABLED", True):
        return

    try:
        if _pixel is None:
            import neopixel
            from machine import Pin
            _pixel = neopixel.NeoPixel(
                Pin(getattr(settings, "STATUS_PIN", 48), Pin.OUT), 1)

        r, g, b = STATUS_COLOURS.get(state, (0, 0, 0))
        scale = getattr(settings, "STATUS_BRIGHTNESS", 24) / 255.0
        _pixel[0] = (int(r * scale), int(g * scale), int(b * scale))
        _pixel.write()
    except Exception:
        # Once, quietly. A per-tick complaint about a missing LED would bury
        # the modem log this board exists to produce.
        if getattr(settings, "STATUS_ENABLED", True):
            settings.STATUS_ENABLED = False
            log("no status LED on pin %s - carrying on without it"
                % getattr(settings, "STATUS_PIN", 48))


# ---------------------------------------------------------------- networking

def same_subnet(a, b):
    """True if two IPv4 addresses share a /24.

    Crude on purpose. This board and the PC are always on one small flat
    network - a phone hotspot - so anything finer would be pretending to a
    precision the setup does not have. What it has to catch is the case that
    actually happens: the board holding a lease from a previous hotspot
    session while the laptop is somewhere else entirely.
    """
    try:
        pa, pb = a.split("."), b.split(".")
        if len(pa) != 4 or len(pb) != 4:
            # "", "0.0.0.0" mid-DHCP, or something that is not an address at
            # all. Answering False here would tear down the association on
            # the strength of a reading that says nothing, so say True and
            # let the connect attempt be the judge.
            return True
        return pa[:3] == pb[:3]
    except Exception:
        return True          # unparseable - do not tear down a working link


def wifi_connect():
    """Bring up Wi-Fi. Blocks until connected, retrying forever."""
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)

    # How many times we are willing to reject an association for being on the
    # wrong subnet. Bounded, because rejoining is only a fix when a BETTER
    # network is available: if two APs are broadcasting this SSID and the
    # board keeps landing on the wrong one, an unbounded loop would leave it
    # rejoining forever and never even attempt a connection. After this many
    # tries it accepts what it has and lets the connect and the discovery
    # listen fail honestly, which at least produces a diagnosable log.
    wrong_network = 0

    while True:
        if wlan.isconnected():
            ip = wlan.ifconfig()[0]

            # AN ASSOCIATION IS NOT A USABLE NETWORK.
            #
            # isconnected() reports that the radio is joined to an AP, and it
            # stays True across a soft reboot. So a board that joined the
            # hotspot yesterday, on a subnet the laptop has since left, sits
            # here answering "already up" forever while every connect fails
            # with ECONNABORTED and every discovery listen times out. That is
            # precisely the state this project kept ending up in.
            #
            # The board cannot ping the server to check, but it does not need
            # to: if its own address is not even on the same /24 as the one it
            # is trying to reach, the association is wrong no matter what the
            # radio says. Waiting for three TCP failures to prove it wastes
            # thirty seconds on a question already answered.
            if not same_subnet(ip, settings.SERVER_HOST) and wrong_network < 2:
                wrong_network += 1
                log("wifi is up on %s but the server is on %s - wrong network"
                    % (ip, settings.SERVER_HOST))
                log("dropping the association and rejoining (%d/2)"
                    % wrong_network)
                try:
                    wlan.disconnect()
                    wlan.active(False)
                    time.sleep(1)
                    wlan.active(True)
                except Exception as e:
                    log("could not reset wifi:", e)
                continue

            if not same_subnet(ip, settings.SERVER_HOST):
                # Rejoining did not move us. Two APs with this SSID, or the
                # hotspot itself has changed subnet and SERVER_HOST is simply
                # out of date. Say which, and carry on - discovery may still
                # find the server if they are bridged.
                log("STILL on %s, server expected on %s."
                    % (ip, settings.SERVER_HOST))
                log("Either two networks share this SSID, or SERVER_HOST is "
                    "stale. Continuing anyway.")

            # Say so even when there is nothing to do. A silent return here is
            # what made a stale lease from a previous session invisible: the
            # board looked like it had just booted onto the right network when
            # it had in fact never left the old one.
            log("wifi already up:", ip)
            return wlan

        status("no_wifi")
        log("connecting to", settings.WIFI_SSID)
        try:
            wlan.connect(settings.WIFI_SSID, settings.WIFI_PASSWORD)
        except OSError as e:
            log("wifi connect failed:", e)

        deadline = time.time() + settings.WIFI_TIMEOUT
        while time.time() < deadline and not wlan.isconnected():
            time.sleep(0.5)

        if wlan.isconnected():
            log("wifi up:", wlan.ifconfig()[0])
            return wlan

        log("wifi timed out, retrying")
        wlan.active(False)
        time.sleep(2)
        wlan.active(True)


# Consecutive failed connects before the Wi-Fi association is torn down and
# rebuilt. Three is roughly 30 s with discovery in the loop - long enough not
# to thrash a server that is briefly restarting, short enough that a stale
# link does not strand the board until somebody notices.
WIFI_RESET_AFTER = 3

# Last address that actually worked. Starts as the configured one and is
# replaced by whatever discovery finds, so a laptop that moves once costs one
# retry cycle rather than a re-flash.
_server_host = None


def own_ip():
    """This board's own IP, or a note saying why there isn't one."""
    try:
        import network
        wlan = network.WLAN(network.STA_IF)
        if not wlan.isconnected():
            return "no wifi"
        return wlan.ifconfig()[0]
    except Exception:
        return "unknown"


def discover_server(timeout=8):
    """Listen for the server's UDP hello and return the IP it came from.

    AudioServer.py broadcasts b"GENIE_SERVER_HELLO" to 255.255.255.255 on
    DISCOVERY_PORT every two seconds, and the mic and speaker boards have
    always used it. This board did not, which is why a laptop IP change -
    five of them in one session - meant editing settings.py and re-flashing
    while the board sat there retrying an address that no longer existed.

    Returns None if nothing arrives. That is a real possibility rather than a
    fault: a hotspot with AP/client isolation drops device-to-device
    broadcasts silently, which is exactly why the IoT relay still uses a
    hand-set IP. Discovery is therefore a fallback, never the primary.
    """
    port = getattr(settings, "DISCOVERY_PORT", 9999)
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        sock.bind(("0.0.0.0", port))
        data, addr = sock.recvfrom(64)
        if b"GENIE_SERVER_HELLO" in data:
            log("discovered server at", addr[0])
            return addr[0]
        log("ignoring unexpected broadcast:", data[:32])
    except OSError as e:
        log("no discovery broadcast (%s)" % e)
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
    return None


def server_connect():
    """Open the trigger socket. Returns a connected socket, retrying forever."""
    global _server_host

    if _server_host is None:
        _server_host = settings.SERVER_HOST

    failures = 0

    while True:
        # WI-FI FIRST, EVERY TIME ROUND.
        #
        # This used to be called once, from main(), and never again - so a
        # link that dropped after boot left this loop opening TCP sockets
        # forever on a dead radio. Every attempt failed with ECONNABORTED and
        # every discovery listen timed out, which reads as "the server is
        # unreachable" when the truth is that this board is not on a network
        # at all. Hours went into firewall rules and bind addresses on the
        # strength of that misreading.
        #
        # wifi_connect() returns immediately when already associated, so this
        # costs nothing in the healthy case and is the whole fix in the
        # unhealthy one.
        wifi_connect()

        try:
            sock = socket.socket()
            sock.connect((_server_host, settings.EMERGENCY_PORT))
            log("connected to server %s:%d"
                % (_server_host, settings.EMERGENCY_PORT))
            failures = 0
            status("ready")
            return sock
        except OSError as e:
            # Our own address goes in the message on purpose. "server
            # unreachable" reads as a server fault and sent us hunting through
            # firewall rules and bind addresses more than once, when the board
            # was simply on a different subnet - reusing a lease from an
            # earlier session, so not even a "wifi up" line to hint at it.
            # Two addresses side by side make a subnet mismatch obvious.
            status("no_server")
            log("server unreachable: %s -> %s:%d (%s)"
                % (own_ip(), _server_host, settings.EMERGENCY_PORT, e))
            try:
                sock.close()
            except Exception:
                pass

            # The configured address is wrong or the laptop moved. Spend one
            # retry cycle listening instead of sleeping through it.
            if getattr(settings, "SERVER_DISCOVERY", True):
                found = discover_server(settings.RECONNECT_DELAY)
                if found and found != _server_host:
                    log("server moved: %s -> %s" % (_server_host, found))
                    _server_host = found
                    failures = 0
                    continue          # try the new address immediately

            # A LINK THAT SAYS IT IS UP AND IS NOT.
            #
            # wlan.isconnected() reports the ASSOCIATION, not whether traffic
            # moves. A board that kept its association across a soft reboot
            # while the hotspot changed subnet underneath it answers True
            # here forever, so wifi_connect() above returns instantly and
            # nothing ever re-associates. The symptom is what we spent the
            # evening on: ECONNABORTED on every connect and every discovery
            # listen timing out, together, indefinitely.
            #
            # Nothing below can tell that state from a server that is simply
            # down, so this does not try. After enough consecutive failures
            # it drops the association and lets wifi_connect() build a fresh
            # one. If the server really was down, the cost is one extra
            # reconnect; if the link was stale, this is the only thing that
            # recovers it without a power cycle.
            failures += 1
            if failures >= WIFI_RESET_AFTER:
                log("%d failed attempts - re-associating wifi" % failures)
                failures = 0
                try:
                    wlan = network.WLAN(network.STA_IF)
                    wlan.disconnect()
                    wlan.active(False)
                    time.sleep(1)
                    wlan.active(True)
                except Exception as e:
                    log("could not reset wifi:", e)
                continue              # wifi_connect() at the top rebuilds it

            log("retrying in %ds" % settings.RECONNECT_DELAY)
            time.sleep(settings.RECONNECT_DELAY)


class FrameReader:
    """
    Accumulates bytes from a socket that times out instead of blocking, and
    hands back one complete server->board frame at a time.

    This used to be read_trigger(), which called a blocking read_exactly()
    and simply never returned until a full trigger arrived. That doesn't
    work any more: the board also has to get back to the top of the loop
    on its own, with no server message pending, to poll the modem for new
    inbound SMS (see poll_inbound_sms()). So the socket in main() is given
    a short timeout instead, and this class is what lets a message that
    takes more than one recv() to arrive survive across ticks - the same
    "TCP is a stream, not a message queue" fact test_wifi_uplink.py checks
    for on the server side applies here too.

    Wire formats:

        0x01  <len:1>  <message utf-8, len bytes>              TRIGGER
        0x04  <num_len:1> <number>  <text_len:1> <text utf-8>  SMS_OUT

    Length-prefixed rather than newline-delimited on purpose: the board
    never has to guess how long to wait for the rest of a message, so a
    half-received frame cannot be mistaken for a complete one.
    """

    def __init__(self):
        self._buf = b""

    def feed(self, sock):
        """
        One non-blocking-ish read. Returns:

            "closed"    the peer went away (recv returned nothing)
            None        no complete frame yet - a timeout, or a real
                        frame still waiting on more bytes
            a dict      one complete frame, freshly popped off the buffer

        A dict result has "op" plus TRIGGER_BYTE's "message", or SMS_OUT's
        "number" and "text".
        """
        try:
            chunk = sock.recv(256)
        except OSError as e:
            if e.args and e.args[0] in _NO_DATA_ERRNOS:
                # No NEW bytes this tick - but do not just return None. Two
                # frames can arrive in one TCP packet (a trigger right after
                # an SMS reply, say); _try_parse() only ever pops ONE frame
                # per call, so the second one is left sitting in self._buf
                # for a "next time" that never comes if the server has
                # nothing further to send. Parsing the existing buffer here
                # is what lets it come out on the very next tick instead of
                # waiting on more network traffic that may not arrive.
                return self._try_parse()
            raise                            # a real socket failure

        if not chunk:
            return "closed"

        self._buf += chunk
        return self._try_parse()

    def _try_parse(self):
        if len(self._buf) < 1:
            return None

        op = self._buf[0]

        if op == TRIGGER_BYTE:
            if len(self._buf) < 2:
                return None
            length = self._buf[1]
            if len(self._buf) < 2 + length:
                return None
            body = self._buf[2:2 + length]
            self._buf = self._buf[2 + length:]
            try:
                message = body.decode("utf-8") if body else ""
            except Exception:
                message = ""
            return {"op": TRIGGER_BYTE, "message": message or settings.DEFAULT_MESSAGE}

        if op == SMS_OUT:
            if len(self._buf) < 2:
                return None
            n_len = self._buf[1]
            if len(self._buf) < 3 + n_len:
                return None
            number = self._buf[2:2 + n_len]
            t_len = self._buf[2 + n_len]
            total = 3 + n_len + t_len
            if len(self._buf) < total:
                return None
            text = self._buf[3 + n_len:total]
            self._buf = self._buf[total:]
            try:
                number = number.decode("utf-8")
            except Exception:
                number = ""
            try:
                text = text.decode("utf-8")
            except Exception:
                text = ""
            return {"op": SMS_OUT, "number": number, "text": text}

        # An opcode neither side should send here. Drop just this one byte
        # and keep scanning rather than getting stuck on it forever - the
        # rest of the buffer may still be a perfectly good frame.
        log("ignoring unknown opcode", op)
        self._buf = self._buf[1:]
        return self._try_parse()


# ---------------------------------------------------------------- the alert

def build_modem():
    """Bring the modem up. Returns a live Modem, or None if it never came up."""
    try:
        modem = Modem(uart_id=settings.MODEM_UART_ID,
                        tx=settings.MODEM_TX,
                        rx=settings.MODEM_RX,
                        rst=settings.MODEM_RST,
                        baud=settings.MODEM_BAUD,
                        smsc=getattr(settings, "SMSC", None))
        # Fallback is 2 (LTE), matching settings.py. It used to be 13 (GSM
        # only), from when calls were in scope - so a settings.py without the
        # key would silently put the radio on 2G, where a transmit burst
        # browns this supply out and SMS fails on every attempt.
        modem.begin(network_mode=getattr(settings, "NETWORK_MODE", 2))
    except ModemError as e:
        log("modem did not come up:", e)
        log("almost always power - it needs its OWN 5V/2A supply")
        status("no_modem")
        return None

    if not modem.wait_network():
        log("no network registration - SIM? antenna? coverage?")
        # Return it anyway: registration can arrive later, and a modem that
        # is merely unregistered right now is still worth keeping hold of.
    return modem


def on_dtmf(digit):
    """Digits the escalation does not handle itself land here.

    DTMF_ACK and DTMF_ESCALATE are dealt with inside emergency_alert(). This
    leaves DTMF_ASK, which is the hook for asking Genie something over the call
    -- that needs the server, the intent classifier and a fresh Piper render,
    so it is not wired up yet.
    """
    if digit == settings.DTMF_ASK:
        log("digit %s pressed - question window not implemented yet" % digit)
    else:
        log("unhandled digit:", digit)


def run_alert(modem, sock, message):
    """One complete escalation. Never raises.

    Tells the server the instant a call is answered, so GENIE can announce in
    the ROOM that help has been called. The person who answered hears nothing -
    the modem has no microphone - but anyone present does, and that is worth
    saying out loud during a fire.
    """
    log("ALERT:", message)
    status("alert")

    if modem is None:
        log("no modem - cannot alert")
        status("no_modem")
        return

    def on_answered():
        # Fire-and-forget. The call and the SMS are the alert; a local
        # announcement is a bonus, and losing it must never break either.
        try:
            sock.send(bytes([EV_ANSWERED]))
            log("answered - server will announce in the room")
        except OSError as e:
            log("could not reach the server to announce:", e)

    try:
        result = modem.emergency_alert(
            settings.EMERGENCY_CONTACTS,
            message,
            on_answered=on_answered,
            call_timeout=settings.CALL_TIMEOUT,
            hold_seconds=settings.HOLD_SECONDS,
            ack_window=settings.ACK_WINDOW,
            ack_poll=settings.ACK_POLL,
            dtmf_ack=settings.DTMF_ACK,
            dtmf_escalate=settings.DTMF_ESCALATE,
            dtmf_ask=settings.DTMF_ASK,
            on_dtmf=on_dtmf,
            calls_enabled=getattr(settings, "CALLS_ENABLED", True),
        )
        log("outcome:", result.get("outcome"))
    except Exception as e:
        log("alert blew up:", e)
        try:
            modem.hangup()
        except Exception:
            pass
    finally:
        # Back to green whatever happened. In a finally because the blue
        # "alert running" light must not outlive the alert - a board stuck
        # showing blue would say an alert is in progress forever, which is a
        # worse lie than showing nothing at all. The socket is still up: this
        # function is only ever reached from a live server frame.
        status("ready")


def handle_frame(modem, sock, frame):
    """Dispatch one complete frame from FrameReader. Never raises."""
    try:
        if frame["op"] == TRIGGER_BYTE:
            run_alert(modem, sock, frame["message"])
        elif frame["op"] == SMS_OUT:
            send_sms_reply(modem, frame["number"], frame["text"])
    except Exception as e:
        log("handling a server frame blew up:", e)


def send_sms_reply(modem, number, text):
    """Send exactly this text to exactly this number. No escalation, no
    ack window, no call - that machinery belongs to run_alert() alone.
    This is the reply to a command someone texted in: they asked a
    question, the server answered it, this says the answer back."""
    if modem is None:
        log("SMS_OUT for %s but no modem - dropping" % number)
        return
    if not number or not text:
        log("SMS_OUT with an empty number or text - ignoring")
        return
    try:
        ok = modem.send_sms_retry(number, text)
        log("SMS reply to %s: %s" % (number, "sent" if ok else "FAILED"))
    except Exception as e:
        log("SMS reply blew up:", e)


def poll_inbound_sms(modem, sock):
    """
    Forward any SMS that landed on the SIM outside of an alert's ack
    window, as a command for the server to run through AI inference.

    CALL THIS ONLY FROM main()'s IDLE LOOP. Never from inside run_alert():
    see the module docstring and read_unread_sms()'s own warning for why -
    both this and the ack window read "REC UNREAD", and whichever asks
    first marks the messages read for the other. main() below only ever
    reaches this line between frames, after run_alert() (if any) has
    already returned, so the two cannot overlap as long as nothing calls
    this from a second thread or timer.
    """
    if modem is None:
        return

    # Delivery reports first, and only logged - nothing acts on them. They
    # exist to settle one question the send path cannot: "+CMGS: 23" proves
    # the service centre accepted the message, not that a handset received
    # it. A +CDS with status 00 means it actually arrived.
    try:
        for report in modem.read_delivery_reports():
            log("DELIVERY REPORT:", report)
    except Exception as e:
        log("could not read delivery reports:", e)

    try:
        messages = modem.read_unread_sms()
    except Exception as e:
        log("could not check for new SMS:", e)
        return

    for _index, sender, text in messages:
        text = text.strip()
        if not text:
            continue

        log("SMS from %s: %s" % (sender, text))

        sender_b = sender.encode("utf-8")[:255]
        text_b = text.encode("utf-8")[:255]
        packet = (bytes([SMS_IN])
                  + bytes([len(sender_b)]) + sender_b
                  + bytes([len(text_b)]) + text_b)

        try:
            sock.sendall(packet)
        except OSError as e:
            # Best-effort, same trade-off as EV_ANSWERED: the message is
            # already gone from the modem's store (read_unread_sms()
            # deletes by default), so a dropped socket here loses the
            # command rather than queuing it for the next reconnect. A
            # missed "what's the temperature" is not the failure mode
            # this board exists to prevent; a missed emergency call is,
            # and that path never touches this function.
            log("could not forward SMS to server:", e)


# ---------------------------------------------------------------- entry point

def main():
    log("booting")
    status("boot")

    wifi_connect()

    # The SERVER LINK COMES FIRST, before the modem is touched.
    #
    # Bringing the modem up can take a minute - a 2G scan is slow, and a
    # module that reboots on the way takes longer still. Doing that before
    # connecting meant the server had no board for that whole window, so any
    # trigger in it failed with "modem ESP not connected" even though
    # everything was working and simply not ready yet.
    #
    # Connecting first costs nothing: build_modem() runs immediately
    # afterwards, and a trigger arriving in the gap is handled by the
    # re-check in the loop below rather than being lost.
    modem = None

    while True:
        sock = server_connect()

        # A short timeout, not the old indefinite block: this loop has to
        # come back around on its own, with no server frame pending, so it
        # can also check the modem for new inbound SMS. See FrameReader
        # and poll_inbound_sms().
        sock.settimeout(settings.SMS_POLL_INTERVAL)
        reader = FrameReader()

        if modem is None:
            modem = build_modem()

        try:
            while True:
                frame = reader.feed(sock)

                if frame == "closed":
                    log("server closed the connection")
                    break

                if frame is not None:
                    # Re-check the modem here rather than at boot only: it
                    # may have come up after this board did, or been
                    # power-cycled since.
                    if modem is None:
                        modem = build_modem()
                    handle_frame(modem, sock, frame)

                # Whether or not a server frame arrived this tick, this is
                # also the moment to check for new SMS - never from inside
                # handle_frame()/run_alert(), which owns the ack window's
                # own view of "REC UNREAD". By the time control reaches
                # here any run_alert() call above has already returned, so
                # the two cannot race.
                poll_inbound_sms(modem, sock)

        except OSError as e:
            log("socket error:", e)
        finally:
            try:
                sock.close()
            except Exception:
                pass

        time.sleep(settings.RECONNECT_DELAY)


# MicroPython runs main.py with __name__ == "__main__", so the board still
# starts on boot. The guard exists so the protocol parsing above can be
# imported and tested from CPython without the board running its main loop.
if __name__ == "__main__":
    main()
