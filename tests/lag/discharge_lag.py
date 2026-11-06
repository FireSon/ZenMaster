#!/usr/bin/env python3
"""Discharge lag per device over three command paths: hemsEP, the old zendure_ha properties, and the local zensdk HTTP.

Pattern per device: 10, 25, 50, 75 and 100 percent of its own inverseMaxPower, all devices stepping at the same moment.
Measurement channel is the device itself: GET /properties/report every second for the five with a local HTTP API, and a
one-second property poll over the broker for the Hyper. Paths a device cannot reach are printed as skipped, never dropped.
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

_SECRETS = json.load(open("secrets.json", encoding="utf-8"))  # untracked, keeps the broker login and the HA token out of the repo
BROKER, PORT, USER, PSW = _SECRETS["MQTT_BROKER"], _SECRETS["MQTT_PORT"], _SECRETS["MQTT_USER"], _SECRETS["MQTT_PASSWORD"]  # local broker, kept for the local test later

STEPS = [0.10, 0.25, 0.50, 0.75, 1.00]  # fractions of the device's own discharge limit
HOLD_SECONDS = 45.0  # long enough for the slowest measured device response plus settling
ZERO_SECONDS = 30.0  # back to 0 between steps, so every step starts from rest
POLL_SECONDS = 1.0  # local HTTP report poll per device
MOVE_WATT = 50  # smallest change that counts as the device reacting
HTTP_TIMEOUT = 3.0

# ip verified device by device from its own report payload; device_id from config.json, matched on prodkey because the names there are stale
DEVICES: list[dict[str, Any]] = [
    {"label": "SF 2400 AC", "prodkey": "BC8B7F", "device_id": "MPEt6K1P", "ip": "192.168.2.3", "sn": "HOA1NPN3N210137"},
    {"label": "SF 800 Pro", "prodkey": "R3mn8U", "device_id": "p0S70q2x", "ip": "192.168.2.5", "sn": "EOA1NHN3N180450"},
    {"label": "SF 2400 AC Plus", "prodkey": "5fG27j", "device_id": "BVng8146", "ip": "192.168.2.17", "sn": "HEC4AENCN520012"},
    {"label": "SF 2400 Pro", "prodkey": "2Qe7C9", "device_id": "Vw6mE0cP", "ip": "192.168.2.18", "sn": "EEB4AENCN520045"},
    {"label": "SF 1600 AC Plus", "prodkey": "65174u", "device_id": "Cd3wbW9V", "ip": "192.168.2.21", "sn": "HED4NENBN470562"},
    {"label": "Hyper 2000", "prodkey": "gDa3tb", "device_id": "9RUB1mW7", "ip": None, "sn": "EE1LHNBTN151322"},
]

# the old zendure_ha property set, identical to _BLEHTTP in solarflow.py: off, charge, discharge
PROPS_OFF = {"smartMode": 0, "acMode": 2, "outputLimit": 0, "inputLimit": 0}
PROPS_DISCHARGE = {"smartMode": 1, "acMode": 2, "outputLimit": 0, "inputLimit": 0}

clients: dict[str, mqtt.Client] = {}  # label -> the broker that carries this device; empty while no broker is configured
samples: dict[str, list[tuple[float, float, float, float]]] = {device["label"]: [] for device in DEVICES}
lock = threading.Lock()
stop_polling = threading.Event()
message_id = 700000
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


def report(ip: str) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://{ip}/properties/report", timeout=HTTP_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8", "ignore"))
    except Exception:
        return None


def record(label: str, payload: dict) -> None:
    properties = payload.get("properties", {})
    packs = payload.get("packData", []) or []

    # a partial report without the power fields is no measurement: counting it as 0 puts false zeros in the trace
    if "outputHomePower" not in properties and "gridInputPower" not in properties:
        return
    power = float(properties.get("outputHomePower", 0) or 0) - float(properties.get("gridInputPower", 0) or 0)

    # decikelvin, both in packData and in the polled hyperTmp property
    raw = max((float(pack.get("maxTemp", 0) or 0) for pack in packs), default=float(properties.get("hyperTmp", 0) or 0))
    with lock:
        samples[label].append((time.monotonic(), power, float(properties.get("electricLevel", 0) or 0), round((raw - 2731) / 10, 1) if raw else 0.0))


def poll_http(device: dict) -> None:
    while not stop_polling.is_set():
        payload = report(device["ip"])
        if payload is not None:
            record(device["label"], payload)
        stop_polling.wait(POLL_SECONDS)


def poll_mqtt(device: dict) -> None:
    # the device only reports in bursts, so ask for the four properties we measure, every second
    while not stop_polling.is_set():
        if (client := clients.get(device["label"])) is not None:
            client.publish(f"iot/{device['prodkey']}/{device['device_id']}/properties/read", wrap(device["device_id"], {"properties": ["outputHomePower", "gridInputPower", "electricLevel", "hyperTmp"]}))
        stop_polling.wait(POLL_SECONDS)


def limit_http(device: dict) -> int:
    payload = report(device["ip"])
    return int(payload.get("properties", {}).get("inverseMaxPower", 0)) if payload else 0


def on_connect(client: mqtt.Client, _userdata: Any, _flags: Any, reason_code: Any, _properties: Any = None) -> None:
    print(f"{stamp()} broker connected rc={reason_code}")
    for device in DEVICES:
        client.subscribe(f"/+/{device['device_id']}/#")


def on_message(_client: mqtt.Client, _userdata: Any, message: mqtt.MQTTMessage) -> None:
    # a polled device answers on properties/read/reply, a reporting device on properties/report
    if not message.topic.endswith(("properties/report", "properties/read/reply")):
        return
    for device in DEVICES:
        if device["device_id"] in message.topic:
            try:
                record(device["label"], json.loads(message.payload.decode("utf-8", "ignore")))
            except json.JSONDecodeError:
                return


def send(device: dict, path: str, watts: int) -> None:
    device_id, prodkey = device["device_id"], device["prodkey"]
    client = clients.get(device["label"])
    if path in ("hems_mqtt", "zendure_mqtt") and client is None:
        print(f"{stamp()} {device['label']}: no broker for {path}, skipped")
        return
    if path == "hems_mqtt":
        client.publish(f"iot/{prodkey}/{device_id}/properties/write", wrap(device_id, {"properties": {"hemsState": 1 if watts else 0}}))
        body = {"function": "hemsEP", "arguments": {"outputPower": watts, "chargePower": 0, "freq": 0, "minSoc": 100, "mode": 9}}
        client.publish(f"iot/{prodkey}/{device_id}/function/invoke", wrap(device_id, body))
    elif path == "zendure_mqtt":
        properties = dict(PROPS_DISCHARGE, outputLimit=watts) if watts else dict(PROPS_OFF)
        client.publish(f"iot/{prodkey}/{device_id}/properties/write", wrap(device_id, {"properties": properties}))
    elif path == "zensdk_http":
        properties = dict(PROPS_DISCHARGE, outputLimit=watts) if watts else dict(PROPS_OFF)
        # the local API rejects a write without the serial: {"error":"invalid SN in JSON","code":401}
        request = urllib.request.Request(f"http://{device['ip']}/properties/write", data=json.dumps({"sn": device["sn"], "properties": properties}).encode(), headers={"content-type": "application/json; charset=UTF-8"})
        try:
            urllib.request.urlopen(request, timeout=HTTP_TIMEOUT).read()
        except Exception as error:
            print(f"{stamp()} {device['label']}: zensdk write failed: {type(error).__name__}")


def paths_for(device: dict) -> list[str]:
    # both MQTT paths need a broker the device reports on; zensdk needs a local HTTP API, which the Hyper has not
    paths = ["hems_mqtt", "zendure_mqtt"] if device["label"] in clients else []
    return paths + (["zensdk_http"] if device["ip"] else [])


def analyse(label: str, target: int, sent_at: float) -> dict[str, Any]:
    with lock:
        series = list(samples[label])
    before = [power for moment, power, _, _ in series if sent_at - 12 <= moment <= sent_at]
    after = [(moment, power, temperature) for moment, power, _, temperature in series if moment > sent_at]
    if not before or not after:
        return {"label": label, "target": target, "note": f"no samples (before={len(before)} after={len(after)})"}
    baseline = statistics.median(before)

    # dead time to the first real movement, rise time to 90 percent of the requested step, and what is left after settling
    dead = next((moment - sent_at for moment, power, _ in after if abs(power - baseline) >= MOVE_WATT), None)
    reached = next((moment - sent_at for moment, power, _ in after if abs(power - baseline) >= 0.9 * abs(target - baseline)), None)
    settled = statistics.median([power for moment, power, _ in after if moment >= after[-1][0] - 10]) if after else 0.0
    return {"label": label, "target": target, "baseline": baseline, "dead": dead, "rise": reached, "settled": settled, "gap": target - settled, "temp": after[-1][2]}


def show(rows: list[dict[str, Any]]) -> None:
    print(f"{'device':16s} {'target':>7s} {'base':>7s} {'dead':>7s} {'rise':>7s} {'settled':>8s} {'gap':>7s} {'temp':>6s}")
    for row in rows:
        if "note" in row:
            print(f"{row['label']:16s} {row['target']:7d}  {row['note']}")
            continue
        dead = f"{row['dead']:.1f}s" if row["dead"] is not None else "n/a"
        rise = f"{row['rise']:.1f}s" if row["rise"] is not None else "n/a"
        print(f"{row['label']:16s} {row['target']:7d} {row['baseline']:7.0f} {dead:>7s} {rise:>7s} {row['settled']:8.0f} {row['gap']:7.0f} {row['temp']:6.1f}")


def release() -> None:
    for device in DEVICES:
        if (client := clients.get(device["label"])) is not None:
            client.publish(f"iot/{device['prodkey']}/{device['device_id']}/function/invoke", wrap(device["device_id"], {"function": "hemsEP", "arguments": {"outputPower": 0, "chargePower": 0, "freq": 0, "mode": 9, "chargeMode": 3}}))
            client.publish(f"iot/{device['prodkey']}/{device['device_id']}/properties/write", wrap(device["device_id"], {"properties": {"hemsState": 0}}))
        if device["ip"]:
            send(device, "zensdk_http", 0)
    print(f"{stamp()} released: 0 W and hemsState 0 on every device")


def preconditions() -> bool:
    # our own distribution and the zendure_ha automation must be out of the way, or the run measures their corrections
    head = {"Authorization": "Bearer " + _SECRETS["HA_TOKEN"]}
    clean = True
    for entity, wanted in (("select.zenmaster_mode", "off"), ("automation.zendure_smart_charging", "off")):
        try:
            request = urllib.request.Request(f"{_SECRETS['HA_URL'].rstrip('/')}/api/states/{entity}", headers=head)
            state = json.load(urllib.request.urlopen(request, timeout=10))["state"]
        except Exception as error:
            print(f"  {entity}: cannot read ({type(error).__name__})")
            clean = False
            continue
        print(f"  {entity} = {state} (wanted {wanted})")
        clean = clean and state == wanted
    return clean


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true", help="actually command the devices; without it the script only listens")
    parser.add_argument("--seconds", type=float, default=20.0, help="listen time in dry-run")
    parser.add_argument("--paths", default="hems_mqtt,zendure_mqtt,zensdk_http", help="comma separated subset of the three command paths")
    parser.add_argument("--force", action="store_true", help="run even when the preconditions fail")
    parser.add_argument("--out", help="write every sample to this jsonl for later analysis")
    parser.add_argument("--only", help="comma separated device labels to command; the rest is left alone")
    arguments = parser.parse_args()
    wanted_paths = [path.strip() for path in arguments.paths.split(",") if path.strip()]

    # the Hyper is provisioned to the local broker, the five others report to the cloud broker
    connected: list[mqtt.Client] = []
    local = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="em_discharge_lag_local")
    local.username_pw_set(USER, PSW)
    local.on_connect = on_connect
    local.on_message = on_message
    local.connect(BROKER, PORT, 30)
    local.loop_start()
    connected.append(local)
    for device in DEVICES:
        if device["ip"] is None:
            clients[device["label"]] = local
    for device in DEVICES:
        clients[device["label"]] = local  # every device is provisioned to the local broker since 2026-08-24

    pollers = [threading.Thread(target=poll_http if device["ip"] else poll_mqtt, args=(device,), daemon=True) for device in DEVICES]
    for poller in pollers:
        poller.start()

    try:
        print("preconditions:")
        clean = preconditions()
        if not clean and arguments.run and not arguments.force:
            print("preconditions not met, stopping. Use --force to override.")
            return

        # the discharge limit is per device and comes from the device, not from PRODKEYS: two of them are set lower in the app
        limits = {}
        for device in DEVICES:
            limits[device["label"]] = limit_http(device) if device["ip"] else 1200
            print(f"  {device['label']:16s} discharge limit {limits[device['label']]} W, paths {paths_for(device)}")

        if not arguments.run:
            print(f"\nDRY-RUN: listening {arguments.seconds:.0f}s, no commands sent.")
            time.sleep(arguments.seconds)
            with lock:
                for label, series in samples.items():
                    print(f"  {label:16s} {len(series)} samples")
            return

        for path in wanted_paths:
            chosen = [label.strip() for label in arguments.only.split(",")] if arguments.only else None
            active = [device for device in DEVICES if path in paths_for(device) and (chosen is None or device["label"] in chosen)]
            skipped = [device["label"] for device in DEVICES if path not in paths_for(device)]
            if not active:
                print(f"\n== {path}: no device supports this path, skipped ==")
                continue
            print(f"\n== {path} == devices {[device['label'] for device in active]}, skipped {skipped}")

            release()
            time.sleep(ZERO_SECONDS)
            for fraction in STEPS:
                targets = {device["label"]: int(limits[device["label"]] * fraction) for device in active}
                sent_at = time.monotonic()
                for device in active:
                    send(device, path, targets[device["label"]])
                print(f"{stamp()} >>> {path} step {fraction:.0%}: {targets}")
                time.sleep(HOLD_SECONDS)
                show([analyse(device["label"], targets[device["label"]], sent_at) for device in active])

                # back to rest, so the next step measures a cold start again
                for device in active:
                    send(device, path, 0)
                time.sleep(ZERO_SECONDS)
    finally:
        stop_polling.set()
        if arguments.run:
            release()
            time.sleep(1.0)
        if arguments.out:
            with open(arguments.out, "w", encoding="utf-8") as handle:
                with lock:
                    for label, series in samples.items():
                        for moment, power, level, temperature in series:
                            handle.write(json.dumps({"device": label, "t": round(moment - start_time, 2), "power": power, "soc": level, "temp": temperature}) + "\n")
            print(f"samples written to {arguments.out}")
        for client in connected:
            client.loop_stop()
            client.disconnect()


if __name__ == "__main__":
    main()
