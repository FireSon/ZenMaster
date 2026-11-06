#!/usr/bin/env python3
"""Charge lag per device over the local broker, hemsEP only: every device is provisioned locally since 2026-08-24.

Pattern per device: 10, 25, 50, 75 and 100 percent of its own charge limit, all devices stepping at the same moment.
Measurement is the device's own report on the local broker, polled once a second; the run aborts after the first step
when no device moved, so a broken measurement never costs twenty minutes again.
"""

from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
import urllib.request
from typing import Any

import paho.mqtt.client as mqtt

_SECRETS = json.load(open("secrets.json", encoding="utf-8"))
BROKER, PORT, USER, PSW = _SECRETS["MQTT_BROKER"], _SECRETS["MQTT_PORT"], _SECRETS["MQTT_USER"], _SECRETS["MQTT_PASSWORD"]

STEPS = [0.10, 0.25, 0.50, 0.75, 1.00]  # fractions of the device's own charge limit
HOLD_SECONDS = 45.0
ZERO_SECONDS = 30.0
POLL_SECONDS = 1.0
MOVE_WATT = 50  # smallest change that counts as the device reacting
SOC_HEADROOM = 3.0  # a device this close to its own maximum has no charge room left

DEVICES: list[dict[str, Any]] = [
    {"label": "SF 2400 AC", "prodkey": "BC8B7F", "device_id": "MPEt6K1P", "limit": 2400},
    {"label": "SF 800 Pro", "prodkey": "R3mn8U", "device_id": "p0S70q2x", "limit": 1000},
    {"label": "SF 2400 AC Plus", "prodkey": "5fG27j", "device_id": "BVng8146", "limit": 2400},
    {"label": "SF 2400 Pro", "prodkey": "2Qe7C9", "device_id": "Vw6mE0cP", "limit": 2400},
    {"label": "SF 1600 AC Plus", "prodkey": "65174u", "device_id": "Cd3wbW9V", "limit": 1600},
    {"label": "Hyper 2000", "prodkey": "gDa3tb", "device_id": "9RUB1mW7", "limit": 1200},
]

client: mqtt.Client | None = None
samples: dict[str, list[tuple[float, float, float]]] = {device["label"]: [] for device in DEVICES}
soc_now: dict[str, float] = {}
lock = threading.Lock()
stop_polling = threading.Event()
message_id = 800000
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
    if not isinstance(properties, dict):
        return
    if "gridInputPower" not in properties and "outputHomePower" not in properties:
        return
    # charging is positive here: grid in minus what goes to the house
    power = float(properties.get("gridInputPower", 0) or 0) - float(properties.get("outputHomePower", 0) or 0)
    with lock:
        samples[device["label"]].append((time.monotonic(), power, float(properties.get("electricLevel", 0) or 0)))
        if "electricLevel" in properties:
            soc_now[device["label"]] = float(properties["electricLevel"])


def poll() -> None:
    while not stop_polling.is_set():
        for device in DEVICES:
            client.publish(f"iot/{device['prodkey']}/{device['device_id']}/properties/read", wrap(device["device_id"], {"properties": ["gridInputPower", "outputHomePower", "electricLevel"]}))
        stop_polling.wait(POLL_SECONDS)


def send(device: dict, watts: int) -> None:
    device_id, prodkey = device["device_id"], device["prodkey"]
    client.publish(f"iot/{prodkey}/{device_id}/properties/write", wrap(device_id, {"properties": {"hemsState": 1 if watts else 0}}))
    body = {"function": "hemsEP", "arguments": {"outputPower": 0, "chargePower": watts, "freq": 0, "mode": 9, "chargeMode": 3}}
    client.publish(f"iot/{prodkey}/{device_id}/function/invoke", wrap(device_id, body))


