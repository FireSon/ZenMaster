from __future__ import annotations

import json
import logging
from datetime import datetime

import aiohttp
from aiohttp import ClientTimeout
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ..dynamicprice import DynamicPrice, Provider, ProviderError

_LOGGER = logging.getLogger(__name__)

_URL = "https://api.oeg-kraken.energy/v1/graphql/"
_HTTP_TIMEOUT = ClientTimeout(total=15)

_LOGIN = """
mutation Login($email: String!, $password: String!) {
  obtainKrakenToken(input:{email:$email,password:$password}) {
    token
  }
}
"""

_ACCOUNTS = """
query {
  viewer {
    accounts {
      number
      status
    }
  }
}
"""

_COMPREHENSIVE_QUERY = """
query ComprehensiveDataQuery($accountNumber: String!) {
  account(accountNumber: $accountNumber) {
    id
    allProperties {
      id
      electricityMalos {
        maloNumber
        agreements {
          isActive
          isRevoked
          isTerminated
          validFrom
          validTo
          product {
            code
            description
            fullName
            isTimeOfUse
          }
          unitRateInformation {
            ... on SimpleProductUnitRateInformation {
              __typename
              latestGrossUnitRateCentsPerKwh
            }
            ... on TimeOfUseProductUnitRateInformation {
              __typename
              rates {
                timeslotName
                latestGrossUnitRateCentsPerKwh
                timeslotActivationRules {
                  activeFromTime
                  activeToTime
                }
              }
            }
          }
          unitRateForecast {
            validFrom
            validTo
          }
        }
      }
    }
  }
}
"""


class OctopusProvider(Provider):
    async def fetch(self, dynamic_price: DynamicPrice, now: datetime):
        buy, sell = await super().fetch(dynamic_price, now)

        account = dynamic_price.token.get("account")
        if not account:
            raise ProviderError("Missing Octopus account")

        payload = await self._post(
            dynamic_price, _COMPREHENSIVE_QUERY, {"accountNumber": account}
        )

        _LOGGER.warning(
            "===== OCTOPUS RAW RESPONSE =====\n%s", json.dumps(payload, indent=2)
        )

        active_agreement = None

        for property_data in (
            payload.get("data", {}).get("account", {}).get("allProperties", [])
        ):
            for malo in property_data.get("electricityMalos", []):
                for agreement in malo.get("agreements", []):
                    self._log_agreement(agreement)
                    if agreement.get("isActive"):
                        active_agreement = agreement

        if not active_agreement:
            raise ProviderError("No active agreement found")

        unit_info = active_agreement.get("unitRateInformation", {})
        typename = unit_info.get("__typename")

        if typename == "SimpleProductUnitRateInformation":
            price = float(unit_info["latestGrossUnitRateCentsPerKwh"]) / 100.0
            buy[:] = price
            sell[:] = self.export_markup * (1 + self.vat / 100.0)

        elif typename == "TimeOfUseProductUnitRateInformation":
            rules = []
            for rate in unit_info.get("rates", []):
                try:
                    price = float(rate["latestGrossUnitRateCentsPerKwh"]) / 100.0
                except Exception:
                    continue

                for activation in rate.get("timeslotActivationRules", []):
                    rules.append(
                        {
                            "price": price,
                            "from": activation.get("activeFromTime"),
                            "to": activation.get("activeToTime"),
                        }
                    )

            base = int(now.timestamp()) // 900 * 900 - 12 * 900

            for slot in range(buy.size):
                ts = base + slot * 900
                local = datetime.fromtimestamp(ts, now.tzinfo)
                minute = local.hour * 60 + local.minute

                for rule in rules:
                    sh, sm, _ = map(int, rule["from"].split(":"))
                    eh, em, _ = map(int, rule["to"].split(":"))
                    start = sh * 60 + sm
                    end = eh * 60 + em

                    if self._time_match(minute, start, end):
                        buy[slot] = rule["price"]
                        sell[slot] = self.export_markup * (1 + self.vat / 100.0)

                        break

        self.known_until = now.timestamp() + buy.size * 900
        return buy, sell

    async def _post(self, dynamic_price, query, variables, auth=True):
        token = dynamic_price.token.get("token")
        headers = {"Authorization": token} if auth and token else {}
        session = async_get_clientsession(dynamic_price.hass)

        async with session.post(
            _URL,
            json={"query": query, "variables": variables},
            headers=headers,
            timeout=_HTTP_TIMEOUT,
        ) as resp:
            return json.loads(await resp.text())

    async def submit_otp(self, dynamic_price, password: str) -> bool:
        payload = await self._post(
            dynamic_price,
            _LOGIN,
            {"email": dynamic_price.email, "password": password},
            auth=False,
        )

        token = payload.get("data", {}).get("obtainKrakenToken", {}).get("token")
        if not token:
            raise ProviderError(f"Login failed: {payload}")

        dynamic_price.token = {"token": token}

        accounts = await self._post(dynamic_price, _ACCOUNTS, {})
        dynamic_price.token["account"] = accounts["data"]["viewer"]["accounts"][0][
            "number"
        ]
        return True

    @staticmethod
    def _time_match(minute: int, start: int, end: int) -> bool:
        if start < end:
            return start <= minute < end
        return minute >= start or minute < end

    def _log_agreement(self, agreement: dict) -> None:
        _LOGGER.warning("===== OCTOPUS AGREEMENT =====")
        _LOGGER.warning(json.dumps(agreement, indent=2))
