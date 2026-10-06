# Emergency alert path: ESP32-S3 -> A7670E

Pipeline:

    sensor / voice trigger -> whisper_service (port 9002) -> modem ESP32-S3
      -> SMS to contacts -> wait for a reply -> dial -> hold -> hang up

Hardware: ESP32-S3 and a SIMCom A7670E-LASA on a BK-7670 v1 carrier. Three
signal wires and a power pair. No DAC, no audio hardware of any kind.

**The call carries no audio.** See section 2.


## 1. Bill of materials

| Ref | Part | Notes |
|-----|------|-------|
| C4  | 470-1000 uF electrolytic, 16 V+ | across the modem's V and G. **Required** - see section 4 |

That is the entire list. Everything else was for audio paths that do not work
on this hardware, and all of it has been removed:

    R1   10k series resistor          } the attenuator that fed the mic pins
    R2   10k trimpot                  }
    C1   10 uF coupling cap           }
    C2   10 uF coupling cap           }
    C3   100 pF RF bypass             }
    PCM5102A I2S DAC                  no longer connected to anything


## 2. Why the call is silent

The MIC+ / MIC- pads on the carrier tore off, taking the electret capsule with
them. With no microphone the modem has nothing to encode, so the far end hears
silence for the whole call.

That was the last of four routes. The other three were closed before it, each
for its own reason, and all are written up with measurements in
`docs/Audio Injection Investigation.docx`:

**Analog injection.** A PCM5102A, attenuated ~46 dB and AC-coupled into
MIC+/MIC-. It worked electrically - the far end heard something, and the
trimpot changed its level - but what it heard was 217 Hz and its harmonics. An
FFT of a recording put *every* spectral peak on a multiple of 216.7 Hz, the GSM
TDMA frame rate. The modem's own 2G transmit bursts contaminate the shared 5 V
rail and the DAC's regulator does not reject them. The decisive observation:
the room was completely clean whenever the DAC was muted, which rules out RF
pickup on the wires and ground bounce alike, and pins it on the DAC's own
output. Fixing it wants a separate regulator and a ground plane - a PCB.

**Digital playback.** `AT+CCMXPLAY` exists on this firmware and reports
`+AUDIOSTATE: audio play`, but decodes to noise. The uploaded file was proven
byte-identical by reading it back off the module, so the transfer was sound.
And `play_path` 1 and 2 - remote, and both - return ERROR; only 0 is accepted,
which is almost certainly "local". This build appears to have no route from
stored audio into the call uplink at all.

**Acoustic coupling.** GENIE's own speaker beside the modem's electret, so the
message reached the call through the air. This would have worked - it avoids
both mechanisms above entirely. The pads broke before it could be tested.

### 2.1 What the alert still delivers

Not nothing, and worth being clear about:

| | |
|---|---|
| **SMS** | says WHAT happened - "smoke detected", "a fall was detected". Lands even on a silent phone. This carries the actual information, and it works reliably. |
| **Acknowledgement** | a reply during the window cancels the call entirely. Most alerts should end here. |
| **Call** | a ringing phone from a known number, at 3am, gets attention a text does not. It says SOMETHING IS WRONG, GO AND LOOK - and the SMS already said why. |
| **Local announcement** | when a call is answered, GENIE says so out loud in the room. For whoever is present, not for the caller. |

### 2.2 If the pads are ever repaired

Solder an electret back onto MIC+/MIC-, put GENIE's speaker a few centimetres
from it pointing at it, and raise `HOLD_SECONDS` again. **No code changes** -
the server already speaks when a call is answered; it just has nothing
listening to it right now.


## 3. Wiring

Three signal wires and a power pair. That is the whole build.

### 3.1 ESP32-S3 -> A7670E carrier

    GPIO17 -> R (RXD)      ESP transmits, module receives
    GPIO18 -> T (TXD)      module transmits, ESP receives
    K                      LEAVE UNCONNECTED - probably PWRKEY, not RESET
    V      -> supply +     DIRECT to the terminals, never a breadboard rail
    G      -> supply -     DIRECT, same

Plus, at the module:

  - **470 uF across V and G**, stripe leg to G. Not optional - see section 4.
  - the **u.FL antenna**. Never power the module without it; transmitting into
    an open connector can damage the power amplifier.
  - the **SIM**, in the micro-SIM socket.

The MIC+/MIC- and SPK+/SPK- pads are unused. The mic pads are broken anyway.

### 3.2 Power and grounding

The A7670E draws ~2 A bursts on transmit. Run its V and G as their own pair,
straight to the supply's output terminals, with the shortest and thickest wire
available. The ESP32 can run off USB.

The two grounds meet at the supply's negative terminal, which gives the UART
its reference while keeping the heavy current off any breadboard contact.

### 3.3 What to check before powering on

| Probe | Expect | If wrong |
|---|---|---|
| modem **V -> G**, supply on | 5.0 V | power is not arriving |
| modem **T** pad at idle | ~3.3 V | 1.8 V needs a level shifter; 0 V means the module is not running |
| across the 470 uF, power off | climbs to open | 0 ohm means it is reversed or shorted - do not power it |

