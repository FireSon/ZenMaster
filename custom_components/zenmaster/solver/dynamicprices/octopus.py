from __future__ import annotations

import asyncio
from datetime import datetime, time

import aiohttp
from aiohttp import ClientTimeout
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ..dynamicprice import DynamicPrice, Provider, ProviderError

_URL = "https://api.oeg-kraken.energy/v1/graphql/"
_HTTP_TIMEOUT = ClientTimeout(total=10)
_LOGIN = "mutation Login($input: ObtainJSONWebTokenInput!) { obtainKrakenToken(input: $input) { token refreshToken } }"
_ACCOUNTS = "query Accounts { viewer { accounts { number status } } }"
# the agreements with their current rate (flat or time of use) and the forecast periods of a dynamic tariff
_RATES = """
query Rates($account: String!) {
  account(accountNumber: $account) {
    allProperties { electricityMalos { agreements { isActive unitRateInformation { ...Rate } unitRateForecast { validFrom validTo unitRateInformation { ...Rate } } } } }
  }
}
fragment Rate on UnitRateInformation {
  ... on SimpleProductUnitRateInformation { latestGrossUnitRateCentsPerKwh }
  ... on TimeOfUseProductUnitRateInformation { rates { latestGrossUnitRateCentsPerKwh timeslotActivationRules { activeFromTime activeToTime } } }
}
"""


def _quarters(info: dict) -> list[float]:
    # the 96 quarter prices of a day in EUR/kWh: one flat rate, or each time-of-use rate over its slots
    if (cents := info.get("latestGrossUnitRateCentsPerKwh")) is not None:
        return [float(cents) / 100.0] * 96
    day = [None] * 96
    for rate in info["rates"]:
        for rule in rate["timeslotActivationRules"]:
            start, end = (time.fromisoformat(rule[k]) for k in ("activeFromTime", "activeToTime"))
            first = start.hour * 4 + start.minute // 15
            for i in range((end.hour * 4 + end.minute // 15 - first) % 96 or 96):
                day[(first + i) % 96] = float(rate["latestGrossUnitRateCentsPerKwh"]) / 100.0
    if None in day:
        raise ProviderError("Octopus: the time-of-use rates leave a gap in the day")
    return day


class OctopusProvider(Provider):
    async def fetch(self, dynamic_price: DynamicPrice, now: datetime):
        buy, sell = await super().fetch(dynamic_price, now)  # raw wholesale forecast in working copies

        # the account from the login, and the agreements of its metering points
        if not (account := dynamic_price.token.get("account")):
            raise ProviderError("Octopus: not linked, enter the password on the price card")
        payload = await self._post(dynamic_price, _RATES, {"account": account})
        properties = ((payload.get("data") or {}).get("account") or {}).get("allProperties") or []
        malos = [m for p in properties for m in p.get("electricityMalos") or []]
        if not (agreement := next((a for m in malos for a in m.get("agreements") or [] if a.get("isActive")), None)):
            raise ProviderError("Octopus: no active agreement on this account")

        # each forecast period of a dynamic tariff, else the current rate over the whole horizon (slot 12 = the quarter containing now)
        base = int(now.timestamp()) // 900 * 900 - 12 * 900
        periods = agreement.get("unitRateForecast") or [{"unitRateInformation": agreement["unitRateInformation"]}]
        tail, delta_sum, n = 12, 0.0, 0
        try:
            for period in periods:
                day = _quarters(period["unitRateInformation"])
                start = (int(datetime.fromisoformat(period["validFrom"]).timestamp()) - base) // 900 if period.get("validFrom") else 0
                end = (int(datetime.fromisoformat(period["validTo"]).timestamp()) - base) // 900 if period.get("validTo") else buy.size
                for slot in range(max(0, start), min(buy.size, end)):
                    local = datetime.fromtimestamp(base + slot * 900, now.tzinfo)
                    buy[slot] = day[local.hour * 4 + local.minute // 15]
                    delta_sum += buy[slot] - sell[slot]
                    n += 1
                    tail = max(tail, slot + 1)
        except (KeyError, TypeError, ValueError) as err:
            raise ProviderError(f"Octopus rates malformed: {err}") from err

        # forecast tail past the periods: real day's intraday shape plus the average buy-spot delta; feed-in from the country's export tariff
        self.extend_tail(sell, tail, base, "sell")
        buy[tail:] = sell[tail:] + (delta_sum / n if n else 0.0)
        sell[12:] = sell[12:] * self.export_factor + self.export_markup
        self.extend_buy(buy, sell, tail, base)
        return buy, sell

    async def _post(self, dynamic_price, query: str, variables: dict, retry: bool = True) -> dict:
        # one GraphQL call; login and renewal go without the token, since Kraken rejects any call with a bad header
        auth = dynamic_price.token.get("token") if query is not _LOGIN else None
        headers = {"Authorization": auth} if auth else {}
        try:
            session = async_get_clientsession(dynamic_price.hass)  # HA's shared session, never closed here
            async with session.post(_URL, json={"query": query, "variables": variables}, headers=headers, timeout=_HTTP_TIMEOUT) as resp:
                resp.raise_for_status()
                payload = await resp.json() or {}
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise ProviderError(f"Octopus fetch failed: {err}") from err
        # Kraken answers 200 with errorType AUTHORIZATION; an expired token is renewed once
        if any((e.get("extensions") or {}).get("errorType") == "AUTHORIZATION" for e in payload.get("errors") or []):
            if retry and auth and await self.refresh(dynamic_price):
                await dynamic_price._store(dynamic_price.hass)
                return await self._post(dynamic_price, query, variables, retry=False)
            raise ProviderError("Octopus: login expired, enter the password on the price card again")
        return payload

    async def submit_otp(self, dynamic_price, password: str) -> bool:
        # the password from the price card logs in once and is never stored; kept are the token pair and the account
        if not dynamic_price.email:
            raise ProviderError("Octopus: missing 'email' in config")
        dynamic_price.token = {}
        payload = await self._post(dynamic_price, _LOGIN, {"input": {"email": dynamic_price.email, "password": password}}, retry=False)
        if not (login := (payload.get("data") or {}).get("obtainKrakenToken")):
            raise ProviderError(f"Octopus login failed: {str(payload.get('errors'))[:200]}")
        dynamic_price.token = {"token": login["token"], "refreshToken": login["refreshToken"]}
        # the active account, else the first one
        accounts = (((await self._post(dynamic_price, _ACCOUNTS, {})).get("data") or {}).get("viewer") or {}).get("accounts") or []
        if not (found := next((a for a in accounts if a.get("status") == "ACTIVE"), accounts[0] if accounts else None)):
            raise ProviderError("Octopus: no account on this login")
        dynamic_price.token["account"] = found["number"]
        return True

    async def refresh(self, dynamic_price) -> bool:
        # trade the refresh token for a new pair; the account stays
        token = dynamic_price.token
        payload = await self._post(dynamic_price, _LOGIN, {"input": {"refreshToken": token.get("refreshToken")}}, retry=False)
        if not (renewed := (payload.get("data") or {}).get("obtainKrakenToken")):
            return False
        token.update(token=renewed["token"], refreshToken=renewed["refreshToken"])
        return True
