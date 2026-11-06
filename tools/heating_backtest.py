"""Read-only backtest of the space-heating heat pump against the last week on production.

Fetches the MHI power and capacity, the room and outdoor temperature and the buy price per quarter, fits the house
model of heating.py on that week, checks it by simulating the room from the real power, and replays ZenMaster
quarter by quarter: each quarter the real HeatingHeatPump plans 48 hours ahead and its first quarter heats the model
house and becomes a capacity. Nothing is written back to HA. Optional argument: a CSV path for the per-quarter data.
"""
from __future__ import annotations

import asyncio
import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(ROOT / "tests" / "solver")]
import conftest  # noqa: E402,F401  the homeassistant shim of the tests, so the integration imports without HA
import numpy as np  # noqa: E402
import websockets  # noqa: E402
from test_heating_scenarios import _Grid, _Prices  # noqa: E402

from custom_components.zenmaster.const import Phase  # noqa: E402
from custom_components.zenmaster.solver.heating import HeatingHeatPump, _cop  # noqa: E402
from custom_components.zenmaster.solver.solver import LpSolver  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
SECRETS = json.loads((ROOT / "secrets.json").read_text(encoding="utf-8"))
TZ = ZoneInfo("Europe/Amsterdam")
POWER, CAPACITY, ROOM, OUTDOOR, PRICE = "sensor.fs_evcharger_sdm220m_total_power","sensor.fs_ahu_mhi_capacity_modbus", "sensor.fs_ahu_verventa_temperature", "sensor.fs_ahu_mhi_outside_temperature", "sensor.zenmaster_price_buy"
IDS = [POWER, CAPACITY, ROOM, OUTDOOR, PRICE]
DAYS, H = 7, 192  # the week under test, and the solver horizon in quarters
GAP_S = 1800  # a quarter without any update in the half hour before its end lies in a recorder gap (HA down)
CAP_MIN = 6.0  # below this capacity the MHI stops (the 600 in ahu.cpp)


async def fetch(start: datetime, end: datetime) -> tuple[dict[str, list[tuple[float, float]]], np.ndarray]:
    # raw states per entity as (epoch seconds, value), plus the time of every update, text states included, for the gap check
    url = SECRETS["HA_URL"].rstrip("/").replace("http", "ws", 1) + "/api/websocket"
    async with websockets.connect(url, max_size=None) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "auth", "access_token": SECRETS["HA_TOKEN"]}))
        assert json.loads(await ws.recv())["type"] == "auth_ok"
        await ws.send(json.dumps({"id": 1, "type": "history/history_during_period", "start_time": start.isoformat(), "end_time": end.isoformat(), "entity_ids": IDS, "minimal_response": True, "no_attributes": True}))
        result = json.loads(await ws.recv())["result"]
    series, stamps = {}, []
    for eid in IDS:
        series[eid] = []
        for r in result.get(eid, []):
            stamps.append(ts := float(r.get("lu", r.get("lc", 0.0))))
            try:
                series[eid].append((ts, float(r["s"])))
            except (TypeError, ValueError):
                continue
    return series, np.sort(np.array(stamps))


def quarters(series: dict, stamps: np.ndarray, t0: float, n: int) -> tuple[dict[str, np.ndarray], np.ndarray]:
    # step-hold every series on a minute grid and average it per quarter; a quarter in a recorder gap is invalid
    minutes = t0 + 60.0 * np.arange(n * 15)
    q = {}
    for eid, rows in series.items():
        ts, vals = np.array([r[0] for r in rows]), np.array([r[1] for r in rows])
        idx = np.searchsorted(ts, minutes, side="right") - 1
        held = np.where(idx >= 0, vals[np.clip(idx, 0, None)], np.nan) if rows else np.full(len(minutes), np.nan)
        q[eid] = held.reshape(n, 15).mean(axis=1)
    q[POWER] = -q[POWER] / 1000.0  # W -> kW, the unit of the LP power column; the SDM220 counts the MHI negative
    ends = t0 + 900.0 * (np.arange(n) + 1)
    last = np.searchsorted(stamps, ends, side="right") - 1
    valid = (last >= 0) & (ends - stamps[np.clip(last, 0, None)] <= GAP_S) & np.isfinite(q[ROOM]) & np.isfinite(q[OUTDOOR])
    return q, valid


