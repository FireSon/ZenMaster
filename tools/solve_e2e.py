"""Read-only end-to-end run of the ZenMaster LP against live Home Assistant data.

Builds the same entry set the coordinator builds (prices, forecast, baseload, distribution, heating,
ev, appliances) plus a seeded battery, wires a fake hass whose recorder is backed by the HA WebSocket
statistics API, runs solver.run() twice (cold tick schedules calibration, warm tick uses it), and prints
the resulting 96-slot plan with invariant checks and totals. Nothing is written back to HA.
"""
from __future__ import annotations

import asyncio
import importlib.abc
import importlib.machinery
import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SECRETS = json.loads((ROOT / "secrets.json").read_text(encoding="utf-8"))
HA = SECRETS["LOCAL_URL"]
TOKEN = SECRETS["LOCAL_TOKEN"]
WS = HA.replace("http", "ws", 1) + "/api/websocket"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
LOCAL_TZ = ZoneInfo("Europe/Amsterdam")  # replaced from /api/config at startup


# --- fabricate homeassistant.* modules so the integration imports without HA installed (same shim as tests/conftest) ---
def _instance_getattr(self, name):
    if name.startswith("__"):
        raise AttributeError(name)
    if name.startswith("_attr_"):
        return None  # real HA base classes define `_attr_*` defaults as None
    return _AnyMeta(name, (), {"__getattr__": _instance_getattr})  # chainable: dummy().method().attr keeps working


