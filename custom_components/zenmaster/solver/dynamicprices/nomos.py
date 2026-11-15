from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import aiohttp
from aiohttp import ClientTimeout
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ..dynamicprice import DynamicPrice, Provider, ProviderError

_NOMOS_URL = "https://api.nomos.energy"
_HTTP_TIMEOUT = ClientTimeout(total=10)


class NomosProvider(Provider):
    async def fetch(self, dynamic_price: DynamicPrice, now: datetime):
        buy, sell = await super().fetch(dynamic_price, now)  # raw wholesale forecast in working copies

        # a fresh client-credentials token per refresh: it lives an hour, a refresh needs it only once
        session = async_get_clientsession(dynamic_price.hass)  # HA's shared session, never closed here
        auth = aiohttp.BasicAuth(dynamic_price.client_id, dynamic_price.client_secret)
        params = {"start": now.date().isoformat(), "end": (now.date() + timedelta(days=1)).isoformat()}
        try:
            async with session.post(f"{_NOMOS_URL}/oauth/token", data={"grant_type": "client_credentials"}, auth=auth, timeout=_HTTP_TIMEOUT) as resp:
                resp.raise_for_status()
                headers = {"Authorization": f"Bearer {(await resp.json())['access_token']}"}
            # the prices belong to a subscription: the active one of this account
            async with session.get(f"{_NOMOS_URL}/subscriptions", headers=headers, timeout=_HTTP_TIMEOUT) as resp:
                resp.raise_for_status()
                if not (sub := next((s["id"] for s in (await resp.json())["items"] if s.get("status") == "active"), None)):
                    raise ProviderError("NOMOS: no active subscription")
            async with session.get(f"{_NOMOS_URL}/subscriptions/{sub}/prices", params=params, headers=headers, timeout=_HTTP_TIMEOUT) as resp:
                resp.raise_for_status()
                price = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise ProviderError(f"NOMOS fetch failed: {err}") from err
        except (KeyError, TypeError) as err:
            raise ProviderError(f"NOMOS payload malformed: {err}") from err

        # every item in ct/kWh incl. VAT: the total is the buy price, its electricity share the spot that shapes the tail
        size = buy.size
        step = 1 if price.get("resolution") == "15min" else 4
        base = int(now.timestamp()) // 900 * 900 - 12 * 900  # slot 12 = the quarter containing now
        tail, delta_sum, n = 12, 0.0, 0
        for item in price.get("items") or []:
            try:
                if item["amount"] is None:
                    continue
                total = float(item["amount"]) / 100.0
                energy = next((c["amount"] for c in item["components"] if c["type"] == "electricity"), None)
                start = int(datetime.fromisoformat(item["timestamp"]).timestamp())
            except (KeyError, TypeError, ValueError) as err:
                raise ProviderError(f"NOMOS item malformed: {err}") from err
            idx = (start - base) // 900
            for slot in range(idx, idx + step):
                if 0 <= slot < size:
                    buy[slot] = total
                    if energy is not None:
                        sell[slot] = float(energy) / 100.0
                    delta_sum += total - sell[slot]
                    n += 1
                    tail = max(tail, slot + 1)

        # forecast tail: real day's intraday shape, then extend with the average buy-spot delta; feed-in from the country's export tariff
        self.extend_tail(sell, tail, base, "sell")
        buy[tail:] = sell[tail:] + (delta_sum / n if n else 0.0)
        sell[12:] = sell[12:] * self.export_factor + self.export_markup
        self.extend_buy(buy, sell, tail, base)
        return buy, sell
