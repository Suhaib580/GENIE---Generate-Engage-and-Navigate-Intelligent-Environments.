"""
Turns the room's own readings into action: warm or humid runs the air relay.

    api.py /sensors  ->  this file  ->  POST /relay  +  a spoken reason

Third watcher, same shape as the other two, and the distinction between
them is worth keeping straight:

    sensor_watch    reads STATE, asks "is this an emergency"
    reaction_watch  reads MOMENTS, asks "did something just happen"
    climate_watch   reads STATE, asks "should I do something about it"

This one is the only one that CHANGES the room rather than describing it,
which is why every rule below is about not surprising the person in it.

--------------------------------------------------------------------------
ONE DECISION, NOT SEVERAL RULES
--------------------------------------------------------------------------
The obvious build is a rule per condition -- "hot turns the fan on",
"humid turns the dehumidifier on" -- and it breaks the moment two of them
address the same relay, because nothing decides which wins. With one
physical fan wired to relay 1, "hot" and "humid" are two reasons for the
same switch.

So `decide()` takes every reading at once and returns ONE desired state
plus the reason for it. Conflicts cannot arise because there is only ever
one answer. It also scales the way the user wants: a second relay means a
second call to decide(), not a rewrite.

--------------------------------------------------------------------------
WHAT IT WILL NOT DO
--------------------------------------------------------------------------
  Flap.        Two thresholds per condition, so a room hovering at 28.0
               does not switch the fan every ten seconds.
  Nag.         It speaks only when the state CHANGES, never while it holds.
  Overrule.    "Turn off the fan" wins, and keeps winning until the
               situation itself changes.
  Guess.       A missing or unreadable value is not a cold room. If the
               DHT11 stops answering, the rule holds its last decision
               rather than switching anything off.
"""

import sys
import os
import threading
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import requests

from config import (
    IOT_API_URL,
    CLIMATE_ENABLED,
    CLIMATE_POLL_SECONDS,
    CLIMATE_TEMP_ASK,
    CLIMATE_TEMP_ACT,
    CLIMATE_TEMP_OFF,
    CLIMATE_HUMID_ON,
    CLIMATE_HUMID_OFF,
    CLIMATE_COOLING_NAME,
    CLIMATE_DEHUMIDIFIER_NAME,
)

# The relay this watcher owns. LIGHT is deliberately untouched: nothing the
# face board senses says anything about whether a light should be on. That
# becomes possible when the ToF array works -- somebody in the room is a
# reason for a light in a way that temperature never is.
AIR = "FAN"


def decide(temp_c, humidity):
    """(action, band, words) from all readings at once.

    action is one of:
        "on"       switch it on and say so
        "off"      switch it off and say so
        "suggest"  SAY it and stop -- the decision stays with the person
        None       no opinion -- hold whatever is currently true

    `band` names the SITUATION, not the action. The override watches it, so
    that overruling a warm room does not also silence the rule when the
    room later turns humid as well; the suggest-once bookkeeping does too.

    None is NOT False. An unreadable DHT11 must not read as a cold, dry
    room -- the caller holds its last decision rather than switching
    hardware off on the strength of a sensor that stopped answering.
    """
    if temp_c is None and humidity is None:
        return None, "unknown", "no temperature or humidity reading"

    act_hot = temp_c is not None and temp_c >= CLIMATE_TEMP_ACT
    ask_hot = temp_c is not None and temp_c >= CLIMATE_TEMP_ASK
    humid = humidity is not None and humidity >= CLIMATE_HUMID_ON

    # Releasing uses the LOWER threshold, so the room has to genuinely
    # improve before the fan stops. Between the two it holds whatever it
    # was doing, which is the whole point of having two.
    cool = temp_c is not None and temp_c <= CLIMATE_TEMP_OFF
    dry = humidity is not None and humidity <= CLIMATE_HUMID_OFF

    if act_hot and humid:
        return "on", "hot+humid", ("It is %.0f degrees and %.0f percent "
                                   "humidity. Turning on the %s."
                                   % (temp_c, humidity,
                                      CLIMATE_DEHUMIDIFIER_NAME))
    if act_hot:
        return "on", "hot", ("The weather is hot, %.0f degrees. "
                             "Turning on the %s."
                             % (temp_c, CLIMATE_COOLING_NAME))
    if humid:
        # Named for the JOB, not the part. One relay serves both roles, so
        # what GENIE calls it follows what it is doing about the reading
        # that triggered it -- humidity gets the dehumidifier, heat gets
        # the fan. Swap in a real dehumidifier and only these two config
        # strings change.
        return "on", "humid", ("The air is humid at %.0f percent. "
                               "Turning on the %s."
                               % (humidity, CLIMATE_DEHUMIDIFIER_NAME))
    if ask_hot:
        # Warm, not hot. SUGGEST and stop -- no question, no waiting, no
        # pending state to get out of step with the conversation.
        #
        # An earlier build asked "shall I turn the fan on?" and held a
        # pending yes/no. Handing the decision back entirely is better on
        # every count: the answer arrives as an ordinary "turn on the fan"
        # through the command path that already exists and already works,
        # GENIE never holds a question that can go stale, and a suggestion
        # ignored costs nothing.
        return "suggest", "warm", ("It is %.0f degrees in here. You can "
                                   "command me to turn on the %s if you "
                                   "would like."
                                   % (temp_c, CLIMATE_COOLING_NAME))

    if cool and dry:
        return "off", "comfortable", "It is comfortable now. Turning the fan off."
    if cool and humidity is None:
        return "off", "comfortable", "It has cooled down. Turning the fan off."
    if dry and temp_c is None:
        return "off", "comfortable", "The air is drier now. Turning the fan off."

    # In the gap between the on and off thresholds: hold.
    return None, "between", "between thresholds, holding"


