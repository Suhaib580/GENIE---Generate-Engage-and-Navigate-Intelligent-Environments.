# Services files

Sixteen Python modules. Thirteen are required at runtime; three are operator
tools you will actually use.

These go into a folder named exactly **`Services/`**, directly inside the
project root beside `config.py`. The name matters: `api.py` looks for
`Services` by that name, and `whisper_service.py` finds `config.py` by going
one level up from its own folder.

---

## The main program

### `whisper_service.py` — the orchestrator

```
python Services\whisper_service.py
```

Watches `recordings/` for the .wav files `AudioServer.py` writes, and carries
each one through to a spoken answer.

```
new recording
   → Whisper (faster-whisper, base, int8 on CPU)
   → confidence gate — reject if it is probably not speech,
                       or not English, or too uncertain
   → ai.classify(text)
        ├─ emergency → tell the modem board (TCP :9002)
        ├─ sensor    → read api.py, phrase it with sensor.aiml
        ├─ query     → read the relay state
        ├─ command   → switch the light or fan
        └─ no match  → Mistral, via Ollama
   → Piper → stream to the speaker board (TCP :9001)
```

It also runs the background threads: the three watchers below, the sensor
bridge, the speaker link and the emergency link.

**The follow-up window.** After GENIE finishes speaking, the microphone stays
open for eight seconds, so you can ask a second question without saying "Hey
Genie" again. Up to three in a row. While GENIE is talking its own microphone
is muted — otherwise the wake word fires on its own voice and it answers
itself.

---

## Speech

| File | Role |
|---|---|
| `wake_word.py` | openWakeWord. Loads `hey_genie.onnx`, needs three consecutive hits above 0.65 |
| `vad_detector.py` | webrtcvad. Decides when you started and stopped speaking |
| `rms_detector.py` | Loudness numbers for the console. Display only — it decides nothing |
| `llm_service.py` | The Ollama / Mistral call |
| `tts_service.py` | Piper. Streams audio out in chunks as it is generated, rather than waiting for the whole sentence |
| `ai.py` | The AIML kernel. Classifies what you said, and phrases sensor answers |

### About `ai.py`

One AIML kernel with **both** `relay.aiml` and `sensor.aiml` loaded into it.
It does no networking — it decides *what you asked* and, given readings
fetched by `whisper_service.py`, phrases the reply.

It is also the only place in the project that converts **millimetres to
centimetres**, right at the point of speech. Everything upstream — the ToF
board, the ESP-NOW frame, `api.py` — is millimetres end to end. Mixing units
earlier is how a 1.2 m reading becomes 12 cm.

---

## Sensors

| File | Role |
|---|---|
| `node_bridge.py` | Translates board vocabulary into `api.py` vocabulary. The single mapping; nothing else duplicates it |
| `genie_serial.py` | Reads the board's serial protocol. **A vendored copy** — the original lives with the sensor firmware, and the two must stay byte-identical so both ends parse the same way |
| `sensor_watch.py` | **The only thing allowed to decide an emergency** |
| `climate_watch.py` | Warm or humid → runs the fan relay, and says why |
| `reaction_watch.py` | Somebody pushed or lifted the unit → a spoken reaction |

### The three watchers, and why they are three

```
sensor_watch     reads STATE     "is this an emergency?"
reaction_watch   reads MOMENTS   "did something just happen?"
climate_watch    reads STATE     "should I do something about it?"
```

### One owner for alerts

`sensor_watch.poll_once()` is the only code that can start an emergency. It
holds all the policy: edge detection (a fire that burns for an hour is one
emergency, not one per poll), a settling delay, and a ten-minute cooldown.

Everything else — including the sensor bridge, which can nudge it the instant
a bad reading lands — can only make an alert happen **sooner**, never twice. A
second place with its own copy of the logic would not share the cooldown, and
one smoke event would ring the responder's phone from both.

The settling delay is per sensor, on purpose:

| Sensor | Wait | Why |
|---|---|---|
| Smoke | 4 s | cheap MQ-2 modules twitch; this is what stops a call at 3 a.m. |
| Flame | 0 s | the board already confirmed it across three readings |
| Fall | 0 s | its state machine *is* the confirmation |

---

## Tools you will use

| Run | Does |
|---|---|
| `python Services\sensor_monitor.py` | Live table of all twelve channels. Your demo screen. `--once` for a snapshot, `--json` for raw |
| `python Services\serial_bridge.py` | **Required** if the face board is connected by USB rather than WiFi. Reads its serial port and feeds `api.py`. Nothing else does this |
| `python Services\fake_node.py --list` | Simulates both boards. Twelve scenarios including the failure cases — run the whole pipeline with no hardware at all |

### If the face board is on USB, read this

**A COM port has exactly one owner.** If Thonny is connected to the board,
`serial_bridge.py` cannot open it, and you will see telemetry scrolling in
Thonny while `api.py` stays empty — which looks exactly like a dead board.

Close Thonny first.

---

## The modem driver

`modem.py` drives the A7670E: SMS, voice calls, and DTMF replies. It is
**dual-use** — the same file runs here on the laptop and on the modem board
itself, so it is also listed in `SIMCom A7670E files/`.

**It is one file with two homes. Do not let the copies diverge.**

---

## A rule that runs through all of it

**The absence of a reading is not a reading.**

When a sensor fails or a board goes quiet, every channel it owned is set to
`unknown` — not to its last value, and never to `clear`. `unknown` matches no
alerting state, so it cannot raise an alarm, and it lands on `sensor.aiml`'s
fallback line, so it cannot give an all-clear either.

There is a specific trap this guards against: the firmware scores a **failed**
sensor the same as a **mildly elevated** one. Read as a level alone, a gas
sensor with a broken wire becomes a permanent quiet warning that everyone
learns to ignore. That is why the board sends `gas_ok`, `flame_ok` and
`imu_ok` alongside the readings, and why the bridge checks those **first**.
