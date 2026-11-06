#!/usr/bin/env python3
"""hemsEP freq sweep: for each freq value a hot step 200 -> 400 W, dead time per device, to see if freq changes the lag."""

from __future__ import annotations

import json
import statistics
import threading
import time
from typing import Any
import urllib.request

import paho.mqtt.client as mqtt

_SECRETS = json.load(open("secrets.json", encoding="utf-8"))
BROKER, PORT, USER, PSW = _SECRETS["MQTT_BROKER"], _SECRETS["MQTT_PORT"], _SECRETS["MQTT_USER"], _SECRETS["MQTT_PASSWORD"]

FREQS = [0, 1, 2, 3, 4, 5, 10]
BASE_W = 200
STEP_W = 400
SETTLE_SECONDS = 18.0
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
message_id = 820000
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
            client.publish(f"iot/{device['prodkey']}/{device['device_id']}/properties/read", wrap(device["device_id"], {"properties": ["gridInputPower", "outputHomePower"]}))
        stop_polling.wait(POLL_SECONDS)


def send(device: dict, watts: int, freq: int) -> None:
    device_id, prodkey = device["device_id"], device["prodkey"]
    client.publish(f"iot/{prodkey}/{device_id}/properties/write", wrap(device_id, {"properties": {"hemsState": 1 if watts else 0}}))
    client.publish(f"iot/{prodkey}/{device_id}/function/invoke", wrap(device_id, {"function": "hemsEP", "arguments": {"outputPower": 0, "chargePower": watts, "freq": freq, "mode": 9, "chargeMode": 3}}))


def dead_time(label: str, sent_at: float) -> float | None:
    with lock:
        series = list(samples[label])
    before = [power for moment, power in series if sent_at - 12 <= moment <= sent_at]
    after = [(moment, power) for moment, power in series if moment > sent_at]
    if not before or not after:
        return None
    baseline = statistics.median(before)
    return next((moment - sent_at for moment, power in after if abs(power - baseline) >= MOVE_WATT), None)


def release(freq: int = 0) -> None:
    for device in DEVICES:
        send(device, 0, freq)


def ha(entity: str) -> str:
    request = urllib.request.Request(f"{_SECRETS['HA_URL'].rstrip('/')}/api/states/{entity}", headers={"Authorization": "Bearer " + _SECRETS["HA_TOKEN"]})
    return json.load(urllib.request.urlopen(request, timeout=10))["state"]


def ha_set(domain: str, service: str, data: dict) -> None:
    request = urllib.request.Request(f"{_SECRETS['HA_URL'].rstrip('/')}/api/services/{domain}/{service}", data=json.dumps(data).encode(),
                                     headers={"Authorization": "Bearer " + _SECRETS["HA_TOKEN"], "Content-Type": "application/json"})
    urllib.request.urlopen(request, timeout=15).read()


def main() -> None:
    global client
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="em_freq_sweep")
    client.username_pw_set(USER, PSW)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(BROKER, PORT, 30)
    client.loop_start()
    threading.Thread(target=poll, daemon=True).start()

    restore_mode = None
    results: dict[int, dict[str, float | None]] = {}
    try:
        restore_mode = ha("select.zenmaster_mode")
        ha_set("select", "select_option", {"entity_id": "select.zenmaster_mode", "option": "off"})
        print(f"{stamp()} distribution off (was {restore_mode}), settling")
        time.sleep(20.0)

        for freq in FREQS:
            for device in DEVICES:
                send(device, BASE_W, freq)
            time.sleep(SETTLE_SECONDS)  # establish the 200 W baseline at this freq
            sent_at = time.monotonic()
            for device in DEVICES:
                send(device, STEP_W, freq)
            time.sleep(HOLD_SECONDS)
            row = {device["label"]: dead_time(device["label"], sent_at) for device in DEVICES}
            results[freq] = row
            print(f"{stamp()} freq={freq:>2}: " + "  ".join(f"{d['label'].split()[-1]}={('%.1fs' % row[d['label']]) if row[d['label']] is not None else 'n/a':>5}" for d in DEVICES))
            release(freq)
            time.sleep(10.0)
    finally:
        release()
        stop_polling.set()
        if restore_mode:
            ha_set("select", "select_option", {"entity_id": "select.zenmaster_mode", "option": restore_mode})
            print(f"{stamp()} distribution back to {restore_mode}")
        time.sleep(1.0)
        client.loop_stop()
        client.disconnect()

        print("\n== dead time (s) per freq ==")
        print(f"{'device':16s} " + "".join(f"{('f' + str(f)):>7s}" for f in FREQS))
        for device in DEVICES:
            cells = []
            for f in FREQS:
                v = results.get(f, {}).get(device["label"])
                cells.append(f"{v:6.1f} " if v is not None else f"{'n/a':>6s} ")
            print(f"{device['label']:16s} " + "".join(f"{c:>7s}" for c in cells))


main()
