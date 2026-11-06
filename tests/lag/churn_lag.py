#!/usr/bin/env python3
"""Churn test: an alternating charge setpoint every second for 30 s per device; is a continuous 1 Hz command stream safe?"""

# usage: python tests/lag/churn_lag.py --run [--only "SF 2400 AC,SF 2400 Pro"] [--extra prodkey/deviceId] [--out churn.jsonl]
# the distribution mode is NOT touched here; switch it off before running and restore it afterwards.

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

POLL_SECONDS = 0.25  # http where available; mqtt read-polling was probed 2026-08-25 and the devices ignore it
HTTP_TIMEOUT = 3.0
LOW_W = 400
HIGH_W = 800
CHURN_S = 30.0
SETTLE_S = 12.0
FULL_LEVEL = 95.0  # a pack at or above this cannot take the charge churn, skip it

DEVICES: list[dict[str, Any]] = [
    {"label": "SF 2400 AC", "prodkey": "BC8B7F", "device_id": "MPEt6K1P", "ip": "192.168.2.3"},
    {"label": "SF 800 Pro", "prodkey": "R3mn8U", "device_id": "p0S70q2x", "ip": "192.168.2.5"},
    {"label": "SF 2400 AC Plus", "prodkey": "5fG27j", "device_id": "BVng8146", "ip": "192.168.2.17"},
    {"label": "SF 2400 Pro", "prodkey": "2Qe7C9", "device_id": "Vw6mE0cP", "ip": "192.168.2.18"},
    {"label": "SF 1600 AC Plus", "prodkey": "65174u", "device_id": "Cd3wbW9V", "ip": "192.168.2.21"},
    {"label": "Hyper 2000", "prodkey": "gDa3tb", "device_id": "9RUB1mW7", "ip": None},
    {"label": "SF 800 A", "prodkey": "Q331b1", "device_id": "A0r9v5UC", "ip": None},
]

client: mqtt.Client | None = None
samples: dict[str, list[tuple[float, float]]] = {}
levels: dict[str, float] = {}
events: dict[str, list[tuple[float, str]]] = {}
lock = threading.Lock()
stop_polling = threading.Event()
message_id = 860000
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
    if not isinstance(properties, dict):
        return
    if properties.get("electricLevel") is not None:
        with lock:
            levels[label] = float(properties["electricLevel"])
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


def on_message(_cl: mqtt.Client, _userdata: Any, message: mqtt.MQTTMessage) -> None:
    if not message.topic.startswith("/"):
        return  # device-origin traffic only; our own commands go to iot/...
    parts = [p for p in message.topic.split("/") if p]
    device = next((d for d in DEVICES if d["prodkey"] in parts and d["device_id"] in parts), None)
    if device is None or len(parts) < 3:
        return
    if parts[-2:] == ["properties", "report"]:
        try:
            properties = json.loads(message.payload.decode("utf-8", "ignore")).get("properties", {})
        except (json.JSONDecodeError, AttributeError):
            return
        if device["ip"] is None:
            record(device["label"], properties)
        elif isinstance(properties, dict) and properties.get("electricLevel") is not None:
            with lock:
                levels[device["label"]] = float(properties["electricLevel"])
    else:
        with lock:
            events.setdefault(device["label"], []).append((time.monotonic(), "/".join(parts[2:])))


def send(device: dict, watts: int) -> None:
    device_id, prodkey = device["device_id"], device["prodkey"]
    client.publish(f"iot/{prodkey}/{device_id}/properties/write", wrap(device_id, {"properties": {"hemsState": 1 if watts else 0}}))
    client.publish(f"iot/{prodkey}/{device_id}/function/invoke", wrap(device_id, {"function": "hemsEP", "arguments": {"outputPower": 0, "chargePower": watts, "freq": 0, "mode": 9, "chargeMode": 3}}))


