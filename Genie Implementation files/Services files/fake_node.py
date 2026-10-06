"""
Pretend to be the boards. Drives api.py with no hardware attached.

    python Services/fake_node.py                 idle room, values drift gently
    python Services/fake_node.py --scenario fire
    python Services/fake_node.py --list

WHAT IT IS FOR
--------------
Everything downstream of the serial port -- api.py, sensor_watch, ai.py,
sensor.aiml, the spoken replies, the emergency escalation -- can be tested
without a single wire. That matters for three reasons:

  1. You can rehearse the demo answers ("is there smoke", "is anyone in the
     room", "check all sensors") before the boards are assembled.
  2. You can trigger a fire or a fall on demand. Setting off a real MQ-2
     takes butane and about a minute to clear; a real fall means dropping
     the unit on the floor.
  3. When something misbehaves on the bench you can replay the same reading
     here and see whether the fault is in the sensor or in the pipeline.

It POSTs to api.py exactly as node_bridge.py does -- same endpoint, same
translation, same field names -- so what you are testing is the real path,
not a mock of it.

    !! This writes into a running api.py. If the boards are also connected,
       the bridge will overwrite these values on its next tick. Use it with
       the boards unplugged, or with NODE1_SERIAL_PORT set to "off".
"""

import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import math
import time

import requests

from config import IOT_API_URL
import node_bridge


def base(t):
    """A quiet room. Everything nominal, values drifting the way real ones
    do -- a dead-still reading is a giveaway that nothing is real."""
    wobble = math.sin(t / 7.0)
    return {
        "t": "telemetry",
        "temp_c": round(24.0 + wobble * 0.6, 1),
        "humidity": round(52.0 + wobble * 2.0, 1),
        "gas_raw": int(480 + wobble * 25),
        "gas_ratio": round(1.0 + wobble * 0.05, 2),
        "gas_level": "clear",
        "gas_ok": True,
        "flame": False,
        "flame_ok": True,
        "accel_g": round(1.00 + wobble * 0.01, 2),
        "tilt": 2,
        "imu_level": "clear",
        "imu_ok": True,
        "moved": False,
        "knock": False,
        "knock_count": 0,
        "fall": False,
        "fall_state": "idle",
        "fall_rate_ok": True,
        "node2_link": True,
        "present": False,
        "tof_mm": [2400, 2350, 2380, 2410],
        "tof_zone": ["CLEAR", "CLEAR", "CLEAR", "CLEAR"],
    }


def _person(msg, side=1, mm=650):
    msg["present"] = True
    msg["tof_mm"][side] = mm
    msg["tof_zone"][side] = "PRESENT" if mm < 800 else "FAR"
    return msg


SCENARIOS = {}


def scenario(name, description):
    def wrap(fn):
        SCENARIOS[name] = (description, fn)
        return fn
    return wrap


@scenario("idle", "quiet room, nothing happening")
def s_idle(t, msg):
    return msg


@scenario("person", "someone standing to the left, 65 cm away")
def s_person(t, msg):
    return _person(msg)


@scenario("approach", "someone walking towards the front sensor")
def s_approach(t, msg):
    mm = max(150, int(2400 - (t % 20) * 115))
    msg["tof_mm"][0] = mm
    msg["tof_zone"][0] = ("TOUCH" if mm < 40 else "NEAR" if mm < 300
                          else "PRESENT" if mm < 800 else
                          "FAR" if mm < 1500 else "CLEAR")
    msg["present"] = mm < 800
    return msg


@scenario("smoke", "MQ-2 climbing past the 1.20 alert line, then the 1.50 alarm")
def s_smoke(t, msg):
    ratio = 1.0 + (t % 30) * 0.03
    msg["gas_ratio"] = round(ratio, 2)
    msg["gas_raw"] = int(480 * ratio)
    msg["gas_level"] = ("alarm" if ratio > 1.5
                        else "elevated" if ratio > 1.2 else "clear")
    return msg


