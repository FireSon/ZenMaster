#!/usr/bin/env python3
"""Production (our own integration) -> replay series: pull a period into the shared replay JSON.

Unlike replay_production.py this reads the ZenMaster entity names, not the zendure_ha ones.
"""

# usage: python tests/replay/replay_em.py 2026-08-24T09:25 2026-08-24T13:30 [out.json]

import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

SECRETS = "secrets.json"
P1_ENTITY = "sensor.power_actual"
PV_ENTITY = "sensor.inverter_active_power"
MODE_ENTITY = "select.zenmaster_mode"

# rated discharge/charge power per key fragment, mirrors PRODKEYS in zendure/driver.py; the short fragments are the v2 entity names
MODEL_POWER = [
    ("sf2400ac_plus", (2400, -2400)), ("sf2400ac", (2400, -2400)), ("sf2400pro", (2400, -2400)), ("sf2400_", (2400, -2400)),
    ("sf1600ac_plus", (1600, -1600)), ("sf1600_", (1600, -1600)), ("sf800pro", (800, -1000)), ("sf800", (800, -1000)), ("hyper2000", (1200, -1200)),
]

# pack energy per device when sensor.<key>_kwh does not exist (the v1 names), measured fleet: total is 25 kWh per sensor.zenmaster_capacity_kwh
PACK_KWH = {"sf2400ac_ha1_7": 5.76, "sf2400ac_plus_hc4_2": 3.84, "sf2400pro_eb4_5": 3.84, "sf1600ac_plus_hd4_2": 3.84,
            "sf800_wb1_0": 1.92, "sf800_wb1_8": 1.92, "sf800pro_ea1_0": 1.92, "hyper2000_e1_22": 1.92}

# the phase every device sits on, from the zenmaster.devices Store (power_phase) on 2026-09-18; 0 = unknown, spread over three
PHASE = {"sf2400_45": 1, "sf1600_62": 1, "sf2400_12": 1, "sf800_50": 1, "sf2400_37": 2, "sf800_88": 3, "sf800_90": 3, "hyper2000_22": 3}
PHASE_ENTITIES = {n: (f"sensor.power_consumed_phase_{n}", f"sensor.power_produced_phase_{n}") for n in (1, 2, 3)}
MANUAL_ENTITY = "number.zenmaster_manual_power"

CFG = json.load(open(SECRETS, encoding="utf-8"))


def api(path: str):
    request = urllib.request.Request(f"{CFG['HA_URL'].rstrip('/')}{path}", headers={"Authorization": f"Bearer {CFG['HA_TOKEN']}"})
    return json.load(urllib.request.urlopen(request, timeout=300))


def history(entity: str, start: float, end: float) -> list[tuple[float, str]]:
    query = urllib.parse.urlencode({"filter_entity_id": entity, "end_time": datetime.fromtimestamp(end, timezone.utc).isoformat(), "minimal_response": "", "no_attributes": ""})
    begin = datetime.fromtimestamp(start - 900, timezone.utc).isoformat()
    result = api(f"/api/history/period/{urllib.parse.quote(begin)}?{query}")
    out = []
    for point in (result[0] if result else []):
        stamp = point.get("last_changed") or point.get("last_updated")
        if stamp is not None and point.get("state") is not None:
            out.append((datetime.fromisoformat(stamp).timestamp(), point["state"]))
    return out


def numbers(entity: str, start: float, end: float) -> list[tuple[float, float]]:
    out = []
    for stamp, state in history(entity, start, end):
        try:
            out.append((stamp, float(state)))
        except (TypeError, ValueError):
            continue
    return out


def fill(points: list, timeline: list[float], empty=0.0) -> list:
    out, i, last = [], 0, empty
    for t in timeline:
        while i < len(points) and points[i][0] <= t:
            last = points[i][1]
            i += 1
        out.append(last)
    return out


def main() -> None:
    start = datetime.fromisoformat(sys.argv[1]).astimezone().timestamp()
    end = datetime.fromisoformat(sys.argv[2]).astimezone().timestamp()
    target = sys.argv[3] if len(sys.argv) > 3 else "replay-em.json"

    # every device that carries the four sensors the replay needs
    states = {state["entity_id"]: state["state"] for state in api("/api/states")}
    names = list(states)
    keys = sorted({m.group(1) for name in names if (m := re.fullmatch(r"sensor\.(.+)_home_output", name))})
    keys = [k for k in keys if all(f"sensor.{k}_{s}" in names for s in ("home_input", "solar", "soc"))]
    print(f"devices: {keys}")

    timeline = [t for t, _ in numbers(P1_ENTITY, start, end) if t >= start]
    if not timeline:
        raise SystemExit(f"no {P1_ENTITY} data between the given timestamps")
    p1 = [round(v) for v in fill(numbers(P1_ENTITY, start, end), timeline)]
    print(f"timeline: {len(timeline)} samples over {(timeline[-1] - timeline[0]) / 3600:.2f} h")

    meta, power, solar, soc = [], {}, {}, {}
    for key in keys:
        rated = next((r for fragment, r in MODEL_POWER if key.startswith(fragment)), None)
        if rated is None:
            print(f"  skipping {key}: no rated power known")
            continue
        out = fill(numbers(f"sensor.{key}_home_output", start, end), timeline)
        inp = fill(numbers(f"sensor.{key}_home_input", start, end), timeline)
        power[key] = [round(a - b) for a, b in zip(out, inp)]
        solar[key] = [round(v) for v in fill(numbers(f"sensor.{key}_solar", start, end), timeline)]
        soc[key] = fill(numbers(f"sensor.{key}_soc", start, end), timeline)
        try:
            kwh = float(states[f"sensor.{key}_kwh"])
        except (KeyError, ValueError):
            kwh = PACK_KWH.get(key, 1.92)
        meta.append({"key": key, "name": key, "model": key, "kwh": kwh, "power_discharge": rated[0], "power_charge": rated[1], "phase": PHASE.get(key, 0)})

    pv = [round(v) for v in fill(numbers(PV_ENTITY, start, end), timeline)]
    mode = fill(history(MODE_ENTITY, start, end), timeline, empty="off")
    manual = [round(v) for v in fill(numbers(MANUAL_ENTITY, start, end), timeline)]
    payload = {"t": timeline, "p1": p1, "pv": pv, "mode": mode, "manual": manual, "devices": meta, "power": power, "solar": solar, "soc": soc}
    # the meter per phase, net import, when the P1 device has them
    for n, (consumed, produced) in PHASE_ENTITIES.items():
        if f"sensor.power_consumed_phase_{n}" in names:
            cons = fill(numbers(consumed, start, end), timeline)
            prod = fill(numbers(produced, start, end), timeline)
            payload[f"l{n}"] = [round(a - b) for a, b in zip(cons, prod)]
    json.dump(payload, open(target, "w", encoding="utf-8"))
    print(f"written {target}: {len(meta)} devices, modes {sorted(set(mode))}, phases {[k for k in payload if k.startswith('l')]}")


main()
