"""Recorder -> replay series: pull a period from the local recorder into the shared replay JSON."""

# usage: python tests/replay/replay_recorder.py 2026-08-13T06:00 2026-08-13T11:45 [out.json]

import json
import sqlite3
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

DB = "config/home-assistant_v2.db"
ENTITY_REGISTRY = "config/.storage/core.entity_registry"
DEVICE_REGISTRY = "config/.storage/core.device_registry"
SECRETS = "secrets.json"
P1_ENTITY = "sensor.p1_meter_totalpower"
PV_ENTITY = "sensor.inverter_active_power"  # roof PV, only on the production instance
SUFFIXES = ("home_input", "home_output", "solar", "soc")

# pack capacity per model, mirrors the serial mapping in zendure/battery.py
PACK_KWH = {"AB1000": 0.96, "AB1000S": 0.96, "AB2000": 1.92, "AB2000S": 1.92, "AB2000X": 1.92, "AB3000": 2.88, "AB3000L": 2.88, "AIO2400": 2.4}

# rated discharge/charge power per model, mirrors PRODKEYS in zendure/driver.py
MODEL_POWER = {
    "Solarflow 2400 AC": (2400, -2400), "Solarflow 2400 AC Plus": (2400, -2400), "Solarflow 2400 Pro": (2400, -2400), "Solarflow 1600 AC Plus": (1600, -1600),
    "Solarflow 800": (800, -1000), "Solarflow 800 Pro": (800, -1000), "Solarflow 800 Plus": (800, -800), "Hub 1200": (800, -1200), "Hub 2000": (800, -2000),
    "Hyper 2000": (1200, -1200), "Ace 1500": (800, -1500),
}


def series(cur: sqlite3.Cursor, entity: str, start: float, end: float) -> list[tuple[float, float]]:
    """Numeric (timestamp, value) pairs in the window, seeded with the last value before it."""
    rows = cur.execute(
        "SELECT s.last_updated_ts, s.state FROM states s JOIN states_meta m ON m.metadata_id = s.metadata_id"
        " WHERE m.entity_id = ? AND s.last_updated_ts < ? AND s.state NOT IN ('unknown', 'unavailable') ORDER BY s.last_updated_ts DESC LIMIT 1",
        (entity, start),
    ).fetchall()
    rows += cur.execute(
        "SELECT s.last_updated_ts, s.state FROM states s JOIN states_meta m ON m.metadata_id = s.metadata_id"
        " WHERE m.entity_id = ? AND s.last_updated_ts BETWEEN ? AND ? ORDER BY s.last_updated_ts",
        (entity, start, end),
    ).fetchall()
    out = []
    for ts, state in rows:
        try:
            out.append((ts, float(state)))
        except (TypeError, ValueError):
            continue
    return out


def remote(entity: str, start: float, end: float) -> list[tuple[float, float]]:
    """Same pairs from the production instance; the roof PV is not in the local recorder."""
    cfg = json.load(open(SECRETS, encoding="utf-8"))
    query = urllib.parse.urlencode({"filter_entity_id": entity, "end_time": datetime.fromtimestamp(end, timezone.utc).isoformat(), "minimal_response": "", "no_attributes": ""})
    begin = datetime.fromtimestamp(start - 900, timezone.utc).isoformat()
    request = urllib.request.Request(f"{cfg['HA_URL'].rstrip('/')}/api/history/period/{begin}?{query}", headers={"Authorization": f"Bearer {cfg['HA_TOKEN']}"})
    result = json.load(urllib.request.urlopen(request, timeout=180))
    out = []
    for point in (result[0] if result else []):
        stamp = point.get("last_changed") or point.get("last_updated")
        try:
            out.append((datetime.fromisoformat(stamp).timestamp(), float(point["state"])))
        except (TypeError, ValueError, KeyError):
            continue
    return out


def fill(points: list[tuple[float, float]], timeline: list[float]) -> list[float]:
    """Forward-fill a sparse series onto the master timeline; before the first point it is 0."""
    out, i, last = [], 0, 0.0
    for t in timeline:
        while i < len(points) and points[i][0] <= t:
            last = points[i][1]
            i += 1
        out.append(last)
    return out


def main() -> None:
    start = datetime.fromisoformat(sys.argv[1]).astimezone().timestamp()
    end = datetime.fromisoformat(sys.argv[2]).astimezone().timestamp()
    target = sys.argv[3] if len(sys.argv) > 3 else "replay.json"

    # map every entity to its device, then keep the devices that carry a full set of distribution sensors
    entities = json.load(open(ENTITY_REGISTRY, encoding="utf-8"))["data"]["entities"]
    devices = {d["id"]: d for d in json.load(open(DEVICE_REGISTRY, encoding="utf-8"))["data"]["devices"]}
    found: dict[str, dict[str, str]] = {}
    for e in entities:
        if not (dev := devices.get(e.get("device_id") or "")) or not e["entity_id"].startswith("sensor."):
            continue
        for suffix in SUFFIXES:
            if e["entity_id"].endswith("_" + suffix):
                found.setdefault(dev["id"], {})[suffix] = e["entity_id"]

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cur = con.cursor()
    timeline = [t for t, _ in series(cur, P1_ENTITY, start, end) if t >= start]  # the P1 readings are the master clock
    if not timeline:
        raise SystemExit(f"no {P1_ENTITY} data between the given timestamps")
    p1 = fill(series(cur, P1_ENTITY, start, end), timeline)

    meta, power, solar, soc = [], {}, {}, {}
    for device_id, sensors in found.items():
        dev = devices[device_id]
        if len(sensors) < 4 or (rated := MODEL_POWER.get(dev.get("model") or "")) is None:
            continue
        key = sensors["soc"].removeprefix("sensor.").removesuffix("_soc")
        kwh = sum(PACK_KWH.get(c.get("model") or "", 0.0) for c in devices.values() if c.get("via_device_id") == device_id)
        meta.append({"key": key, "name": dev.get("name_by_user") or dev.get("name"), "model": dev.get("model"), "kwh": round(kwh, 2), "power_discharge": rated[0], "power_charge": rated[1]})

        # the device contract is one signed value: positive discharges, negative charges
        out = fill(series(cur, sensors["home_output"], start, end), timeline)
        inp = fill(series(cur, sensors["home_input"], start, end), timeline)
        power[key] = [round(a - b) for a, b in zip(out, inp)]
        solar[key] = [round(v) for v in fill(series(cur, sensors["solar"], start, end), timeline)]
        soc[key] = fill(series(cur, sensors["soc"], start, end), timeline)

    pv = [round(v) for v in fill(remote(PV_ENTITY, start, end), timeline)]
    payload = {
        "meta": {"source": "recorder-local", "start": sys.argv[1], "end": sys.argv[2], "samples": len(timeline), "p1_entity": P1_ENTITY, "pv_entity": PV_ENTITY},
        "devices": meta,
        "t": [round(t, 3) for t in timeline],
        "p1": [round(v) for v in p1],
        "pv": pv,
        "power": power,
        "solar": solar,
        "soc": soc,
    }
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))

    hours = (timeline[-1] - timeline[0]) / 3600
    print(f"{target}: {len(timeline)} samples over {hours:.1f} h, {len(meta)} devices, PV piek {max(pv)} W")
    for d in meta:
        print(f"  {d['key']:<22} {d['model']:<24} {d['kwh']:>5.2f} kWh  {d['power_discharge']:>5} / {d['power_charge']:>6} W")


if __name__ == "__main__":
    main()