@scenario("fire", "flame sensor confirmed - the fastest alarm path")
def s_fire(t, msg):
    msg["flame"] = True
    return msg


@scenario("fall", "fall FSM confirmed, latched")
def s_fall(t, msg):
    msg["fall"] = True
    msg["fall_state"] = "fallen"
    msg["accel_g"] = 1.02
    msg["tilt"] = 88
    return msg


@scenario("moved", "the unit itself was picked up - MOTION, not PRESENCE")
def s_moved(t, msg):
    msg["moved"] = True
    msg["imu_level"] = "alarm"
    msg["accel_g"] = 2.3
    msg["tilt"] = 41
    return msg


@scenario("knock", "somebody tapping the case, counter climbing")
def s_knock(t, msg):
    msg["knock"] = (t % 4) < 2
    msg["knock_count"] = int(t // 2)
    return msg


@scenario("dead-mq2", "MQ-2 wire off. Must read unknown, NOT clear")
def s_dead_mq2(t, msg):
    msg["gas_ok"] = False
    msg["gas_level"] = "elevated"     # what _rate() reports for a failed channel
    return msg


@scenario("dead-tof", "Node 2 gone. PRESENCE/IR/DISTANCE unknown, face node fine")
def s_dead_tof(t, msg):
    msg["node2_link"] = False
    return msg


@scenario("dead-imu", "both MPUs gone. MOTION unknown, TILT and ACCEL null")
def s_dead_imu(t, msg):
    msg["imu_ok"] = False
    msg["accel_g"] = None
    msg["tilt"] = None
    return msg


@scenario("blind", "every safety sensor unreadable - must NOT say 'all clear'")
def s_blind(t, msg):
    msg["gas_ok"] = False
    msg["flame_ok"] = False
    msg["imu_ok"] = False
    msg["node2_link"] = False
    msg.pop("fall_state", None)
    msg.pop("fall", None)
    return msg


def push(url, telemetry):
    body = {
        "readings": node_bridge.translate(telemetry),
        "links": {"node1": True, "node2": bool(telemetry.get("node2_link"))},
        "tof": node_bridge.tof_payload(telemetry),
    }
    r = requests.post("%s/update_sensors" % url, json=body, timeout=4)
    return r.json()


def main(argv):
    url = IOT_API_URL
    name = "idle"
    interval = 2.0

    if "--list" in argv:
        print("\nscenarios:\n")
        for key in sorted(SCENARIOS):
            print("  %-11s %s" % (key, SCENARIOS[key][0]))
        print("\n  python Services/fake_node.py --scenario fire\n")
        return 0

    if "--scenario" in argv:
        name = argv[argv.index("--scenario") + 1]
    if "--url" in argv:
        url = argv[argv.index("--url") + 1]
    if "--interval" in argv:
        interval = float(argv[argv.index("--interval") + 1])

    if name not in SCENARIOS:
        print("unknown scenario %r. --list to see them." % name)
        return 1

    description, fn = SCENARIOS[name]
    print("faking the boards: %s -- %s" % (name, description))
    print("posting to %s every %.1fs. Ctrl-C to stop.\n" % (url, interval))
    print("watch it with:  python Services/sensor_monitor.py\n")

    t = 0.0
    while True:
        msg = fn(t, base(t))
        try:
            push(url, msg)
            readings = node_bridge.translate(msg)
            print("  t+%-5.0f  smoke=%-9s flame=%-7s motion=%-8s presence=%-8s fall=%s"
                  % (t,
                     readings["SMOKE"]["status"], readings["FLAME"]["status"],
                     readings["MOTION"]["status"], readings["PRESENCE"]["status"],
                     readings["FALL"]["status"]))
        except Exception as e:
            print("  cannot reach api.py (%r) -- start it with: python api.py" % e)
        time.sleep(interval)
        t += interval


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print()
