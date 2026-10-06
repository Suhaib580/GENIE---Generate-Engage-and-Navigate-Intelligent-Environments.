"""
Watches the sensor readings and fires the emergency path when one goes bad.

This is the link that turns GENIE from something you have to ASK for help into
something that calls for help on its own. Until now the only route to an alert
was the spoken intent in whisper_service.process_transcript() -- api.py stored
smoke, flame, motion and fall readings faithfully and nothing ever looked at
them.

    sensor ESP32 -> POST /update_sensor -> api.py sensor_state
                                               |
                                     GET /sensors (this file, polling)
                                               |
                                    send_emergency_trigger() -> modem ESP32

Runs as a daemon thread alongside the other servers in whisper_service.

Three behaviours matter more than the polling itself, and all three exist
because the naive version phones people at three in the morning:

  EDGE TRIGGERED   Fires when a sensor ENTERS an alerting state, not while it
                   sits there. A real fire keeps the smoke sensor high for
                   hours; that is one emergency, not one per poll.

  DEBOUNCED        The reading has to hold for SENSOR_ALERT_DEBOUNCE before it
                   counts. Cheap MQ-2 modules twitch, and a single spurious
                   sample should not place a phone call.

  COOLED DOWN      After any alert, every sensor is ignored for
                   SENSOR_ALERT_COOLDOWN. Stops a second call being queued the
                   moment the first one hangs up.
"""

import sys
import os
import threading
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from config import (
    IOT_API_URL,
    SENSOR_ALERT_STATES,
    SENSOR_ALERT_MESSAGES,
    SENSOR_WATCH_INTERVAL,
    SENSOR_ALERT_DEBOUNCE,
    SENSOR_ALERT_DEBOUNCE_OVERRIDE,
    SENSOR_ALERT_COOLDOWN,
    EMERGENCY_SMS_MESSAGE,
)


def _reading_of(entry):
    """Pull the comparable state out of one /sensors entry.

    api.py stores boolean-ish sensors as {"status": "true"} and numeric ones as
    {"value": 27}. Only the status ones can raise an alert, so a numeric entry
    reports None and is skipped.
    """
    if not isinstance(entry, dict):
        return None
    status = entry.get("status")
    return str(status).strip().lower() if status is not None else None


def alert_message(sensor):
    """The SMS text for a given sensor, falling back to the generic one."""
    return SENSOR_ALERT_MESSAGES.get(sensor, EMERGENCY_SMS_MESSAGE)


