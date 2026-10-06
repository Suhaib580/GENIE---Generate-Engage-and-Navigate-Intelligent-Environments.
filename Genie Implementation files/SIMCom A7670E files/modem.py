"""
Modem driver for GENIE's emergency SMS + voice-call path.

Written for the SIMCom A7670E (LTE Cat-1) on a BK-7670 carrier, which is what
GENIE actually uses. The SIM800L (2G) is still supported because the SMS and
call commands are identical on both - where they differ (registration, mic
gain, audio commands) the code tries each and keeps whichever answers.

Runs on MicroPython (ESP32-S3, machine.UART) and on CPython (pyserial),
so the same logic can be bench-tested from the laptop before it goes on the board.

Wiring (see docs/emergency_call_audio.md): UART1 TX=17 -> modem RXD,
RX=18 <- modem TXD.

TWO THINGS THAT ARE NOT OPTIONAL READING
----------------------------------------

1. THE RADIO IS PINNED TO 2G. begin() sends AT+CNMP=13, and that is what makes
   voice calls work at all. This SIM has no VoLTE, so on LTE every call must
   fall back to 2G mid-dial - and on this hardware that handover killed the
   call every single time, either rebooting the module (*ATREADY: 1 about six
   seconds after ATD) or drawing "network not allowed - emergency calls only".
   Forced to GSM only, the first call rang for 27 seconds. Nothing is lost:
   SMS is native to 2G, and the alert path uses no cellular data.
   See set_network_mode().

2. POWER. The A7670E pulls ~2 A bursts on 2G transmit. It needs its own supply
   wired DIRECT to the supply terminals - never through breadboard rails - with
   a 470 uF+ electrolytic across V and G at the board. Without it the module
   reboots mid-call and every symptom looks like something else.

The class was called SIM800L when this only drove a 2G module. Both old names
still work as aliases so nothing breaks, but new code should use Modem.
"""

import time

try:                                    # ---- MicroPython ----
    from machine import UART, Pin
    _MICROPYTHON = True
except ImportError:                     # ---- CPython / pyserial ----
    import serial
    _MICROPYTHON = False


def _ms():
    return time.ticks_ms() if _MICROPYTHON else int(time.monotonic() * 1000)


def _elapsed(start):
    return time.ticks_diff(_ms(), start) if _MICROPYTHON else _ms() - start


def _sleep_ms(ms):
    time.sleep(ms / 1000.0)


def _decode(data):
    """Turn UART bytes into text WITHOUT ever raising.

    CPython's bytes.decode("utf-8", "ignore") drops invalid bytes quietly.
    MicroPython's does not honour the errors argument the same way, so a
    single stray byte raises UnicodeError - and that propagated straight out
    of send_sms(), turning a recoverable line-noise glitch into
    "SMS to +92... blew up: UnicodeError()" with the message never sent.

    A modem emits exactly that kind of garbage while it reboots: partial
    frames at the wrong baud, high-bit bytes, fragments of a boot banner.
    Every one of those moments is when the driver most needs to keep
    working, so this filters to printable ASCII by hand rather than trusting
    the decoder. AT responses are ASCII by definition - nothing real is lost.
    """
    if not data:
        return ""
    try:
        return bytes(b for b in data if b < 128).decode("ascii")
    except Exception:
        # Belt and braces: even the filter above should not be able to fail,
        # but this function must NEVER be the thing that breaks an alert.
        return ""


# call_state() return values that are not a real call state.
#
# Kept distinct because merging them is a real bug: "the modem says there is
# no call" and "the modem did not answer" demand opposite responses. The first
# means stop; the second means wait, because the module goes deaf on the UART
# during a CSFB handover.
CALL_NONE = -1        # answered, no call in progress
CALL_UNKNOWN = None   # no reply at all

# Seconds to wait after registration before using the network.
#
# On 2G the attach finishes noticeably before the SMS service is usable.
# Sending into that window returns "+CMS ERROR: Network timeout", which looks
# like poor coverage and is not - the same message sends first try a few
# seconds later.
SETTLE_AFTER_REGISTER = 6


def numbers_match(a, b):
    """
    True if two phone numbers refer to the same subscriber.

    The modem is inconsistent about how it reports a caller: "+923001234567",
    "03001234567" and "923001234567" all turn up depending on whether the
    number came from the SIM's own records, the network, or an SMS header.
    Comparing them as strings misses acknowledgements from the right person.

    Comparing the last 9 digits is enough to identify a subscriber inside one
    country without needing to know the dialling plan.
    """
    if not a or not b:
        return False
    da = "".join(c for c in str(a) if c.isdigit())
    db = "".join(c for c in str(b) if c.isdigit())
    if not da or not db:
        return False
    return da[-9:] == db[-9:]


class ModemError(Exception):
    pass


