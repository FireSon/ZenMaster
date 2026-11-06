"""Distribution scenarios closed-loop: real Distribution + real ZendureSolarFlow, MQTT intercepted by a per-device sim firmware on sim time."""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from conftest import fake_hass
import custom_components.zenmaster.zendure.solarflow as solarflow_module
from custom_components.zenmaster.binary_sensor import HaBinarySensor
from custom_components.zenmaster.distribution import Distribution, DistributionMode
from custom_components.zenmaster.number import HaNumber
from custom_components.zenmaster.select import HaSelect
from custom_components.zenmaster.sensor import HaSensor
from custom_components.zenmaster.zendure.device import ConnectionType
from custom_components.zenmaster.zendure.solarflow import ZendureSolarFlow
from custom_components.zenmaster.zendure.driver import PRODKEYS, PRODUCTS, ZendureDriver

for cls in (HaSensor, HaNumber, HaBinarySensor, HaSelect):
    cls.add = staticmethod(lambda *a, **k: None)  # no HA platform in tests

# device_id, prodkey, model, pwr_max, pwr_min, pwr_solar, kwh, start_soc, (lag_cold, lag_warm, ramp W/s; 0 = step)
# fleet matches the old ZenMaster sim (config fleet, socSet 95%) so the baselines stay comparable
FLEET = [
    ("SF800PRO", "r3mn8u", "Solarflow 800 Pro", 800, -1000, -1200, 1.92, 95.0, (10.0, 4.0, 0.0)),
    ("SF2400AC", "bc8b7f", "Solarflow 2400 AC", 2400, -2400, 0, 8.64, 90.0, (10.0, 4.0, 0.0)),
    ("SF1600ACP", "65174u", "Solarflow 1600 AC Plus", 1600, -1600, 0, 3.84, 85.0, (10.0, 4.0, 0.0)),
    ("SF2400PRO", "2qe7c9", "Solarflow 2400 Pro", 2400, -2400, 0, 6.72, 80.0, (10.0, 4.0, 0.0)),
    ("SF2400ACP", "5fg27j", "Solarflow 2400 AC Plus", 2400, -2400, 0, 6.72, 75.0, (10.0, 4.0, 0.0)),
    ("HYPER2000", "gda3tb", "Hyper 2000", 1200, -1200, -1600, 1.92, 70.0, (12.0, 5.0, 200.0)),
]