def analyse(label: str, target: int, sent_at: float) -> dict[str, Any]:
    with lock:
        series = list(samples[label])
    before = [power for moment, power, _ in series if sent_at - 12 <= moment <= sent_at]
    after = [(moment, power) for moment, power, _ in series if moment > sent_at]
    if not before or not after:
        return {"label": label, "target": target, "note": f"no samples (before={len(before)} after={len(after)})"}
    baseline = statistics.median(before)
    dead = next((moment - sent_at for moment, power in after if abs(power - baseline) >= MOVE_WATT), None)
    reached = next((moment - sent_at for moment, power in after if abs(power - baseline) >= 0.9 * abs(target - baseline)), None)
    settled = statistics.median([power for moment, power in after if moment >= after[-1][0] - 10])
    return {"label": label, "target": target, "baseline": baseline, "dead": dead, "rise": reached, "settled": settled, "gap": target - settled, "soc": soc_now.get(label, 0.0)}


def show(rows: list[dict[str, Any]]) -> None:
    print(f"{'device':16s} {'target':>7s} {'base':>7s} {'dead':>7s} {'rise':>7s} {'settled':>8s} {'gap':>7s} {'soc':>6s}")
    for row in rows:
        if "note" in row:
            print(f"{row['label']:16s} {row['target']:7d}  {row['note']}")
            continue
        dead = f"{row['dead']:.1f}s" if row["dead"] is not None else "n/a"
        rise = f"{row['rise']:.1f}s" if row["rise"] is not None else "n/a"
        print(f"{row['label']:16s} {row['target']:7d} {row['baseline']:7.0f} {dead:>7s} {rise:>7s} {row['settled']:8.0f} {row['gap']:7.0f} {row['soc']:6.0f}")


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
    parser.add_argument("--run", action="store_true", help="actually command the devices; without it the script only listens")
    parser.add_argument("--seconds", type=float, default=20.0, help="listen time in dry-run")
    parser.add_argument("--out", help="write every sample to this jsonl for later analysis")
    parser.add_argument("--only", help="comma separated device labels to command; the rest is left alone")
    arguments = parser.parse_args()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="em_charge_lag")
    client.username_pw_set(USER, PSW)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(BROKER, PORT, 30)
    client.loop_start()

    restore_mode = None
    poller = threading.Thread(target=poll, daemon=True)
    poller.start()

    try:
        if not arguments.run:
            time.sleep(arguments.seconds)
            with lock:
                for device in DEVICES:
                    series = samples[device["label"]]
                    print(f"{device['label']:16s} samples={len(series):4d} last={series[-1][1] if series else 0:8.0f} W  soc={soc_now.get(device['label'], 0):3.0f}")
            return

        restore_mode = ha("select.zenmaster_mode")
        ha_set("select", "select_option", {"entity_id": "select.zenmaster_mode", "option": "off"})
        print(f"{stamp()} distribution off (was {restore_mode}), letting the fleet settle")
        time.sleep(20.0)

        wanted = [d for d in DEVICES if not arguments.only or d["label"] in [x.strip() for x in arguments.only.split(",")]]
        for device in wanted:
            print(f"  {device['label']:16s} charge limit {device['limit']} W, soc {soc_now.get(device['label'], 0):.0f}%")

        release()
        time.sleep(ZERO_SECONDS)
        for index, fraction in enumerate(STEPS):
            targets = {device["label"]: int(device["limit"] * fraction) for device in wanted}
            print(f"{stamp()} >>> charge step {int(fraction * 100)}%: {targets}")
            sent_at = time.monotonic()
            for device in wanted:
                send(device, targets[device["label"]])
            time.sleep(HOLD_SECONDS)
            rows = [analyse(device["label"], targets[device["label"]], sent_at) for device in wanted]
            show(rows)
            moved = [row for row in rows if row.get("dead") is not None]
            if index == 0 and not moved:
                print(f"{stamp()} nothing moved on the first step, aborting instead of running the whole pattern")
                break
            release()
            time.sleep(ZERO_SECONDS)
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
                    for moment, power, soc in series:
                        handle.write(json.dumps({"device": label, "t": round(moment - start_time, 2), "power": power, "soc": soc}) + "\n")
            print(f"samples written to {arguments.out}")


main()