def rel_cop(outdoor: np.ndarray, room: np.ndarray) -> np.ndarray:
    # the COP of heating.py per quarter, relative to its mean over the series
    cops = np.array([_cop(o, r) for o, r in zip(np.nan_to_num(outdoor), np.nan_to_num(room, nan=20.0))])
    return cops / cops.mean()


def fit(q: dict, valid: np.ndarray) -> tuple[float, float, float, float, int]:
    # one-step least squares on the RC row of heating.py: T[t+1] - T[t] = B*(Tout - T) + A*copf*P + G, over consecutive valid quarters
    t, out, p = q[ROOM], q[OUTDOOR], np.nan_to_num(q[POWER])
    ok = valid[:-1] & valid[1:]
    x = np.c_[(out - t)[:-1], (rel_cop(out, t) * p)[:-1], np.ones(len(t) - 1)][ok]
    y = np.diff(t)[ok]
    coef = np.linalg.lstsq(x, y, rcond=None)[0]
    r2 = 1.0 - np.sum((y - x @ coef) ** 2) / np.sum((y - y.mean()) ** 2)
    return float(coef[0]), float(coef[1]), float(coef[2]), float(r2), int(ok.sum())


def simulate(q: dict, valid: np.ndarray, b: float, a: float, g: float, anchor: int = 0) -> np.ndarray:
    # the room the model predicts from the real power; back on the measured room after a gap and, with anchor, every anchor quarters
    t, out, p = q[ROOM], q[OUTDOOR], np.nan_to_num(q[POWER])
    copf = rel_cop(out, t)
    sim = np.full(len(t), np.nan)
    for i in np.flatnonzero(valid):
        free = i == 0 or not valid[i - 1] or (anchor and i % anchor == 0)
        sim[i] = t[i] if free else (1 - b) * sim[i - 1] + b * out[i - 1] + a * copf[i - 1] * p[i - 1] + g
    return sim


