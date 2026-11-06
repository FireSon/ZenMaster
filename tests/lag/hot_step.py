#!/usr/bin/env python3
"""Hot charge-step lag per device: ramp 100 -> 600 W and back in 100 W steps, each level held, dead time per transition.

Unlike charge_lag every step is hot: the device is already charging at the previous level, so the baseline of each
transition is that previous level, not zero. Local broker, hemsEP, all six devices at once, distribution off then restored.
"""

from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
from typing import Any
import urllib.request

import paho.mqtt.client as mqtt

_SECRETS = json.load(open("secrets.json", encoding="utf-8"))
BROKER, PORT, USER, PSW = _SECRETS["MQTT_BROKER"], _SECRETS["MQTT_PORT"], _SECRETS["MQTT_USER"], _SECRETS["MQTT_PASSWORD"]

LEVELS = [100, 200, 300, 400, 500, 600, 500, 400, 300, 200, 100]  # up to 600 and back down
HOLD_SECONDS = 20.0
POLL_SECONDS = 1.0
MOVE_WATT = 50

DEVICES: list[dict[str, Any]] = [
    {"label": "SF 2400 AC", "prodkey": "BC8B7F", "device_id": "MPEt6K1P"},
    {"label": "SF 800 Pro", "prodkey": "R3mn8U", "device_id": "p0S70q2x"},
    {"label": "SF 2400 AC Plus", "prodkey": "5fG27j", "device_id": "BVng8146"},
    {"label": "SF 2400 Pro", "prodkey": "2Qe7C9", "device_id": "Vw6mE0cP"},
    {"label": "SF 1600 AC Plus", "prodkey": "65174u", "device_id": "Cd3wbW9V"},
    {"label": "Hyper 2000", "prodkey": "gDa3tb", "device_id": "9RUB1mW7"},
]

client: mqtt.Client | None = None
samples: dict[str, list[tuple[float, float]]] = {device["label"]: [] for device in DEVICES}
lock = threading.Lock()
stop_polling = threading.Event()
message_id = 810000
start_time = time.monotonic()


def stamp() -> str:
    return f"[{time.monotonic() - start_time:6.1f}s]"


def wrap(device_id: str, body: dict) -> str:
    global message_id
    message_id += 1
    body["messageId"] = message_id
    body["timestamp"] = int(time.time())
    body["deviceId"] = device_id
    return json.dumps(body)


def on_connect(cl: mqtt.Client, _userdata: Any, _flags: Any, reason_code: Any, _properties: Any = None) -> None:
    print(f"{stamp()} broker connected rc={reason_code}")
    cl.subscribe("/#")


def on_message(_cl: mqtt.Client, _userdata: Any, message: mqtt.MQTTMessage) -> None:
    parts = message.topic.split("/")
    if len(parts) < 3:
        return
    device = next((d for d in DEVICES if d["prodkey"] == parts[1] or d["device_id"] == parts[2]), None)
    if device is None:
        return
    try:
        properties = json.loads(message.payload.decode("utf-8", "ignore")).get("properties", {})
    except (json.JSONDecodeError, AttributeError):
        return
    if not isinstance(properties, dict) or ("gridInputPower" not in properties and "outputHomePower" not in properties):
        return
    power = float(properties.get("gridInputPower", 0) or 0) - float(properties.get("outputHomePower", 0) or 0)
    with lock:
        samples[device["label"]].append((time.monotonic(), power))


def poll() -> None:
    while not stop_polling.is_set():
        for device in DEVICES:
            client.publish(f"iot/{device['prodkey']}/{device['device_id']}/properties/read", wrap(device["device_id"], {"properties": ["gridInputPower", "outputHomePower", "electricLevel"]}))
        stop_polling.wait(POLL_SECONDS)


def send(device: dict, watts: int) -> None:
    device_id, prodkey = device["device_id"], device["prodkey"]
    client.publish(f"iot/{prodkey}/{device_id}/properties/write", wrap(device_id, {"properties": {"hemsState": 1 if watts else 0}}))
    client.publish(f"iot/{prodkey}/{device_id}/function/invoke", wrap(device_id, {"function": "hemsEP", "arguments": {"outputPower": 0, "chargePower": watts, "freq": 0, "mode": 9, "chargeMode": 3}}))


