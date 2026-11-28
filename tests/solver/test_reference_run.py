"""The touchstone of the playground work: the plan and everything sent outside for a fixed input, on the real entry classes.

Every later step must give exactly the same plan and the same outside calls. Write the reference once with
ZM_WRITE_REFERENCE=1; after that the test compares.
"""

import asyncio
import json
import os
import time
import types
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

import numpy as np
import pytest

from custom_components.zenmaster.const import Phase
from custom_components.zenmaster.device import DeviceState
from custom_components.zenmaster.entity import EntityDevice
from custom_components.zenmaster.solver.battery import DistributionEntry
from custom_components.zenmaster.solver.boiler import BoilerElectric, BoilerHeatPump
from custom_components.zenmaster.solver.consumers.appliance import Appliance
from custom_components.zenmaster.solver.consumers.ev import ElectricVehicle
from custom_components.zenmaster.solver.heating import HeatingHeatPump, PoolHeatPump
from custom_components.zenmaster.solver.solver import LpSolver, SolverEntry

_REFERENCE = Path(__file__).with_name("reference_run.json")
_NOW = datetime(2026, 11, 28, 10, 0, tzinfo=timezone.utc)
_RUNS = 2  # two quarters, so the second run sees a shifted history and the settlement of the first


class _World(SolverEntry):
    # prices, weather, solar and base load as fixed arrays from a seeded generator
    def entry_config(self, lp: LpSolver) -> None:
        self.price_buy = lp.price_buy = lp.data("price_buy", "euro", 7)
        self.price_sell = lp.price_sell = lp.data("price_sell", "euro", 7)
        self.solar = lp.solar = lp.data("solar", "kW", 7)
        lp.temp, lp.wind, lp.sun = (lp.data(n, u, 7) for n, u in (("temp", "C"), ("wind", "m/s"), ("sun", "W/m2")))
        self.solar_lp = lp.column(hi=0.0, tags=("supply",))

    async def entry_measure(self, lp: LpSolver, now: datetime) -> None:
        # what the price and forecast entries fetch when the live run measures
        q = np.arange(self.price_buy.data.size)
        self.price_buy.data[:] = 0.25 + 0.08 * np.sin(q / 96 * 2 * np.pi) + 0.02 * np.random.default_rng(7).random(q.size)
        self.price_sell.data[:] = self.price_buy.data - 0.12
        self.solar.data[:] = np.maximum(0.0, 3.0 * np.sin((q % 96) / 96 * 2 * np.pi - 1.0))
        lp.temp.data[:], lp.wind.data[:], lp.sun.data[:] = 4.0 + 3.0 * np.sin(q / 96 * 2 * np.pi), 3.0, 50.0
        lp.globals["price_valid"] = True  # so the fleet publishes its prices, as with a real provider

    def entry_prerun(self, lp: LpSolver, now: datetime) -> None:
        # what the forecast and base load entries compute from that: the free solar and the base load per phase
        rng = np.random.default_rng(7)
        rng.random(self.price_buy.data.size)  # the draws the prices took, so the loads stay the numbers of the reference
        self.solar_lp.upper[:] = self.solar.data[12 : 12 + lp.horizon]
        for load in (lp.l1, lp.l2, lp.l3):
            load[:] = 0.15 + 0.1 * rng.random(lp.horizon)


class _Done:
    # a service call is recorded at once; awaiting it is a no-op
    def __await__(self):
        return iter(())


class _Hass:
    # the cable and the charger number, and every call that leaves the integration
    def __init__(self, out: list, plugged: bool) -> None:
        self.out = out
        entities = {"binary_sensor.plug": types.SimpleNamespace(state="on" if plugged else "off", attributes={}), "number.charge": types.SimpleNamespace(state="0", attributes={"unit_of_measurement": "kW"})}
        self.states = types.SimpleNamespace(get=entities.get)
        self.services = self
        self.config_entries = self

    def async_call(self, domain: str, service: str, data: dict, blocking: bool = False) -> _Done:
        self.out.append(["call", domain, service, data])
        return _Done()

    def async_create_task(self, job) -> None:
        self.out.append(["task", getattr(job, "__qualname__", type(job).__name__)])
        if hasattr(job, "close"):
            job.close()

    def async_update_subentry(self, *args, **kwargs) -> None:
        self.out.append(["subentry", kwargs])


class _Bat(types.SimpleNamespace):
    __hash__ = object.__hash__


def _plain(value):
    # numpy and enums as plain JSON values, everything else by its name
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Enum):
        return value.name
    return repr(value)


