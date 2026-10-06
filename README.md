# GENIE — an offline voice assistant and safety system

**BS Artificial Intelligence, Final Year Project — UMT Lahore**

GENIE listens, answers out loud, controls a room's light and fan, and calls
for help on its own when it detects smoke, fire or a fall.

**Nothing leaves the machine.** Speech recognition (Whisper), the language
model (Mistral 7B) and speech synthesis (Piper) all run locally. There is no
cloud API, no account and no internet requirement — which is the point, for a
device meant to work in a home when the network is down.

---

## What it is made of

Four microcontrollers and one laptop.

```
  ToF BOARD              FACE BOARD                    LAPTOP
  ESP32-S3               ESP32-S3                      Python
  4x VL53L1X             2.4" TFT face                 api.py       :8000
  vibration switches     2x MQ-2 gas                   Whisper      STT
       |                 flame, DHT11                  Mistral 7B   via Ollama
       |                 2x MPU-6050                   Piper        TTS
       +--ESP-NOW------> 2x SW-420          --serial-->     ^
       |                      |             --WiFi---->     |
       +----------- WiFi :9005 (direct) ------------------->|
                                                            |
  MIC BOARD  2x INMP441  ---- WiFi :9000 (audio) ---------->|
  SPEAKER BOARD  MAX98357A <-- WiFi :9001 (audio) ----------+
  MODEM BOARD  A7670E LTE  <-- WiFi :9002 (trigger) --------+
```

### Two paths, kept deliberately separate

**Path A — safety, rule-based.** Smoke, flame and falls never touch Whisper or
the language model. A confirmed flame reaches the alert logic in roughly
150 ms and triggers an SMS and then a phone call.

**Path B — conversation.** Wake word → Whisper → intent → answer → Piper.
Eight to fifteen seconds end to end.

That split is a design decision, not a limitation. Fifteen seconds is fine for
*"what is the temperature"* and unacceptable for a fire.

---

## The folders

At the top level, this repository separates the documents from the code:

| Folder | What is in it |
|---|---|
| `Genie Implementation files/` | **All the code.** Six sub-folders, described below |
| `Documentation/` | The FYP report (PDF — viewable in the browser) |
| `Exhibition Poster/` | The poster |

Inside `Genie Implementation files/`:

| Folder | What is in it |
|---|---|
| `Python Backend server files/` | The server: `api.py`, `config.py`, `AudioServer.py`, both AIML files and the wake-word model |
| `Services files/` | The 13 runtime modules plus 3 operator tools — STT, LLM, TTS, the sensor bridge and the three watchers |
| `Sensor files/` | Firmware for the two sensor boards (face + ToF), ready to flash |
| `Mic+speaker esp files/` | Firmware for the microphone board and the speaker board |
| `WakeWord files/` | The trained "Hey Genie" model, and how it was trained |
| `SIMCom A7670E files/` | The LTE modem board: SMS and voice-call escalation |

Each of those six has its own README with the detail.

**Setting this up on a fresh machine: read `SETUP_GUIDE.md` instead of this
section.** It lists every piece of software to install, which ones can be
copied across and which must be downloaded, and the commands in order.

---

## Setting it up

### What you need first