class Modem:

    # Class-level default so the attribute always exists, however the object
    # was built. ensure_ready() is called from send_sms() and the escalation,
    # and neither should be able to raise AttributeError on a Modem that was
    # constructed unusually - a test double, or a subclass that overrides
    # __init__ without chaining.
    _rebooted = False

    smsc = None          # None = use whatever the SIM carries; see set_smsc()
    delivery_reports = True   # ask the network to confirm actual delivery

    def __init__(self, uart_id=1, tx=17, rx=18, rst=16, baud=115200,
                 port=None, debug=True, smsc=None, delivery_reports=True):
        self.debug = debug
        self.smsc = smsc
        self.delivery_reports = delivery_reports
        self._rst = None

        # Unsolicited result codes (URCs) arrive whenever the modem feels
        # like it, NOT in response to a command -- +DTMF when the responder
        # presses a key, +CMTI when a text lands, RING, NO CARRIER.
        #
        # flush() and expect() both consume the receive buffer, so anything
        # that arrived between two polls used to be thrown away. During the
        # hold phase we poll AT+CLCC once a second, which means a keypress
        # landing in that gap was simply lost.
        #
        # So every byte read from the UART is ALSO appended here, and the
        # URC readers scan this instead of the live buffer. Capped, because
        # a long call would otherwise grow it without bound.
        self._urc = ""
        self._urc_cap = 2000

        # Which DTMF spelling this firmware accepted (see enable_dtmf).
        self._dtmf_cmd = None

        # Why the last dial was refused, if it was. See cs_refused().
        self.last_dial_error = ""

        # Set when a reboot banner is seen on the UART. ensure_ready()
        # re-applies the lost configuration and clears it.
        self._rebooted = False
        if _MICROPYTHON:
            self.uart = UART(uart_id, baudrate=baud, tx=tx, rx=rx,
                             timeout=200, timeout_char=50)
            if rst is not None:
                self._rst = Pin(rst, Pin.OUT, value=1)
        else:
            if port is None:
                raise ModemError("on CPython you must pass port='COM5' (or /dev/ttyUSB0)")
            self.uart = serial.Serial(port, baud, timeout=0.2)

    # ------------------------------------------------------------------ low level

    def _log(self, *a):
        if self.debug:
            print("[modem]", *a)

    def _write(self, data):
        if isinstance(data, str):
            data = data.encode()
        self.uart.write(data)

    def _read_any(self):
        if _MICROPYTHON:
            n = self.uart.any()
            data = self.uart.read(n) if n else b""
        else:
            n = self.uart.in_waiting
            data = self.uart.read(n) if n else b""

        if data:
            # Mirror into the URC buffer before anyone gets to consume it.
            text = _decode(data)
            self._urc += text

            # A reboot mid-session wipes EVERY setting begin() applied. The
            # module comes back in PDU mode with no message store, and the
            # driver carried on regardless - so the next SMS drew
            # "+CMS ERROR: Invalid PDU mode parameter" and the next dial drew
            # "+CME ERROR: memory failure", neither of which names the actual
            # cause. Notice it here and let ensure_ready() put the settings
            # back before the next thing that needs them.
            if "ATREADY" in text or "+CPIN: READY" in text:
                self._rebooted = True
            if len(self._urc) > self._urc_cap:
                self._urc = self._urc[-self._urc_cap:]

        return data

    def flush(self):
        """Drop anything sitting in the receive buffer (stale URCs, boot spam)."""
        start = _ms()
        while _elapsed(start) < 120:
            self._read_any()
            _sleep_ms(20)

    def expect(self, tokens, timeout=3000,
               error_tokens=("ERROR", "+CME ERROR", "+CMS ERROR")):
        """
        Accumulate bytes until one of `tokens` appears. Returns (matched, full_text).
        `matched` is None on timeout, or the error token if the module rejected us.
        """
        if isinstance(tokens, str):
            tokens = (tokens,)
        buf = b""
        start = _ms()
        while _elapsed(start) < timeout:
            chunk = self._read_any()
            if chunk:
                buf += chunk
                text = _decode(buf)
                for t in tokens:
                    if t in text:
                        return t, text
                for e in error_tokens:
                    if e in text:
                        return e, text
                continue
            _sleep_ms(10)
        return None, _decode(buf)

    def cmd(self, at, expect="OK", timeout=3000, retries=1):
        """Send one AT command. Returns the response text, raises on failure.

        `expect` may be a string or a tuple of strings, any one of which counts
        as success. That matters more than it sounds: a URC arriving mid-command
        delays the trailing OK, and waiting only for the literal "OK" then
        declares a command failed when its answer is sitting right there in the
        buffer. Observed:

            AT+CPMS="ME","ME","ME"
            <- SMS DONE | +CPMS: 1,180,1,180,1,180

        - a perfectly good reply that got logged as "not supported" because the
        OK landed after the timeout, behind an unrelated "SMS DONE".

        So callers whose answer has a recognisable prefix should pass it:
        expect=("OK", "+CPMS:").
        """
        tokens = (expect,) if isinstance(expect, str) else tuple(expect)

        text = ""
        for attempt in range(retries + 1):
            self.flush()
            self._log("->", at)
            self._write(at + "\r")
            matched, text = self.expect(tokens, timeout=timeout)
            self._log("<-", text.strip().replace("\r\n", " | "))
            if matched in tokens:
                return text
            if attempt < retries:
                _sleep_ms(300)
        raise ModemError("command failed: %s -> %r" % (at, text.strip()))

    # ------------------------------------------------------------------ lifecycle

    def hard_reset(self):
        if self._rst is None:
            self._log("no RST pin configured, skipping hard reset")
            return
        self._log("hard reset")
        self._rst.value(0)
        _sleep_ms(150)
        self._rst.value(1)
        time.sleep(5)                    # module needs ~3-5 s to come back up

    # AT+CNMP network mode. 13 = GSM only, and it is not a compromise here -
    # it is what makes voice calls work at all. See set_network_mode().
    NETWORK_MODE_GSM_ONLY = 13
    NETWORK_MODE_AUTO = 2

    def set_network_mode(self, mode, settle=8):
        """Pin the module to one radio technology. Returns True if it took.

        WHY THIS EXISTS, AND WHY THE DEFAULT IS GSM ONLY
        ------------------------------------------------
        This SIM has no VoLTE (`+CIREG: 1,0,0`), so on LTE every voice call has
        to fall back to 2G - CSFB. On this hardware that handover is where
        everything died: the module either rebooted mid-transition
        (`*ATREADY: 1`, roughly six seconds after ATD, every single time) or
        the network refused the call outright with
        "network not allowed - emergency calls only".

        Measured 2026-08-26: on LTE with automatic mode, the call NEVER
        reached the alerting state across a dozen attempts. Forced to GSM only
        with AT+CNMP=13, the very first call rang for 27 seconds with no reboot
        and no refusal.

        Nothing here is given up that matters. SMS is native to 2G. Data drops
        to GPRS/EDGE, and the alert path does not use cellular data at all -
        triggers arrive over Wi-Fi.

        CHANGING THE MODE REBOOTS THE MODULE. Observed: *ATREADY: 1 arriving
        as the reply to whatever command came next, then "(U)SIM busy" on the
        one after that, then several minutes of +CREG: 0,2 never resolving -
        because every command after the switch landed on a module that was
        still booting with its SIM not yet initialised.

        So two things happen here. First, the current mode is READ, and if it
        already matches nothing is sent at all - which means only the very
        first boot after a change pays the cost, and every run after that is
        free. Second, when a change IS needed, this waits for the module to
        answer AT again AND report the SIM ready before returning, rather than
        sleeping a fixed guess.
        """
        # Already there? Do not touch it - setting the same value still reboots.
        try:
            current = self.cmd("AT+CNMP?", timeout=3000, retries=0)
            if ("+CNMP: %d" % mode) in current:
                self._log("already on network mode %d" % mode)
                return True
        except ModemError:
            pass                            # cannot read it; just set it

        if not self._try("AT+CNMP=%d" % mode, timeout=6000):
            self._log("could not set network mode %d - carrying on" % mode)
            return False

        self._log("network mode %d set - the module reboots, waiting" % mode)
        return self.wait_ready(settle)

    def wait_ready(self, timeout=30):
        """Block until the module answers AT and reports its SIM ready.

        Used after anything that restarts the radio. A fixed sleep is not
        enough: the module comes back on the UART noticeably before the SIM
        finishes initialising, so a command sent in that gap gets
        "+CMS ERROR: (U)SIM busy" and is silently lost.
        """
        start = _ms()

        while _elapsed(start) < timeout * 1000:
            try:
                self.cmd("AT", timeout=1000, retries=0)
            except ModemError:
                time.sleep(1)
                continue

            try:
                self.cmd("AT+CPIN?", expect="READY", timeout=3000, retries=0)
            except ModemError:
                time.sleep(1)               # answering, SIM not ready yet
                continue

            # Echo is off by default but a reboot restores it.
            self._try("ATE0", timeout=1000)
            self._log("module back up after %.0fs" % (_elapsed(start) / 1000.0))
            return True

        self._log("module did not come back within %ds" % timeout)
        return False

    def _configure_sms_store(self):
        """Point message reception at a store CMGL can actually read.

        Split out of begin() because a module reboot wipes both of these,
        and ensure_ready() has to be able to put them back without
        re-running the whole bring-up.
        """
        # ---- inbound SMS has to be STORED, or the ack window sees nothing ----
        #
        # wait_for_ack() finds replies with AT+CMGL="REC UNREAD", which lists
        # messages held in the modem's memory. Two settings decide whether an
        # arriving message ever gets there, and neither has a safe default:
        #
        #   +CPMS  picks which memory is read, written and received into. If
        #          reception goes to a store CMGL is not reading, the message
        #          exists and is invisible.
        #   +CNMI  decides what happens to a new message. With <mt>=2 the modem
        #          hands it straight to us as a +CMT URC and NEVER STORES IT -
        #          so CMGL returns a bare OK forever and the acknowledgement
        #          path silently never works. <mt>=1 stores it and tells us
        #          where, which is what we want.
        #
        # Preferences are tried in order and failures ignored: module memory
        # first (bigger, survives a SIM swap), then the SIM, then whatever the
        # firmware already had. A modem that will not take any of them still
        # sends alerts - it just cannot notice a reply.
        # A SILENT reply here is worth noticing. AT+CPMS answered
        # "+CPMS: 0,180,..." happily on one run and returned nothing at all on
        # the next - and the run where it went quiet was the run where the
        # module went on to reboot mid-scan and never register. An empty reply
        # from a command that normally answers means the module is already
        # unhealthy, and that is worth saying out loud rather than logging as
        # an unremarkable "not supported".
        stored = False
        for pms in ('AT+CPMS="ME","ME","ME"', 'AT+CPMS="SM","SM","SM"'):
            if self._try(pms, timeout=5000, expect=("OK", "+CPMS:")):
                stored = True
                break

        if not stored:
            self._log("WARNING: no message store configured. Either this "
                      "firmware lacks +CPMS, or the module is not answering - "
                      "check for *ATREADY in the log above. Replies to the "
                      "alert SMS may not be visible to the ack window.")

        # ---- delivery reports ------------------------------------------
        #
        # "+CMGS: 23" means the SERVICE CENTRE took the message. It says
        # nothing about whether a handset ever got it, and the gap between
        # those two facts is where a whole evening can go: the log reads
        # "sent", the phone stays empty, and there is nothing to argue with.
        #
        # A status report closes it. +CSMP's first octet asks the network to
        # report back (bit 5, hence 49 rather than the default 17), and CNMI
        # <ds>=1 routes the resulting +CDS straight to us instead of filing it
        # somewhere CMGL does not look.
        #
        # Both are best-effort. A network that will not do status reports still
        # delivers messages perfectly well - it just cannot prove it.
        if self.delivery_reports:
            self._try("AT+CSMP=49,167,0,0")     # 49 = SUBMIT + SRR, 167 = 24h
            ds = "1"
        else:
            ds = "0"

        # 2,1,0,<ds>,0 = URCs enabled, store-and-notify, no CBM, status
        # reports as configured, do not flush the buffer on mode change.
        if not self._try("AT+CNMI=2,1,0,%s,0" % ds):
            self._try("AT+CNMI=1,1,0,%s,0" % ds)

    def read_delivery_reports(self):
        """Pull any +CDS status reports out of the URC mirror.

        Returns a list of raw report bodies, newest last. Empty is the normal
        case - reports arrive seconds to minutes after the send, so this is
        meant to be polled, not waited on.
        """
        reports = []
        if "+CDS:" not in self._urc:
            return reports

        for chunk in self._urc.split("+CDS:")[1:]:
            body = chunk.split("\n")[0].strip()
            if body:
                reports.append(body)

        # Consume them, so the same report is not reported twice.
        self._urc = self._urc.rsplit("+CDS:", 1)[0]
        return reports


    def _configure(self):
        """Apply the settings a reboot would wipe. Safe to call again."""
        self.cmd("AT+CMGF=1")            # SMS text mode
        self.cmd('AT+CSCS="GSM"')
        self.cmd("AT+CLIP=1")            # caller ID on incoming
        self._configure_sms_store()
        if self.smsc:
            self.set_smsc(self.smsc)
        self._rebooted = False

    def set_smsc(self, number):
        """Override the service centre address the SIM came with.

        Normally the SIM carries the right one and this should be left alone.
        It matters when the SIM has been moved between networks, or was
        provisioned with an SMSC that its current operator does not accept -
        the send then fails with a network timeout or a bare "unknown error",
        which looks like a radio fault and is not one.

        Type 145 means the number is international (leading +); 129 means it
        is not. Getting this wrong is its own silent failure, so it is derived
        from the number rather than left to the caller.
        """
        kind = 145 if str(number).startswith("+") else 129
        try:
            self.cmd('AT+CSCA="%s",%d' % (number, kind), timeout=4000)
            self._log("SMS centre set to %s,%d" % (number, kind))
            return True
        except ModemError as e:
            self._log("could not set SMS centre: %r" % e)
            return False

    def ensure_ready(self):
        """Re-apply configuration if the module rebooted since last time.

        Call before anything that depends on begin()'s settings - sending an
        SMS, listing messages, dialling. Costs three AT commands when nothing
        happened, and saves an alert when something did: a module that reset
        mid-session is back in PDU mode with no message store, and every
        symptom after that points somewhere else entirely.
        """
        if not self._rebooted:
            return True

        self._log("module rebooted - re-applying configuration")
        try:
            self.cmd("AT", timeout=2000, retries=3)
            self.cmd("ATE0")
            self.cmd("AT+CMEE=2")
            self.cmd("AT+CPIN?", expect="READY", timeout=15000, retries=2)
            self._configure()
            return True
        except ModemError as e:
            self._log("could not reconfigure after reboot: %s" % e)
            return False

    def begin(self, tries=3, network_mode=NETWORK_MODE_GSM_ONLY):
        """Sync up, quiet the echo, verify the SIM, put SMS in text mode.

        network_mode defaults to GSM only because that is the only mode in
        which voice calls work on this hardware - see set_network_mode() for
        the measurements. Pass None to leave whatever the module already had.
        """
        for attempt in range(tries):
            try:
                self.cmd("AT", timeout=1000, retries=3)
                break
            except ModemError:
                self._log("no response to AT (attempt %d) - check the 4V rail" % (attempt + 1))
                self.hard_reset()
        else:
            raise ModemError("module never answered AT; almost always a power problem")

        self.cmd("ATE0")                 # echo off, so parsing stays sane
        self.cmd("AT+CMEE=2")            # verbose errors

        # The radio mode goes FIRST, before anything else is configured.
        #
        # Changing it reboots the module, and a reboot wipes every setting
        # applied before it. Do this last and the SMS mode, character set and
        # message storage are all silently lost - which is exactly what
        # happened: AT+CPMS came back as *ATREADY: 1, the retry hit
        # "(U)SIM busy", and the module then sat at +CREG: 0,2 forever.
        #
        # set_network_mode() skips the write entirely when the mode is already
        # correct, so only the first boot after a change pays for a reboot.
        if network_mode is not None:
            self.set_network_mode(network_mode)

        # 15 s, not 5: after a mode change the module answers AT well before
        # its SIM has finished initialising.
        self.cmd("AT+CPIN?", expect="READY", timeout=15000, retries=3)
        self._configure()

        self._log("ready")

    SYSTEM_MODES = {"0": "no service", "1": "GSM", "2": "GPRS", "3": "EDGE",
                    "4": "WCDMA", "5": "HSDPA", "6": "HSUPA", "7": "HSPA",
                    "8": "LTE"}

    def system_mode(self):
        """Which radio technology the module is actually camped on right now.

        Worth logging on every registration, because it is the difference
        between a call that rings and one that never leaves the dialling state.
        Anything in the GSM/GPRS/EDGE family is good; LTE means voice will fail.
        """
        try:
            text = self.cmd("AT+CNSMOD?", timeout=2000, retries=0)
            code = text.split("+CNSMOD:")[1].split(",")[1].strip()[0]
        except (ModemError, IndexError, ValueError):
            return "unknown"
        return self.SYSTEM_MODES.get(code, "mode %s" % code)

    def signal(self):
        """Returns RSSI 0-31, or None if unknown (99)."""
        text = self.cmd("AT+CSQ")
        try:
            rssi = int(text.split("+CSQ:")[1].split(",")[0])
        except (IndexError, ValueError):
            return None
        return None if rssi == 99 else rssi

    def wait_network(self, timeout=90, retry_with_reattach=True):
        """
        Block until the modem is registered. Returns True/False.

        Checks +CEREG (LTE/EPS) as well as +CREG (2G circuit-switched), because an
        A7670E on LTE reports registration through CEREG and can sit at CREG stat 0
        forever while being perfectly online.

        ON TIMEOUT IT RESTARTS THE RADIO AND TRIES ONCE MORE, and that is not
        belt-and-braces - it is load-bearing.

        The module can get stuck searching: +CREG cycling 0,0 -> 0,2 -> 0,0
        indefinitely, sometimes rebooting mid-scan, never landing on a cell.
        A CFUN=0/CFUN=1 cycle gives it a clean radio state and it attaches
        normally.

        This used to happen by accident. begin() sent AT+CNMP=13 on every boot,
        which reboots the module, and that reboot was what cleared the stuck
        search. Making set_network_mode() skip the write when the mode is
        already correct removed the reboot - and with it the accidental
        recovery, which is how a stuck scan turned into a hard failure. The
        recovery is now explicit and only pays for itself when it is needed.
        """
        if self._wait_registered(timeout):
            return True

        if not retry_with_reattach:
            self._log("registration TIMED OUT - antenna? coverage? SIM?")
            return False

        self._log("stuck searching - restarting the radio and trying again")
        self.reattach(timeout=10)           # its own wait is short; ours follows

        if self._wait_registered(timeout):
            return True

        self._log("registration TIMED OUT even after a restart"
                  " - antenna? coverage? SIM?")
        return False

    def _wait_registered(self, timeout):
        """Poll CEREG and CREG until one reports registered, or time runs out."""
        start = _ms()
        while _elapsed(start) < timeout * 1000:
            for query, tag in (("AT+CEREG?", "+CEREG:"), ("AT+CREG?", "+CREG:")):
                try:
                    text = self.cmd(query, timeout=2000, retries=0)
                    stat = int(text.split(tag)[1].split(",")[1].strip()[0])
                except (ModemError, IndexError, ValueError):
                    continue
                if stat in (1, 5):                  # home or roaming
                    self._log("registered via %s (stat=%d, csq=%s) on %s"
                              % (tag.strip(":+"), stat, self.signal(),
                                 self.system_mode()))

                    # Registered is not the same as ready to send.
                    #
                    # On 2G the attach completes several seconds before the
                    # SMS service is usable, and sending into that gap returns
                    # "+CMS ERROR: Network timeout" - which reads like a
                    # coverage problem and is really just impatience. Observed
                    # directly: an SMS fired immediately after CREG went to 1
                    # failed three times; the same message on a settled
                    # registration went first try.
                    #
                    # The service centre address is the thing that has to be
                    # there. Log it, because an empty +CSCA is the one failure
                    # this delay cannot fix.
                    time.sleep(SETTLE_AFTER_REGISTER)

                    # +CSCA can answer "+CMS ERROR: unknown error" for a few
                    # seconds after registration and then report a perfectly
                    # good service centre. Warning on the first failure cried
                    # wolf about an SMS path that was about to work, so try a
                    # few times before saying anything.
                    for attempt in range(3):
                        try:
                            reply = self.cmd("AT+CSCA?", timeout=4000, retries=0)
                            smsc = reply.split("+CSCA:")[1].split("|")[0].strip()
                            self._log("SMS centre: %s" % smsc)
                            break
                        except (ModemError, IndexError):
                            if attempt < 2:
                                time.sleep(3)
                    else:
                        self._log("WARNING: could not read the SMS service "
                                  "centre after 3 tries. If sending fails with "
                                  "a network timeout, that is why.")

                    return True
                self._log("%s stat=%d, not yet" % (tag.strip(":+"), stat))
            time.sleep(2)
        return False

    # ------------------------------------------------------------------ SMS

    def send_sms(self, number, text, timeout=60000):
        """Send one text message. Returns True on a confirmed +CMGS receipt."""
        # A reboot since the last send would have dropped text mode, and the
        # symptom is "+CMS ERROR: Invalid PDU mode parameter" - which reads
        # like a coding mistake rather than a module that reset.
        self.ensure_ready()
        self.flush()
        self._log("SMS -> %s: %s" % (number, text))
        self._write('AT+CMGS="%s"\r' % number)
        matched, resp = self.expect(">", timeout=8000)
        if matched != ">":
            self._log("no '>' prompt, got %r" % resp.strip())
            self._write(b"\x1b")                     # ESC out of a half-open prompt
            return False
        self._write(text)
        self._write(b"\x1a")                         # Ctrl+Z = send
        matched, resp = self.expect("+CMGS:", timeout=timeout)
        ok = matched == "+CMGS:"
        self._log("SMS %s (%s)" % ("sent" if ok else "FAILED", resp.strip()))
        return ok

    def send_sms_retry(self, number, text, attempts=3):
        for i in range(attempts):
            if self.send_sms(number, text):
                return True
            self._log("SMS retry %d/%d" % (i + 1, attempts))
            time.sleep(3)
        return False

    # ------------------------------------------------------------------ voice

    def _try(self, at, timeout=3000, expect="OK"):
        """Send a command whose absence is acceptable. True if it stuck.

        Used for everything that differs between the SIM800L and the A76xx
        families. Run tools/probe_modem.py to find out which of these your
        firmware actually has rather than guessing from the log.

        Pass `expect` as a tuple when the command answers with its own prefix -
        ("OK", "+CPMS:") and the like. A URC landing mid-command can delay the
        trailing OK past the timeout, and without the prefix a working command
        is reported as unsupported.
        """
        try:
            self.cmd(at, expect=expect, timeout=timeout, retries=0)
            return True
        except ModemError:
            self._log("%s not supported here - skipping" % at.split("=")[0])
            return False

    def set_audio(self, mic_gain=None, speaker=80):
        """
        Speaker volume is +CLVL on both families, so that always applies.

        Mic/uplink gain is spelled differently on each: +CMIC on the SIM800L,
        +CTXGAIN on the A76xx. Try both and keep whichever answers.

        None of this is fatal. With the PCM5102A feeding the mic pins, the
        level is set by the R2 trimpot in hardware and the WAV's amplitude in
        software - the modem's own gain is a nice-to-have on top.
        """
        self._try("AT+CLVL=%d" % speaker)

        if mic_gain is None:
            return

        # SIM800L takes a 0-15 channel gain; A76xx CTXGAIN is a much wider
        # scalar. Feed each the form it expects rather than one number.
        if not self._try("AT+CMIC=0,%d" % mic_gain):
            self._try("AT+CTXGAIN=%d" % int(mic_gain * 4096))

    # ------------------------------------------------------------------ DTMF

    def enable_dtmf(self):
        """
        Turn on DTMF detection so keypresses arrive as +DTMF: URCs.

        The two families spell this differently and neither manual agrees with
        the other, so try both and remember which one took. Returns True if
        either worked.

        If this returns False the responder cannot acknowledge by keypad and
        emergency_alert() falls back to timing out on its own.
        """
        for at in ("AT+DDET=1", "AT+CDTMFDET=1"):
            if self._try(at):
                self._dtmf_cmd = at.split("=")[0]
                self._log("DTMF detection on via %s" % self._dtmf_cmd)
                return True

        self._log("no DTMF detection on this firmware - keypad acks disabled")
        return False

    def read_dtmf(self):
        """
        Return any digits pressed since the last call, oldest first.

        Reads from the URC mirror rather than the live UART, so digits that
        landed while some other command was mid-flight are still seen. The
        scanned text is consumed, so each digit is reported exactly once.
        """
        self._read_any()                     # pull in anything waiting

        if "+DTMF" not in self._urc:
            return []

        digits = []
        for line in self._urc.replace("\r", "\n").split("\n"):
            if "+DTMF" not in line:
                continue
            # "+DTMF: 1" on SIM800L, "+CDTMFDET: 1" on some A76xx builds.
            tail = line.split(":", 1)[-1].strip()
            if tail:
                digits.append(tail[0])

        self._urc = ""                       # consumed
        if digits:
            self._log("DTMF pressed: %s" % ",".join(digits))
        return digits

    # ------------------------------------------------------------------ inbound SMS

    def unread_senders(self):
        """
        Numbers that have texted us and whose messages are still unread.

        Used for the acknowledgement window: if a contact replies to the alert
        SMS, they have plainly seen it and nobody needs to be rung.

        Returns a list of raw number strings as the modem reports them, which
        may or may not carry the +92 prefix - match with numbers_match().
        """
        self.ensure_ready()

        try:
            text = self.cmd('AT+CMGL="REC UNREAD"', timeout=8000, retries=0)
        except ModemError:
            return []

        senders = []
        for line in text.replace("\r", "\n").split("\n"):
            if "+CMGL:" not in line:
                continue

            # +CMGL: 1,"REC UNREAD","+923001234567",,"26/08/22,19:04:11+20"
            #
            # Everything before the first comma is the header, and it begins
            # "+CMGL" - which starts with a plus sign exactly like a phone
            # number does. Scanning the whole line therefore returned
            # "+CMGL: 1" as the sender and stopped, so a genuine reply never
            # matched a contact and the acknowledgement window could not fire.
            #
            # Drop the header, then take the first field that is essentially
            # all digits. The status field ("REC UNREAD") is quoted too, so
            # quoting alone does not identify the number.
            body = line.split(",", 1)[1] if "," in line else ""

            for part in body.split(","):
                part = part.strip().strip('"')
                digits = "".join(c for c in part if c.isdigit())
                if len(digits) >= 7 and len(digits) >= len(part) - 1:
                    senders.append(part)
                    break

        return senders

    def read_unread_sms(self, delete=True):
        """
        Every unread SMS sitting in the modem's store right now, as
        (index, sender, text) tuples.

        This exists for the SMS-as-command path (see modem_main.py's
        poll_inbound_sms()) - unlike unread_senders(), which only exists to
        ask "who replied" for the ack window, this also returns the message
        BODY and its store INDEX, because a command has to be read and a
        precise index is what lets the caller delete exactly this message
        rather than guessing.

        delete=True removes each message from the modem's store as soon as
        it is parsed here. The module holds a couple dozen messages at most,
        and once a message is copied into RAM there is nothing left for the
        module to usefully keep - leaving it there just fills the store over
        weeks of uptime. Pass False to leave the store alone.

        DO NOT call this while an emergency_alert() escalation is in
        flight. wait_for_ack() (below) reads the exact same "REC UNREAD"
        list to detect an acknowledgement reply, and AT+CMGL marks whatever
        it lists as read - so whichever of the two calls first steals the
        messages the other one needed to see. modem_main.py's poll_inbound_
        sms() enforces this by only ever calling from the board's idle loop,
        never from inside run_alert(). If you add another caller, keep that
        invariant or a real acknowledgement can be read as a command instead
        of cancelling the call.
        """
        try:
            text = self.cmd('AT+CMGL="REC UNREAD"', timeout=8000, retries=0)
        except ModemError:
            return []

        lines = text.replace("\r", "\n").split("\n")
        out = []
        i = 0
        while i < len(lines):
            line = lines[i]
            if "+CMGL:" not in line:
                i += 1
                continue

            # +CMGL: 3,"REC UNREAD","+923001234567",,"26/08/31,10:02:14+20"
            #
            # Same header-parsing trap unread_senders() already works around:
            # everything before the first comma is "+CMGL: 3", which starts
            # with a plus sign exactly like a phone number does. Drop it,
            # then take the first comma-separated field that is essentially
            # all digits.
            header = line.split(",", 1)[1] if "," in line else ""
            sender = None
            for part in header.split(","):
                part = part.strip().strip('"')
                digits = "".join(c for c in part if c.isdigit())
                if len(digits) >= 7 and len(digits) >= len(part) - 1:
                    sender = part
                    break

            try:
                index = int(line.split("+CMGL:")[1].split(",")[0].strip())
            except (IndexError, ValueError):
                index = None

            # The body is every line up to the next +CMGL: header or the
            # trailing OK - NOT just "the next line", which is what
            # _last_message_text() assumes and which breaks the moment a
            # message contains its own line break.
            body_lines = []
            j = i + 1
            while j < len(lines) and "+CMGL:" not in lines[j] and lines[j].strip() != "OK":
                body_lines.append(lines[j])
                j += 1
            body = "\n".join(l for l in body_lines if l.strip()).strip()

            if sender and index is not None:
                out.append((index, sender, body))

            i = j

        if delete:
            for index, _, _ in out:
                self._try("AT+CMGD=%d" % index)

        return out

    def wait_for_ack(self, contacts, window, poll=5.0, keyword=None):
        """
        Block up to `window` seconds waiting for one of `contacts` to text back.

        Returns the acknowledging number, or None if the window expires.

        Any reply counts by default - somebody who texts back has obviously
        read the alert. Pass keyword="OK" to require a specific word instead,
        which costs a message read per candidate and is usually not worth it.
        """
        if not window or window <= 0:
            return None

        # Clear anything already sitting unread BEFORE the window opens.
        #
        # Without this, one stale text from yesterday - from anyone in the
        # contact list, about anything - is read as an acknowledgement the
        # instant the window starts, and the escalation cancels the call
        # before it is ever placed. Listing them marks them read, so only
        # genuinely new replies count.
        stale = self.unread_senders()
        if stale:
            self._log("cleared %d already-unread message(s) before the window"
                      % len(stale))

        self._log("ack window open for %.0fs" % window)
        start = _ms()

        while _elapsed(start) < window * 1000:
            for sender in self.unread_senders():
                for contact in contacts:
                    if not numbers_match(sender, contact):
                        continue
                    if keyword and keyword.upper() not in self._last_message_text().upper():
                        continue
                    self._log("acknowledged by %s" % contact)
                    return contact
            time.sleep(poll)

        self._log("ack window expired - proceeding to call")
        return None

    def _last_message_text(self):
        """Body of the most recent unread message, or '' if unreadable.

        Only consulted when an ack keyword is configured; the default path
        never calls this.
        """
        try:
            text = self.cmd('AT+CMGL="REC UNREAD"', timeout=8000, retries=0)
        except ModemError:
            return ""
        lines = [l for l in text.replace("\r", "\n").split("\n") if l.strip()]
        # The body is the line after each +CMGL: header.
        for i, line in enumerate(lines):
            if "+CMGL:" in line and i + 1 < len(lines):
                return lines[i + 1]
        return ""

    def dial(self, number):
        """Place a voice call. True if the network accepted the dial.

        Records why it was refused in self.last_dial_error, because the reason
        decides what happens next: "network not allowed" is recoverable with a
        radio re-attach, a busy or unobtainable number is not.
        """
        self.flush()
        self._log("dialling %s" % number)
        self._write("ATD%s;\r" % number)
        matched, resp = self.expect("OK", timeout=10000)
        if matched != "OK":
            self.last_dial_error = resp.strip()
            self._log("dial rejected: %r" % self.last_dial_error)
            return False
        self.last_dial_error = ""
        return True

    def cs_refused(self):
        """True if the last dial failed because the network refused CS service.

        The module attaches for DATA perfectly - registration, full signal, SMS
        all working - while the circuit-switched domain holds it in LIMITED
        SERVICE, where only genuine emergency numbers are allowed. The error
        reads "network not allowed - emergency calls only".

        Seen repeatedly on this hardware. A full radio restart clears it.
        """
        err = (self.last_dial_error or "").lower()
        return "not allowed" in err or "emergency calls only" in err

    def reattach(self, timeout=40):
        """Restart the radio to force a clean attach on BOTH domains.

        AT+CFUN=0 detaches completely, AT+CFUN=1 brings it back. Heavier than
        it sounds - 15 to 25 seconds - so this is never routine, only a
        response to the network actually refusing a call.

        Why it works: repeated failed CSFB attempts can leave the module
        registered for packet service but stuck in limited service for
        circuit-switched, which is precisely the state that produces
        "network not allowed". A fresh attach negotiates both domains again.

        Returns True if CREG and CEREG both come back registered.
        """
        self._log("network refused the call - restarting the radio")

        self._try("AT+CFUN=0", timeout=8000)
        time.sleep(5)
        self._try("AT+CFUN=1", timeout=8000)

        start = _ms()
        while _elapsed(start) < timeout * 1000:
            time.sleep(2)
            cs = ps = None

            for query, tag in (("AT+CREG?", "+CREG:"), ("AT+CEREG?", "+CEREG:")):
                try:
                    text = self.cmd(query, timeout=2000, retries=0)
                    stat = int(text.split(tag)[1].split(",")[1].strip()[0])
                except (ModemError, IndexError, ValueError):
                    continue
                if tag == "+CREG:":
                    cs = stat
                else:
                    ps = stat

            # CS alone is enough. Pinned to GSM (AT+CNMP=13) there IS no LTE
            # attach, so CEREG sits at 0 forever - demanding both meant a
            # perfectly good re-attach reported failure fourteen polls in a
            # row while CREG read 1 every time.
            if cs in (1, 5):
                self._log("re-attached: CS=%s (LTE=%s)" % (cs, ps))
                return True

        self._log("re-attach did not restore circuit-switched service")
        return False

    def call_state(self):
        """
        Current call state, or one of the two sentinels below.

            0=active 1=held 2=dialling 3=alerting 4=incoming 5=waiting
            CALL_NONE     the modem answered and reported no call
            CALL_UNKNOWN  the modem did not answer at all

        Those last two used to be the same value, and conflating them broke
        real calls. On an LTE module without VoLTE, dialling triggers CSFB -
        the module drops to 2G to carry the voice call - and during that radio
        access technology switch it goes briefly deaf on the UART. A poll that
        times out mid-handover is NOT a call that ended; treating it as one
        hangs up on a call that was connecting perfectly well.
        """
        try:
            text = self.cmd("AT+CLCC", timeout=3000, retries=0)
        except ModemError:
            return CALL_UNKNOWN

        if not text.strip():
            return CALL_UNKNOWN          # no reply - probably mid-handover
        if "+CLCC:" not in text:
            return CALL_NONE             # answered, and there is no call

        try:
            return int(text.split("+CLCC:")[1].split(",")[2])
        except (IndexError, ValueError):
            return CALL_UNKNOWN

    def wait_answered(self, timeout=45, max_silent=6):
        """
        Poll until the far end picks up. Returns True if the call went active.

        max_silent is how many consecutive non-answers to tolerate before
        concluding the modem is really gone rather than just busy switching
        networks. Six polls is roughly 18-20 seconds, comfortably longer than
        any CSFB handover, and still well inside the ring timeout.
        """
        start = _ms()
        silent = 0

        while _elapsed(start) < timeout * 1000:
            state = self.call_state()

            if state == 0:
                self._log("call answered")
                return True

            if state == CALL_NONE:
                self._log("call ended before answer")
                return False

            if state == CALL_UNKNOWN:
                silent += 1
                if silent >= max_silent:
                    self._log("modem stopped responding (%d polls) - giving up"
                              % silent)
                    return False
                self._log("no reply to CLCC (%d/%d) - likely CSFB handover, waiting"
                          % (silent, max_silent))
            else:
                silent = 0               # a real state resets the counter
                self._log("call state %d" % state)

            time.sleep(1)

        self._log("no answer within %ds" % timeout)
        return False

    def hangup(self):
        try:
            self.cmd("ATH", timeout=5000)
        except ModemError:
            pass

    # ------------------------------------------------------------------ the feature

    def emergency_alert(self, numbers, message, on_answered=None,
                        call_timeout=45, hold_seconds=300,
                        ack_window=45.0, ack_poll=5.0, ack_keyword=None,
                        dtmf_ack="1", dtmf_escalate="2", dtmf_ask="3",
                        on_dtmf=None, calls_enabled=True):
        """
        Fire the full escalation: text everyone, give them a chance to reply,
        then ring them in turn until somebody picks up.

            SMS all contacts
              -> calls_enabled False?  stop here, the text IS the alert
              -> wait ack_window for any of them to text back
                   acknowledged?  stop here, nobody gets called
              -> dial contacts in order
                   answered? play the message, then hold the line open
                   keypad digit decides what happens next

        on_answered() runs once the call is live. That is where the caller
        un-mutes the DAC and plays emergency_mic.wav into the modem's mic
        input - see I2SVoice below.

        on_dtmf(digit) runs for any digit that is not one of the three handled
        here, so the firmware can act on dtmf_ask without this function needing
        to know anything about intent classification or TTS.

        Returns a dict describing what actually happened. Deliberately never
        raises: this is the path that must not die silently.
        """
        # The module may have rebooted since begin() - between boot and the
        # first alert can be hours. Everything below depends on settings a
        # reset would have wiped.
        self.ensure_ready()

        result = {
            "sms": {},          # number -> delivered?
            "acked_by": None,   # who replied during the ack window
            "called": [],       # numbers actually dialled
            "answered": None,   # who picked up
            "spoke": False,     # did the message play
            "dtmf": [],         # every digit pressed
            "outcome": None,    # why the sequence ended
            "reattached": False,  # did the radio have to be restarted
        }

        # One radio restart per alert, at most. See the retry below.
        reattached = False

        # ---- phase 1: text everybody -----------------------------------
        # First because it is instant, cheap, and lands even if the phone is
        # on silent in another room.
        for n in numbers:
            try:
                result["sms"][n] = self.send_sms_retry(n, message)
            except Exception as e:                    # last line of defence
                self._log("SMS to %s blew up: %r" % (n, e))
                result["sms"][n] = False

        # ---- SMS-only deployment ---------------------------------------
        # No dialling stage exists, so there is nothing for an ack window to
        # cancel and no reason to hold the UART for another 45 seconds. Return
        # now and let the firmware's normal inbound poll pick up any reply -
        # it already treats replies as commands, which is strictly more than
        # the ack window did.
        if not calls_enabled:
            delivered = [n for n, ok in result["sms"].items() if ok]
            result["outcome"] = "sms_sent" if delivered else "sms_failed"
            self._log("alert result: %s" % result)
            return result

        # ---- phase 2: give them a chance to answer the text ------------
        # Ringing three seconds after texting makes the text pointless. If
        # somebody replies, they have seen it and nobody needs a call.
        try:
            acked = self.wait_for_ack(numbers, ack_window, ack_poll, ack_keyword)
        except Exception as e:
            self._log("ack window blew up: %r" % e)
            acked = None

        if acked:
            result["acked_by"] = acked
            result["outcome"] = "acknowledged_by_sms"
            self._log("alert result: %s" % result)
            return result

        # ---- phase 3: call them in order -------------------------------
        self.enable_dtmf()

        for n in numbers:
            try:
                result["called"].append(n)

                if not self.dial(n):
                    # "network not allowed - emergency calls only" means the
                    # circuit-switched domain has us in limited service. A
                    # radio restart clears it, so it is worth one retry - but
                    # only once per alert, because it costs ~20 seconds and
                    # somebody may be waiting on this call.
                    if not (self.cs_refused() and not reattached):
                        continue

                    reattached = True
                    result["reattached"] = True
                    if not self.reattach():
                        continue
                    if not self.dial(n):
                        continue
                if not self.wait_answered(call_timeout):
                    self.hangup()
                    continue

                result["answered"] = n

                if on_answered:
                    on_answered()
                    result["spoke"] = True

                # Hold the line. The DAC is muted again by now, so the modem's
                # own electret is live and the responder hears the actual room
                # - which is usually more use than the recording was.
                verdict = self._hold_and_listen(
                    hold_seconds, result,
                    dtmf_ack, dtmf_escalate, dtmf_ask, on_dtmf)

                self.hangup()

                if verdict == "escalate":
                    continue          # they pressed 2 - try the next contact

                result["outcome"] = verdict
                break

            except Exception as e:
                self._log("call to %s blew up: %r" % (n, e))
                self.hangup()

        if result["outcome"] is None:
            result["outcome"] = ("no_answer" if result["answered"] is None
                                 else "call_ended")

        self._log("alert result: %s" % result)
        return result

    def _hold_and_listen(self, hold_seconds, result,
                         dtmf_ack, dtmf_escalate, dtmf_ask, on_dtmf):
        """
        Keep the call up for hold_seconds, watching for keypresses.

        Returns one of:
            "acknowledged"  - they pressed the ack digit, stop escalating
            "escalate"      - they pressed the escalate digit, try the next one
            "hung_up"       - the far end ended the call
            "held"          - the hold expired with nothing pressed

        Split out of emergency_alert() because that function was already doing
        three separate jobs and this loop is the one with the tricky bit in it:
        call_state() must be polled to notice a hang-up, but polling consumes
        the UART, which is exactly why read_dtmf() reads the URC mirror instead.
        """
        start = _ms()
        silent = 0

        while _elapsed(start) < hold_seconds * 1000:
            for digit in self.read_dtmf():
                result["dtmf"].append(digit)

                if digit == dtmf_ack:
                    self._log("acknowledged by keypad")
                    return "acknowledged"

                if digit == dtmf_escalate:
                    self._log("escalation requested by keypad")
                    return "escalate"

                if digit == dtmf_ask and on_dtmf:
                    # The question window needs the network and the LLM, so it
                    # is the firmware's problem, not the driver's.
                    try:
                        on_dtmf(digit)
                    except Exception as e:
                        self._log("on_dtmf(%s) blew up: %r" % (digit, e))
                elif on_dtmf:
                    try:
                        on_dtmf(digit)
                    except Exception as e:
                        self._log("on_dtmf(%s) blew up: %r" % (digit, e))

            state = self.call_state()

            if state == CALL_NONE:
                self._log("far end hung up")
                return "hung_up"

            if state == CALL_UNKNOWN:
                # Same reasoning as wait_answered: a silent poll mid-call is
                # not a hang-up. Only give up after several in a row.
                silent += 1
                if silent >= 6:
                    self._log("modem unresponsive during hold - ending")
                    return "hung_up"
            else:
                silent = 0

            time.sleep(1)

        return "held"


