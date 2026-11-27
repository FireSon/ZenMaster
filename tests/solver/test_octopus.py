"""Octopus Energy (DE) over Kraken: the password once, the active account, flat, time-of-use and forecast rates, a renewed token."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from custom_components.zenmaster.solver import dynamicprice as dp
from custom_components.zenmaster.solver.dynamicprice import DynamicPrice, ProviderError
from custom_components.zenmaster.solver.dynamicprices import octopus

NOW = datetime(2026, 6, 24, 15, 0, tzinfo=timezone.utc)  # slot 12; slot 0 is 12:00 UTC


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    async def json(self):
        return self._payload


class _Session:
    """Records every post with its headers and answers from a scripted list."""

    def __init__(self, script):
        self.script = script
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((json, headers))
        return _Resp(self.script.pop(0))


def _dp():
    o = DynamicPrice()
    o.email = "user@example.com"
    o.provider = "OctopusProvider"
    o.price_buy, o.price_sell = SimpleNamespace(data=np.zeros(7 * 96)), SimpleNamespace(data=np.zeros(7 * 96))
    return o


def _patch(monkeypatch, script):
    session = _Session(script)
    monkeypatch.setattr(octopus, "async_get_clientsession", lambda *a, **k: session)
    return session


async def _stub_base(self, dynamic_price, now):
    # the wholesale base the provider overlays: flat, with the DE feed-in tariff
    self.vat, self.export_factor, self.export_markup = 19.0, 0.0, 0.077
    self.tou = np.zeros(7 * 96)
    self.last_fetch = now
    return np.full(7 * 96, 0.10), np.full(7 * 96, 0.10)


def _rates(agreement):
    # the Rates answer with one metering point and an ended agreement before the active one
    return {"data": {"account": {"allProperties": [{"electricityMalos": [{"agreements": [{"isActive": False, "unitRateInformation": {"latestGrossUnitRateCentsPerKwh": 99}}, {"isActive": True, **agreement}]}]}]}}}


def _fetch(monkeypatch, agreement):
    monkeypatch.setattr(dp.Provider, "fetch", _stub_base)
    session = _patch(monkeypatch, [_rates(agreement)])
    o = _dp()
    o.token = {"token": "T1", "refreshToken": "R1", "account": "A-1"}
    provider = octopus.OctopusProvider()
    buy, sell = asyncio.run(provider.fetch(o, NOW))
    return provider, buy, sell, session


def test_login_keeps_the_token_pair_and_the_active_account(monkeypatch):
    session = _patch(monkeypatch, [
        {"data": {"obtainKrakenToken": {"token": "T1", "refreshToken": "R1"}}},
        {"data": {"viewer": {"accounts": [{"number": "A-OLD", "status": "WITHDRAWN"}, {"number": "A-1", "status": "ACTIVE"}]}}},
    ])
    o = _dp()
    assert asyncio.run(octopus.OctopusProvider().submit_otp(o, "secret")) is True
    assert o.token == {"token": "T1", "refreshToken": "R1", "account": "A-1"}  # the password itself is not kept
    assert session.calls[0] == ({"query": octopus._LOGIN, "variables": {"input": {"email": "user@example.com", "password": "secret"}}}, {})
    assert session.calls[1][1] == {"Authorization": "T1"}  # the accounts are asked with the new token


def test_login_reports_a_wrong_password(monkeypatch):
    _patch(monkeypatch, [{"errors": [{"message": "Invalid data.", "extensions": {"errorType": "VALIDATION", "errorCode": "KT-CT-1138"}}], "data": {"obtainKrakenToken": None}}])
    o = _dp()
    with pytest.raises(ProviderError, match="login failed"):
        asyncio.run(octopus.OctopusProvider().submit_otp(o, "wrong"))
    assert o.token == {}


def test_a_flat_rate_is_the_buy_price_over_the_whole_horizon(monkeypatch):
    provider, buy, sell, session = _fetch(monkeypatch, {"unitRateInformation": {"latestGrossUnitRateCentsPerKwh": 32.5}, "unitRateForecast": []})
    assert np.allclose(buy, 0.325)
    assert np.allclose(sell[12:], 0.077)  # feed-in from the country's export tariff, as the other providers
    assert provider.known_until == int(NOW.timestamp()) - 12 * 900 + 7 * 96 * 900
    assert session.calls[0][0]["variables"] == {"account": "A-1"} and session.calls[0][1] == {"Authorization": "T1"}


def test_time_of_use_rates_follow_their_slots_across_midnight(monkeypatch):
    rates = [
        {"latestGrossUnitRateCentsPerKwh": 20, "timeslotActivationRules": [{"activeFromTime": "00:00:00", "activeToTime": "05:00:00"}]},
        {"latestGrossUnitRateCentsPerKwh": 35, "timeslotActivationRules": [{"activeFromTime": "05:00:00", "activeToTime": "00:00:00"}]},
    ]
    _, buy, _, _ = _fetch(monkeypatch, {"unitRateInformation": {"rates": rates}, "unitRateForecast": None})
    assert buy[12] == pytest.approx(0.35)  # 15:00
    assert buy[48] == pytest.approx(0.20) and buy[67] == pytest.approx(0.20)  # 00:00 and 04:45 the next day
    assert buy[68] == pytest.approx(0.35)  # 05:00


def test_a_time_of_use_gap_is_refused(monkeypatch):
    rates = [{"latestGrossUnitRateCentsPerKwh": 20, "timeslotActivationRules": [{"activeFromTime": "00:00:00", "activeToTime": "05:00:00"}]}]
    with pytest.raises(ProviderError, match="gap"):
        _fetch(monkeypatch, {"unitRateInformation": {"rates": rates}, "unitRateForecast": []})


def test_forecast_periods_are_real_and_the_tail_is_forecast(monkeypatch):
    periods = [{"validFrom": (NOW + timedelta(hours=h)).isoformat(), "validTo": (NOW + timedelta(hours=h + 1)).isoformat(), "unitRateInformation": {"latestGrossUnitRateCentsPerKwh": 30 + 10 * h}} for h in range(2)]
    provider, buy, _, _ = _fetch(monkeypatch, {"unitRateInformation": {"latestGrossUnitRateCentsPerKwh": 99}, "unitRateForecast": periods})
    assert buy[12] == pytest.approx(0.30) and buy[16] == pytest.approx(0.40)
    assert provider.known_until == int(NOW.timestamp()) + 2 * 3600  # the current rate is no placeholder past the forecast
    assert not np.isclose(buy[20:], 0.99).any()


def test_an_expired_token_is_renewed_once(monkeypatch):
    session = _patch(monkeypatch, [
        {"errors": [{"message": "Signature of the JWT has expired.", "extensions": {"errorType": "AUTHORIZATION", "errorCode": "KT-CT-1120"}}]},
        {"data": {"obtainKrakenToken": {"token": "T2", "refreshToken": "R2"}}},
        {"data": {"viewer": {"accounts": []}}},
    ])
    o = _dp()
    o.token = {"token": "T1", "refreshToken": "R1", "account": "A-1"}
    stored = []
    async def store(hass):
        stored.append(dict(o.token))
    o._store = store
    asyncio.run(octopus.OctopusProvider()._post(o, octopus._ACCOUNTS, {}))
    assert o.token == {"token": "T2", "refreshToken": "R2", "account": "A-1"}
    assert stored == [o.token]  # the new pair is saved
    assert session.calls[1] == ({"query": octopus._LOGIN, "variables": {"input": {"refreshToken": "R1"}}}, {})  # renewal without the stale header
    assert session.calls[2][1] == {"Authorization": "T2"}  # the call goes again with the new token


def test_a_refused_renewal_asks_for_the_password(monkeypatch):
    _patch(monkeypatch, [
        {"errors": [{"extensions": {"errorType": "AUTHORIZATION", "errorCode": "KT-CT-1120"}}]},
        {"errors": [{"extensions": {"errorType": "VALIDATION"}}], "data": {"obtainKrakenToken": None}},
    ])
    o = _dp()
    o.token = {"token": "T1", "refreshToken": "R1", "account": "A-1"}
    with pytest.raises(ProviderError, match="login expired"):
        asyncio.run(octopus.OctopusProvider()._post(o, octopus._ACCOUNTS, {}))
