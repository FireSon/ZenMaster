"""Replay the recorded series through the live Distribution and compare with the meter of that day."""

import asyncio
import json
from pathlib import Path

if __name__ == "__main__":  # run directly for --repin: pytest's conftest has not fabricated the HA modules yet
    import importlib.util
    import sys

    sys.path[:0] = [str(Path(__file__).resolve().parents[2]), str(Path(__file__).parent)]
    # load the suite's conftest as `conftest`, so replay.py finds it and the HA modules are fabricated once
    _spec = importlib.util.spec_from_file_location("conftest", Path(__file__).resolve().parents[1] / "conftest.py")
    sys.modules["conftest"] = _module = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_module)

import pytest

from custom_components.zenmaster.distribution import DistributionModi
from replay import replay

# per series the current control's result, measured 2026-09-22 under the device model measured on the fleet
# (setpoint in after 1.8 s, 200 W/s, 8.4 s from standstill): one-sided, worse on any measure fails; after a
# deliberate improvement re-pin the numbers.
# entries with a soc are control-off scenario windows (bare unpolluted P1: two silent-fleet nights and the 08-13
# cooking hour with only a slow drain left) where every pack starts at that level.
BENCHMARKS = {
    "em-0824-clean.json": {"total_wh": 1141, "median": 90, "p90": 1108, "within100": 52.6},
    "local-0813-cook.json": {"soc": 50.0, "total_wh": 72, "median": 11, "p90": 158, "within100": 83.7},
    "local-0813.json": {"total_wh": 731, "median": 37, "p90": 397, "within100": 65.7},
    "local-0814-night.json": {"soc": 50.0, "total_wh": 23, "median": 5, "p90": 16, "within100": 98.5},
    "local-0815-night.json": {"soc": 50.0, "total_wh": 14, "median": 4, "p90": 11, "within100": 99.3},
    "prod-0807.json": {"total_wh": 414, "median": 27, "p90": 195, "within100": 80.4},
    "prod-0808.json": {"total_wh": 149, "median": 24, "p90": 108, "within100": 88.3},
    "prod-0809.json": {"total_wh": 8867, "median": 850, "p90": 7350, "within100": 28.6},
    "prod-0810.json": {"total_wh": 2247, "median": 48, "p90": 1509, "within100": 62.1},
}


def run(name: str, soc: float | None = None) -> dict:
    data = json.loads((Path(__file__).parent / name).read_text(encoding="utf-8"))
    if soc is not None:  # every pack starts at this level; a device without a pack keeps its 0
        kwh = {spec["key"]: spec["kwh"] for spec in data["devices"]}
        for key, series in data["soc"].items():
            if kwh[key]:
                series[0] = soc
    return asyncio.run(replay(data, next(key for key, value in DistributionModi.items() if value == "smart")))


@pytest.mark.parametrize("name", sorted(BENCHMARKS))
def test_replay_benchmark(name: str) -> None:
    bench = BENCHMARKS[name]
    result = run(name, soc=bench.get("soc"))
    assert result["hours"] > 0, f"{name} replayed nothing"
    assert result["total_wh"] <= bench["total_wh"] * 1.01 + 1, f"{name}: {result['total_wh']:.0f} Wh against {bench['total_wh']} Wh"
    assert result["median"] <= bench["median"] * 1.01 + 1, f"{name}: median {result['median']:.0f} W against {bench['median']} W"
    assert result["p90"] <= bench["p90"] * 1.01 + 1, f"{name}: p90 {result['p90']:.0f} W against {bench['p90']} W"
    assert result["within"][100] >= bench["within100"] - 1.0, f"{name}: within 100 W {result['within'][100]:.1f}% against {bench['within100']}%"
    # a result far better than the pin means the pin is stale: leave it and a later regression slips under it
    assert result["total_wh"] >= bench["total_wh"] * 0.95 - 1, f"{name}: {result['total_wh']:.0f} Wh well under the pinned {bench['total_wh']} Wh; re-pin with: python tests/replay/test_replay.py --repin"


def test_replay_beats_the_previous_control() -> None:
    # the morning of 08-24, when the previous control (damping 0.7/0.3, zone 0.85, no quiet window) really steered and
    # its meter made 1636 Wh. The replayed side carries the 5 s report smearing, so passing is conservative. The old
    # comparison against the recorded cloud days was dropped: there the smearing corrupts the whole reconstruction.
    result = run("em-0824-clean.json")
    assert result["total_wh"] <= result["recorded_wh"], f"{result['total_wh']:.0f} Wh against the recorded {result['recorded_wh']:.0f} Wh"


def test_replay_holds_every_phase_under_its_limit() -> None:
    # 18-09 11:50-12:35Z: the plan charges 9.75 kW out of the grid with four packs on L1, and L1 sat over 17 kW / 3 for 2146 of the
    # 2638 s recorded (peak 7310 W). The replay feeds the meter per phase and the recorded manual setpoint. Measured 2026-09-22 under
    # the device model measured on the fleet: 68 s over in total, peak 7715 W; the same series without the guard makes 2140 s.
    result = run("em-0918-phase.json")
    assert result["recorded_over_s"] > 2000, "the recording is supposed to be the incident"
    assert result["over_s"] <= 120, f"a phase sat over the limit for {result['over_s']:.0f} s (recorded {result['recorded_over_s']:.0f} s), peak {result['phase_peak']:.0f} W"


def test_a_tick_costs_under_five_milliseconds() -> None:
    # the control runs once per meter reading, 1 Hz. Measured 2026-09-21 in the harness (entities detached, so without HA's state
    # writes and the MQTT publish per command): median 0.03 ms, p99 0.1 ms, max 1.3 ms over 11676 ticks. A loop per device per tick shows up here.
    result = run("em-0824-clean.json")
    assert result["tick_ms"]["p99"] <= 5, f"p99 {result['tick_ms']['p99']:.2f} ms per tick (median {result['tick_ms']['median']:.3f}, max {result['tick_ms']['max']:.2f})"


def _repin() -> None:
    """python tests/replay/test_replay.py --repin: measure every series and write the numbers back."""
    lines = []
    for name in sorted(BENCHMARKS):
        soc = BENCHMARKS[name].get("soc")
        r = run(name, soc=soc)
        pre = f'"soc": {soc}, ' if soc is not None else ""
        lines.append(f'    "{name}": {{{pre}"total_wh": {r["total_wh"]:.0f}, "median": {r["median"]:.0f}, "p90": {r["p90"]:.0f}, "within100": {r["within"][100]:.1f}}},')
        print(lines[-1])
    src = Path(__file__).read_text(encoding="utf-8")
    head, rest = src.split("BENCHMARKS = {", 1)
    nl = chr(10)
    src = head + "BENCHMARKS = {" + nl + nl.join(lines) + nl + "}" + rest.split(nl + "}", 1)[1]
    Path(__file__).write_text(src, encoding="utf-8", newline="")
    print("BENCHMARKS bijgewerkt")


if __name__ == "__main__":
    import sys

    if "--repin" in sys.argv:
        _repin()