class SensorWatcher:
    """Polls /sensors and calls on_alert(sensor, message) on a fresh alarm."""

    def __init__(self, on_alert, api_url=None, interval=None,
                 debounce=None, cooldown=None, log=print):
        self.on_alert = on_alert
        self.api_url = api_url or IOT_API_URL
        self.interval = interval if interval is not None else SENSOR_WATCH_INTERVAL
        self.debounce = debounce if debounce is not None else SENSOR_ALERT_DEBOUNCE
        # Per-sensor override. Passing an explicit debounce= (tests do)
        # means exactly that debounce for every sensor, overrides included
        # -- otherwise a test asking for 0 s would still wait 4 s on SMOKE.
        self.debounce_by_sensor = ({} if debounce is not None
                                   else dict(SENSOR_ALERT_DEBOUNCE_OVERRIDE))
        self.cooldown = cooldown if cooldown is not None else SENSOR_ALERT_COOLDOWN
        self.log = log

        # sensor -> was it alerting at the previous poll (the edge)
        self._was_alerting = {}
        # sensor -> when it first went alerting (the debounce)
        self._since = {}
        # when the last alert fired, for the cooldown
        self._last_alert = 0.0
        # so a failing api.py does not print once per poll forever
        self._warned_unreachable = False

    # ------------------------------------------------------------------

    def poll_once(self):
        """One cycle. Returns the sensor that alerted, or None.

        Split out from run() so it can be tested without threads or
        sleeping -- and so node_bridge.py can call it the instant a bad
        reading lands, instead of waiting up to SENSOR_WATCH_INTERVAL for
        the next tick.

        SAFE TO CALL EARLY AND OFTEN. Every piece of alert policy lives in
        here -- the edge detection, the per-sensor debounce and the
        cooldown -- so an extra call can only ever make an alert happen
        SOONER, never twice. That property is the reason this stays the one
        and only place allowed to decide an emergency; a second caller with
        its own copy of the logic would not share the cooldown, and one
        smoke event would ring the responder's phone from both.
        """
        readings = self._fetch()
        if readings is None:
            return None

        now = time.monotonic()
        fired = None

        for sensor, alerting_states in SENSOR_ALERT_STATES.items():
            state = _reading_of(readings.get(sensor))
            alerting = state is not None and state in alerting_states

            if not alerting:
                # Reset both the edge and the debounce clock. This is what lets
                # a SECOND fire alert later, after the first has cleared.
                self._was_alerting[sensor] = False
                self._since.pop(sensor, None)
                continue

            if self._was_alerting.get(sensor):
                continue                      # already counted this episode

            held = now - self._since.setdefault(sensor, now)
            # A blanket debounce is right for exactly one sensor here. The
            # MQ-2 twitches, so smoke must hold. Flame does not: Node 1
            # already confirms it across three consecutive 50 ms polls
            # before reporting at all, and its fall FSM needs a freefall
            # AND an impact AND a settled tilt. Debouncing those a second
            # time just adds four seconds to the two events where seconds
            # are the entire argument for the rule-based path.
            settle = self.debounce_by_sensor.get(sensor, self.debounce)
            if held < settle:
                continue                      # still settling

            # Confirmed. Mark it seen BEFORE any cooldown check, so a reading
            # suppressed by the cooldown does not re-fire the instant the
            # cooldown lifts -- it has to go low and high again like any other.
            self._was_alerting[sensor] = True

            if now - self._last_alert < self.cooldown:
                self.log("[sensor_watch] %s alerting, suppressed by cooldown "
                         "(%.0fs left)"
                         % (sensor, self.cooldown - (now - self._last_alert)))
                continue

            self._last_alert = now
            fired = sensor
            self.log("[sensor_watch] ALERT: %s = %s" % (sensor, state))

            try:
                self.on_alert(sensor, alert_message(sensor))
            except Exception as e:
                self.log("[sensor_watch] on_alert(%s) failed: %r" % (sensor, e))

            # One alert per cycle. If smoke and flame both trip, they are the
            # same event and the responder does not need two calls.
            break

        return fired

    def run(self):
        """Poll forever. Intended as a daemon thread target."""
        self.log("[sensor_watch] watching %s every %.1fs (debounce %.0fs, "
                 "cooldown %.0fs)"
                 % (", ".join(sorted(SENSOR_ALERT_STATES)), self.interval,
                    self.debounce, self.cooldown))
        while True:
            try:
                self.poll_once()
            except Exception as e:
                # Never let this thread die - it is the whole autonomous path.
                self.log("[sensor_watch] poll blew up: %r" % e)
            time.sleep(self.interval)

    # ------------------------------------------------------------------

    def _fetch(self):
        try:
            response = requests.get("%s/sensors" % self.api_url, timeout=4)
            data = response.json()
        except Exception as e:
            if not self._warned_unreachable:
                self.log("[sensor_watch] cannot reach api.py (%r) - "
                         "sensor alerts are OFF until it answers" % e)
                self._warned_unreachable = True
            return None

        if self._warned_unreachable:
            self.log("[sensor_watch] api.py reachable again")
            self._warned_unreachable = False

        return data if isinstance(data, dict) else None


def start(on_alert, **kwargs):
    """Spawn the watcher as a daemon thread. Returns the SensorWatcher."""
    watcher = SensorWatcher(on_alert, **kwargs)
    threading.Thread(target=watcher.run, daemon=True).start()
    return watcher


if __name__ == "__main__":
    # Standalone: python Services/sensor_watch.py
    # Prints alerts instead of calling anyone. Push a reading at it with:
    #   curl -X POST http://127.0.0.1:8000/update_sensor \
    #        -H "Content-Type: application/json" \
    #        -d "{\"sensor\":\"SMOKE\",\"status\":\"true\"}"
    SensorWatcher(
        lambda sensor, message: print("  would alert: %s -> %r" % (sensor, message))
    ).run()
