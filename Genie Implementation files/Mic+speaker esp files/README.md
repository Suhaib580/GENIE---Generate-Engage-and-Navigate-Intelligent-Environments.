# Mic+speaker esp files — the audio boards

Two ESP32-S3 boards, one for hearing and one for speaking. Both run
**MicroPython**, edited in **Thonny**.

| File | Board | Hardware |
|---|---|---|
| `mic esp.py` | **microphone board** | 2× INMP441 MEMS microphones |
| `speaker esp.py` | **speaker board** | MAX98357A class-D amplifier → speaker |

Save each one to its board as **`main.py`** so it starts on power-up.

> **Rename them without spaces first** — `mic_esp.py`, `speaker_esp.py`.
> Spaces in filenames break `mpremote` and cannot be imported as modules.

---

## How they find the laptop

Neither board has an IP address written into it. Both join the WiFi network,
then **listen for a UDP broadcast on port 9999** which the laptop sends every
two seconds, and connect back to whoever sent it.

That means the laptop's address can change — a new DHCP lease, a different
router — without reflashing anything.

Both boards use:

```python
SSID     = "GENIE1"
PASSWORD = "12345678"
```

This must match `config.py` on the laptop **and** `wifi_cfg.py` on the two
sensor boards. Five files in total, and they all have to agree.

---

## Microphone board — `mic esp.py`

### Wiring

Both microphones share **one** I²S bus. The only difference between them is
the L/R pin, which decides which half of the stereo stream each one fills.

| INMP441 pin | Mic A (left) | Mic B (right) |
|---|---|---|
| VDD | 3.3 V | 3.3 V |
| GND | GND | GND |
| SCK | **GPIO 4** | **GPIO 4** |
| WS | **GPIO 5** | **GPIO 5** |
| SD | **GPIO 6** | **GPIO 6** |
| **L/R** | **GND** | **3.3 V** |

Status LED: the onboard NeoPixel on **GPIO 48**.

### What it does

Captures 16 kHz, 16-bit stereo and streams it continuously to the laptop on
**TCP port 9000**. It does no processing of its own — no wake word, no
filtering. All of that happens on the laptop in `AudioServer.py`, because it
needs a real CPU.

Sharing one I²S bus is what makes the two microphones **sample-synchronous**,
which is what lets the laptop simply add the two channels together to improve
range. Two separate buses would drift apart and the trick would not work.

### What the light means

| Colour | Meaning |
|---|---|
| Blinking blue | looking for WiFi, or for the laptop |
| Green | connected, streaming |
| **Orange** | the wake word fired — **GENIE is listening to you now** |
| Blinking red | lost the connection, retrying |

---

## Speaker board — `speaker esp.py`

### Wiring

| MAX98357A pin | Goes to |
|---|---|
| Vin | 5 V |
| GND | GND — **must be shared with the ESP32's ground** |
| BCLK | **GPIO 15** |
| LRC | **GPIO 16** |
| DIN | **GPIO 17** |

Status LED: NeoPixel on **GPIO 48**. Audio runs at **22050 Hz**, which must
match Piper's output rate on the laptop.

### What it does

Connects to the laptop on **TCP port 9001** and waits for single-byte
commands:

| Opcode | Means | The board does |
|---|---|---|
| 1 | thinking | pulse purple |
| 2 | speaking, starting | solid purple |
| 3 | audio chunk | 4-byte length, then raw audio to play |
| 4 | speaking, finished | back to idle |
| 5 | listening started | orange, rising beep |
| 6 | listening stopped | falling beep, idle |

**These numbers must match `config.py` exactly.** They are just bytes on a
wire — if the two sides disagree, the board will quietly ignore commands it
does not recognise and produce no sound and no error.

---

## Wiring the amplifier — read before soldering

The amplifier draws **sudden current spikes well over half an amp** while it
speaks. Everything below follows from that.

- **Do not run its power through a breadboard or a dupont connector.** Those
  are spring contacts. They pass a small current fine and add resistance under
  a spike, and the amplifier browns out and cuts off. Solder the power and the
  speaker wires directly.

- **Fit a 470 µF electrolytic capacitor across Vin and GND**, as close to the
  amplifier's pins as possible, with short legs. It holds a local reserve for
  the spikes. The **long leg goes to Vin, the striped side to GND** — backwards
  it will overheat and can burst. Keep its solder away from the neighbouring
  **SD** pin: SD connected to ground is the amplifier's off switch.

- **Check the power supply's rating.** Two amplifiers plus a board with WiFi
  can peak near 2 A. A 1 A adapter will sag no matter what capacitor you fit.

- ⚠ **A multimeter reading 5 V proves very little here.** With no current
  flowing there is no voltage drop across a bad joint, so a broken connection
  reads a perfect 5 V at rest and collapses the moment the amplifier speaks.
  **Measure at the amplifier's own pins while it is talking.**

### If it goes quiet at a specific point

If the sound cuts out at one exact position of a plug or a wire, and returns
at one exact position, that is a **mechanical contact**, not software and not a
bad solder joint. Loose joints fail randomly; a repeatable position is a
switch. Check bare crimps that can touch each other, and connectors that have
lost their plastic housing — a housing is what holds a crimp firmly on its
pin, and a shorted speaker output makes the amplifier shut itself off and
retry, which sounds exactly like *"speaks a little, then stops"*.