### 3.4 If the modem never answers AT

In order of likelihood, learned the hard way:

1. **No power at the V pad.** Meter it. A missing V wire looks exactly like a
   logic-level or baud problem and cost most of a session here.
2. **Never switched on.** The "K" pad is most likely PWRKEY, which wants a
   ~1 second LOW pulse. Jumper K to G briefly by hand.
3. **TX/RX swapped.** ESP32 TX goes to the module's RXD. Swapping them is
   silent - no error, just nothing.
4. **Logic level.** See the T pad check above.


## 4. Bulk capacitance - the modem WILL brown out without it

Confirmed on this hardware 2026-08-22, and it cost most of a bench session.

Symptom: the modem is perfectly stable on LTE - registers, reports CSQ 31, sends
SMS end to end - but every voice call dies about three seconds after ATD, before
the far end ever rings. AT+CEER reports only "0 Unknown".

The tell is in the URC stream during the call:

    0s  +CLCC: 1,0,2,...      dialling
    1s  +CLCC: 1,0,2,...      dialling
    2s  (no reply)            switching to 2G
    3s  *ATREADY: 1           <-- the module REBOOTED

`*ATREADY: 1` is what SIMCom sends when the AT interface comes up after boot.
Seeing it mid-call means the module reset. Repeated `+CPIN: READY` and
`SMS DONE` are the same story - those are boot-time messages.

Why it only happens on calls: this SIM has no VoLTE (`+CIREG: 1,0,0`, though
`+CAVIMS: 1` says the module supports it). Without VoLTE, every voice call has
to fall back to 2G - CSFB. GSM transmit bursts pull ~2 A peaks, far more than
LTE ever asks for, and the supply collapses. Forcing `AT+CNMP=13` (GSM only)
reproduces it immediately: the module sits in a reboot loop, `+CREG: 0,2`
(searching) and `+CNSMOD: 0,0` (no service), never registering.

The fix:

  1. A 470-1000 uF electrolytic, 16 V or higher, soldered DIRECTLY across the
     modem's V and G pads. Not on the breadboard - at the board. The carrier's
     own tantalum is nowhere near enough for a 2 A burst; the electrolytic is a
     local reservoir covering the milliseconds the supply cannot.
     Watch polarity: the striped leg goes to G.
  2. Supply current limit set to at least 2 A.
  3. Power wires direct to the supply terminals, shortest and thickest
     available. Dupont jumpers have real resistance at 2 A.

To confirm before and after: meter DC volts across V and G AT THE MODEM while
it dials. A dip below ~4.5 V or a bounce is the brownout. Steady 5 V is not.

Note this is a different failure from a modem that never answers AT at all -
that one is usually PWRKEY (section 6) or a missing V wire. This one answers
fine and only dies under RF load.


## 5. AT command sequence

Confirmed on both SIM800L and A76XX, already implemented in modem.py:

    ATE0 / AT+CMEE=2 / AT+CPIN? / AT+CMGF=1 / AT+CSCS="GSM" / AT+CLIP=1
    AT+CEREG?   registration on LTE (A7670E reports here, not +CREG)
    AT+CMGS     send SMS
    ATD<n>;     dial
    AT+CLCC     call state, 0 = active
    ATH         hang up
    AT+CLVL     speaker volume

Verify these on the device before relying on them - availability varies by
firmware revision. Query each with the '=?' form first:

    AT+CSDVC=1        select handset audio channel (vs 3 = speaker)
    AT+CTXGAIN=?      uplink/mic gain on A76XX. AT+CMIC is SIM800-only and will
                      ERROR here - set_audio() already tolerates that.
    AT+DDET=1         DTMF detection. CONFIRMED ABSENT on A7670E-LASA V1.11.2 -
                      both this and AT+CDTMFDET return ERROR. See 8.1.
    AT+VTS="1"        send a DTMF digit outbound
    AT+CCMXPLAY=?     see section 6.1

### 5.1 AT+CCMXPLAY - tested, does not work here

SIMCom's A76XX manual documents `AT+CCMXPLAY` as playing an audio file from the
module's own flash into the call's uplink. It would have removed the need for
any external audio hardware. It was tested thoroughly and does not work on this
firmware.

    AT+CCMXPLAY=?   ->  +CCMXPLAY: (0-2),(0-255)      the command exists
    play_path 1 (remote)  ->  ERROR
    play_path 2 (both)    ->  ERROR
    play_path 0 (local)   ->  +CCMXPLAY: OK | +AUDIOSTATE: audio play

Only the local path is accepted during a call, and the far end hears buzz rather
than the message. The file was proven intact by reading it back off the module
byte-for-byte, so this is not a transfer problem.

`+AUDIOSTATE: audio play` reports only that playback STARTED. It says nothing
about whether the decoder understood the file, or where the output was routed.