class SimZendure:
    """Fake paho client + firmware physics for one device: parses the real command payloads, reports on sim time via the real driver routing."""

    def __init__(self, device: ZendureSolarFlow, driver: ZendureDriver, start_soc: float, physics: tuple[float, float, float]) -> None:
        self.device = device
        self.driver = driver
        self.lag_cold, self.lag_warm, self.ramp = physics
        self.target = 0
        self.actual = 0.0
        self.soc = start_soc
        self._pending: int | None = None
        self._pending_at = 0.0
        self._reply_id: int | None = None
        self._report_at = 0.0

    def publish(self, topic: str, payload: str) -> None:
        # the device's real mqtt_publish lands here; parse exactly what the firmware would
        command = json.loads(payload)
        props = command.get("properties")
        if props is not None:
            # SF models: the limits as a property write; the hems flag alone moves no power, no reply
            if "outputLimit" in props:
                self._pending = props["outputLimit"] - props["inputLimit"]
            return
        if "function" not in command:
            return
        args = command["arguments"]
        if command["function"] == "deviceAutomation":  # legacy / hub models
            value = args[0]["autoModelValue"]
            program = args[0]["autoModelProgram"]
            self._pending = 0 if program == 0 else -value["chargingPower"] if program == 1 else value["outPower"]
        else:  # hemsEP (Hyper): charging carries chargePower, discharging outputPower, both zero is off
            self._pending = -args["chargePower"] if args.get("chargePower") else args.get("outputPower", 0)
        self._reply_id = command["messageId"]  # the real device replies after the send, never inside it; newer commands overwrite the value, not the lag timer

    def _route(self, topic: str, payload: dict) -> None:
        msg = SimpleNamespace(topic=f"/{self.device.prodkey}/{self.device.device_id}/{topic}", payload=json.dumps(payload).encode())
        self.driver._on_mqtt_message(self, ConnectionType.LOCAL, msg)

    def tick(self, now: float, dt: float) -> None:
        # ack the last command, then let the pending value take effect after the lag
        if self._reply_id is not None:
            self._route("function/invoke/reply", {"messageId": self._reply_id})
            self._reply_id = None
        if self._pending is not None and self._pending_at == 0.0:
            self._pending_at = now + (self.lag_cold if self.actual == 0 else self.lag_warm)
        if self._pending is not None and now >= self._pending_at:
            self.target, self._pending, self._pending_at = self._pending, None, 0.0

        # clamp on rated power and SoC limits, then ramp (0 = step)
        profile = self.device.power_profile[self.target < 0]
        target = max(-abs(profile.power), min(abs(profile.power), self.target))
        if target > 0 and self.soc <= self.device.soc_min or target < 0 and self.soc >= self.device.soc_max:
            target = 0
        self.actual = float(target) if self.ramp == 0 else self.actual + max(-self.ramp * dt, min(self.ramp * dt, target - self.actual))
        self.soc = max(0.0, min(100.0, self.soc - self.actual * dt / 3600.0 / (self.device.kwh * 10)))

        if now >= self._report_at:
            self._report_at = now + 1.0
            watts = int(self.actual)
            props = {"electricLevel": int(self.soc), "hyperTmp": 3131, "outputHomePower": max(0, watts), "gridInputPower": max(0, -watts)}
            self._route("properties/report", {"properties": props})


@dataclass
class AppliancePhase:
    watts: int
    duration_s: float
    cycling: bool = False
    cycle_on_s: float = 0.0
    cycle_off_s: float = 0.0


class Appliance:
    def __init__(self, name: str, phases: list[AppliancePhase]) -> None:
        self.name = name
        self.phases = phases
        self._total_s = sum(p.duration_s for p in phases)

    @property
    def total_duration_s(self) -> float:
        return self._total_s

    def watts_at(self, elapsed_s: float) -> int:
        if elapsed_s < 0 or elapsed_s >= self._total_s:
            return 0
        cumulative = 0.0
        for phase in self.phases:
            if elapsed_s < cumulative + phase.duration_s:
                phase_elapsed = elapsed_s - cumulative
                if phase.cycling and phase.cycle_on_s > 0:
                    cycle_len = phase.cycle_on_s + phase.cycle_off_s
                    pos = phase_elapsed % cycle_len
                    return phase.watts if pos < phase.cycle_on_s else 0
                return phase.watts
            cumulative += phase.duration_s
        return 0


_REAL_PROFILES = json.loads((Path(__file__).parent / "real_profiles.json").read_text())


class RealAppliance(Appliance):
    # per-second profile from production data (real_profiles.json), extracted 2026-07-10/11
    def __init__(self, name: str) -> None:
        self.name = name
        self._watts = _REAL_PROFILES[name]["watts"]
        self._total_s = float(len(self._watts))

    def watts_at(self, elapsed_s: float) -> int:
        return self._watts[int(elapsed_s)] if 0 <= elapsed_s < self._total_s else 0


def baseload() -> Appliance:
    return Appliance("baseload", [AppliancePhase(200, 7200)])


def kettle() -> Appliance:
    return RealAppliance("koffie")


def toaster() -> Appliance:
    return Appliance("toaster", [AppliancePhase(1000, 180)])


def hair_dryer() -> Appliance:
    return Appliance("hair_dryer", [AppliancePhase(1800, 480)])


def microwave() -> Appliance:
    return Appliance("microwave", [AppliancePhase(1200, 120, cycling=True, cycle_on_s=30, cycle_off_s=10)])


def oven() -> Appliance:
    return RealAppliance("koken")


def induction_hob_high() -> Appliance:
    return RealAppliance("inductie")