# ---------------------------------------------------------------------- PCM5102A side

class I2SVoice:
    """
    Plays a 16-bit mono WAV out of the PCM5102A, into a LOUDSPEAKER.

    The speaker sits a few centimetres from the modem's electret capsule, so
    the message reaches the call acoustically rather than electrically. The
    DAC's output no longer connects to MIC+/MIC- at all - the divider and
    coupling capacitors that used to sit between them are gone.

    That earlier design is documented rather than deleted, because the reason
    it failed is not obvious and someone will otherwise rebuild it: the far end
    heard nothing but harmonics of 216.7 Hz, the GSM TDMA frame rate. The
    modem's 2G transmit bursts contaminate the shared supply, and the
    PCM5102A's regulator does not reject them, so the buzz rides out on its
    analog output. It vanishes when the DAC is muted, which is what proves the
    DAC is the source rather than RF picked up on the wires. Fixing it wants a
    separate regulator and a ground plane - a PCB. See
    docs/Audio Injection Investigation.docx.

    Play the WAV at FULL amplitude for this path. The old clip was scaled to
    ~0.4 to suit the resistor divider; a speaker wants all of it.

    This class does no DSP - per-sample maths in MicroPython cannot keep up at
    these rates. rate MUST match the WAV: Piper's en_US-amy-medium is 22050 Hz,
    and the old default of 16000 played the message noticeably pitch-shifted.
    """

    def __init__(self, bclk=5, ws=6, din=7, xsmt=15, rate=22050):
        from machine import I2S, Pin
        # xsmt is the PCM5102A's soft-mute pin: LOW = muted, HIGH = playing.
        # Optional - if the board's H3L jumper already ties it high, leave this
        # None. Still worth wiring even now the DAC drives a speaker rather than
        # the mic pins: muting between clips keeps the DAC's idle hiss out of
        # the room, and the electret is listening to that room for the rest of
        # the call.
        self.xsmt = Pin(xsmt, Pin.OUT, value=0) if xsmt is not None else None
        self.audio = I2S(0, sck=Pin(bclk), ws=Pin(ws), sd=Pin(din),
                         mode=I2S.TX, bits=16, format=I2S.MONO,
                         rate=rate, ibuf=16384)       # deep buffer: server chunks arrive late

    def play(self, path, skip_header=44, repeat=2):
        """Play the clip `repeat` times - a responder who just picked up will miss
        the first second or two of the first pass."""
        if self.xsmt:
            self.xsmt.value(1)          # un-mute
            _sleep_ms(20)
        try:
            for _ in range(repeat):
                with open(path, "rb") as f:
                    f.read(skip_header)              # skip the RIFF header
                    buf = bytearray(1024)
                    while True:
                        n = f.readinto(buf)
                        if not n:
                            break
                        self.audio.write(buf[:n])
                _sleep_ms(500)
        finally:
            if self.xsmt:
                self.xsmt.value(0)      # mute again -> silent mic input


