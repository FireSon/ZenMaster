"""Production -> replay series: pull a period from the production instance, where the Zendure integration steered the fleet."""

# usage: python tests/replay/replay_production.py 2026-08-07T06:00 2026-08-07T18:00 [out.json]

import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime

SECRETS = "secrets.json"
SOC_BAND = (10.0, 100.0)  # minSoc/socSet only start being recorded on 11 August; that first pair is the setting that was already there
PV_ENTITY = "sensor.inverter_active_power"
MODE_ENTITY = "select.zendure_manager_operation"
P1_IMPORT = "sensor.jmdp12pslp_power_consumed"  # kW
P1_EXPORT = "sensor.jmdp12pslp_power_produced"  # kW

# the Zendure integration names its telemetry differently; the values behind them are the same device properties
SUFFIXES = {"home_output": "output_home_power", "home_input": "grid_input_power", "solar": "solar_input_power", "soc": "electric_level"}

# rated discharge/charge power per base entity name, mirrors PRODKEYS in zendure/driver.py
MODEL_POWER = {
    "solarflow_2400_ac": (2400, -2400), "solarflow_2400_ac_plus": (2400, -2400), "solarflow_2400_pro": (2400, -2400), "solarflow_1600_ac_plus": (1600, -1600),
    "solarflow_800": (800, -1000), "solarflow_8002": (800, -1000), "solarflow_800_pro": (800, -1000), "hyper_2000": (1200, -1200),
}


def history(entity: str, start: float, end: float) -> list[tuple[float, float]]:
    """Numeric (timestamp, value) pairs from the production history API, seeded with a quarter hour of lead-in."""
    cfg = json.load(open(SECRETS, encoding="utf-8"))
    query = urllib.parse.urlencode({"filter_entity_id": entity, "end_time": datetime.utcfromtimestamp(end).isoformat() + "+00:00", "minimal_response": "", "no_attributes": ""})
    begin = datetime.utcfromtimestamp(start - 900).isoformat() + "+00:00"
    request = urllib.request.Request(f"{cfg['HA_URL'].rstrip('/')}/api/history/period/{begin}?{query}", headers={"Authorization": f"Bearer {cfg['HA_TOKEN']}"})
    result = json.load(urllib.request.urlopen(request, timeout=300))
    out = []
    for point in (result[0] if result else []):
        stamp = point.get("last_changed") or point.get("last_updated")
        try:
            out.append((datetime.fromisoformat(stamp).timestamp(), float(point["state"])))
        except (TypeError, ValueError, KeyError):
            continue
    return out


def text_history(entity: str, start: float, end: float) -> list[tuple[float, str]]:
    """Same as history, but keeps the state as text: the operation mode is not a number."""
    cfg = json.load(open(SECRETS, encoding="utf-8"))
    query = urllib.parse.urlencode({"filter_entity_id": entity, "end_time": datetime.utcfromtimestamp(end).isoformat() + "+00:00", "minimal_response": "", "no_attributes": ""})
    begin = datetime.utcfromtimestamp(start - 86400).isoformat() + "+00:00"
    request = urllib.request.Request(f"{cfg['HA_URL'].rstrip('/')}/api/history/period/{begin}?{query}", headers={"Authorization": f"Bearer {cfg['HA_TOKEN']}"})
    result = json.load(urllib.request.urlopen(request, timeout=300))
    out = []
    for point in (result[0] if result else []):
        stamp = point.get("last_changed") or point.get("last_updated")
        if stamp and point.get("state") not in ("unknown", "unavailable", None):
            out.append((datetime.fromisoformat(stamp).timestamp(), point["state"]))
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
    target = sys.argv[3] if len(sys.argv) > 3 else "replay-prod.json"

    # the import meter is the master clock; the export series is filled onto it and both are kW
    consumed = [(t, v) for t, v in history(P1_IMPORT, start, end) if t >= start]
    if not consumed:
        raise SystemExit(f"no {P1_IMPORT} data between the given timestamps")
    timeline = [t for t, _ in consumed]
    produced = fill(history(P1_EXPORT, start, end), timeline)
    p1 = [round((v - produced[i]) * 1000) for i, (_, v) in enumerate(consumed)]
    pv = [round(v) for v in fill(history(PV_ENTITY, start, end), timeline)]

    meta, power, solar, soc = [], {}, {}, {}
    for base, rated in MODEL_POWER.items():
        levels = history(f"sensor.{base}_{SUFFIXES['soc']}", start, end)
        outputs = history(f"sensor.{base}_{SUFFIXES['home_output']}", start, end)
        solars = history(f"sensor.{base}_{SUFFIXES['solar']}", start, end)
        if not levels and not outputs and not solars:
            continue
        # total_kwh is not recorded here, but available_kwh runs linear with the level, so its slope gives the capacity
        available = fill(history(f"sensor.{base}_available_kwh", start, end), timeline)
        levels_filled = fill(levels, timeline)
        pairs = [(levels_filled[i], available[i]) for i in range(len(timeline)) if available[i]]
        kwh = (pairs[-1][1] - pairs[0][1]) / (pairs[-1][0] - pairs[0][0]) * 100 if len(pairs) > 1 and pairs[-1][0] != pairs[0][0] else 0.0
        # min_soc and soc_set have no history here, so the band comes from what our own integration read off the same hardware
        meta.append(
            {"key": base, "name": base, "model": base, "kwh": round(kwh, 2), "power_discharge": rated[0], "power_charge": rated[1], "soc_min": SOC_BAND[0], "soc_max": SOC_BAND[1]}
        )

        # the device contract is one signed value: positive discharges, negative charges
        out = fill(outputs, timeline)
        inp = fill(history(f"sensor.{base}_{SUFFIXES['home_input']}", start, end), timeline)
        power[base] = [round(a - b) for a, b in zip(out, inp)]
        solar[base] = [round(v) for v in fill(solars, timeline)]
        soc[base] = fill(levels, timeline)

    # the mode the other integration was in; without it we would replay our own mode over their day
    modes, i, last = [], 0, ""
    operation = [(t, str(v)) for t, v in [(x[0], x[1]) for x in text_history(MODE_ENTITY, start, end)]]
    for stamp in timeline:
        while i < len(operation) and operation[i][0] <= stamp:
            last = operation[i][1]
            i += 1
        modes.append(last)

    payload = {
        "meta": {"source": "recorder-production", "start": sys.argv[1], "end": sys.argv[2], "samples": len(timeline), "p1_entity": P1_IMPORT, "pv_entity": PV_ENTITY},
        "mode": modes,
        "devices": meta,
        "t": [round(t, 3) for t in timeline],
        "p1": p1,
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
        print(f"  {d['key']:<24} {d['kwh']:>5.2f} kWh  {d['power_discharge']:>5} / {d['power_charge']:>6} W")


if __name__ == "__main__":
    main()