class _AnyMeta(type):
    def __getattr__(cls, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _AnyMeta(name, (), {"__getattr__": _instance_getattr})

    def __call__(cls, *args, **kwargs):
        # dummies accept any constructor args (Store, DeviceInfo, ...); kwargs become attrs
        try:
            return super().__call__(*args, **kwargs)
        except TypeError:
            obj = cls.__new__(cls)
            for k, v in kwargs.items():
                setattr(obj, k, v)
            return obj


class _FakeModule(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        obj = _AnyMeta(name, (), {"__getattr__": _instance_getattr})
        setattr(self, name, obj)
        return obj


class _Finder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname, path, target=None):
        if fullname.split(".")[0] == "homeassistant":
            return importlib.machinery.ModuleSpec(fullname, self)
        return None

    def create_module(self, spec):
        module = _FakeModule(spec.name)
        module.__path__ = []
        return module

    def exec_module(self, module):
        pass


sys.meta_path.insert(0, _Finder())

import aiohttp  # noqa: E402
import numpy as np  # noqa: E402


# --- recorder shim: HA WebSocket statistics, returned in the shape the in-process API yields ---
async def _ws_stats(hass, start, end, statistic_ids, period, units=None, types_=None) -> dict:
    # statistics_during_period(hass, start, end, ids, period, units, types) -> {id: [{start: seconds, mean}]}
    req = {
        "id": 2, "type": "recorder/statistics_during_period",
        "start_time": _iso(start), "end_time": _iso(end),
        "statistic_ids": list(statistic_ids), "period": period, "types": ["mean"],
    }
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(WS) as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": TOKEN})
            await ws.receive_json()
            await ws.send_json(req)
            while True:
                msg = await ws.receive_json()
                if msg.get("id") == 2:
                    break
    out = {}
    for sid, rows in (msg.get("result") or {}).items():
        out[sid] = [{"start": r["start"] / 1000.0, "mean": r.get("mean")} for r in rows]  # WS ms -> seconds
    return out


class _Recorder:
    async def async_add_executor_job(self, func, *args):
        r = func(*args)
        return await r if asyncio.iscoroutine(r) else r


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.isoformat()


import homeassistant.components.recorder as _rec  # noqa: E402
import homeassistant.components.recorder.statistics as _recstats  # noqa: E402
import homeassistant.helpers.entity_registry as _er  # noqa: E402
import homeassistant.util.dt as _hadt  # noqa: E402

_rec.get_instance = lambda hass: _Recorder()
_recstats.statistics_during_period = _ws_stats
_er.async_entries_for_device = lambda *a, **k: []
_hadt.as_local = lambda dt: (dt.replace(tzinfo=LOCAL_TZ) if dt.tzinfo is None else dt).astimezone(LOCAL_TZ)
_hadt.utcnow = lambda: datetime.now(timezone.utc)
_hadt.now = lambda: datetime.now(LOCAL_TZ)
_hadt.utc_from_timestamp = lambda s: datetime.fromtimestamp(s, timezone.utc)

async def _anull(*a, **k):
    return None


# EmDevice + Store/load_params use Store(...) and DeviceInfo(...); patch the stub source modules
# (solver.py imports Store locally at call time, so patching only entity.py is not enough)
class _DummyStore:
    def __init__(self, *a, **k):
        pass

    async def async_load(self):
        return None

    async def async_save(self, *a, **k):
        return None


import homeassistant.helpers.storage as _store_mod  # noqa: E402
import homeassistant.helpers.device_registry as _dr  # noqa: E402

_store_mod.Store = _DummyStore
_dr.DeviceInfo = dict


from custom_components.zenmaster.config import Config  # noqa: E402
from custom_components.zenmaster.const import Phase  # noqa: E402
from custom_components.zenmaster.coordinator import SUBENTRY_TYPES  # noqa: E402

CONSUMER_KINDS = {cls.__name__: cls for cls in SUBENTRY_TYPES.values()}  # this tool's payload export keys consumers on the class name
from custom_components.zenmaster.device import DeviceState  # noqa: E402
from custom_components.zenmaster.distribution import Distribution  # noqa: E402
from custom_components.zenmaster.solver.solver import LpSolver, SolverEntry  # noqa: E402
from custom_components.zenmaster.solver.forecast import Forecast  # noqa: E402
from custom_components.zenmaster.solver.baseload import BaseLoad  # noqa: E402
from custom_components.zenmaster.solver.dynamicprice import DynamicPrice  # noqa: E402
from custom_components.zenmaster.solver.consumers import ev as _ev  # noqa: E402
from custom_components.zenmaster.sensor import EmSensor  # noqa: E402
from custom_components.zenmaster.number import EmNumber  # noqa: E402
from custom_components.zenmaster.select import EmSelect  # noqa: E402
from custom_components.zenmaster.binary_sensor import EmBinarySensor  # noqa: E402
from custom_components.zenmaster.switch import EmSwitch  # noqa: E402

for _cls in (EmSensor, EmNumber, EmSelect, EmBinarySensor, EmSwitch):  # no HA platform in the harness -> entity registration is a no-op
    _cls.add = staticmethod(lambda *a, **k: None)

_ev._SOLAR_PRICE_FRACTION = 0.33  # TEST: solar-laaddrempel = 0.33 × compensatie

Distribution.entity_create = lambda self: None  # skip HA-entity creation we don't use in the solve path
Distribution.entry_postrun = lambda self, lp: None  # skip live per-device dispatch; we only analyse the LP plan


async def ha_state(session: aiohttp.ClientSession, entity: str) -> str:
    async with session.get(f"{HA}/api/states/{entity}", headers=HEADERS) as r:
        return (await r.json()).get("state", "")


async def ha_config(session: aiohttp.ClientSession) -> dict:
    async with session.get(f"{HA}/api/config", headers=HEADERS) as r:
        return await r.json()


def make_hass(cfg: dict, bg: list):
    h = types.SimpleNamespace()
    h.config = types.SimpleNamespace(
        country=cfg.get("country") or "NL",
        latitude=cfg.get("latitude", 0.0),
        longitude=cfg.get("longitude", 0.0),
        time_zone=cfg.get("time_zone") or "Europe/Amsterdam",
    )
    h.async_create_task = lambda coro, *a, **k: bg.append(asyncio.ensure_future(coro)) or bg[-1]
    h.services = types.SimpleNamespace(async_call=_anull)
    h.loop = asyncio.get_event_loop()
    return h


async def seed_battery(session: aiohttp.ClientSession) -> tuple[Any, float, float] | None:
    # one aggregate fleet battery from our own integration's sensors: capacity + live available
    try:
        total = float(await ha_state(session, "sensor.zenmaster_capacity_kwh"))
        avail = float(await ha_state(session, "sensor.zenmaster_available_kwh"))
    except (ValueError, TypeError):
        return None
    if total <= 0:
        return None
    dev = types.SimpleNamespace(kwh=total, soc=avail / total * 100.0, soc_min=0.0, soc_max=100.0, state=DeviceState.ACTIVE, power_profile=[types.SimpleNamespace(power=12000), types.SimpleNamespace(power=-12000)])  # ~fleet max
    return dev, total, avail


def col(x: np.ndarray, c, h: int = 96) -> np.ndarray:
    return x[c.index:c.index + h] if c is not None else np.zeros(h)


async def main() -> None:
    global LOCAL_TZ
    bg: list = []
    tracks: list = []  # (entity_id, callback, raw_string)

    def track(eid: str, cb) -> None:
        if eid:
            tracks.append((eid, cb, False))

    def state(eid: str, cb) -> None:
        if eid:
            tracks.append((eid, cb, True))

    async with aiohttp.ClientSession() as session:
        hacfg = await ha_config(session)
        LOCAL_TZ = ZoneInfo(hacfg.get("time_zone") or "Europe/Amsterdam")
        hass = make_hass(hacfg, bg)

        store = json.loads((ROOT / "config" / ".storage" / "zenmaster.devices").read_text(encoding="utf-8"))
        cfg = Config()
        cfg.payload = store.get("data") or {}

        lp = LpSolver(track, state)
        lp.hass = hass

        prices = DynamicPrice()
        cfg.deserialize(prices, DynamicPrice.CONFIG)
        prices.hass = hass
        baseload = BaseLoad()
        distribution = Distribution(hass, "", [])
        cfg.deserialize(distribution, Distribution.CONFIG)
        forecast = Forecast(cfg.payload, hass)

        consumers: list[SolverEntry] = []
        for item in cfg.payload.get("consumers") or []:
            if (cls := CONSUMER_KINDS.get(item.get("type"))) is None:
                continue
            c = cls()
            for f in cls.CONFIG:
                if f.cast is not None and f.id in item:
                    setattr(c, f.id, f.cast(item[f.id]))
            c.hass = hass
            await c.restore(hass)
            consumers.append(c)
        for e in consumers:
            if type(e).__name__ == "ElectricVehicle":
                e.phase = Phase.all  # TEST: 3-phase charger (4.2 kW = total over 3 phases)

        battery = await seed_battery(session)
        if battery is not None:
            dev, total, avail = battery
            distribution.devices.append(dev)
            distribution.capacity = total

        entries = [prices, forecast, baseload, distribution, *consumers]
        lp.init(entries)

        # push live "now" values through the registered track/state callbacks (heating temp pin, etc.)
        for eid, cb, raw in tracks:
            st = await ha_state(session, eid)
            if raw:
                cb(st)
            else:
                try:
                    cb(float(st))
                except (ValueError, TypeError):
                    pass

        now = datetime.now()
        await lp.run(now)                                   # cold tick: schedules calibrations + loads history
        if bg:
            await asyncio.gather(*bg, return_exceptions=True)  # let POA / appliance-profile calibration finish
        x = await lp.run(now)                               # warm tick: calibrated POA + profiles applied

    report(lp, distribution, forecast, consumers, x, now, battery)


def report(lp, distribution, forecast, consumers, x, now, battery) -> None:
    h = lp.horizon
    end = 12 + h
    price_buy = lp.price_buy.data[12:end]
    price_sell = lp.price_sell.data[12:end]
    solar = lp.solar.data[12:end]
    l1, l2, l3 = lp.l1, lp.l2, lp.l3
    load = l1 + l2 + l3
    heat = next((c for c in consumers if type(c).__name__ == "Heating"), None)
    limit = lp.limit

    # --- data-sourcing diagnostics ---
    print("=" * 78)
    print(f"E2E LP run  {now:%a %d-%m %H:%M}  (warm tick)")
    print("-" * 78)
    print(f"prijs koop {price_buy.min():.3f}-{price_buy.max():.3f}  verkoop {price_sell.min():.3f}-{price_sell.max():.3f} eur/kWh"
          f"   ({'leeg!' if not price_buy.any() else 'ok'})")
    print(f"solar POA gekalibreerd voor {len(forecast._poa)} meter(s)   piek {solar.max():.2f} kW   som {solar.sum() * 0.25:.1f} kWh")
    print(f"achtergrondlast (kWh/24h)  L1 {l1.sum() * 0.25:.1f}  L2 {l2.sum() * 0.25:.1f}  L3 {l3.sum() * 0.25:.1f}"
          f"   ({'GEEN historie!' if not load.any() else 'ok'})")
    if heat is not None:
        print(f"heating '{heat.name}' fase {heat.phase}  temp-pin {heat.temp.lower[0]:.1f}C  slope {heat.slope:.3f} tau {heat.tau:.0f}  max {heat.max_power} kW")
    if battery is not None:
        total, avail = battery[1], battery[2]
        tail = float(np.mean(lp.price_sell.data[end : end + 16]))
        print(f"batterij fleet  cap {total:.1f} kWh  SoC0 {avail / total * 100:.0f}%  eindwaarde {tail:.3f} eur/kWh (verkoopprijs na horizon)")
    else:
        print("batterij: niet geseed (geen live zenmaster capacity/available gevonden)")
    ev = next((c for c in consumers if type(c).__name__ == "ElectricVehicle"), None)
    if ev is not None:
        evp = ev.ev_power.data[12:end]
        drempel = ev.charge_compensation_eur_kwh * _ev._SOLAR_PRICE_FRACTION
        cheap = (price_sell < drempel)
        chargeable = cheap & (solar >= 3.8)  # cheap AND enough solar to reach the 4.2 kW charger minimum
        now_slot = (now.hour * 60 + now.minute) // 15
        try:
            dh, dm = ev.departure_time.split(":"); sud = (int(dh) * 4 + int(dm) // 15 - now_slot) % 96 or 96
        except (ValueError, AttributeError):
            sud = 96
        print(f"EV '{ev.id}' mode {ev.mode.name} connected {ev.connected}  laadt {evp.sum() * 0.25:.1f} kWh in {int((evp > 0.01).sum())} slots"
              f"   vertrek over {sud} slots; cheap+zon>=3.8kW totaal {int(chargeable.sum())}, binnen vertrekvenster {int(chargeable[:sud].sum())}")

    # --- feasibility diagnostics (run regardless of solve status) ---
    print("-" * 78)
    print(f"fase-limiet {limit:.3f} kW   max last  L1 {l1.max():.2f}  L2 {l2.max():.2f}  L3 {l3.max():.2f}"
          f"   overschrijdingen L1/L2/L3: {int((l1 > limit + 1e-6).sum())}/{int((l2 > limit + 1e-6).sum())}/{int((l3 > limit + 1e-6).sum())}")
    if heat is not None and heat.kind.value == "house":
        outdoor = lp.temp.data[12:end]
        A = 0.25 / (heat.tau * heat.slope)
        B = 0.25 / heat.tau
        G = heat.icept * A
        free = np.zeros(h)              # power=0 trajectory = the coolest the house can get (no cooling path)
        free[0] = heat.temp.lower[0]
        for t in range(h - 1):
            free[t + 1] = (1 - B) * free[t] + B * outdoor[t] + G
        ceiling = "geen (house, geen plafond)" if heat.kind.value == "house" else f"{heat.t_max}C"
        print(f"heating buitentemp {outdoor.min():.1f}-{outdoor.max():.1f}C   vrije baan (P=0) {free.min():.1f}-{free.max():.1f}C   bovengrens {ceiling}")

    if x is None:
        print("-" * 78)
        print("LP solve NIET optimaal (kInfeasible). Plan/totalen overgeslagen; zie diagnostiek hierboven.")
        return

    x = np.asarray(x, float)
    grid_buy = col(x, distribution.grid_buy, h)
    grid_sell = col(x, distribution.grid_sell, h)
    solar_used = col(x, forecast.solar_lp, h)
    heat_kw = col(x, heat.power, h) if heat else np.zeros(h)
    heat_t = col(x, heat.temp, h) if heat else np.zeros(h)
    bat_ch = col(x, distribution._bat_charge, h)
    bat_dis = col(x, distribution._bat_discharge, h)
    bat_soc = col(x, distribution._bat_soc, h)

    # --- invariants ---
    supply = grid_buy + solar_used + bat_dis
    draw = grid_sell + bat_ch + heat_kw
    residual = supply - draw - load
    limit = lp.limit
    over_l1 = max(0.0, float((l1 + heat_kw * (1 if (heat and heat.phase == Phase.l1) else 0) - limit).max()))
    print("-" * 78)
    print(f"balans-residu max |supply-draw-load|  {np.abs(residual).max():.4f} kW   (~0 = ok)")
    print(f"fase-limiet {limit:.2f} kW   grootste L1-overschrijding {over_l1:.3f} kW")

    # --- totals ---
    cost = float((grid_buy * price_buy - grid_sell * price_sell).sum() * 0.25)
    baseline = float((np.maximum(0.0, load) * price_buy).sum() * 0.25)  # naive: buy the whole load, no solar/battery
    print(f"kosten geoptimaliseerd {h // 4}u  {cost:+.2f} eur     naive-baseline (alles kopen) {baseline:+.2f} eur     verschil {baseline - cost:+.2f} eur")
    print(f"piek import {grid_buy.max():.2f} kW   piek export {grid_sell.max():.2f} kW")

    # --- per-slot plan ---
    print("-" * 78)
    print(f"{'tijd':>5} {'prijs':>6} {'last':>6} {'koop':>6} {'verk':>6} {'zon':>5} {'b.laad':>6} {'b.ont':>6} {'soc':>5} {'heat':>5} {'binT':>5}")
    for t in range(h):
        ts = (now + timedelta(minutes=15 * t)).strftime("%H:%M")
        print(f"{ts:>5} {price_buy[t]:6.3f} {load[t]:6.2f} {grid_buy[t]:6.2f} {grid_sell[t]:6.2f} {solar_used[t]:5.2f}"
              f" {bat_ch[t]:6.2f} {bat_dis[t]:6.2f} {bat_soc[t] * 100:5.0f} {heat_kw[t]:5.2f} {heat_t[t]:5.1f}")


if __name__ == "__main__":
    asyncio.run(main())