# ---------------------------------------------------------------------- compatibility

# The file and class used to be named for the SIM800L, back when that was the
# only module this drove. Anything still importing the old names keeps working.
SIM800L = Modem
SIM800LError = ModemError


# ---------------------------------------------------------------------- example

if __name__ == "__main__":
    # On the board this is driven by esp32/modem_main.py instead; this block is
    # for bench-testing the sequence from the laptop over USB serial, where
    # there is no I2S peripheral and on_answered is left out entirely.
    #
    #     python Services/modem.py COM5
    #
    # Run tools/probe_modem.py FIRST - it reports which of the optional audio
    # and DTMF commands this firmware actually has.
    import sys

    CONTACTS = ["+92XXXXXXXXXX", "+92YYYYYYYYYY"]
    MESSAGE = "GENIE EMERGENCY: assistance needed. This alert was sent automatically."

    if _MICROPYTHON:
        modem = Modem(uart_id=1, tx=17, rx=18, rst=16)
        voice = I2SVoice()                        # PCM5102A on GPIO5/6/7, XSMT 15
        on_answered = lambda: voice.play("/emergency_mic.wav")
    else:
        if len(sys.argv) < 2:
            raise SystemExit("usage: python Services/modem.py <PORT>   e.g. COM5")
        modem = Modem(port=sys.argv[1])
        on_answered = None                        # no I2S on the laptop

    modem.begin()
    if modem.wait_network():
        modem.set_audio(speaker=90)
        modem.emergency_alert(CONTACTS, MESSAGE, on_answered=on_answered)
    else:
        print("no network - cannot send the alert")