async def replay(q: dict, valid: np.ndarray, starts: np.ndarray, b: float, a: float, g: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    # every quarter the real HeatingHeatPump plans 48 hours on the real prices and outdoor; its first quarter heats the model house
    n = len(starts) - H
    price, out = np.nan_to_num(q[PRICE]), np.nan_to_num(q[OUTDOOR])
    copf = rel_cop(out, q[ROOM])
    house = HeatingHeatPump()
    house.id = house.name = "house"
    house.phase, house.temp_entity = Phase.all, ""
    house.tau, house.slope, house.icept = 0.25 / b, b / a, g / a  # A = 0.25/(tau*slope), B = 0.25/tau, G = icept*A
    grid, prices = _Grid([0.0] * H), _Prices([0.0] * H)
    lp = LpSolver(track=lambda *args, **kw: None, state=lambda *args, **kw: None, horizon=H)
    lp.hass = None
    lp.init([prices, grid, house])

    # the model house starts on the measured room and goes back to it after a gap, because the week before is unknown
    plan, temp, floor = np.full(n, np.nan), np.full(n, np.nan), np.full(n, np.nan)
    for i in range(n):
        if not valid[i]:
            continue
        temp[i] = q[ROOM][i] if i == 0 or not valid[i - 1] or np.isnan(temp[i - 1]) else (1 - b) * temp[i - 1] + b * out[i - 1] + a * copf[i - 1] * plan[i - 1] + g
        grid.price, prices.buy, prices.outside = list(price[i:i + H] * 0.25), list(price[i:i + H]), list(out[i:i + H])
        house._temp0 = temp[i]
        x = await lp.run(datetime.fromtimestamp(starts[i], TZ))
        plan[i] = house.power.dest.data[12] if x is not None else 0.0
        floor[i] = house._tmin[0]
    return plan, temp, floor


def capacity_map(q: dict, valid: np.ndarray) -> tuple[float, float, float]:
    # kW per capacity percent from the quarters the MHI ran: P = k*cap + c
    cap, p = q[CAPACITY], q[POWER]
    on = valid & np.isfinite(cap) & np.isfinite(p) & (cap >= CAP_MIN) & (p > 0.05)
    k, c = np.polyfit(cap[on], p[on], 1)
    r2 = 1.0 - np.sum((p[on] - (k * cap[on] + c)) ** 2) / np.sum((p[on] - p[on].mean()) ** 2)
    return float(k), float(c), float(r2)


def report(q, valid, starts, model, sim_open, sim_day, cmap, plan, temp, floor) -> None:
    # the fit, the model check and per day the real heat pump against the replayed ZenMaster
    b, a, g, r2, nfit = model
    k, c, cr2 = cmap
    room = q[ROOM]
    print(f"data: {int(valid.sum())} van {len(valid)} kwartieren geldig ({(len(valid) - int(valid.sum())) / 4:.1f} uur in gaten)")
    print(f"fit ({nfit} kwartierstappen, R2 {r2:.3f}): tau {0.25 / b:.1f} h  slope {b / a:.3f} kW/C  icept {g / a:.3f} kW")
    for name, sim in (("open lus", sim_open), ("dagelijks herankerd", sim_day)):
        err = (sim - room)[np.isfinite(sim)]
        print(f"modeltoets {name}: RMSE {np.sqrt(np.mean(err ** 2)):.2f} C  max {np.abs(err).max():.2f} C")
    print(f"capaciteit: {k:.4f} kW per % + {c:.3f} kW  (R2 {cr2:.3f})")

    # per local day: energy, cost at the real price, mean capacity while running, comfort against the floor
    n = len(plan)
    cap_em = np.where(plan > k * CAP_MIN + c, np.clip((plan - c) / k, 0, 100), 0.0)
    real_p, price, cap = np.nan_to_num(q[POWER][:n]), np.nan_to_num(q[PRICE][:n]), np.nan_to_num(q[CAPACITY][:n])
    days = np.array([datetime.fromtimestamp(s, TZ).date() for s in starts[:n]])
    ok = np.isfinite(plan)
    short_r, short_e = np.maximum(0.0, floor - room[:n]), np.maximum(0.0, floor - temp)  # degC under the floor, the measure the LP penalises
    print(f"{'dag':>10} {'kWh echt':>8} {'kWh EM':>7} {'EUR echt':>8} {'EUR EM':>7} {'cap echt':>8} {'cap EM':>7} {'Ckw<vloer echt':>14} {'Ckw<vloer EM':>12} {'kamer echt':>11} {'kamer EM':>11}")
    for d in sorted(set(days[ok])) + ["totaal"]:
        m = ok & ((days == d) if d != "totaal" else True)
        run_r, run_e = m & (cap >= CAP_MIN), m & (cap_em > 0)
        print(f"{str(d):>10} {real_p[m].sum() * 0.25:8.2f} {plan[m].sum() * 0.25:7.2f} {(real_p * price)[m].sum() * 0.25:8.2f} {(plan * price)[m].sum() * 0.25:7.2f}"
              f" {cap[run_r].mean() if run_r.any() else 0:8.1f} {cap_em[run_e].mean() if run_e.any() else 0:7.1f}"
              f" {np.nansum(short_r[m]):14.1f} {np.nansum(short_e[m]):12.1f}"
              f" {np.nanmin(room[:n][m]):5.1f}-{np.nanmax(room[:n][m]):<5.1f} {np.nanmin(temp[m]):5.1f}-{np.nanmax(temp[m]):<5.1f}")

    if len(sys.argv) > 1:
        with open(sys.argv[1], "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["tijd", "geldig", "prijs", "buiten", "kamer", "kamer_model", "vermogen", "capaciteit", "plan_kw", "cap_em", "kamer_em", "vloer"])
            for i in range(n):
                w.writerow([datetime.fromtimestamp(starts[i], TZ).isoformat(), int(valid[i]), price[i], q[OUTDOOR][i], room[i], sim_open[i], real_p[i], cap[i], plan[i], cap_em[i], temp[i], floor[i]])


async def main() -> None:
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    end -= timedelta(minutes=end.minute % 15)
    start = end - timedelta(days=DAYS)
    n = DAYS * 96
    series, stamps = await fetch(start, end)
    q, valid = quarters(series, stamps, start.timestamp(), n)
    starts = start.timestamp() + 900.0 * np.arange(n)

    # the house model fitted on this week, checked against the measured room, then ZenMaster replayed on it
    model = fit(q, valid)
    b, a, g = model[:3]
    if b <= 0 or a <= 0:
        print(f"fit onbruikbaar: B {b:.5f} A {a:.5f} (beide moeten positief zijn)")
        return
    sim_open, sim_day = simulate(q, valid, b, a, g), simulate(q, valid, b, a, g, anchor=96)
    plan, temp, floor = await replay(q, valid, starts, b, a, g)
    report(q, valid, starts, model, sim_open, sim_day, capacity_map(q, valid), plan, temp, floor)


if __name__ == "__main__":
    asyncio.run(main())
