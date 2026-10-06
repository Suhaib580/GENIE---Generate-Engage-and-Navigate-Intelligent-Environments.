# WakeWord files

The custom **"Hey Genie"** wake word, and the recordings it was trained on.

```
hey_genie.onnx      the trained model — 205 KB
ww_raw/             original recordings, as captured (5 speakers)
ww_wav/             converted to WAV, plus _manifest.csv
ww_clean/           trimmed and normalised
ww_positives/       the final training clips — 242 files
```

`hey_genie.onnx` is the only file the running system needs. It is also copied
into `Python Backend server files/`, because `config.py` looks for it in the project
root. Everything else here is the evidence of how it was made.

---

## Why a custom model

The wake-word library ships with ready-made models — "hey jarvis",
"alexa", "hey mycroft". Any of those would have worked technically.

Training our own on the actual phrase, recorded by the actual people who would
use it, in the actual room, does two things a stock model cannot:

- It responds to **"Hey Genie"**, which is the assistant's name.
- It is tuned to **these five voices**, including the accents and the room.

---

## The recordings

**242 clips from 5 speakers**, close to evenly balanced:

| Speaker | Clips |
|---|---|
| arman | 49 |
| huzaifa | 49 |
| farhal | 48 |
| sir | 48 |
| suhaib | 48 |

Balance matters. A model trained mostly on one voice learns that voice rather
than the phrase, and then works beautifully for whoever recorded the most and
poorly for everyone else — including, at a demonstration, the examiner.

### The four stages

| Folder | What happened |
|---|---|
| `ww_raw/` | Phones, straight off the device (`.m4a`) |
| `ww_wav/` | Converted to WAV at a uniform sample rate. `_manifest.csv` records `person, source file, output file, duration` |
| `ww_clean/` | Silence trimmed, levels normalised, so no clip is louder just because someone held the phone closer |
| `ww_positives/` | The final flat set, named `<speaker>_<number>.wav` |

The manifest is what makes this reproducible: every finished clip can be
traced back to the original recording it came from.

---

## How it is used

`Services/wake_word.py` loads it through openWakeWord, and `config.py` sets
how eager it is:

```python
WAKE_MODEL       = "hey_genie.onnx"
WAKE_THRESHOLD   = 0.65   # score above this counts as a hit
WAKE_CONSECUTIVE = 3      # this many in a row before it fires
WAKE_COOLDOWN    = 1.0    # seconds of deafness after firing
```

**Three consecutive hits, not one.** A single frame crossing the threshold is
a coincidence; three in a row is the phrase. This was raised from two after
false triggers persisted — most of a second of continuous agreement now.

Tuning, if you need it:

| Problem | Change |
|---|---|
| Wakes up on its own | raise `WAKE_THRESHOLD` toward 0.75, or `WAKE_CONSECUTIVE` to 4 |
| Does not hear you from across the room | lower the threshold toward 0.55 first, before touching `WAKE_CONSECUTIVE` |

The wake word listens to **both microphones summed together**, which is worth
around 3 dB of range over a single one.

---

## Retraining

If you add speakers or want to redo it:

1. Record each person saying "Hey Genie" 40–60 times — different distances,
   different rooms, a few while something else is going on. Keep the counts
   per speaker roughly equal.
2. Drop them in `ww_raw/<speaker>/`.
3. Convert to WAV, trim and normalise into `ww_clean/`, then flatten into
   `ww_positives/` as `<speaker>_<number>.wav`.
4. Train with the openWakeWord training pipeline, which supplies its own
   negative examples.
5. Replace `hey_genie.onnx` in **both** places — here and
   `Python Backend server files/`.

**Test it before trusting it.** The useful test is not "does it wake when I
say it" — it is **leave it running in a quiet room for five minutes and count
how many times it wakes on its own.** That number should be zero. It is the
one people skip, and the one that embarrasses you in front of an examiner.

