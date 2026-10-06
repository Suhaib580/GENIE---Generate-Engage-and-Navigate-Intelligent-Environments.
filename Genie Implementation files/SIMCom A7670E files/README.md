# SIMCom A7670E files — the emergency system

The part of GENIE that reaches a human being when nobody in the room can.

A SIMCom **A7670E** LTE modem on its own ESP32-S3. When smoke, flame or a fall
is confirmed, it sends an SMS, waits, and then places a voice call that speaks
a recorded message down the line.

```
smoke / flame / fall confirmed
        |
   sensor_watch  (laptop)  →  TCP :9002  →  this board
        |
   SMS to contact 1
        |
   wait for an acknowledgement (45 s)
        |
   place a voice call, play the clip
        |
   the person presses a key:   1 = I've got it    2 = not me, try the next
                               3 = tell me more
```

| File | Where it runs |
|---|---|
| `modem_main.py` | this board — save it as **`main.py`** |
| `settings.py` | this board — WiFi and phone numbers |
| `modem.py` | **this board AND the laptop.** One file, two homes |
| `make_emergency_clip.py` | the laptop — generates the spoken clip |
| `probe_modem.py` | the laptop — AT-command diagnostic |
| `emergency_call_audio.md` | wiring and power notes. **Read before building** |

---

## Why the modem gets its own board

Because this has to work when everything else has failed.

The clip is **recorded ahead of time and stored on this board's flash.** At
emergency time nothing is synthesised and nothing is streamed. The alert still
goes out with the WiFi down, the laptop crashed, or the router unplugged. The
network link carries a trigger, never audio.

The board keeps its socket to the laptop open and reconnects forever if it
drops. Failing to reach the laptop must never leave it unable to place a call.

---

## Wiring

| A7670E | ESP32-S3 |
|---|---|
| RXD | GPIO **17** (ESP TX) |
| TXD | GPIO **18** (ESP RX) |
| RST | GPIO **16** |
| Baud | 115200 |

### ⚡ Power — the section people skip

**The A7670E pulls close to 2 A in bursts while transmitting.**

- Give it **its own supply**, wired **directly** to the module's power
  terminals. Never through breadboard rails.
- Fit **470 µF or more** across V and GND **at the module**, plus a 100 nF
  ceramic.
- Share the ground with the ESP32.

Without this the module reboots mid-call, and every symptom points somewhere
else entirely — you will chase AT commands, SIM registration and antennas for
a day before suspecting the supply. Section 4.2 of `emergency_call_audio.md`
covers it properly.

---

## How the call actually speaks

A phone call has no speaker to play into — it has a microphone input. So the
clip is played as **real audio into the modem's microphone pins**:

```
emergency_mic.wav on flash
   → I²S → PCM5102A DAC → attenuator → A7670E MIC+ / MIC−
```

The attenuator matters. A line-level signal straight into a microphone input
is far too hot and arrives clipped and unintelligible.

**Speech in the other direction was considered and rejected.** A live
microphone and a live speaker in one loop with no echo canceller means the
call feeds GENIE's own voice back into Whisper. Keypad digits arrive as clean
signals instead and survive a poor voice channel, which is why the replies are
**1, 2 and 3** rather than spoken words.

---

## ⚠ The clip is missing

`emergency_mic.wav` **is not in this folder.** Without it the call connects
and plays silence.

To create it:

```
python make_emergency_clip.py
```

Then upload the result to the board's flash as `/emergency_mic.wav`:

```
mpremote connect COM21 cp emergency_mic.wav :emergency_mic.wav
```

Its sample rate must match `I2S_RATE` in `config.py` (22050 Hz).

---

## Deploying the board

```
mpremote connect COM21 cp modem.py          :modem.py
mpremote connect COM21 cp settings.py       :settings.py
mpremote connect COM21 cp modem_main.py     :main.py
mpremote connect COM21 cp emergency_mic.wav :emergency_mic.wav
```

Set the phone numbers in `config.py` on the laptop:

```python
EMERGENCY_CONTACTS   = ["+92XXXXXXXXXX"]   # tried in order
EMERGENCY_ACK_WINDOW = 45.0                # seconds to wait after the SMS
EMERGENCY_CLIP_REPEAT = 2                  # times the clip repeats per call
EMERGENCY_HOLD_SECONDS = 300               # how long the line stays open
```

Check the modem is alive before trusting any of it:

```
python probe_modem.py
```

---

## Two things about the SIM

**Keep the SMS text plain English (ASCII).** The modem is put into GSM
character mode, which cannot represent Urdu or any non-Latin script. Such a
message arrives mangled. Sending it properly needs UCS2 encoding and PDU mode,
which is not implemented here.

**A SIM must be registered with PTA to work on a Pakistani network at all.**
An unregistered device is blocked after a grace period, and the symptom —
refusing to attach — looks identical to a hardware fault. Check this before
assuming the module is broken.

---

## Why it speaks before it dials

When a sensor alert fires, GENIE says *"Smoke detected. Calling for help
now"* **out loud first**, and only then triggers the modem.

Two reasons. Anyone in the room deserves to know what is happening before the
phone starts ringing. And if this board is unreachable, that spoken warning is
the only alert they get — so the system says so explicitly: *"I could not
reach the emergency module. Please call for help yourself."*