class ClimateWatcher:
    """Polls the readings; calls on_change(device, on, reason) to act."""

    def __init__(self, on_change, api_url=None, interval=None, log=print):
        self.on_change = on_change
        self.api_url = (api_url or IOT_API_URL).rstrip("/")
        self.interval = CLIMATE_POLL_SECONDS if interval is None else interval
        self.log = log

        self.state = None           # what the rule last asked for
        self.band = None            # the situation that asked for it
        self._override_band = None  # band in force when a human overruled
        self._asked_band = None     # band we have already offered about
        self._warned = False
        self.acted = 0

    # ------------------------------------------------------------------
    def manual_override(self, device, on):
        """A person just commanded this relay by voice. Their word stands.

        Called by whisper_service on every relay command. The rule stops
        touching this relay until the band changes -- a different
        situation earns the automation another say, a timer does not.
        """
        if str(device).upper() != AIR:
            return
        self._override_band = self.band or "unknown"
        self.state = bool(on)
        self.log("[climate] manual %s %s - automatic control paused until "
                 "the conditions change (was '%s')"
                 % (device, "ON" if on else "OFF", self._override_band))

    # ------------------------------------------------------------------
    def _fetch(self):
        try:
            r = requests.get("%s/sensors" % self.api_url, timeout=4)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            if not self._warned:
                self._warned = True
                self.log("[climate] cannot reach api.py (%r) - climate "
                         "control is OFF until it answers" % e)
            return None
        if self._warned:
            self._warned = False
            self.log("[climate] api.py reachable again")
        return data

    def poll_once(self):
        """One cycle. Returns the reason acted on, or None."""
        readings = self._fetch()
        if readings is None:
            return None

        temp = (readings.get("TEMPERATURE") or {}).get("value")
        humid = (readings.get("HUMIDITY") or {}).get("value")
        action, band, words = decide(temp, humid)

        # The situation changed, so earlier decisions about it no longer
        # apply -- neither a human's override nor an offer already made.
        # Overruling a warm room said nothing about a humid one, and an
        # offer declined at 29 degrees should be made again at 33.
        if band != self.band:
            if self._override_band is not None:
                self.log("[climate] conditions changed (%s -> %s) - "
                         "automatic control resumed"
                         % (self._override_band, band))
                self._override_band = None
            self._asked_band = None
        self.band = band

        if action is None:
            return None                 # no opinion, or nothing readable
        if self._override_band is not None:
            return None                 # a human said otherwise

        if action == "suggest":
            # Never suggest something already done. Beyond being
            # nonsense, this is what stops the two tiers flapping into
            # each other: a room drifting across the act threshold moves
            # hot -> warm -> hot, and each band change would otherwise
            # re-arm the offer even though the fan never stopped.
            if self.state is True:
                return None
            # ONCE per situation. An assistant that re-offers the same
            # thing every ten seconds is worse than one that never offers.
            if self._asked_band == band:
                return None
            self._asked_band = band
            if not CLIMATE_ENABLED:
                self.log("[climate] would suggest %s (%s) - disabled"
                         % (AIR, band))
                return None
            self.on_change(AIR, "suggest", words)
            return words

        want = (action == "on")
        if want == self.state:
            return None                 # already there; say nothing

        self.state = want
        self.acted += 1
        if not CLIMATE_ENABLED:
            self.log("[climate] would %s %s (%s) - disabled"
                     % ("turn on" if want else "turn off", AIR, band))
            return None

        self.on_change(AIR, want, words)
        return words

    def run(self):
        if not CLIMATE_ENABLED:
            self.log("[climate] disabled (CLIMATE_ENABLED = False)")
            return
        self.log("[climate] watching: offer above %.0f C, act above "
                 "%.0f C or %.0f%% RH, off below %.1f C and %.0f%%, "
                 "every %.0fs"
                 % (CLIMATE_TEMP_ASK, CLIMATE_TEMP_ACT,
                    CLIMATE_HUMID_ON, CLIMATE_TEMP_OFF,
                    CLIMATE_HUMID_OFF, self.interval))
        while True:
            try:
                self.poll_once()
            except Exception as e:
                self.log("[climate] poll blew up: %r" % e)
            time.sleep(self.interval)


