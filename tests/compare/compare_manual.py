"""Play one fixed setpoint pattern at the manual power of a distribution and log what the fleet does with it."""

# usage: python tests/compare/compare_manual.py play ours|theirs both|charge|discharge|<apparaat> [out.json]
#        python tests/compare/compare_manual.py report a.json b.json

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SECRETS = "secrets.json"
PROFILES = "tests/simulation/real_profiles.json"  # per-second appliance recordings from production
SETTLE_W = 100  # a segment counts as reached at this error, or at 10 percent of the demand

# the two managers, each in manual: write the setpoint, read back what the fleet delivers
TARGETS = {
    "ours": {"url": "LOCAL_URL", "token": "LOCAL_TOKEN", "mode": ("select.zenmaster_mode", "manual"), "set": "number.zenmaster_manual_power", "get": ("sensor.zenmaster_discharge_power", "sensor.zenmaster_charge_power")},
    "theirs": {"url": "HA_URL", "token": "HA_TOKEN", "mode": ("select.zendure_manager_operation", "manual"), "set": "number.zendure_manager_manual_power", "get": ("sensor.zendure_manager_power",)},
}

# (seconds, from watts, to watts); positive discharges, negative charges. Equal ends hold a level, unequal ones ramp per second
PATTERNS = {
    "both": [
        (60, 0, 0),  # rest, so the starting point is visible
        (120, -1000, -1000),  # charge step within a single device
        (120, -3000, -3000),  # charge step that forces the set to grow
        (60, 0, 0),
        (120, 1000, 1000),  # sign flip
        (120, 3000, 3000),
        (60, 0, 0),
        (30, -50, -50),  # around the dead zone, both signs
        (30, 50, 50),
        (30, -150, -150),
        (30, 150, 150),
        (120, -4000, 4000),  # slow sweep through the whole band
        (60, 0, 0),  # settle
    ],
    "charge": [
        (60, 0, 0),
        (120, -1000, -1000),
        (120, -3000, -3000),
        (60, 0, 0),
        (30, -50, -50),
        (30, -150, -150),
        (120, 0, -4000),
        (60, 0, 0),
    ],
    "discharge": [
        (60, 0, 0),
        (120, 1000, 1000),
        (120, 3000, 3000),
        (60, 0, 0),
        (30, 50, 50),
        (30, 150, 150),
        (120, 0, 4000),
        (60, 0, 0),
    ],
}


def pattern(kind: str) -> tuple[list[int], list[tuple[int, int, int]]]:
    """The demand per second plus the segments the report groups by; an appliance profile is grouped per minute."""
    if kind not in PATTERNS:
        # a recorded appliance: the house asks for this, so the fleet has to discharge it
        watts = json.load(open(PROFILES, encoding="utf-8"))[kind]["watts"]
        return watts, [(len(chunk), chunk[0], chunk[-1]) for chunk in (watts[i : i + 60] for i in range(0, len(watts), 60))]

    out = []
    for seconds, start, end in PATTERNS[kind]:
        out += [round(start + (end - start) * i / max(1, seconds - 1)) for i in range(seconds)]
    return out, PATTERNS[kind]


def call(target: dict, path: str, payload: dict | None = None):
    cfg = json.load(open(SECRETS, encoding="utf-8"))
    url = f"{cfg[target['url']].rstrip('/')}/api/{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, headers={"Authorization": f"Bearer {cfg[target['token']]}", "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(request, timeout=10))


def play(name: str, kind: str, out: str) -> None:
    target = TARGETS[name]
    entity, expected = target["mode"]
    if (state := call(target, f"states/{entity}")["state"]) != expected:
        raise SystemExit(f"{entity} is {state}, not {expected}: put the manager in manual first")

    restore = float(call(target, f"states/{target['set']}")["state"])
    values, segments = pattern(kind)
    demanded, achieved, stamps = [], [], []
    print(f"{name} {kind}: {len(values)} seconds, setpoint {target['set']}, restoring to {restore:.0f} W afterwards")

    try:
        for i, watts in enumerate(values):
            tick = time.time()
            call(target, "services/number/set_value", {"entity_id": target["set"], "value": watts})
            fleet = sum(float(call(target, f"states/{e}")["state"]) * (1 if n == 0 else -1) for n, e in enumerate(target["get"]))
            stamps.append(round(tick, 3))
            demanded.append(watts)
            achieved.append(round(fleet))
            if i % 30 == 0:
                print(f"  {i:4d}s  gevraagd {watts:6d} W   geleverd {fleet:7.0f} W")
            time.sleep(max(0.0, 1.0 - (time.time() - tick)))
    finally:
        call(target, "services/number/set_value", {"entity_id": target["set"], "value": restore})

    payload = {"meta": {"target": name, "pattern": kind, "setpoint": target["set"], "seconds": len(values)}, "segments": segments, "t": stamps, "demanded": demanded, "achieved": achieved}
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    print(f"{out}: {len(values)} samples")


def metrics(data: dict) -> list[dict]:
    out, i = [], 0
    for seconds, start, end in data["segments"]:
        demanded = data["demanded"][i : i + seconds]
        achieved = data["achieved"][i : i + seconds]
        band = max(SETTLE_W, abs(demanded[0]) * 0.1)
        settle = next((n for n, value in enumerate(achieved) if abs(value - demanded[n]) <= band), None)
        half = seconds // 2
        error = sum(abs(a - d) for a, d in zip(achieved[half:], demanded[half:])) / max(1, seconds - half)
        out.append({"demand": f"{start}" if start == end else f"{start}..{end}", "seconds": seconds, "settle": settle, "error": error, "peak": max(achieved, key=abs, default=0)})
        i += seconds
    return out


def report(first: str, second: str) -> None:
    runs = [json.load(open(path, encoding="utf-8")) for path in (first, second)]
    names = [run["meta"]["target"] for run in runs]
    print(f"{'segment':>12} {'s':>4} | {names[0]:>10} {'inregel':>8} {'fout':>7} | {names[1]:>10} {'inregel':>8} {'fout':>7}")
    for a, b in zip(*[metrics(run) for run in runs]):
        settles = [f"{m['settle']:>6}s" if m["settle"] is not None else "   nooit" for m in (a, b)]
        print(f"{a['demand']:>12} {a['seconds']:>4} | {a['peak']:>10} {settles[0]} {a['error']:>7.0f} | {b['peak']:>10} {settles[1]} {b['error']:>7.0f}")


if __name__ == "__main__":
    if sys.argv[1] == "play":
        play(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else f"compare-{sys.argv[3]}-{sys.argv[2]}.json")
    else:
        report(sys.argv[2], sys.argv[3])