Getting a file onto the module at all took some reverse engineering, recorded
here in case it is ever useful:

  - `AT+CFTRANRX` returns ERROR in every argument form tried
  - the working API is `AT+FSOPEN` / `AT+FSWRITE` / `AT+FSCLOSE`
  - `AT+FSWRITE` answers **CONNECT**, not a `>` prompt - watching for `>` makes
    a working command look dead
  - its `<length>` and `<timeout>` arguments are refused, and
    `AT+FSWRITE=<h>,5,1000` REBOOTS the module
  - there is a 10240-byte cap per write session, applied silently
  - `AT+FSREAD` frames its output as `CONNECT <length>` then exactly that many
    bytes, which is what makes a reliable binary read possible

Full detail in `docs/Audio Injection Investigation.docx`.


## 6. Escalation state machine

What emergency_alert() does today: SMS every contact, then immediately call every
contact in order. What the requirement asks for is a delay and an acknowledgement
check between those two phases.

    trigger
      |
      +-> SMS all contacts                    (cheap, survives no-answer)
      |
      +-> wait ack_window seconds
      |     poll AT+CMGL="REC UNREAD" for a reply
      |     if a contact replies -> stop, alert acknowledged
      |
      +-> dial contacts in order
            on answer:
              AT+DDET=1
              tell the server (0x02) so it announces in the ROOM
              hold the line (electret live, room audible)
              on +DTMF:
                1 -> acknowledged, hang up, stop escalating
                2 -> escalate to the next contact
                3 -> open a question window (see 8)
            on no answer -> next contact

Needed in modem.py: an ack_window parameter and an inbound-SMS poll between the
two loops, and a DTMF URC reader during the hold.


## 7. Bidirectional path - not possible

There is no audio in either direction on this hardware.

**Uplink** is dead because the MIC pads broke (section 2). **Downlink** would
mean reading the modem's SPK+/SPK- output - a differential amplifier built to
drive an 8 ohm speaker at up to a watt - into the ESP32's ADC, which needs
~40 dB of attenuation, a mid-rail bias and an anti-alias filter. Even built,
it would put a live microphone and a live speaker in one loop with no echo
canceller anywhere.

And the obvious control channel is missing too:

### 7.1 DTMF is not available on this firmware

Tested 2026-08-25 on A7670E-LASA V1.11.2: BOTH spellings return ERROR.

    AT+DDET=1       -> ERROR
    AT+CDTMFDET=1   -> ERROR

So the responder cannot press 1 to acknowledge. emergency_alert() falls back to
timing out on its own, which it already does correctly - nothing is broken,
there is simply no interactive branch.

**The SMS acknowledgement window is therefore the primary acknowledgement
mechanism, not a fallback.** A contact replies to the alert text and the
escalation stops before anyone is called. That is implemented, tested and
working, and it needs no hardware this build does not have.


## 8. Bring-up order

Each step isolates one failure. Going straight to a live alert means debugging
several things at once, which is how most of this project's bench time went.

1. **Modem alive.** `esp32/test_modem.py`. It must report *alive and
   initialised* - not merely that AT answered. A module still booting, or
   browning out, replies to AT and then rejects everything after it.

2. **One call, nothing else.** `esp32/test_call.py`. Watch the state go
   2 -> 3 -> 0. Silence after you answer is correct. `*ATREADY: 1` anywhere is
   the module rebooting: that is power, see section 4.

3. **The whole sequence.** `esp32/test_alert.py` - SMS, acknowledgement window,
   escalation, call, hold. Run it twice:
     - **reply to the SMS** during the window: no call should be placed. This
       is the branch most real alerts should end on, and the one worth demoing.
     - **ignore it**: the phone should ring.

4. **End to end.** Start `python Services/whisper_service.py`, confirm it logs
   *Modem ESP connection established*, then trip a sensor or say the emergency
   phrase. Everything from detection to ringing phone should follow on its own.

## 9. Configuration reference

Everything that has to agree across files:

| Setting | Where | Notes |
|---|---|---|
| `EMERGENCY_CONTACTS` | `config.py` **and** `esp32/settings.py` | must match. Dialled in list order |
| `WIFI_SSID` / `WIFI_PASSWORD` | `esp32/settings.py` | board side only |
| `SERVER_HOST` | `esp32/settings.py` | the PC running whisper_service. A DHCP lease that moves breaks the alert silently |
| `MODEM_TX` / `MODEM_RX` / `MODEM_BAUD` | `esp32/settings.py` | 17 / 18 / 115200 |
| `HOLD_SECONDS` | `esp32/settings.py` | 20 s. Was 300 when the plan was live room audio; a silent line does not need holding |
| `EV_ANSWERED` | `config.py` **and** `esp32/modem_main.py` | 0x02, must match |

Deploy:

    mpremote connect COM21 cp Services/modem.py   :modem.py
    mpremote connect COM21 cp esp32/settings.py   :settings.py
    mpremote connect COM21 cp esp32/modem_main.py :main.py

Copying it as `main.py` makes the board run it on power-up, with no laptop
attached.