def start(on_change, **kwargs):
    watcher = ClimateWatcher(on_change, **kwargs)
    threading.Thread(target=watcher.run, daemon=True).start()
    return watcher


# ======================================================================
# self-test -- no api.py, no hardware:  python Services/climate_watch.py
# ======================================================================
def _selftest():
    fails = [0]
    acted = []

    def check(label, cond, detail=""):
        print("  %-5s %s%s" % ("PASS" if cond else "FAIL", label,
                               "" if cond else "   <-- %s" % (detail,)))
        if not cond:
            fails[0] += 1

    class Fake(ClimateWatcher):
        def __init__(self, script):
            ClimateWatcher.__init__(
                self, lambda d, act, why: acted.append((d, act)),
                log=lambda *a: None)
            self.script = list(script)

        def _fetch(self):
            if not self.script:
                return None
            t, h = self.script.pop(0)
            return {"TEMPERATURE": {"value": t}, "HUMIDITY": {"value": h}}

    print("--- two tiers: suggest when warm, act when hot ---")
    check("29 C suggests rather than acts", decide(29, 50)[0] == "suggest",
          decide(29, 50))
    # Asserts the CONTRACT -- it states a fact and leaves the doing to the
    # person -- not one particular phrasing. This checked for the literal
    # word "please" and broke when the wording changed to "you can command
    # me to...", which is the same suggestion and arguably a better one.
    # A question mark is the thing that would actually be wrong here,
    # because nothing is waiting for an answer.
    words = decide(29, 50)[2]
    check("it states, and leaves the doing to the person",
          "?" not in words and "you can" in words.lower(), words)
    check("33 C acts without asking", decide(33, 50)[0] == "on",
          decide(33, 50))
    check("25 C turns it off", decide(25, 50)[0] == "off")
    check("75 percent RH acts even when cool", decide(24, 75)[0] == "on",
          decide(24, 75))
    check("27 C is below the offer line and holds",
          decide(27, 50)[0] is None, decide(27, 50))
    check("hot AND humid is its own band", decide(33, 75)[1] == "hot+humid")

    print("--- a suggestion is made ONCE per situation ---")
    acted[:] = []
    w = Fake([(29, 50)] * 5)
    for _ in range(5):
        w.poll_once()
    check("suggested once, not five times", acted == [("FAN", "suggest")], acted)

    print("--- but warm becoming HOT earns a fresh decision ---")
    acted[:] = []
    w = Fake([(29, 50), (33, 50)])
    w.poll_once(); w.poll_once()
    check("suggested, then acted",
          acted == [("FAN", "suggest"), ("FAN", True)], acted)

    print("--- an unreadable sensor is not a cold room ---")
    check("both missing -> no opinion", decide(None, None)[0] is None)
    check("and it is called out as unknown", decide(None, None)[1] == "unknown")
    check("temp alone still decides", decide(33, None)[0] == "on")

    print("--- it does not flap at the boundary ---")
    acted[:] = []
    w = Fake([(32.1, 50), (31.9, 50), (32.1, 50), (31.5, 50)])
    for _ in range(4):
        w.poll_once()
    check("one switch-on, no chatter", acted == [("FAN", True)], acted)

    print("--- and does not repeat itself while the state holds ---")
    acted[:] = []
    w = Fake([(33, 50)] * 5)
    for _ in range(5):
        w.poll_once()
    check("spoke once, not five times", acted == [("FAN", True)], acted)

    print("--- it turns off once the room is genuinely comfortable ---")
    acted[:] = []
    w = Fake([(33, 50), (26.0, 60)])
    w.poll_once(); w.poll_once()
    check("on then off", acted == [("FAN", True), ("FAN", False)], acted)

    print("--- a person overrules it, and keeps overruling it ---")
    acted[:] = []
    w = Fake([(33, 50), (34, 50), (35, 50)])
    w.poll_once()                       # rule turns it on
    w.manual_override("FAN", False)     # "turn off the fan"
    acted[:] = []
    w.poll_once(); w.poll_once()
    check("stays off while it is still just hot", acted == [], acted)

    print("--- but a NEW situation earns the rule another say ---")
    acted[:] = []
    w = Fake([(33, 50), (33, 80)])
    w.poll_once()
    w.manual_override("FAN", False)
    acted[:] = []
    w.poll_once()                       # now hot AND humid: a new band
    check("hot+humid overrides the override", acted == [("FAN", True)], acted)

    print("--- overriding the LIGHT does not pause the fan rule ---")
    acted[:] = []
    w = Fake([(33, 50)])
    w.manual_override("LIGHT", False)
    w.poll_once()
    check("fan still acts", acted == [("FAN", True)], acted)

    print("--- api.py unreachable changes nothing ---")
    acted[:] = []
    w = Fake([])                        # _fetch returns None
    check("no action, no crash", w.poll_once() is None and acted == [])

    print()
    if fails[0]:
        print("%d FAILED" % fails[0])
    else:
        print("all climate_watch self-tests passed")
    return fails[0]


if __name__ == "__main__":
    raise SystemExit(1 if _selftest() else 0)
