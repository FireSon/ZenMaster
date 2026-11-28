"""DistributionEntry: the fleet in the LP. Grid columns, one unit per battery, the savings bookkeeping and the plan-driven mode."""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

from ..device import DeviceState, DistributionDevice
from ..distribution import WEAR_BIAS, Distribution, DistributionMode
from ..sensor import HaSensor
from ..switch import HaSwitch
from .solver import ALT_EVEN_EUR, LIMIT_EUR, LpSolver, SolverEntry

_LOGGER = logging.getLogger(__name__)

EPS_KW = 0.01  # below this an LP charge/discharge counts as zero
AC_IN = 0.84  # of what a pack takes in over AC, this much comes back out
DC_IN = 0.95  # of what a pack takes in from its own panels, this much comes back out
WEAR_EUR = 0.03  # wear per kWh discharged, for a pack with the fleet's average wear
POWER_SHARE = 0.95  # of its rated charge and discharge power, this much the plan counts on per quarter


class BatteryUnit:
    """One battery in the LP: AC in and out, its own panels into the pack or out through its inverter, and its SoC."""

    def __init__(self, lp: LpSolver, device: DistributionDevice) -> None:
        self.device = device
        self.gain = 0.0  # SoC per kW in one quarter: 0.25 / the kWh of its window
        self.cap = 0.0  # kWh of its SoC window, 0 while it is offline or has no pack
        self.out_max = 0.0  # kW its inverter delivers, battery and own panels together
        self.solar = np.zeros(lp.horizon)  # kW per quarter of its own panels
        key, name = device.device_id, device.name
        # charging counts on its own phase; discharging stays out of the phase rows, they only add load
        self.charge = lp.column(hi=0.0, tags=("draw",), dest=lp.data(f"{key}_charge", "kW", name=name), phase=int(device.power_phase or 0))
        self.discharge = lp.column(hi=0.0, tags=("supply",), dest=lp.data(f"{key}_discharge", "kW", name=name))
        self.dc_in = lp.column(hi=0.0, dest=lp.data(f"{key}_solar_in", "kW", name=name))
        self.dc_out = lp.column(hi=0.0, tags=("supply",), dest=lp.data(f"{key}_solar_out", "kW", name=name))
        self.soc = lp.column(lo=0.0, hi=1.0, dest=lp.data(f"{key}_soc", "0-1", name=name))
        self.rebuild = lp.row_delta(lambda t1, t2: self.soc[t1] - self.soc[t2] - self.gain * (AC_IN * self.charge[t2] + DC_IN * self.dc_in[t2]) + self.gain * self.discharge[t2], const=0.0, label=("soc", key, lambda: 1 / self.cap if self.cap else 0.0, "EUR/kWh"))
        # its own panels either fill the pack or leave through its inverter, which also carries the discharge
        lp.row_quarter({self.dc_in: 1.0, self.dc_out: 1.0}, hi=lambda t: self.solar[t], label=("own_solar", key, 4.0, "EUR/kWh"))
        lp.row_quarter({self.discharge: 1.0, self.dc_out: 1.0}, hi=lambda t: self.out_max, label=("inverter", key, 4.0, "EUR/kWh"))

    def prerun(self, h: int, meters: dict[str, np.ndarray], wear_mean: float, terminal: float) -> None:
        x = self.device
        self.cap = x.kwh * (x.soc_max - x.soc_min) / 100 if x.state > DeviceState.OFFLINE else 0.0
        if self.gain != (gain := 0.25 / self.cap if self.cap else 0.0):
            self.gain = gain
            self.rebuild()
        own = meters.get(getattr(x.entities.get("solar"), "entity_id", None) or "")
        self.solar[:] = own[:h] if own is not None and self.cap else 0.0

        # out of the plan: nothing flows and the SoC stays put
        if not self.cap:
            self.out_max = 0.0
            for col in (self.charge, self.discharge, self.dc_in, self.dc_out):
                col.upper[0:h] = 0.0
            self.soc.lower[0:h] = self.soc.upper[0:h] = 0.0
            return

        # its own limits, the last flow slot pinned to 0 because it is not SoC-coupled
        self.out_max = POWER_SHARE * abs(x.power_profile[False].power) / 1000.0
        self.charge.upper[0:h] = POWER_SHARE * abs(x.power_profile[True].power) / 1000.0
        self.discharge.upper[0:h] = self.out_max
        self.dc_in.upper[0:h] = self.dc_out.upper[0:h] = self.solar
        self.charge.upper[h - 1] = self.discharge.upper[h - 1] = self.dc_in.upper[h - 1] = 0.0

        # SoC slot 0 = its live level; a pack above its window only has to stop charging, it need not dump the excess
        start = max(0.0, x.kwh * (x.soc - x.soc_min) / 100 / self.cap)  # a reading under soc_min never forces a charge
        self.soc.lower[0] = self.soc.upper[0] = start
        self.soc.upper[1:h] = max(1.0, start)
        self.soc.costs[h - 1] = -self.cap * terminal  # realizable terminal value = sell price (prevents buy-to-hoard)

        # a pack that has worked more than the fleet's average costs more per kWh, one that worked less costs less, so the plan turns to it later; the average stays WEAR_EUR
        self.discharge.costs[0:h] = WEAR_EUR * (1 + WEAR_BIAS * (x.wear / wear_mean - 1) if wear_mean else 1) * 0.25


