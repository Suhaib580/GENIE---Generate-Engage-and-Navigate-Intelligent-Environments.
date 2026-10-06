# GENIE — setup guide

**For: Mahmood Hussain (Supervisor)**
**From: M. Suhaib — BS Artificial Intelligence, FYP, UMT Lahore**

This guide installs GENIE on a Windows PC from nothing. Everything in it was run
on the development machine; the versions and sizes below are measured, not
estimated.

Read this file rather than the folder table in `README.md`. `README.md` explains
what the system *is*; this one explains how to make it run.

---

## First, the honest version

**You do not need the hardware to see GENIE work.** The four microcontrollers are
what make it a device; the laptop half is what makes it intelligent, and the
laptop half runs on its own with simulated sensors.

So there are two levels, and you can stop after either one:

| | What you get | What it costs |
|---|---|---|
| **Level 1 — laptop only** | The full voice pipeline, simulated sensors, every AIML answer, the safety logic | ~40 minutes, ~5 GB of downloads |
| **Level 2 — with the boards** | The real thing: four microcontrollers, twelve sensor channels, the LTE emergency call | Level 1, plus flashing four boards and a 5 V 3 A supply |

If you only want to confirm the project works, **Level 1 is enough**, and
`Running Demo Videos/` shows the hardware doing what Level 1 simulates.

---

## What you have received

This repository contains:

- all the source code (six folders under `Genie Implementation files/`)
- the trained **"Hey Genie"** wake-word model — `hey_genie.onnx`
- both AIML brains — `relay.aiml` and `sensor.aiml`
- the FYP report, as a PDF you can read in the browser

**What it does not contain** is the software that has to be installed, and
three model files that are too large to keep in a code repository. Those are
the next two sections.

---

## Part A — software to install (download these yourself)

Four installers. Only the first two are needed for Level 1.

