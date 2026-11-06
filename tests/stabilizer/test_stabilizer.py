"""Stabilizer draft against real recorder windows: control-free P1 is the pure disturbance."""

import datetime
import json
import math
import os
import sqlite3

import pytest

from stabilizer_draft import Stabilizer

DB = "config/home-assistant_v2.db"
PROFILES = "tests/simulation/real_profiles.json"

pytestmark = pytest.mark.skipif(not os.path.exists(DB), reason="local recorder not present")


def window(y, m, d, h1, m1, h2, m2):
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    a = datetime.datetime(y, m, d, h1, m1).timestamp()
    b = datetime.datetime(y, m, d, h2, m2).timestamp()
    rows = con.execute(
        "SELECT s.last_updated_ts, s.state FROM states s JOIN states_meta m ON m.metadata_id = s.metadata_id"
        " WHERE m.entity_id = 'sensor.meter_p1_61_total_power' AND s.last_updated_ts BETWEEN ? AND ?"
        " AND s.state NOT IN ('unknown','unavailable') ORDER BY s.last_updated_ts", (a, b)).fetchall()
    con.close()
    out = []
    for ts, state in rows:
        try:
            out.append((ts, float(state)))
        except (TypeError, ValueError):
            continue
    assert len(out) > 100, "window has too little recorder data"
    return out


def run(data):
    # ideal actuator: the fleet delivers the accepted target from the next sample on
    stab = Stabilizer()
    output = 0.0
    nets = []
    writes = 0
    prev_t = data[0][0]
    for t, dist in data:
        net = dist - output
        nets.append(net)
        target = stab.process(t, net + output, 0.0, min(3.0, t - prev_t) or 1.0)
        prev_t = t
        if target is not None:
            output = target
            writes += 1
    hours = (data[-1][0] - data[0][0]) / 3600
    rms = math.sqrt(sum(n * n for n in nets) / len(nets))
    return rms, writes / hours, nets


def test_nacht_stil():
    rms, wph, _ = run(window(2026, 8, 15, 1, 0, 3, 0))
    assert rms < 60
    assert wph < 30  # zendure_ha logic produces ~600/h on this same window


def test_stap_echt():
    data = window(2026, 8, 14, 0, 0, 1, 30)
    rms, wph, nets = run(data)
    assert rms < 100
    assert wph < 60
    # the -1338 W flank at ~129 s is corrected within 4 s
    t0 = data[0][0]
    flank = next(i for i, (t, _) in enumerate(data) if t - t0 > 129)
    settle = [abs(n) for (t, _), n in zip(data[flank:], nets[flank:]) if t - data[flank][0] <= 4.0]
    assert min(settle) < 100


def test_zon_wisselend():
    rms, wph, _ = run(window(2026, 8, 14, 8, 15, 11, 10))
    assert rms < 200
    assert wph < 350  # zendure_ha logic produces ~660/h here with a worse rms


def test_wasmachine_cyclus():
    base = window(2026, 8, 15, 1, 0, 3, 0)
    prof = json.load(open(PROFILES, encoding="utf-8"))["wasmachine"]["watts"]
    t0 = base[0][0]
    data = [(t, v + (prof[int(t - t0)] if int(t - t0) < len(prof) else 0)) for t, v in base]
    rms, wph, _ = run(data)
    assert rms < 120
    assert wph < 250