class DistributionEntry(SolverEntry):
    def __init__(self, distribution: Distribution) -> None:
        super().__init__()
        self.distribution = distribution
        self.id = "solver"
        self._units: list[BatteryUnit] = []  # one per battery, built in entry_config
        self.stock = 0.0  # kWh in the fleet whose cost basis we know
        self.basis = 0.0  # weighted average price paid for that stock
        self.savings = 0.0  # realised saving, cumulative, never reset
        self._counters: dict[str, float] = {}  # last read of the kWh aggregators, for the per-quarter delta
        self.savings_midnight = 0.0  # savings level at the last midnight, splits today off the total
        self._day = ""  # the calendar day savings_midnight belongs to
        self._now = None  # clock of the running solve, set in entry_prerun

        # the solver's entities on the fleet device: the master switch, the live price and the realised saving in money
        HaSwitch(distribution, "optimize", on_changed=self._set_optimize, restore=True, default=False)
        HaSensor(distribution, "price_buy", uom="EUR/kWh", state_class="measurement", precision=4)
        HaSensor(distribution, "price_sell", uom="EUR/kWh", state_class="measurement", precision=4)
        HaSensor(distribution, "savings", uom="EUR", state_class="total", precision=3)
        HaSensor(distribution, "savings_today", uom="EUR", state_class="measurement", precision=3)

    def load(self, store: dict[str, Any]) -> None:
        for key in ("stock", "basis", "savings", "savings_midnight"):
            if (value := store.get(key)) is not None:
                setattr(self, key, float(value))
        self._day = str(store.get("day", ""))
        self.distribution.entity_update("savings", self.savings)

    def save(self, store: dict[str, Any]) -> None:
        for key in ("stock", "basis", "savings", "savings_midnight"):
            store[key] = getattr(self, key)
        store["day"] = self._day

    def entry_config(self, lp: LpSolver) -> None:
        # publish house-wide values to the shared globals so other entries (e.g. BaseLoad) can read them
        d = self.distribution
        lp.globals["phase_limit_kw"] = d.max_kw / 3
        lp.globals["price_device"] = d  # owns the price sensors whose history feeds the price model
        lp.globals.setdefault("savings_pending", [])  # consumers append their realised shift saving here

        self.grid_buy = lp.column(tags=("supply",), dest=lp.data("grid_buy", "kW"))
        self.grid_sell = lp.column(tags=("draw",), dest=lp.data("grid_sell", "kW"))

        # every battery on its own; the fleet columns are their sums, so the mode, the chart and the savings read the fleet as before
        self._units = [BatteryUnit(lp, x) for x in self.distribution.devices]
        self._bat_charge = lp.column(dest=lp.data("bat_charge", "kW"))
        self._bat_discharge = lp.column(dest=lp.data("bat_discharge", "kW"))
        self._bat_soc = lp.data("bat_soc", "0-1")
        lp.row_quarter({self._bat_charge: 1.0, **{u.charge: -1.0 for u in self._units}, **{u.dc_in: -1.0 for u in self._units}}, lo=0.0, hi=0.0)
        lp.row_quarter({self._bat_discharge: 1.0, **{u.discharge: -1.0 for u in self._units}}, lo=0.0, hi=0.0)

        # the regulatory cap on what the fleet delivers to the house (Germany: 800 W), in the plan as in the live setpoint
        if d.max_output > 0:
            lp.row_quarter({**{u.discharge: 1.0 for u in self._units}, **{u.dc_out: 1.0 for u in self._units}}, hi=d.max_output / 1000, label=("max_output", "", 4.0, "EUR/kWh"))

    @staticmethod
    def _walk(stock: float, basis: float, charge: float, discharge: float, charge_grid: float, discharge_grid: float, buy: float, sell: float) -> tuple[float, float, float]:
        # one step of the stock: a charge sets its average price (grid at buy, own solar at sell), a discharge saves against that price
        saving = 0.0
        if charge > 0:
            basis = (stock * basis + charge_grid * buy + (charge - charge_grid) * sell) / (stock + charge)
            stock += charge
        if discharge > 0:
            saving = (discharge - discharge_grid) * (buy - basis) + discharge_grid * (sell - basis)
            stock = max(0.0, stock - discharge)
        return stock, basis, saving

    async def entry_measure(self, lp: LpSolver, now: Any) -> None:
        # Once per quarter: publish the live price and settle the realised saving. The fleet is stock
        # with a weighted average cost: charging adds kWh at the price of that quarter (solar enters at
        # the export price we gave up), discharging books the difference against the price avoided.
        # Prices are flat within a quarter, so the money is exact; only the grid/solar split of the
        # charge is taken at quarter level instead of per tick.
        d = self.distribution
        buy = float(lp.price_buy.data[12]) if lp.price_buy is not None else 0.0
        sell = float(lp.price_sell.data[12]) if lp.price_sell is not None else 0.0
        if lp.globals.get("price_valid"):
            d.entity_update("price_buy", buy)
            d.entity_update("price_sell", sell)

            delta, fresh = {}, {}
            for key in ("charge_power", "discharge_power", "grid_import", "grid_export"):
                value = getattr(d.entities.get(f"{key}_aggr"), "state", None)
                if value is None:
                    continue
                fresh[key] = float(value)
                delta[key] = max(0.0, fresh[key] - self._counters.get(key, fresh[key]))
            if len(delta) == 4 and self._counters:
                charge, discharge = delta["charge_power"], delta["discharge_power"]
                self.stock, self.basis, saving = self._walk(self.stock, self.basis, charge, discharge, min(charge, delta["grid_import"]), min(discharge, delta["grid_export"]), float(lp.price_buy.data[lp.settle]), float(lp.price_sell.data[lp.settle]))
                self.savings += saving
            self._counters.update(fresh)

        self._now = now  # the solve clock, so entry_apply uses the same moment
        day = str(now.date())
        if day != self._day:  # a new day starts today's realised part at zero
            self._day, self.savings_midnight = day, self.savings

        # consumers book their own shift saving into the same total
        if pending := lp.globals.get("savings_pending"):
            self.savings += sum(pending)
            pending.clear()
        d.entity_update("savings", self.savings)

    def entry_prerun(self, lp: LpSolver, now: Any) -> None:
        # the grid per quarter: the price of a kWh in and out, and the connection as its limit both ways
        d = self.distribution
        h = lp.horizon
        self.grid_buy.costs[:] = lp.price_buy.data[12:12 + h] * 0.25
        self.grid_sell.costs[:] = -lp.price_sell.data[12:12 + h] * 0.25
        self.grid_buy.upper[:] = self.grid_sell.upper[:] = d.max_kw

        # every battery on its own: the loss of the round trip sits in its SoC (AC_IN, DC_IN), its wear in what discharging costs
        meters = lp.globals.get("solar_meters", {})
        active = [x.wear for x in d.devices if x.kwh and x.state > DeviceState.OFFLINE]  # only the packs in the plan: an offline one or one without a pack would pull the mean down
        wear_mean = sum(active) / len(active) if active else 0.0
        terminal = float(np.mean(lp.price_sell.data[12 + h : 12 + h + 16]))
        for u in self._units:
            u.prerun(h, meters, wear_mean, terminal)

    def entry_setup(self, lp: LpSolver) -> None:
        # the live distribution holds every phase to the same limit the plan uses
        d = self.distribution
        d.phase_limit_w = d.max_kw * 1000 / 3

    async def _set_optimize(self, _entity: HaSwitch, on: bool) -> None:
        # master switch: ON -> the mode select follows the solver plan each solve; OFF -> back to off (manual/HA)
        d = self.distribution
        d.optimize = bool(on)
        if d.optimize:
            self._apply_plan_mode()
        else:
            d.power_off()
        if d.on_save is not None:
            await d.on_save()

    def _plan_direction(self) -> DistributionMode:
        # the mode implied by the solver plan for the current slot; trading with the grid gives every battery its planned setpoint
        if getattr(self, "_bat_charge", None) is None or self._bat_charge.dest is None:
            return DistributionMode.OFF
        # the AC side, as the setpoints count it: a pack filling from its own panels leaves the house surplus to the plan
        net = sum(u.charge.dest.data[12] - u.discharge.dest.data[12] for u in self._units)  # >0 charge, <0 discharge
        if net > EPS_KW:
            return DistributionMode.INDIVIDUAL if self.grid_buy.dest.data[12] > EPS_KW else DistributionMode.SMART_CHARGING
        if net < -EPS_KW:
            return DistributionMode.INDIVIDUAL if self.grid_sell.dest.data[12] > EPS_KW else DistributionMode.SMART_DISCHARGING
        return DistributionMode.OFF

    def _apply_plan_mode(self) -> None:
        # plan OFF only sets the mode: the P1 tick holds every battery at 0 W while optimize is on
        self.distribution.entities["mode"].update_value(self._plan_direction())

    def _rank(self, lp: LpSolver, flow) -> dict[DistributionDevice, tuple[int, float]]:
        # planned now first, the largest first; then planned later, the earliest start first; a battery without a plan in this direction is left out
        rank = {}
        for u in self._units:
            planned = flow(u)[12 : 12 + lp.horizon]
            if planned[0] > EPS_KW:
                rank[u.device] = (0, -float(planned[0]))
            elif (later := np.flatnonzero(planned > EPS_KW)).size:
                rank[u.device] = (1, float(later[0]))
        return rank

    def entry_postfail(self, lp: LpSolver) -> None:
        # no plan this quarter: with optimize on the fleet stops instead of repeating the last plan
        if self.distribution.optimize:
            self.distribution.entities["mode"].update_value(DistributionMode.OFF)

    def _windows(self, lp: LpSolver, flow: np.ndarray, way: str) -> list[dict]:
        # the two largest stretches of quarters with a flow this way, by energy, with their start and end as epoch seconds
        on = np.concatenate(([False], flow > EPS_KW, [False]))
        edges = np.flatnonzero(np.diff(on.astype(int)))
        spans = sorted(zip(edges[::2], edges[1::2]), key=lambda s: -flow[s[0]:s[1]].sum())[:2]
        t0 = lp._last_slot * 900
        alt = self._alternative(lp, flow, way)
        return [{"code": "battery_window", "v": {"way": way, "start": t0 + 900 * int(a), "end": t0 + 900 * int(b), "kwh": round(float(flow[a:b].sum() * 0.25), 1), **self._why(lp, way, int(a), int(b)), **alt}} for a, b in spans]

    def _why(self, lp: LpSolver, way: str, a: int, b: int) -> dict:
        # why this window: the buy price weighted by what every pack moves over AC, its bound after loss (charge) or wear (discharge),
        # against the value of a stored kWh in that same pack at the end of each quarter, weighted the same way (Jan, 2026-11-26)
        buy = lp.price_buy.data[12:12 + lp.horizon]
        price = wear = value = moved = 0.0
        for u in (u for u in self._units if u.cap):
            f = (u.charge if way == "charge" else u.discharge).dest.data[12 + a:12 + b]
            worth = np.append(lp.label_duals("soc", u.device.device_id), 0.0)[a + 1:b + 1]  # the row of quarter t+1 carries the value at the end of t
            # the wear of this pack per kWh discharged, as the plan charges it: its own share of WEAR_EUR by how much it has worked
            price, wear = price + float((f * buy[a:b]).sum()), wear + float((f * u.discharge.costs[a:b] * 4).sum())
            value, moved = value + float((f * worth).sum()), moved + float(f.sum())
        if not moved:
            return {}
        price, wear, value = price / moved, wear / moved, value / moved
        return {"price": round(price, 4), "bound": round(price / AC_IN if way == "charge" else price - wear, 4), "value": round(value, 4), "wear": round(wear, 4)}

    def _alternative(self, lp: LpSolver, flow: np.ndarray, way: str) -> dict:
        # the next best quarter this way: outside every window of it, the reduced cost of the pack it costs least there, in EUR/kWh
        if lp.duals is None:
            return {}
        h, alt = lp.horizon, np.full(lp.horizon, np.inf)
        for u in (u for u in self._units if u.cap):
            col = u.charge if way == "charge" else u.discharge
            rc = lp.duals[1][col.index:col.index + h] * 4
            # a pack that cannot move this way there is no alternative: full for charging, empty for discharging, or the flow pinned to 0
            soc = u.soc.dest.data[12:12 + h]
            rc[(soc >= u.soc.upper - 1e-6) if way == "charge" else (soc <= 1e-6)] = np.inf
            rc[col.upper <= 0] = np.inf
            alt = np.minimum(alt, rc)
        alt[(flow > EPS_KW) | (alt < 0)] = np.inf
        alt[0] = np.inf  # quarter 0 is the one running now
        if not np.isfinite(k := int(np.argmin(alt))) or not np.isfinite(alt[k]):
            return {}
        return {"alt_at": lp._last_slot * 900 + 900 * k, "alt_eur": round(float(alt[k]), 4), "even": bool(alt[k] < ALT_EVEN_EUR)}

    def _summarize(self, lp: LpSolver) -> None:
        # the plan as a whole: its cost, what the fleet buys and sells at which price against its loss and wear, its two largest windows per way, and where a pack is full or empty
        h = lp.horizon
        buy, sell = lp.price_buy.data[12:12 + h], lp.price_sell.data[12:12 + h]
        cost = float((self.grid_buy.dest.data[12:12 + h] * buy - self.grid_sell.dest.data[12:12 + h] * sell).sum() * 0.25)
        lp.summary.append({"code": "cost", "v": {"eur": round(cost, 2), "hours": h // 4}})
        charge, discharge = self._bat_charge.dest.data[12:12 + h], self._bat_discharge.dest.data[12:12 + h]
        if not (charge.sum() or discharge.sum()):
            return
        # both ways weighted by the buy price of their quarters (Jan, 2026-11-26)
        price = lambda f: round(float((f * buy).sum() / f.sum()), 4) if f.sum() else 0.0
        lp.summary.append({"code": "battery", "v": {"charge_kwh": round(float(charge.sum() * 0.25), 1), "charge_price": price(charge), "discharge_kwh": round(float(discharge.sum() * 0.25), 1), "discharge_price": price(discharge), "loss": round(1 - AC_IN, 2), "wear": WEAR_EUR}})
        lp.summary += self._windows(lp, charge, "charge") + self._windows(lp, discharge, "discharge")
        if lp.duals is None:
            return
        # a pack at its top or bottom: the shadow price of that bound per kWh of its window, from LIMIT_EUR on; quarter 0 is the live level, pinned
        for u in self._units:
            if not u.cap:
                continue
            soc, worth = u.soc.dest.data[12:12 + h], np.abs(lp.duals[1][u.soc.index:u.soc.index + h]) / u.cap
            for code, at in (("battery_full", soc[1:] >= 1.0 - 1e-6), ("battery_empty", soc[1:] <= 1e-6)):
                if (hits := worth[1:][at & (worth[1:] >= LIMIT_EUR)]).size:
                    lp.summary.append({"code": code, "v": {"id": u.device.device_id, "name": u.device.name, "quarters": int(hits.size), "max": round(float(hits.max()), 4)}})

    def entry_postrun(self, lp: LpSolver) -> None:
        # the fleet's SoC for the chart: the units weighted by the kWh of their window
        if total := sum(u.cap for u in self._units):
            h = lp.horizon
            self._bat_soc.data[12 : 12 + h] = sum(u.cap * u.soc.dest.data[12 : 12 + h] for u in self._units) / total
        self._summarize(lp)

    async def entry_apply(self, lp: LpSolver) -> None:
        d = self.distribution
        # the plan's order per direction over AC; the distribution takes it over while optimize is on, from the next P1 tick
        if self._units:
            d.plan_rank = {True: self._rank(lp, lambda u: u.charge.dest.data), False: self._rank(lp, lambda u: u.discharge.dest.data + u.dc_out.dest.data)}
            # every battery with storage its AC setpoint for this quarter, for the individual mode
            for u in self._units:
                u.device.individual = int(1000 * (u.discharge.dest.data[12] + u.dc_out.dest.data[12] - u.charge.dest.data[12])) if u.cap else None
                if u.device.individual is not None:
                    u.device.entity_update("individual", u.device.individual)  # shown in HA, where it can also be set by hand
        if d.optimize:
            self._apply_plan_mode()
            d.order_date = 0

        # "savings_today" runs from forecast to fact: realised since midnight plus the plan's expected
        # saving over the quarters left today. At 00:00 it is a pure estimate, at 24:00 pure realisation,
        # so the accuracy of a day is its own last value against its first. The forecast walks stock and
        # basis forward the way the settlement books them, so both halves value a kWh on the same footing.
        now = self._now
        left = 0 if now is None else max(0, ((23 - now.hour) * 60 + (60 - now.minute)) // 15)
        expected = 0.0
        if left and lp.price_buy is not None:
            buy, sell = lp.price_buy.data, lp.price_sell.data
            gb, gs = self.grid_buy.dest.data, self.grid_sell.dest.data
            bc, bd = self._bat_charge.dest.data, self._bat_discharge.dest.data
            stock, basis = self.stock, self.basis
            for i in range(12, 12 + left):
                charge, discharge = bc[i] * 0.25, bd[i] * 0.25
                stock, basis, saving = self._walk(stock, basis, charge, discharge, min(charge, gb[i] * 0.25), min(discharge, gs[i] * 0.25), buy[i], sell[i])
                expected += saving
        d.entity_update("savings_today", self.savings - self.savings_midnight + expected)