def _build(out: list, plugged: bool, unmanaged: str = ""):
    # the real classes for heating, pool, electric boiler, heat pump boiler, EV, appliance and four batteries
    hass = _Hass(out, plugged)
    note = lambda name: lambda key, value: out.append(["entity", name, key, value])
    bats = [_Bat(device_id=f"b{i}", name=f"b{i}", power_phase=1 + i % 3, kwh=2.0 + i, soc=40.0 + 5 * i, soc_min=10.0, soc_max=95.0, state=DeviceState.ACTIVE, wear=1.0 + 0.2 * i,
                 power_profile=[types.SimpleNamespace(power=1200), types.SimpleNamespace(power=-1200)], entities={}, individual=None, entity_update=note(f"b{i}")) for i in range(4)]
    dist = types.SimpleNamespace(entities={}, name="zm", _restore=[], devices=bats, max_kw=17.0, max_output=0, optimize=False, plan_rank={}, phase_limit_w=0, entity_update=note("zm"), add_entity=lambda e: None)
    fleet = DistributionEntry(dist)
    dist.entities.setdefault("surplus", types.SimpleNamespace(entity_id="sensor.surplus"))

    house = HeatingHeatPump()
    house.id = house.name = "house"
    house.phase, house.max_power, house._temp0 = Phase.l1, 3.0, 19.8
    pool = PoolHeatPump()
    pool.id = pool.name = "pool"
    pool.phase, pool._temp0 = Phase.l2, 27.0
    tank = BoilerElectric()
    tank.id = tank.name = "tank"
    tank.phase, tank.volume, tank._temp0 = Phase.l3, 200.0, 48.0
    dhw = BoilerHeatPump()
    dhw.id = dhw.name = "dhw"
    dhw.phase, dhw.volume, dhw.max_power, dhw.hass = Phase.l2, 200.0, 0.6, hass
    ev = ElectricVehicle()
    ev.id, ev.capacity_kwh, ev.max_power_kw, ev.ev_max_soc, ev.charge_compensation_eur_kwh = "ev", 60.0, 11.0, 80.0, 0.20
    ev.departure_time, ev.connected_entity, ev.charge_power_entity, ev.hass = "08:00", "binary_sensor.plug", "number.charge", hass
    wash = Appliance()
    wash.id, wash.kind, wash.switch_type, wash.hass = "wash", "washingmachine", "automation", hass
    wash.armed, wash.armed_at = True, _NOW.timestamp()
    consumers = [house, pool, tank, dhw, ev, wash]
    for c in consumers:
        c.entities = {}  # so a status change publishes, as on a real device
        c.manage = c.id != unmanaged

    # built as the coordinator builds the live solver: the managed entries, then what only the live run sets up, for every entry
    lp = LpSolver(track=lambda *a, **k: None, horizon=192)
    lp.hass = hass
    entries = [_World(), fleet, *consumers]
    lp.init([e for e in entries if e.manage])
    for entry in entries:
        entry.entry_setup(lp)
    ev.current_soc = 0.4
    dhw.on_temp(44.0)
    return lp, dist, bats, entries


def _scenario(monkeypatch, plugged: bool) -> list:
    # per run the plan, the sorted calls to the outside, and what the run left on the shared fleet objects
    out: list = []
    clock = [_NOW.timestamp()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    monkeypatch.setattr(EntityDevice, "entity_update", lambda self, key, value: out.append(["entity", getattr(self, "id", ""), key, value]))
    lp, dist, bats, entries = _build(out, plugged)
    runs = []
    for i in range(_RUNS):
        now = _NOW + timedelta(minutes=15 * i)
        clock[0] = now.timestamp()
        out.clear()
        x = asyncio.run(lp.run_live(now, entries))
        assert x is not None, "no plan"
        shared = {"phase_limit_w": dist.phase_limit_w, "plan_rank": {str(k): {b.device_id: v for b, v in r.items()} for k, r in dist.plan_rank.items()}, "individual": {b.device_id: b.individual for b in bats}}
        calls = sorted(json.dumps(o, sort_keys=True, default=_plain) for o in out)
        runs.append(json.loads(json.dumps({"plan": lp.plan(), "outside": calls, "shared": shared}, sort_keys=True, default=_plain)))
    return runs


def test_unmanaged_consumer_is_measured(monkeypatch) -> None:
    # a car ZenMaster does not steer: the live run reads its cable, and nothing goes out for it
    out: list = []
    monkeypatch.setattr(time, "time", lambda: _NOW.timestamp())
    monkeypatch.setattr(EntityDevice, "entity_update", lambda self, key, value: out.append(["entity", getattr(self, "id", ""), key, value]))
    lp, dist, bats, entries = _build(out, True, unmanaged="ev")
    ev = next(e for e in entries if getattr(e, "id", "") == "ev")
    out.clear()
    assert asyncio.run(lp.run_live(_NOW, entries)) is not None
    assert ev.connected and ev not in lp._entries
    assert not [o for o in out if (o[0] == "entity" and o[1] == "ev") or (o[0] == "call" and o[3].get("entity_id") == "number.charge")]


def test_reference_run(monkeypatch) -> None:
    result = {"plugged": _scenario(monkeypatch, True), "unplugged": _scenario(monkeypatch, False)}
    if os.environ.get("ZM_WRITE_REFERENCE"):
        _REFERENCE.write_text(json.dumps(result, sort_keys=True, indent=1), encoding="utf-8")
        pytest.skip("reference written")
    assert result == json.loads(_REFERENCE.read_text(encoding="utf-8"))
