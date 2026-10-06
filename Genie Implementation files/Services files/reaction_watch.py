"""
Drains api.py's handling-event log and turns it into speech.

    face board IMU -> handling_fsm.py -> {"t":"event","event":"pushed"}
                                              |
                                        api.py /events log
                                              |
                                  GET /events?since=N  (this file)
                                              |
                                   ai.build_reaction_reply()  -> speak()

Runs as a daemon thread alongside sensor_watch, and is deliberately the
same shape: poll, decide nothing, hand the decision to a callback.

WHY IT IS NOT sensor_watch
--------------------------
sensor_watch reads STATE and asks "is this bad". This reads MOMENTS and
asks "did something just happen". A state can be re-read a second later and
still be true; a shove cannot. That difference is the whole reason api.py
keeps an append-only log with a cursor instead of a flag -- a flag either
gets missed between polls or gets spoken twice.

FOUR THINGS IT MUST NOT DO
--------------------------
  Speak a backlog.      On the first poll it jumps the cursor to `latest`
                        rather than replaying from 0. Nobody wants "why
                        are you pushing me" about a shove from before the
                        laptop was even running.
  Speak twice.          The cursor only ever moves forward.
  Speak stale news.     If several events arrive in one window it says the
                        NEWEST only. lifted -> still_lifted -> set_down in
                        one poll means the unit is back on the table, so
                        the only true thing to say is thank you.
  Interrupt.            speak_fn is expected to refuse while GENIE is
                        already talking, and a refused reaction is DROPPED
                        rather than queued -- a reaction to something that
                        happened eight seconds ago is not a reaction.
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
    REACTIONS_ENABLED,
    REACTION_POLL_SECONDS,
    REACTION_MIN_GAP_SECONDS,
)


class ReactionWatcher:
    """Polls /events; calls on_reaction(event_name, entry) for the newest."""

    def __init__(self, on_reaction, api_url=None, interval=None,
                 min_gap=None, log=print):
        self.on_reaction = on_reaction
        self.api_url = (api_url or IOT_API_URL).rstrip("/")
        self.interval = REACTION_POLL_SECONDS if interval is None else interval
        self.min_gap = (REACTION_MIN_GAP_SECONDS if min_gap is None
                        else min_gap)
        self.log = log

        # None means "not yet synced" -- the first successful poll adopts
        # api.py's latest seq so history is skipped, not spoken.
        self.cursor = None
        self._last_spoke = 0.0
        self._warned_api = False
        self.spoken = 0
        self.dropped = 0

    # ------------------------------------------------------------------
    def _fetch(self, since):
        try:
            r = requests.get("%s/events" % self.api_url,
                             params={"since": since}, timeout=4)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            if not self._warned_api:
                self._warned_api = True
                self.log("[reaction_watch] cannot reach api.py (%r) - "
                         "reactions are OFF until it answers" % e)
            return None

        if self._warned_api:
            self._warned_api = False
            self.log("[reaction_watch] api.py reachable again")
        return data

    # ------------------------------------------------------------------
    def poll_once(self, now=None):
        """One cycle. Returns the event spoken, or None.

        Safe to call as often as you like: the cursor and the min-gap are
        both enforced in here, so an extra call can only make a reaction
        happen sooner, never twice. Same property sensor_watch relies on.
        """
        now = time.monotonic() if now is None else now

        data = self._fetch(0 if self.cursor is None else self.cursor)
        if data is None:
            return None

        latest = int(data.get("latest", 0))

        # First successful poll: adopt the current position silently.
        if self.cursor is None:
            self.cursor = latest
            self.log("[reaction_watch] watching for handling events "
                     "(from seq %d, every %.2fs)" % (latest, self.interval))
            return None

        # api.py restarted: its sequence went back to zero while ours did
        # not, so `since=cursor` would match nothing for ever. Re-adopt.
        if latest < self.cursor:
            self.log("[reaction_watch] api.py sequence reset (%d < %d) - "
                     "resyncing" % (latest, self.cursor))
            self.cursor = latest
            return None

        events = data.get("events") or []
        if not events:
            return None

        # We fell behind and api.py dropped entries off the front of the
        # log. Say so rather than pretending the record is complete.
        oldest = int(data.get("oldest", 0))
        if oldest and oldest > self.cursor + 1:
            self.log("[reaction_watch] missed %d event(s) - log rolled over"
                     % (oldest - self.cursor - 1))

        # Always move the cursor past everything we were handed, whether or
        # not we end up speaking. Anything else re-reads them next tick.
        newest = events[-1]
        self.cursor = int(newest.get("seq", self.cursor))

        if not REACTIONS_ENABLED:
            return None

        if now - self._last_spoke < self.min_gap:
            self.dropped += 1
            return None

        name = str(newest.get("event") or "").lower()
        if not name:
            return None

        # The callback is allowed to refuse -- GENIE may already be
        # talking. A refusal is a drop, never a queue.
        if self.on_reaction(name, newest) is False:
            self.dropped += 1
            return None

        self._last_spoke = now
        self.spoken += 1
        return name

    # ------------------------------------------------------------------
    def run(self):
        """Poll forever. Intended as a daemon thread target."""
        if not REACTIONS_ENABLED:
            self.log("[reaction_watch] disabled (REACTIONS_ENABLED = False)")
            return
        while True:
            try:
                self.poll_once()
            except Exception as e:
                # Never let this thread die quietly -- it is the only thing
                # making the board's reactions audible.
                self.log("[reaction_watch] poll blew up: %r" % e)
            time.sleep(self.interval)


def start(on_reaction, **kwargs):
    """Spawn the watcher as a daemon thread. Returns the ReactionWatcher."""
    watcher = ReactionWatcher(on_reaction, **kwargs)
    threading.Thread(target=watcher.run, daemon=True).start()
    return watcher


# ======================================================================
# self-test -- no api.py, no board:  python Services/reaction_watch.py
# ======================================================================
def _selftest():
    fails = [0]
    said = []

    def check(label, cond):
        print("  %-5s %s" % ("PASS" if cond else "FAIL", label))
        if not cond:
            fails[0] += 1

    class Fake(ReactionWatcher):
        """Replaces the network with a scripted list of /events replies."""
        def __init__(self, replies, **kw):
            ReactionWatcher.__init__(
                self, lambda n, e: said.append(n), log=lambda *a: None, **kw)
            self.replies = list(replies)

        def _fetch(self, since):
            return self.replies.pop(0) if self.replies else {"latest": 0,
                                                             "events": []}

    def ev(seq, name):
        return {"seq": seq, "event": name, "at": 0.0}

    print("--- a backlog is skipped, not spoken ---")
    said[:] = []
    w = Fake([{"latest": 9, "oldest": 1, "events": [ev(9, "pushed")]}])
    w.poll_once(now=100)
    check("said nothing on the first poll", said == [])
    check("cursor adopted latest", w.cursor == 9)

    print("--- a new event is spoken ---")
    said[:] = []
    w = Fake([{"latest": 5, "oldest": 1, "events": []},
              {"latest": 6, "oldest": 1, "events": [ev(6, "pushed")]}])
    w.poll_once(now=100)
    w.poll_once(now=200)
    check("spoke pushed", said == ["pushed"])

    print("--- the same event is never spoken twice ---")
    said[:] = []
    w = Fake([{"latest": 5, "oldest": 1, "events": []},
              {"latest": 6, "oldest": 1, "events": [ev(6, "pushed")]},
              {"latest": 6, "oldest": 1, "events": []}])
    w.poll_once(now=100)
    w.poll_once(now=200)
    w.poll_once(now=300)
    check("once only", said == ["pushed"])

    print("--- several in one window: only the NEWEST is true ---")
    said[:] = []
    w = Fake([{"latest": 1, "oldest": 1, "events": []},
              {"latest": 4, "oldest": 1,
               "events": [ev(2, "lifted"), ev(3, "still_lifted"),
                          ev(4, "set_down")]}])
    w.poll_once(now=100)
    w.poll_once(now=200)
    check("said set_down, not all three", said == ["set_down"])

    print("--- the minimum gap holds ---")
    said[:] = []
    w = Fake([{"latest": 1, "oldest": 1, "events": []},
              {"latest": 2, "oldest": 1, "events": [ev(2, "pushed")]},
              {"latest": 3, "oldest": 1, "events": [ev(3, "pushed")]}],
             min_gap=5.0)
    w.poll_once(now=100)
    w.poll_once(now=101)
    w.poll_once(now=102)          # 1 s later, inside the gap
    check("second one dropped", said == ["pushed"])
    check("and counted as dropped", w.dropped == 1)
    check("cursor still advanced past it", w.cursor == 3)

    print("--- a refusal drops, it does not queue ---")
    said[:] = []
    w = Fake([{"latest": 1, "oldest": 1, "events": []},
              {"latest": 2, "oldest": 1, "events": [ev(2, "pushed")]}])
    w.on_reaction = lambda n, e: False        # GENIE is busy talking
    w.poll_once(now=100)
    w.poll_once(now=200)
    check("nothing spoken", said == [])
    check("counted as dropped", w.dropped == 1)

    print("--- api.py restarting does not wedge the cursor ---")
    said[:] = []
    w = Fake([{"latest": 40, "oldest": 30, "events": []},
              {"latest": 2, "oldest": 1, "events": [ev(2, "pushed")]},
              {"latest": 3, "oldest": 1, "events": [ev(3, "lifted")]}])
    w.poll_once(now=100)
    check("cursor at 40", w.cursor == 40)
    w.poll_once(now=200)
    check("resynced down, said nothing yet", said == [] and w.cursor == 2)
    w.poll_once(now=300)
    check("and speaks again afterwards", said == ["lifted"])

    print("--- api.py unreachable is survivable ---")
    said[:] = []
    w = Fake([None, None, {"latest": 1, "oldest": 1, "events": []}])
    w.poll_once(now=100)
    w.poll_once(now=200)
    check("still unsynced, nothing said", w.cursor is None and said == [])
    w.poll_once(now=300)
    check("syncs once it answers", w.cursor == 1)

    print()
    if fails[0]:
        print("%d FAILED" % fails[0])
    else:
        print("all reaction_watch self-tests passed")
    return fails[0]


if __name__ == "__main__":
    raise SystemExit(1 if _selftest() else 0)