def induction_hob_low() -> Appliance:
    return Appliance("induction_low", [AppliancePhase(800, 1200)])


def dishwasher() -> Appliance:
    return RealAppliance("vaatwasser")


def washing_machine() -> Appliance:
    return RealAppliance("wasmachine")


def tumble_dryer() -> Appliance:
    return Appliance("tumble_dryer", [AppliancePhase(2500, 2700, cycling=True, cycle_on_s=45, cycle_off_s=15), AppliancePhase(200, 600)])


def ev_charger() -> Appliance:
    return RealAppliance("ev")


def heat_pump() -> Appliance:
    phases = []
    for _ in range(6):
        phases.append(AppliancePhase(1200, 1140))
        phases.append(AppliancePhase(2400, 60))
    return Appliance("heat_pump", phases)


def solar_generation(peak_w: int = 3000, duration_s: int = 7200) -> Appliance:
    q = duration_s / 4
    return Appliance("solar", [AppliancePhase(-peak_w // 2, q), AppliancePhase(-peak_w, q * 2), AppliancePhase(-peak_w // 2, q)])


@dataclass
class ScheduledAppliance:
    appliance: Appliance
    start_offset_s: float


@dataclass
class HourBucket:
    load_wh: float = 0.0
    solar_wh: float = 0.0
    fleet_wh: float = 0.0
    grid_wh: float = 0.0
    dev_setpoint: list = field(default_factory=list)
    dev_soc: list = field(default_factory=list)
    _dev_setpoint_sum: list = field(default_factory=list)
    _ticks: int = 0


@dataclass
class Metrics:
    name: str
    duration_s: float = 0.0
    grid_import_wh: float = 0.0
    grid_export_wh: float = 0.0
    fleet_discharge_wh: float = 0.0
    fleet_charge_wh: float = 0.0
    device_switches: int = 0
    peak_overshoot_w: int = 0
    peak_undershoot_w: int = 0
    below_floor_ticks: int = 0
    total_ticks: int = 0
    _prev_active: set = field(default_factory=set)
    hourly: list = field(default_factory=list)
    _dev_names: list = field(default_factory=list)
    _dev_kwh: list = field(default_factory=list)

    def report(self) -> str:
        return (
            f"\n  {'-' * 60}\n"
            f"  Scenario: {self.name}\n"
            f"  Duration: {self.duration_s:.0f}s ({self.duration_s / 60:.1f} min)\n"
            f"  {'-' * 60}\n"
            f"    Grid import (uncovered):  {self.grid_import_wh:8.1f} Wh\n"
            f"    Grid export (overshoot):  {self.grid_export_wh:8.1f} Wh\n"
            f"    Fleet discharged:         {self.fleet_discharge_wh:8.1f} Wh\n"
            f"    Fleet charged:            {self.fleet_charge_wh:8.1f} Wh\n"
            f"    Device switches:          {self.device_switches:8d}\n"
            f"    Peak overshoot:           {self.peak_overshoot_w:8d} W\n"
            f"    Peak undershoot:          {self.peak_undershoot_w:8d} W\n"
            f"    Below-floor ticks:        {self.below_floor_ticks:8d} / {self.total_ticks}\n"
            f"  {'-' * 60}"
        )

    def hourly_report(self) -> str:
        if not self.hourly:
            return ""
        n_dev = len(self._dev_names)
        hdr = f"  {'Hour':>4s} | {'Load':>6s} | {'Solar':>6s} | {'Fleet':>6s} | {'Grid':>6s}"
        for i in range(n_dev):
            hdr += f" | {self._dev_names[i]}({self._dev_kwh[i]:.0f}kWh)"
        lines = [f"\n  {'-' * len(hdr)}", hdr, f"  {'-' * len(hdr)}"]
        for h, b in enumerate(self.hourly):
            row = f"  {h:3d}h | {b.load_wh:5.0f}W | {b.solar_wh:5.0f}W | {b.fleet_wh:+6.0f}W | {b.grid_wh:+6.0f}W"
            for i in range(n_dev):
                sp = b.dev_setpoint[i] if i < len(b.dev_setpoint) else 0
                soc = b.dev_soc[i] if i < len(b.dev_soc) else 0
                row += f" | {sp:+5.0f}W {soc:5.1f}%"
            lines.append(row)
        lines.append(f"  {'-' * len(hdr)}")
        return "\n".join(lines)


class SimHarness:
    P1_INTERVAL = 1.0  # live, the distribution receives a P1 value every second
    NOISE_W = 50
    SOLAR_AVG_S = 60.0

    def __init__(self, name: str) -> None:
        self.name = name

    async def run(self, schedule: list[ScheduledAppliance], duration_s: float | None = None, seed: int = 42) -> Metrics:
        # everything is built inside the running loop: ZendureSolarFlow starts (and we immediately cancel) its device task
        hass = fake_hass()
        dist = Distribution(hass, "entry")
        dist.wear_date = float("inf")  # no recorder in the sim
        dist.mode = DistributionMode.SMART.value
        driver = ZendureDriver(hass, "entry", [], dist)

        devs: list[ZendureSolarFlow] = []
        firmwares: list[SimZendure] = []
        for device_id, prodkey, model, pwr_max, pwr_min, _, kwh, start_soc, physics in FLEET:
            cls = PRODUCTS[PRODKEYS[prodkey]].cls  # the class the driver builds for this product (SolarFlow, Hyper, ...)
            d = cls(hass, None, None, device_id, {"prodkey": prodkey, "device_id": device_id, "sn": device_id}, model, pwr_max, pwr_min)
            d.driver = driver
            d.unload()  # the device task polls wall-clock staleness; the sim runs on sim time
            d.kwh = d.kwh = kwh
            d.soc_max = 95.0  # the old sim sent socSet 950
            driver.devices[device_id] = d
            dist.devices.append(d)
            devs.append(d)
            firmwares.append(SimZendure(d, driver, start_soc, physics))

        if duration_s is None:
            duration_s = max(sa.start_offset_s + sa.appliance.total_duration_s for sa in schedule)
        m = Metrics(name=self.name, _dev_names=[d.device_id for d in devs], _dev_kwh=[d.kwh for d in devs])
        m.duration_s = duration_s
        hours = self.P1_INTERVAL / 3600.0

        rng = random.Random(seed)
        solar_buf: deque[int] = deque(maxlen=max(1, int(self.SOLAR_AVG_S / self.P1_INTERVAL)))
        sim_time = 1000000.0
        elapsed = 0.0

        # power() holds a device HOLD_S after a command on the wall clock; the sim runs on sim time, so the driver reads the sim clock here
        real_time, solarflow_module.time = solarflow_module.time, SimpleNamespace(time=lambda: sim_time)
        try:
            while elapsed < duration_s:
                sim_time += self.P1_INTERVAL
                elapsed += self.P1_INTERVAL

                loads_w = 0
                solar_instant = 0
                for sa in schedule:
                    w = sa.appliance.watts_at(elapsed - sa.start_offset_s)
                    if w < 0:
                        solar_instant += abs(w)
                    else:
                        loads_w += w
                solar_buf.append(solar_instant)
                devs[0].solar = sum(solar_buf) // len(solar_buf)

                # physics + telemetry through the real driver routing, then the P1 tick
                for fw in firmwares:
                    fw.tick(sim_time, self.P1_INTERVAL)
                fleet_output = int(sum(fw.actual for fw in firmwares))
                p1 = loads_w - solar_instant - fleet_output + rng.randint(-self.NOISE_W, self.NOISE_W)
                await dist._process_p1(p1, dist.power, now=sim_time)

                m.total_ticks += 1
                if p1 > 0:
                    m.grid_import_wh += p1 * hours
                    if fleet_output > 0:
                        m.peak_undershoot_w = max(m.peak_undershoot_w, p1)
                if p1 < 0 and fleet_output > 0:
                    m.grid_export_wh += abs(p1) * hours
                    m.peak_overshoot_w = max(m.peak_overshoot_w, abs(p1))
                if fleet_output > 0:
                    m.fleet_discharge_wh += fleet_output * hours
                elif fleet_output < 0:
                    m.fleet_charge_wh += abs(fleet_output) * hours
                for d in devs:
                    if d.power_setpoint != 0 and abs(d.power_setpoint) < abs(d.power_profile[d.power_setpoint < 0].floor):
                        m.below_floor_ticks += 1
                current = {d.device_id for d in devs if d.power_setpoint != 0}
                m.device_switches += len(current - m._prev_active) + len(m._prev_active - current)
                m._prev_active = current

                hour = int(elapsed / 3600)
                while len(m.hourly) <= hour:
                    m.hourly.append(HourBucket(_dev_setpoint_sum=[0.0] * len(devs)))
                b = m.hourly[hour]
                b._ticks += 1
                b.load_wh += loads_w * hours
                b.solar_wh += solar_instant * hours
                b.fleet_wh += fleet_output * hours
                b.grid_wh += p1 * hours
                for i, d in enumerate(devs):
                    b._dev_setpoint_sum[i] += d.power_setpoint
                b.dev_soc = [d.soc for d in devs]
        finally:
            solarflow_module.time = real_time

        for b in m.hourly:
            if b._ticks > 0 and not b.dev_setpoint:
                b.dev_setpoint = [s / b._ticks for s in b._dev_setpoint_sum]
        return m


def test_scenario_morning_rush() -> None:
    m = asyncio.run(
        SimHarness("morning_rush").run(
            [ScheduledAppliance(baseload(), 0), ScheduledAppliance(kettle(), 30), ScheduledAppliance(toaster(), 30), ScheduledAppliance(hair_dryer(), 90)], 600
        )
    )
    print(m.report())
    assert m.fleet_discharge_wh > 0


def test_scenario_cooking_dinner() -> None:
    m = asyncio.run(SimHarness("cooking_dinner").run([ScheduledAppliance(baseload(), 0), ScheduledAppliance(oven(), 0), ScheduledAppliance(induction_hob_high(), 60)], 1200))
    print(m.report())
    assert m.fleet_discharge_wh > 0


def test_scenario_solar_midday() -> None:
    m = asyncio.run(
        SimHarness("solar_midday").run(
            [
                ScheduledAppliance(baseload(), 0),
                ScheduledAppliance(solar_generation(3000, 7200), 0),
                ScheduledAppliance(dishwasher(), 300),
                ScheduledAppliance(washing_machine(), 600),
            ],
            4800,
        )
    )
    print(m.report())
    assert m.fleet_charge_wh > 0


def test_scenario_evening_peak() -> None:
    m = asyncio.run(SimHarness("evening_peak").run([ScheduledAppliance(baseload(), 0), ScheduledAppliance(ev_charger(), 0), ScheduledAppliance(oven(), 300)], 3600))
    print(m.report())
    assert m.fleet_discharge_wh > 0


def test_scenario_rapid_cycling() -> None:
    m = asyncio.run(SimHarness("rapid_cycling").run([ScheduledAppliance(baseload(), 0), ScheduledAppliance(microwave(), 0), ScheduledAppliance(kettle(), 60)], 300))
    print(m.report())


def test_scenario_winter_evening() -> None:
    m = asyncio.run(
        SimHarness("winter_evening").run(
            [ScheduledAppliance(baseload(), 0), ScheduledAppliance(heat_pump(), 0), ScheduledAppliance(oven(), 300), ScheduledAppliance(induction_hob_low(), 600)], 3600
        )
    )
    print(m.report())
    assert m.fleet_discharge_wh > 0


def test_scenario_laundry_day() -> None:
    m = asyncio.run(SimHarness("laundry_day").run([ScheduledAppliance(baseload(), 0), ScheduledAppliance(washing_machine(), 0), ScheduledAppliance(tumble_dryer(), 1800)], 5400))
    print(m.report())
    assert m.fleet_discharge_wh > 0


def test_scenario_solar_to_cooking() -> None:
    solar_short = Appliance("solar_short", [AppliancePhase(-2500, 600), AppliancePhase(-500, 120), AppliancePhase(0, 3000)])
    m = asyncio.run(
        SimHarness("solar_to_cooking").run(
            [ScheduledAppliance(baseload(), 0), ScheduledAppliance(solar_short, 0), ScheduledAppliance(induction_hob_high(), 720), ScheduledAppliance(oven(), 780)], 2400
        )
    )
    print(m.report())
    assert m.fleet_charge_wh > 0
    assert m.fleet_discharge_wh > 0


def test_scenario_overnight_baseload() -> None:
    fridge = Appliance("fridge", [AppliancePhase(150, 7200, cycling=True, cycle_on_s=900, cycle_off_s=900)])
    m = asyncio.run(SimHarness("overnight_baseload").run([ScheduledAppliance(Appliance("standby", [AppliancePhase(180, 7200)]), 0), ScheduledAppliance(fridge, 0)], 7200))
    print(m.report())


def test_scenario_everything_at_once() -> None:
    m = asyncio.run(
        SimHarness("everything_at_once").run(
            [
                ScheduledAppliance(baseload(), 0),
                ScheduledAppliance(ev_charger(), 0),
                ScheduledAppliance(oven(), 0),
                ScheduledAppliance(dishwasher(), 0),
                ScheduledAppliance(washing_machine(), 60),
            ],
            1800,
        )
    )
    print(m.report())
    assert m.fleet_discharge_wh > 0


def test_scenario_kettle_during_discharge() -> None:
    m = asyncio.run(SimHarness("kettle_during_discharge").run([ScheduledAppliance(Appliance("steady", [AppliancePhase(500, 600)]), 0), ScheduledAppliance(kettle(), 120)], 600))
    print(m.report())
    assert m.fleet_discharge_wh > 0


def test_scenario_solar_clouds() -> None:
    cloud_phases = []
    for _ in range(8):
        cloud_phases.append(AppliancePhase(-2500, 300))
        cloud_phases.append(AppliancePhase(-400, 120))
    m = asyncio.run(SimHarness("solar_clouds").run([ScheduledAppliance(baseload(), 0), ScheduledAppliance(Appliance("solar_cloudy", cloud_phases), 0)], 3360))
    print(m.report())


def test_scenario_full_day() -> None:
    fridge = Appliance("fridge", [AppliancePhase(150, 86400, cycling=True, cycle_on_s=900, cycle_off_s=900)])
    standby = Appliance("standby", [AppliancePhase(180, 86400)])
    ev_short = Appliance("ev_short", [AppliancePhase(3600, 1800)])
    solar_day = Appliance("solar_day", [AppliancePhase(-1000, 7200), AppliancePhase(-3000, 14400), AppliancePhase(-1000, 7200)])
    m = asyncio.run(
        SimHarness("full_day").run(
            [
                ScheduledAppliance(standby, 0),
                ScheduledAppliance(fridge, 0),
                ScheduledAppliance(kettle(), 6 * 3600),
                ScheduledAppliance(toaster(), 6 * 3600),
                ScheduledAppliance(hair_dryer(), 6.5 * 3600),
                ScheduledAppliance(solar_day, 10 * 3600),
                ScheduledAppliance(dishwasher(), 11 * 3600),
                ScheduledAppliance(washing_machine(), 12 * 3600),
                ScheduledAppliance(induction_hob_high(), 16 * 3600),
                ScheduledAppliance(oven(), 17 * 3600),
                ScheduledAppliance(ev_short, 17.5 * 3600),
                ScheduledAppliance(heat_pump(), 19 * 3600),
                ScheduledAppliance(tumble_dryer(), 20 * 3600),
            ],
            86400,
        )
    )
    print(m.report())
    print(m.hourly_report())
    assert m.fleet_discharge_wh > 0
    assert m.fleet_charge_wh > 0