def analyse(label: str, target: int, sent_at: float) -> dict[str, Any]:
    with lock:
        series = list(samples[label])
    before = [power for moment, power in series if sent_at - 12 <= moment <= sent_at]
    after = [(moment, power) for moment, power in series if moment > sent_at]
    if not before or not after:
        return {"label": label, "target": target, "note": f"no samples (before={len(before)} after={len(after)})"}
    baseline = statistics.median(before)
    dead = next((moment - sent_at for moment, power in after if abs(power - baseline) >= MOVE_WATT), None)
    reached = next((moment - sent_at for moment, power in after if abs(power - baseline) >= 0.9 * abs(target - baseline)), None)
    settled = statistics.median([power for moment, power in after if moment >= after[-1][0] - 8])
    return {"label": label, "base": baseline, "target": target, "dead": dead, "rise": reached, "settled": settled}


def show(rows: list[dict[str, Any]]) -> None:
    print(f"{'device':16s} {'from':>6s} {'to':>6s} {'dead':>7s} {'rise':>7s} {'settled':>8s}")
    for row in rows:
        if "note" in row:
            print(f"{row['label']:16s} {'':>6s} {row['target']:6d}  {row['note']}")
            continue
        dead = f"{row['dead']:.1f}s" if row["dead"] is not None else "n/a"
        rise = f"{row['rise']:.1f}s" if row["rise"] is not None else "n/a"
        print(f"{row['label']:16s} {row['base']:6.0f} {row['target']:6d} {dead:>7s} {rise:>7s} {row['settled']:8.0f}")


def release() -> None:
    for device in DEVICES:
        send(device, 0)
    print(f"{stamp()} released: 0 W and hemsState 0 on every device")


def ha(entity: str) -> str:
    request = urllib.request.Request(f"{_SECRETS['HA_URL'].rstrip('/')}/api/states/{entity}", headers={"Authorization": "Bearer " + _SECRETS["HA_TOKEN"]})
    return json.load(urllib.request.urlopen(request, timeout=10))["state"]


def ha_set(domain: str, service: str, data: dict) -> None:
    request = urllib.request.Request(f"{_SECRETS['HA_URL'].rstrip('/')}/api/services/{domain}/{service}", data=json.dumps(data).encode(),
                                     headers={"Authorization": "Bearer " + _SECRETS["HA_TOKEN"], "Content-Type": "application/json"})
    urllib.request.urlopen(request, timeout=15).read()


def main() -> None:
    global client
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", help="write every sample to this jsonl")
    arguments = parser.parse_args()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="em_hot_step")
    client.username_pw_set(USER, PSW)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(BROKER, PORT, 30)
    client.loop_start()
    threading.Thread(target=poll, daemon=True).start()

    restore_mode = None
    try:
        restore_mode = ha("select.zenmaster_mode")
        ha_set("select", "select_option", {"entity_id": "select.zenmaster_mode", "option": "off"})
        print(f"{stamp()} distribution off (was {restore_mode}), settling")
        time.sleep(20.0)

        # start the ramp from the first level, held long enough to be the baseline of the first hot step
        print(f"{stamp()} priming at {LEVELS[0]} W")
        for device in DEVICES:
            send(device, LEVELS[0])
        time.sleep(HOLD_SECONDS)

        for previous, level in zip(LEVELS, LEVELS[1:]):
            print(f"{stamp()} >>> hot step {previous} -> {level} W")
            sent_at = time.monotonic()
            for device in DEVICES:
                send(device, level)
            time.sleep(HOLD_SECONDS)
            show([analyse(device["label"], level, sent_at) for device in DEVICES])
    finally:
        release()
        stop_polling.set()
        if restore_mode:
            ha_set("select", "select_option", {"entity_id": "select.zenmaster_mode", "option": restore_mode})
            print(f"{stamp()} distribution back to {restore_mode}")
        time.sleep(1.0)
        client.loop_stop()
        client.disconnect()
        if arguments.out:
            with open(arguments.out, "w", encoding="utf-8") as handle:
                for label, series in samples.items():
                    for moment, power in series:
                        handle.write(json.dumps({"device": label, "t": round(moment - start_time, 2), "power": power}) + "\n")
            print(f"samples written to {arguments.out}")


main()
