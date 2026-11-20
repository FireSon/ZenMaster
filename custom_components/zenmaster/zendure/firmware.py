"""Firmware from the Zendure account: the catalog once a week, the files when an update is installed, served to the device by the local API."""

from __future__ import annotations

import hashlib
import logging
import os
from typing import TYPE_CHECKING, Any

from aiohttp import ClientTimeout
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .local_api import esp_version

if TYPE_CHECKING:
    from .driver import ZendureDriver

_LOGGER = logging.getLogger(__name__)

BASES = {"app": "https://app.zendure.tech/eu", "pre": "http://pre.zendure.tech/eu"}  # production, and the test environment with the betas
APP_ID = "121c83f761305d6cf7e"
USER_AGENT = "Zendure/4.3.1 (iPhone; iOS 14.4.2; Scale/3.00)"


def file_name(module: dict) -> str:
    return str(module["firmwareUrl"]).split("?")[0].rsplit("/", 1)[-1]


def manifest(entry: dict, api_url: str) -> dict:
    # the list a device fetches from ota/preFetch: the catalog's modules, each file at the local API
    modules = [{"module": m["moduleName"], "version": int(m["intVersion"]), "firmwareId": int(m["firmwareId"]), "len": int(m["fileLen"]), "sign": int(m["sign"]), "md5": m["md5"], "firmwareUrl": f"{api_url}/fw/{file_name(m)}"} for m in entry.get("firmwareList") or []]
    return {"id": int(entry["id"]), "protocol": "V_1", "moduleList": modules}


def versions(entry: dict | None, running: int) -> tuple[str | None, str | None, str | None]:
    # installed and latest zenSDK version, and the modules on offer; the cloud no longer knows what runs, the device does
    installed = esp_version(running) if running else None
    modules = (entry or {}).get("firmwareList") or []
    esp = next((int(m["intVersion"]) for m in modules if m.get("moduleName") == "ESP32"), 0)
    latest = esp_version(esp) if esp > running else installed
    return installed, latest, ", ".join(f"{m['moduleName']} {m['version']}" for m in modules) or None


class FirmwareCatalog:
    """The account's firmware list per device serial, and the files fetched with an md5 check."""

    def __init__(self, driver: ZendureDriver) -> None:
        self.driver = driver
        self.entries: dict[str, dict] = {}  # serial -> ota/batch/firmware answer

    async def _post(self, path: str, body: dict, token: str | None) -> Any:
        # every account call is a JSON POST with the bearer token in Blade-Auth; without appVersion the server refuses
        headers = {"Content-Type": "application/json", "Accept-Language": "en-EN", "appVersion": "4.3.1", "User-Agent": USER_AGENT, "Accept": "*/*", "Blade-Auth": f"bearer {token or '(null)'}"}
        base = BASES.get(self.driver.account_env, BASES["app"])
        async with async_get_clientsession(self.driver.hass).post(base + path, json=body, headers=headers, timeout=ClientTimeout(total=30)) as response:
            res = await response.json(content_type=None)
        if not res.get("success") or (data := res.get("data")) is None:
            raise HomeAssistantError(f"Zendure account {path}: code {res.get('code')} {res.get('msg')}")
        return data

    async def refresh(self) -> None:
        # log in, list the account's devices and ask the firmware list for each
        login = {"account": self.driver.account, "password": self.driver.account_psw, "appId": APP_ID, "appType": "iOS", "grantType": "password", "tenantId": ""}
        token = (await self._post("/auth/app/token", login, None))["accessToken"]
        entries = {}
        for d in await self._post("/productModule/device/queryDeviceListByConsumerId", {}, token):
            if sn := d.get("snNumber"):
                entries[sn] = await self._post("/ota/batch/firmware", {"deviceId": d["id"]}, token)
        self.entries = entries
        _LOGGER.info("Zendure firmware catalog: %d device(s)", len(entries))

    async def fetch(self, entry: dict) -> None:
        # every file of the entry in the firmware folder, downloaded once and checked against the catalog's md5
        session = async_get_clientsession(self.driver.hass)
        for m in entry.get("firmwareList") or []:
            path = os.path.join(self.driver.firmware_dir, file_name(m))
            if not await self.driver.hass.async_add_executor_job(os.path.isfile, path):
                async with session.get(m["firmwareUrl"], timeout=ClientTimeout(total=300)) as response:
                    response.raise_for_status()
                    data = await response.read()
                if hashlib.md5(data).hexdigest() != str(m["md5"]).lower():
                    raise HomeAssistantError(f"Zendure firmware {file_name(m)}: md5 does not match")
                await self.driver.hass.async_add_executor_job(self._write, path, data)

    @staticmethod
    def _write(path: str, data: bytes) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