def churn(device: dict, low: int, high: int, seconds: float, settle: float) -> None:
    label = device["label"]
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        with lock:
            fresh = samples[label] and time.monotonic() - samples[label][-1][0] < 15.0
        if fresh:
            break
        time.sleep(0.5)
    else:
        print(f"{stamp()} {label:16s} SKIP: geen rapporten, device niet bereikbaar", flush=True)
        return
    level = levels.get(label)
    if level is not None and level >= FULL_LEVEL:
        print(f"{stamp()} {label:16s} SKIP: electricLevel {level:.0f}%, laden klemt af", flush=True)
        return

    t0 = time.monotonic()
    count = 0
    while time.monotonic() - t0 < seconds:
        watts = high if count % 2 else low
        send(device, watts)
        count += 1
        time.sleep(1.0)
    last_watts = watts
    time.sleep(settle)

    with lock:
        series = [(t, p) for t, p in samples[label] if t >= t0]
        churn_events = [tail for t, tail in events.get(label, []) if t0 <= t <= t0 + seconds + settle]
    during = [(t, p) for t, p in series if t <= t0 + seconds]
    tail = sorted(p for t, p in series if t >= t0 + seconds + settle - 5.0)
    rate = len(during) / seconds
    gap = max((b - a for (a, _), (b, _) in zip(during, during[1:])), default=float("inf")) if len(during) > 1 else float("inf")
    mid = (low + high) / 2
    signs = [p > mid for _, p in during if abs(p - mid) > 50]
    transitions = sum(1 for a, b in zip(signs, signs[1:]) if a != b)
    settled = tail[len(tail) // 2] if tail else float("nan")
    verdict = "landt op laatste" if tail and abs(settled - last_watts) < 100 else "LANDT NIET op laatste"
    print(f"{stamp()} {label:16s} cmds {count} | {rate:4.1f} samples/s, max gat {gap:4.1f} s, {transitions:2d} wissels gezien | settled {settled:5.0f} W na laatste {last_watts} W -> {verdict} | events: {len(churn_events)}", flush=True)
    for tail_topic in sorted(set(churn_events)):
        print(f"{stamp()}   event: {tail_topic}", flush=True)
    send(device, 0)


def main() -> None:
    global client
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true", help="actually command the devices")
    parser.add_argument("--only", help="comma separated labels to churn")
    parser.add_argument("--extra", action="append", default=[], help="extra device as prodkey/deviceId, measured over the broker")
    parser.add_argument("--low", type=int, default=LOW_W)
    parser.add_argument("--high", type=int, default=HIGH_W)
    parser.add_argument("--seconds", type=float, default=CHURN_S)
    parser.add_argument("--settle", type=float, default=SETTLE_S)
    parser.add_argument("--out", help="append every sample to this jsonl")
    arguments = parser.parse_args()

    for extra in arguments.extra:
        prodkey, _, device_id = extra.partition("/")
        DEVICES.append({"label": device_id, "prodkey": prodkey, "device_id": device_id, "ip": None})
    for device in DEVICES:
        samples[device["label"]] = []
    selected = [d for d in DEVICES if not arguments.only or d["label"] in [p.strip() for p in arguments.only.split(",")]]

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="em_churn_lag")
    client.username_pw_set(USER, PSW)
    client.on_message = on_message
    client.on_connect = lambda cl, u, f, rc, p=None: (print(f"{stamp()} broker rc={rc}", flush=True), cl.subscribe("/#"))
    client.connect(BROKER, PORT, 30)
    client.loop_start()
    for device in DEVICES:
        if device["ip"]:
            threading.Thread(target=poll_http, args=(device,), daemon=True).start()

    try:
        time.sleep(4.0)
        with lock:
            for device in selected:
                rate = len(samples[device["label"]]) / 4.0
                print(f"{stamp()} {device['label']:16s} {'http ' + str(device['ip']) if device['ip'] else 'broker':22s} {rate:4.1f} samples/s", flush=True)
        if not arguments.run:
            return
        for device in selected:
            send(device, 0)
        time.sleep(5.0)
        for device in selected:
            churn(device, arguments.low, arguments.high, arguments.seconds, arguments.settle)
    finally:
        if arguments.run:
            for device in selected:
                send(device, 0)
        stop_polling.set()
        time.sleep(1.0)
        client.loop_stop()
        client.disconnect()
        if arguments.out:
            with open(arguments.out, "a", encoding="utf-8") as handle:
                for label, series in samples.items():
                    for moment, power in series:
                        handle.write(json.dumps({"device": label, "t": round(moment - start_time, 3), "power": power}) + "\n")
            print(f"samples appended to {arguments.out}", flush=True)


main()
