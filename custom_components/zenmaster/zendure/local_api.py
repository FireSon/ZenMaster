"""The cloud API of the Zendure devices, answered by ZenMaster itself: platform config, HEMS base and firmware."""

from __future__ import annotations

import json
import logging
import os
import re
import zlib
from typing import TYPE_CHECKING, Any

from aiohttp import web
from homeassistant.helpers.http import HomeAssistantView
from homeassistant.helpers.network import is_cloud_connection

from ..const import DOMAIN

if TYPE_CHECKING:
    from .driver import ZendureDriver

_LOGGER = logging.getLogger(__name__)

API_PATH = f"/api/{DOMAIN}/zendure"
REAL_TIME_RATE = 2  # seconds between the reports of a zenSDK 2 device; zenSDK 3 never fetches the platform config
NOT_HERE = (404, {"code": 404, "success": False, "msg": "not here"})


def hems_id(sn: str) -> int:
    # a fixed id per device, derived from its serial: unique in practice, so no two devices form an LCN cluster
    return 100000 + zlib.crc32(sn.encode()) % 900000


def esp_version(version: int) -> str:
    # the firmware number as the device reports it (major << 12 | minor << 8 | patch) as 3.0.21
    return f"{version >> 12}.{version >> 8 & 15}.{version & 255}"


def device_at(driver: ZendureDriver, ip: str, body: dict) -> Any:
    # the device the request comes from: its own address, and the device key it sends must be its own
    if (d := next((d for d in driver.devices.values() if getattr(d, "ip", None) == ip), None)) is None:
        return None
    if (key := str(body.get("deviceKey") or "")) and d.device_key and key != d.device_key:
        return None
    return d


def soc_raw(device: Any, key: str) -> int | None:
    # a SoC limit in the unit of the device (permille), from the number the user sees in percent
    if (e := device.entities.get(key)) is None or getattr(e, "_attr_native_value", None) is None:
        return None
    return int(round(e._attr_native_value / e.factor))


def answer(driver: ZendureDriver, ip: str, path: str, body: dict) -> tuple[int, dict]:
    # one cloud call of a device on the LAN; anything ZenMaster does not serve gets a 404 and changes nothing
    if (d := device_at(driver, ip, body)) is None:
        return NOT_HERE
    ok = {"code": 200, "success": True, "msg": ""}
    if path.endswith("zen-iot/platform/config"):
        # zenSDK 2 takes its broker from here once it has a platform config, so the broker is always in it
        return 200, {**ok, "data": {"configs": {"realTimeRate": REAL_TIME_RATE, "mqttUrl": driver.broker_host}}}
    if path.endswith("zen-iot/platform/config/reply"):
        return 200, ok
    if path.endswith("zen-iot/hems/base") and d.esp_version >= 0x3000:
        # the hemsId switches on the 1.5 s energy report; HEMS keeps the limits of the device itself
        data = {"hemsId": hems_id(d.sn), **{k: v for k in ("minSoc", "socSet") if (v := soc_raw(d, k)) is not None}}
        return 200, {**ok, "data": data}
    if path.endswith("zen-iot/ota/preFetch") and d.ota:
        return 200, {**ok, "data": d.ota}
    return NOT_HERE


def firmware_file(driver: ZendureDriver, ip: str, name: str) -> str | None:
    # a firmware file only for the device that has it in its pending update
    if (d := device_at(driver, ip, {})) is None or not d.ota or not re.fullmatch(r"[\w.\-]+\.bin", name):
        return None
    if name not in {m["firmwareUrl"].rsplit("/", 1)[-1] for m in d.ota["moduleList"]}:
        return None
    return path if os.path.isfile(path := os.path.join(driver.firmware_dir, name)) else None


class ZendureApiView(HomeAssistantView):
    """The API address the devices get at registration; only devices of the fleet get an answer."""

    url = f"{API_PATH}/{{path:.*}}"
    name = f"api:{DOMAIN}:zendure"
    requires_auth = False  # the devices cannot log in; the address and device key of the fleet stand in for it

    def __init__(self, hass: Any) -> None:
        self.hass = hass

    def _driver(self) -> ZendureDriver | None:
        # the driver of the running coordinator: a reload builds a new one, the view stays registered
        return next((c.drivers["zendure"] for c in self.hass.data.get(DOMAIN, {}).values() if hasattr(c, "drivers")), None)

    async def post(self, request: web.Request, path: str) -> web.Response:
        # a request through Home Assistant Cloud never comes from a device
        if is_cloud_connection(self.hass) or (driver := self._driver()) is None:
            return web.json_response(NOT_HERE[1], status=404)
        try:
            body = json.loads(await request.text() or "{}")
        except ValueError:
            body = {}
        status, data = answer(driver, request.remote or "", path, body if isinstance(body, dict) else {})
        _LOGGER.debug("Zendure API %s %s -> %s", request.remote, path, status)
        return web.json_response(data, status=status)

    async def get(self, request: web.Request, path: str) -> web.StreamResponse:
        # the firmware files of a pending update, with Range so a device can resume a broken download
        if is_cloud_connection(self.hass) or (driver := self._driver()) is None or not path.startswith("fw/"):
            return web.Response(status=404)
        if (file := firmware_file(driver, request.remote or "", path[3:])) is None:
            return web.Response(status=404)
        return web.FileResponse(file)
