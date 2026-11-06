#!/usr/bin/env python3
"""Onset and redirect lag per device, ported from the previous generation (test_http_lag.py, test_hemsep_redirect.py).

Two things the MQTT tests cannot do. The SolarFlows only push a report every 5 s, so a dead time measured on the broker is
that raster and not the device. Here the measurement channel is the device's own HTTP report, polled four times a second,
which is what the earlier generation used. The Hyper has no HTTP API and is polled over the broker instead, where it
reports every 0.6 s anyway. Second, the redirect case: a command sent while an earlier one is still in flight, to see
whether the device takes the newest value and whether it still lands on time.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.request
from typing import Any

import paho.mqtt.client as mqtt

_SECRETS = json.load(open("secrets.json", encoding="utf-8"))
BROKER, PORT, USER, PSW = _SECRETS["MQTT_BROKER"], _SECRETS["MQTT_PORT"], _SECRETS["MQTT_USER"], _SECRETS["MQTT_PASSWORD"]

POLL_SECONDS = 0.25  # the whole point: four times a second beats the 5 s report raster
MOVE_WATT = 30  # same threshold the earlier generation used to call it moved
WARM_W = 200  # the device runs at this before every hot step, so nothing is measured cold by accident
STEP_W = 600
REDIRECT_FIRST_W = 1200  # sent first, then overruled before it can land
REDIRECT_AFTER_S = 1.0
REDIRECT_FINAL_W = 400
SETTLE_SECONDS = 25.0
WATCH_SECONDS = 15.0
HTTP_TIMEOUT = 3.0

DEVICES: list[dict[str, Any]] = [
    {"label": "SF 2400 AC", "prodkey": "BC8B7F", "device_id": "MPEt6K1P", "ip": "192.168.2.3", "sn": "HOA1NPN3N210137"},
    {"label": "SF 800 Pro", "prodkey": "R3mn8U", "device_id": "p0S70q2x", "ip": "192.168.2.5", "sn": "EOA1NHN3N180450"},
    {"label": "SF 2400 AC Plus", "prodkey": "5fG27j", "device_id": "BVng8146", "ip": "192.168.2.17", "sn": "HEC4AENCN520012"},
    {"label": "SF 2400 Pro", "prodkey": "2Qe7C9", "device_id": "Vw6mE0cP", "ip": "192.168.2.18", "sn": "EEB4AENCN520045"},
    {"label": "SF 1600 AC Plus", "prodkey": "65174u", "device_id": "Cd3wbW9V", "ip": "192.168.2.21", "sn": "HED4NENBN470562"},
    {"label": "Hyper 2000", "prodkey": "gDa3tb", "device_id": "9RUB1mW7", "ip": None, "sn": "EE1LHNBTN151322"},
]

client: mqtt.Client | None = None
samples: dict[str, list[tuple[float, float]]] = {device["label"]: [] for device in DEVICES}
lock = threading.Lock()
stop_polling = threading.Event()
message_id = 830000
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


def record(label: str, properties: dict) -> None:
    if "gridInputPower" not in properties and "outputHomePower" not in properties:
        return
    power = float(properties.get("gridInputPower", 0) or 0) - float(properties.get("outputHomePower", 0) or 0)
    with lock:
        samples[label].append((time.monotonic(), power))


def poll_http(device: dict) -> None:
    while not stop_polling.is_set():
        try:
            with urllib.request.urlopen(f"http://{device['ip']}/properties/report", timeout=HTTP_TIMEOUT) as response:
                record(device["label"], json.loads(response.read().decode("utf-8", "ignore")).get("properties", {}))
        except Exception:
            pass
        stop_polling.wait(POLL_SECONDS)


def poll_mqtt(device: dict) -> None:
    while not stop_polling.is_set():
        client.publish(f"iot/{device['prodkey']}/{device['device_id']}/properties/read", wrap(device["device_id"], {"properties": ["gridInputPower", "outputHomePower", "electricLevel"]}))
        stop_polling.wait(0.5)


def on_message(_cl: mqtt.Client, _userdata: Any, message: mqtt.MQTTMessage) -> None:
    parts = message.topic.split("/")
    if len(parts) < 3:
        return
    device = next((d for d in DEVICES if d["prodkey"] == parts[1] or d["device_id"] == parts[2]), None)
    if device is None or device["ip"] is not None:
        return  # the http poll is the channel for everything that has an api
    try:
        properties = json.loads(message.payload.decode("utf-8", "ignore")).get("properties", {})
    except (json.JSONDecodeError, AttributeError):
        return
    if isinstance(properties, dict):
        record(device["label"], properties)


def send(device: dict, watts: int) -> None:
    device_id, prodkey = device["device_id"], device["prodkey"]
    client.publish(f"iot/{prodkey}/{device_id}/properties/write", wrap(device_id, {"properties": {"hemsState": 1 if watts else 0}}))
    client.publish(f"iot/{prodkey}/{device_id}/function/invoke", wrap(device_id, {"function": "hemsEP", "arguments": {"outputPower": 0, "chargePower": watts, "freq": 0, "mode": 9, "chargeMode": 3}}))


def onset(label: str, sent_at: float) -> tuple[float | None, float, float]:
    """First movement past MOVE_WATT after the command, the baseline it left, and where it ended."""
    with lock:
        series = list(samples[label])
    before = [power for moment, power in series if sent_at - 6 <= moment <= sent_at]
    after = [(moment, power) for moment, power in series if moment > sent_at]
    if not before or not after:
        return None, 0.0, 0.0
    baseline = sorted(before)[len(before) // 2]
    dead = next((moment - sent_at for moment, power in after if abs(power - baseline) >= MOVE_WATT), None)
    settled = sorted(power for moment, power in after if moment >= after[-1][0] - 5)
    return dead, baseline, settled[len(settled) // 2] if settled else 0.0


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
    parser.add_argument("--run", action="store_true", help="actually command the devices; without it the script only checks the measurement channel")
    parser.add_argument("--seconds", type=float, default=10.0, help="listen time without --run")
    parser.add_argument("--out", help="write every sample to this jsonl")
    arguments = parser.parse_args()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="em_onset_lag")
    client.username_pw_set(USER, PSW)
    client.on_message = on_message
    client.on_connect = lambda cl, u, f, rc, p=None: (print(f"{stamp()} broker rc={rc}", flush=True), cl.subscribe("/#"), cl.subscribe("iot/#"))
    client.connect(BROKER, PORT, 30)
    client.loop_start()
    for device in DEVICES:
        threading.Thread(target=poll_http if device["ip"] else poll_mqtt, args=(device,), daemon=True).start()

    restore_mode = None
    try:
        time.sleep(4.0)
        with lock:
            print("meetkanaal:")
            for device in DEVICES:
                series = samples[device["label"]]
                rate = len(series) / 4.0
                print(f"  {device['label']:16s} {'http ' + str(device['ip']) if device['ip'] else 'broker':22s} {rate:4.1f} samples/s")
        if not arguments.run:
            time.sleep(max(0.0, arguments.seconds - 4.0))
            return

        restore_mode = ha("select.zenmaster_mode")
        ha_set("select", "select_option", {"entity_id": "select.zenmaster_mode", "option": "off"})
        print(f"{stamp()} distribution off (was {restore_mode})")
        release()
        time.sleep(10.0)

        print(f"\n{stamp()} warm up at {WARM_W} W")
        for device in DEVICES:
            send(device, WARM_W)
        time.sleep(SETTLE_SECONDS)

        print(f"\n{stamp()} >>> hot step {WARM_W} -> {STEP_W} W")
        sent_at = time.monotonic()
        for device in DEVICES:
            send(device, STEP_W)
        time.sleep(WATCH_SECONDS)
        print(f"{'device':16s}{'base':>7}{'onset':>8}{'settled':>9}")
        for device in DEVICES:
            dead, baseline, settled = onset(device["label"], sent_at)
            print(f"{device['label']:16s}{baseline:>7.0f}{(f'{dead:.2f}s' if dead is not None else 'n/a'):>8}{settled:>9.0f}")

        print(f"\n{stamp()} >>> redirect: {REDIRECT_FIRST_W} W, after {REDIRECT_AFTER_S} s overruled by {REDIRECT_FINAL_W} W")
        sent_at = time.monotonic()
        for device in DEVICES:
            send(device, REDIRECT_FIRST_W)
        time.sleep(REDIRECT_AFTER_S)
        for device in DEVICES:
            send(device, REDIRECT_FINAL_W)
        time.sleep(WATCH_SECONDS)
        print(f"{'device':16s}{'base':>7}{'onset':>8}{'settled':>9}  verdict")
        for device in DEVICES:
            dead, baseline, settled = onset(device["label"], sent_at)
            verdict = "took the newest value" if abs(settled - REDIRECT_FINAL_W) < 100 else ("landed on the first value" if abs(settled - REDIRECT_FIRST_W) < 200 else "neither")
            print(f"{device['label']:16s}{baseline:>7.0f}{(f'{dead:.2f}s' if dead is not None else 'n/a'):>8}{settled:>9.0f}  {verdict}")
    finally:
        if arguments.run:
            release()
            if restore_mode:
                ha_set("select", "select_option", {"entity_id": "select.zenmaster_mode", "option": restore_mode})
                print(f"{stamp()} distribution back to {restore_mode}")
        stop_polling.set()
        time.sleep(1.0)
        client.loop_stop()
        client.disconnect()
        if arguments.out:
            with open(arguments.out, "w", encoding="utf-8") as handle:
                for label, series in samples.items():
                    for moment, power in series:
                        handle.write(json.dumps({"device": label, "t": round(moment - start_time, 3), "power": power}) + "\n")
            print(f"samples written to {arguments.out}")


main()