| # | Software | Where | Needed for |
|---|---|---|---|
| 1 | **Python 3.13** (64-bit) | [python.org/downloads](https://www.python.org/downloads/) | Level 1 — **required** |
| 2 | **Ollama** | [ollama.com/download](https://ollama.com/download) | Level 1 — **required** |
| 3 | **Thonny** | [thonny.org](https://thonny.org) | Level 2 only (~20 MB) |
| 4 | **MicroPython firmware** | [micropython.org/download/ESP32_GENERIC_S3](https://micropython.org/download/ESP32_GENERIC_S3/) | Level 2 only |

Notes on each:

1. **Python** — tick **"Add python.exe to PATH"** on the first screen of the
   installer. This is the single most common cause of a setup failing.

   **Please use 3.13 rather than 3.14, even though 3.14.3 is what I developed
   on.** The reason is one package: `webrtcvad-wheels` publishes ready-made
   Windows binaries for 3.11, 3.12 and 3.13, but **not** for 3.14. On 3.14 pip
   falls back to the source code and tries to compile it, which fails on a
   normal Windows machine with a long wall of errors about Microsoft Visual C++
   Build Tools. On 3.13 every package in `requirements.txt` installs as a
   ready-made binary and nothing needs compiling. I checked all four versions
   directly rather than assuming.

   Anything from 3.11 to 3.13 works. If you specifically want 3.14, you need
   either the Build Tools or the offline bundle described below, which contains
   a 3.14 binary I built here.
2. **Ollama** — runs the language model locally. Large download.
3. **Thonny** — how the microcontrollers are programmed.
4. **MicroPython** — download the build named **`ESP32_GENERIC_S3-SPIRAM_OCT`**,
   version **v1.27.0**. The plain `ESP32_GENERIC_S3` build also installs, and
   then fails at runtime with `WiFi Out of Memory`. See Part H.

---

## Part B — the three model files (the awkward ones)

These are the reason this cannot be a single download.

### 1. Mistral 7B — 4.4 GB

Cannot be sent by email, and not sensibly by USB either. Install it directly,
once Ollama is installed:

```bash
ollama pull mistral
```

It takes a while. Verify with `ollama list` — you should see
`mistral:latest   4.4 GB`.

This is GENIE's conversational brain. Without it, sensor questions and
light/fan commands still work — those are answered by AIML, not by the model —
but general conversation will not.

### 2. The Piper voice — 61 MB

The voice GENIE speaks with. It is a third-party file from the Piper project,
not something this project built, which is why it is not committed here.

Download these two files:

  <https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_US/amy/medium>

    en_US-amy-medium.onnx
    en_US-amy-medium.onnx.json

and put **both** of them in `C:\GENIE\piper_voices\`. Create that folder if
it is not there. Both files are needed — the `.json` describes the voice to
Piper, and without it the `.onnx` is unusable.

If you would rather a different voice, any Piper `en_US` voice works; set
`PIPER_MODEL_PATH` in `config.py` (line 457) to match the filename.

### 3. Whisper "base" — 142 MB

Speech recognition. **You do not have to do anything about this one.**
`faster-whisper` downloads it automatically the first time the pipeline starts,
which needs an internet connection for that one run only. After that it is
cached and GENIE is fully offline.

If the PC will have no internet at all, ask me for the cache folder
(`models--Systran--faster-whisper-base`, 142 MB) and drop it into:

```
C:\Users\<YourName>\.cache\huggingface\hub\
```

---

## Part C — the Python packages

These are the only things installed by `requirements.txt`, and they come to
roughly 500 MB once installed. The file is at `Genie Implementation
files/Python Backend server files/requirements.txt`, with every version pinned
to what the project was demonstrated on.

| Package | Version | What it does |
|---|---|---|
| `fastapi`, `uvicorn[standard]`, `pydantic`, `python-multipart` | 0.140.13 / 0.52.0 / 2.13.4 / 0.0.32 | the `api.py` server |
| `requests` | 2.34.2 | talking to Ollama and to `api.py` |
| `faster-whisper` | 1.2.1 | speech to text |
| `openwakeword` | 0.6.0 | the "Hey Genie" detector |
| `onnxruntime` | 1.28.0 | runs both `.onnx` models |
| `webrtcvad-wheels` | 2.0.14 | detects when you stop speaking |
| `piper-tts` | 1.6.0 | text to speech |
| `aiml.py` | 0.9.2 | the AIML interpreter |
| `pyserial` | 3.5 | the USB link to the boards |
| `numpy` | 2.5.1 | audio maths |

**One trap worth knowing about**, because the error message is misleading: the
package is **`webrtcvad-wheels`**, not `webrtcvad`. They are the same library
and both import as `webrtcvad`. The plain one is source-only on every Python
version — on Windows it tries to compile C code and fails with a wall of errors
about Microsoft Visual C++ Build Tools. The shipped `requirements.txt` already
specifies the `-wheels` build, so this only bites if someone "corrects" the
name.

The `-wheels` build ships ready-made binaries **for Python 3.11, 3.12 and 3.13
— but not for 3.14**, where it also falls back to source. That is the whole
reason Part A asks for 3.13. If you see those Build Tools errors on 3.14, this
is why, and it is not something you have done wrong.

### Installing these with no internet

**A `wheels\` folder is included at the root of the USB stick** — every package
above and all of their dependencies, pre-downloaded. You do not need a
connection for this part.

Copy `wheels\` into `C:\GENIE\` alongside `requirements.txt`, and Step 2's
install command becomes:

```bash
pip install --no-index --find-links wheels -r requirements.txt
```

**That bundle only works on 64-bit Windows with Python 3.14.** Several of these
packages ship compiled binaries built against one specific Python version —
`webrtcvad-wheels`, for instance, is tagged `cp314-win_amd64`. On Python 3.11 or
3.13 pip will report that it cannot find a matching distribution, which reads
like a corrupt download but is really a version mismatch. If you would rather
use a different Python, skip the bundle and let Step 2 install from the internet
normally.

That covers the Python packages. Ollama and Mistral still have to be fetched on
a machine that does have a connection; there is no way around 4.4 GB.

---

## Part D — setting it up, step by step

### Step 1 — assemble the project folder

**This step is necessary and easy to miss.** The six code folders in this
submission are organised *by category, so they can be read and marked*. Python
finds its imports by folder shape, so they have to be copied into the shape the
code expects before it will run.

Make a new folder — `C:\GENIE` is fine — and copy files into it like this:

```
C:\GENIE\
├── api.py                 ┐
├── AudioServer.py         │
├── config.py              │  everything from
├── relay.aiml             ├─ "Python Backend server files\"
├── sensor.aiml            │  (the files, not the folder itself)
├── hey_genie.onnx         │
├── requirements.txt       │
├── piper_voices\          ┘  ← keep this a sub-folder, do not flatten it
│   ├── en_US-amy-medium.onnx
│   └── en_US-amy-medium.onnx.json
└── Services\                 ← everything from "Services files\"
    ├── whisper_service.py
    ├── node_bridge.py
    └── …  (16 .py files in all)
```

Three rules:

1. **The contents of `Python Backend server files\` go in the root** of
   `C:\GENIE\` — not the folder, its contents.
2. **`Services\` must be named exactly that.** The imports say
   `from Services import …`.
3. **`piper_voices\` stays a sub-folder**, and you must run Python *from*
   `C:\GENIE\`. The voice, the wake-word model and the recordings folder are all
   located relative to the working directory.

The `README.md` inside each code folder is documentation; copying it across does
no harm.

### Step 2 — install the Python packages

Open **Command Prompt** and run these one at a time:

```bash
cd C:\GENIE
```
```bash
python -m venv .venv
```
```bash
.venv\Scripts\activate
```
```bash
pip install -r requirements.txt
```

After `activate`, the prompt gains a `(.venv)` prefix. **Every command from here
on needs that prefix** — so if you open a new Command Prompt window, run
`cd C:\GENIE` and `.venv\Scripts\activate` in it first.

The install takes several minutes; `onnxruntime` and `faster-whisper` are large.

Then check it worked:

```bash
python -c "import fastapi, faster_whisper, openwakeword, webrtcvad, piper, aiml, serial, numpy; print('all imports OK')"
```

If that prints `all imports OK`, the Python side is done.

### Step 3 — get the language model running

```bash
ollama pull mistral
```

Then leave this running in **its own window**, and do not close it:

```bash
ollama serve
```

On Windows, Ollama often starts its own background service at login. If
`ollama serve` complains that the address is already in use, that is fine — it
means it is already running, and you can close that window.

### Step 4 — start GENIE

Three more Command Prompt windows, in this order. Each one needs `cd C:\GENIE`
and `.venv\Scripts\activate` first.

**Window 1 — the server.** Holds the sensor readings and the relay control:

```bash
python api.py
```

**Window 2 — the audio server.** Only needed with the real microphone board;
harmless to skip for Level 1:

```bash
python AudioServer.py
```

**Window 3 — the voice pipeline.** This is the one that makes GENIE talk:

```bash
python Services\whisper_service.py
```

**Window 3 takes about a minute to start** — it is loading Whisper, connecting to
Mistral and warming up Piper. That silence is normal; wait for it to report that
it is listening before deciding something is wrong.

---

## Part E — checking it actually works

### Is the server alive?

Open **http://127.0.0.1:8000/sensors** in a browser. You should get JSON with
twelve sensor channels. With no boards connected most will read `unknown` — that
is correct behaviour, not a fault, and the reason is the one design decision
worth knowing about:

> **An unreadable sensor is never reported as a safe one.** A failed sensor, or a
> board that has gone quiet, reports `unknown` — never `clear`. `unknown` cannot
> raise an alarm and cannot give an all-clear; it produces *"I cannot read the
> smoke sensor right now."* A stale reading that looks live is more dangerous
> than a visibly dead one, because it reads as a confident all-clear over a blind
> spot.

### A live view of all twelve channels

```bash
python Services\sensor_monitor.py
```

### Running it with no hardware at all

This is the Level 1 demonstration. It simulates both sensor boards, including
their failure modes:

```bash
python Services\fake_node.py --list
```

That prints the twelve available scenarios. Then run one — note that the
`--scenario` flag is required, because a bare name is ignored and it quietly
runs `idle` instead:

```bash
python Services\fake_node.py --scenario smoke
```

Leave that running and watch it in another window with `sensor_monitor.py`, or
in the browser at **http://127.0.0.1:8000/sensors**.

The twelve scenarios, which are also the twelve things worth demonstrating:

| Scenario | What it shows |
|---|---|
| `idle` | a quiet room, nothing happening |
| `smoke` | the MQ-2 climbing past the alert line, then the alarm |
| `fire` | flame confirmed — the fastest alarm path |
| `fall` | the fall detector confirmed and latched |
| `person` | someone standing to the left, 65 cm away |
| `approach` | someone walking towards the front sensor |
| `moved` | the unit itself picked up — `MOTION`, not `PRESENCE` |
| `knock` | somebody tapping the case, the counter climbing |
| `blind` | **every safety sensor unreadable — it must not say "all clear"** |
| `dead-mq2` | gas wire off: reads `unknown`, never `clear` |
| `dead-tof` | the ToF board gone, the face board still fine |
| `dead-imu` | both IMUs gone: `MOTION` unknown, tilt and acceleration null |

The bottom four are there deliberately. `blind` is the one I would ask you to
try if you try only one — it is the scenario that proves the principle quoted
above, and the four failure cases took more care to get right than the four
working ones.

With `fake_node.py` feeding it, every question in the table below is answerable,
and the safety path can be demonstrated end to end on the laptop alone.

### Talking to it

Say **"Hey Genie"**, wait for the acknowledgement, then ask:

| Say | Answered from |
|---|---|
| "is there smoke" / "is there a fire" | the gas and flame sensors |
| "what is the temperature" | DHT11 |
| "is anyone in the room" / "am I alone" | the ToF array, any direction |
| "how far away is it" / "which side" | nearest ToF reading, spoken in cm |
| "did you move" / "are you tilted" | the IMUs — the unit itself |
| "has anyone fallen" / "are you ok" | the fall detector |
| "check all sensors" | everything, with a safety verdict |
| "turn on the light" / "is the fan on" | the relay board |
| "this is an emergency" / "help me" | SMS, then a phone call |

Every one of those is answered from stored sensor readings by `sensor.aiml`,
**never** by the language model. Mistral is asked only when nothing else
matches, and it is explicitly forbidden from stating a sensor reading — a model
that can invent *"no, there's no smoke"* is more dangerous than one that says
*"ask me directly."*

---

## Part F — what to change for your machine

All of it is in `config.py`, in the root of `C:\GENIE\` after Step 1:

| Line | Setting | Change it to |
|---|---|---|
| 1222 | `WIFI_SSID = "GENIE1"` | your access point's name |
| 1223 | `WIFI_PASSWORD = "12345678"` | its password |
| 745 | `EMERGENCY_CONTACTS` | the number the emergency call should reach. **It is a placeholder** (`+923001234567`), so it must be set to a real number before that path will work. The same number is mirrored in `SIMCom A7670E files/settings.py` line 70 |
| 542 | `IOT_ESP_IP` | the relay board's address on your network |
| 400 | `LLM_MODEL_NAME = "mistral"` | leave it, unless you want a different Ollama model |

**If you change the WiFi name or password it must change in five places, and they
must all agree** — `config.py`, `wifi_cfg.py` on each of the two sensor boards,
and the top of both files in `Mic+speaker esp files/`. A mismatch is silent:
ESP-NOW and WiFi share one radio on the ESP32, so boards on different channels
drop every frame with no error on either side.

For Level 1 you can leave all of this alone.

---

## Part G — if something goes wrong

| Symptom | Cause |
|---|---|
| `'python' is not recognized` | Python was installed without **"Add python.exe to PATH"**. Re-run the installer and tick it |
| `ModuleNotFoundError` for anything | The `(.venv)` prefix is missing from the prompt. Run `.venv\Scripts\activate` |
| A wall of C compiler errors about **Visual C++ Build Tools** | You are on Python 3.14, which has no ready-made `webrtcvad-wheels` binary. Use 3.13 instead — see Part A. (The other cause is someone changing `webrtcvad-wheels` back to `webrtcvad` in `requirements.txt`) |
| `whisper_service.py` sits silent for a minute | Normal. It is loading three models |
| `Connection refused` on port 11434 | Ollama is not running. Start `ollama serve` |
| Every sensor reads `unknown` | No boards connected. Correct behaviour — use `fake_node.py` |
| `FileNotFoundError: piper_voices\…` | Python is being run from the wrong folder, or `piper_voices` was flattened. Run from `C:\GENIE\` |
| AIML answers nothing at all | `relay.aiml` and `sensor.aiml` must sit **beside** `config.py` |

---

## Part H — the hardware, honestly

For Level 2, `Genie Implementation files/Sensor files/README.md` and
`Mic+speaker esp files/README.md` have the flashing instructions. Each board
takes a **flat** set of files at its device root — no folders on the device,
because every import in the firmware is flat.

Three things are known issues rather than mysteries, and I would rather write
them down than have you find them:

- ⚡ **The 5 V 3 A external supply is not optional, and the grounds must be
  tied.** Four ToF sensors ranging draw about 160 mA between them, the radio
  adds 350 mA bursts, and the amplifiers peak near 1 A. USB alone does not
  carry this, and the symptoms of trying look exactly like software faults:
  sensors that work in a *different random combination on every boot*, and a
  display that resets repeatedly. That diagnosis cost me several days, which is
  how I know a varying subset cannot be code (code is deterministic) or wiring
  (wiring is static).
- **The face board needs the `SPIRAM_OCT` MicroPython build.** It is an N8R8
  module with octal PSRAM; the default quad-PSRAM firmware boots fine and then
  fails to bring up WiFi with `WiFi Out of Memory`.
- **The gas baseline must be calibrated in clean air** before a demonstration —
  `genie_menu.run()`, option 9. Without it, smoke can never cross the alarm
  threshold. A new MQ-2 also needs 24–48 hours powered before its baseline
  settles.

Two things are genuinely incomplete, and the recordings in
`Running Demo Videos/` show the system working around them:

- **`emergency_mic.wav` has not been generated**, so the emergency call connects
  and plays silence. The SMS escalation works.
- **The speaker amplifier has an unresolved hardware fault** — it works only
  under probe pressure, which traced to a bare crimp on the speaker output. The
  `Speaker working.mp4` recordings were made before that regression.

---

## Anything else

If a step here does not match what you see on screen, that is my error and not
yours — please tell me which step and what it said, and I will correct it.

**M. Suhaib** — suhaibmamoon03@gmail.com