- **Python 3.14** on the laptop
- **[Ollama](https://ollama.com)** with Mistral: `ollama pull mistral`
- **Thonny** for the boards, with **MicroPython v1.27.0**
- A **WiFi access point** named `GENIE1`, password `12345678`
  (change it in `config.py` *and* in `wifi_cfg.py` on both sensor boards, and
  at the top of both files in `Mic+speaker esp files/` — five places, and they must agree)
- A **5 V 3 A** power supply. Not a phone charger — see the warning below

### Step 1 — assemble the laptop project

The folders in this submission are organised **by category, for reading**. The
Python code finds its imports by folder shape, so before running it, copy the
files into this layout:

```
GENIE/
├── api.py                 ┐
├── AudioServer.py         │
├── config.py              │ everything from
├── relay.aiml             ├─ "Python Backend server files/"
├── sensor.aiml            │
├── hey_genie.onnx         │
├── requirements.txt       │
├── piper_voices/          ┘   (keep this subfolder intact)
│   ├── en_US-amy-medium.onnx
│   └── en_US-amy-medium.onnx.json
└── Services/              ← everything from "Services files/"
    ├── whisper_service.py
    ├── api …  (16 .py files)
    └── …
```

Two rules that matter:

- **`relay.aiml` and `sensor.aiml` must sit beside `config.py`.** It finds them
  relative to its own location.
- **`piper_voices/` must stay a subfolder**, and you must run Python from the
  `GENIE/` folder. The wake-word model, the voice and the recordings folder are
  all found relative to the working directory.

### Step 2 — install the Python packages

```
cd GENIE
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### Step 3 — flash the boards

See `Sensor files/README.md` and `Mic+speaker esp files/README.md`. Each board gets a flat
set of files at its device root — no folders on the device, because every
import in the firmware is flat.

Before flashing, run `check_pack.py` if you have it: it catches an incomplete
upload set, which otherwise shows up as an `ImportError` for a file that is
sitting right there on your PC.

### Step 4 — start the laptop side

Four terminals, in this order:

```
ollama serve
```
```
cd GENIE
python api.py
```
```
cd GENIE
python AudioServer.py
```
```
cd GENIE
python Services\whisper_service.py
```

`api.py` alone is enough to see sensor values. `whisper_service.py` is what
makes GENIE talk, and it takes about a minute to load Whisper, Mistral and
Piper.

### Step 5 — check it is alive

```
python Services\sensor_monitor.py
```

A live table of all twelve sensor channels. Open
**http://127.0.0.1:8000/sensors** for the same thing as JSON.

**No boards to hand?** `python Services\fake_node.py --list` simulates them,
including the failure cases, so the whole pipeline can be demonstrated on the
laptop alone.

---

## Talking to it

Say **"Hey Genie"**, wait for the orange light, then ask.

| Say | Answered from |
|---|---|
| "is there smoke" / "is there a fire" | the gas and flame sensors |
| "what is the temperature" | DHT11 |
| "is anyone in the room" / "am I alone" | the ToF array, any direction |
| "is someone in front of you" | the ToF array, front only |
| "how far away is it" / "which side" | nearest ToF reading, spoken in cm |
| "did you move" / "are you tilted" | the IMUs — the unit itself |
| "has anyone fallen" / "are you ok" | the fall detector |
| "check all sensors" | everything, with a safety verdict |
| "turn on the light" / "is the fan on" | the relay board |
| "this is an emergency" / "help me" | SMS, then a phone call |

Everything in that table is answered from stored sensor readings by
`sensor.aiml`, **never** by the language model. The model is asked only when
nothing else matches, and it is explicitly forbidden from stating a sensor
reading — a model that can invent *"no, there's no smoke"* is more dangerous
than one that says *"ask me directly"*.

---

## One principle worth knowing

**An unreadable sensor is never reported as a safe one.**

A sensor that has failed, or a board that has gone quiet, reports `unknown` —
never `clear`. `unknown` cannot raise an alarm and cannot give an all-clear;
it produces *"I cannot read the smoke sensor right now."*

This matters more than it sounds. A stale reading that looks live is worse
than a visibly dead sensor, because it reads as a confident all-clear over a
blind spot. Several parts of the system exist only to preserve that
distinction — the `_ok` health flags, the link staleness timer, and the
separation between "nothing detected" and "nothing known".

---

## Before a live demonstration

- ⚡ **Use the external 5 V supply, and tie the grounds.** Four ToF sensors
  ranging draw about 160 mA between them; the radio adds 350 mA bursts; the
  amplifiers peak near 1 A. USB alone does not carry this, and the symptoms of
  trying — sensors that work in a different random combination on every boot,
  a display that resets repeatedly — look exactly like software faults.
- **Calibrate the gas baseline** with `genie_menu.run()` → option 9, in clean
  air. Without it smoke can never reach the alarm threshold. A new MQ-2 also
  needs 24–48 hours powered before its baseline settles.
- **Set the face board's MAC** in `node2_main.py`. Broadcast works, but every
  ESP-NOW device in the room receives it.
- **Check the WiFi channel matches on both sensor boards** — run `wifi_cfg.py`
  on each and compare. ESP-NOW and WiFi share one radio, and a mismatch drops
  every frame in silence, with no error on either side.
- **Generate `emergency_mic.wav`** and upload it to the modem board, or the
  emergency call will connect and play nothing.

---

## Credits

| | |
|---|---|
| **M. Suhaib** | Audio pipeline, laptop server, integration |
| **Arman Adil Mangat** | Sensor nodes, the ESP-NOW link, enclosure — [@ArmanAdilMangat](https://github.com/ArmanAdilMangat) |
| **M. Farhal Raza** | IoT relay box |
| **Huzaifa Furqan** | Documentation |
| **Mahmood Hussain** | Supervisor |

A parallel upload of this project, from the sensor side, is at
[ArmanAdilMangat/GENIE](https://github.com/ArmanAdilMangat/GENIE).
