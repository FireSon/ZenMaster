"""Firmware from the account: the versions shown, the list a device fetches, the install and its progress."""

import asyncio

from test_local_api import Client, make_driver, make_solarflow
from custom_components.zenmaster.update import HaUpdate
from custom_components.zenmaster.zendure.device import ConnectionType
from custom_components.zenmaster.zendure.firmware import manifest, versions
from custom_components.zenmaster.zendure.local_api import answer

HaUpdate.add = staticmethod(lambda *a, **k: None)  # no HA platform in tests

# the account's answer for SF1600_62 (17 November), trimmed to what is used
ENTRY = {"id": 424, "upgradeVersion": "2.0.30", "firmwareList": [{"moduleName": "ESP32", "version": "3.0.21", "intVersion": "12309", "firmwareId": "1683", "fileLen": "1956576", "sign": "6681", "md5": "522cd16977f7d22ce1b721204b9e79b2", "firmwareUrl": "https://zendure-app-pre.s3.amazonaws.com/upload/2026-11-07/zenSDK_solarFlow1600AC_esp32c3_3.0.21_20261107_104342.bin"}]}


def test_manifest_points_at_the_local_api() -> None:
    m = manifest(ENTRY, "http://192.168.1.250:8123/api/zenmaster/zendure")
    assert m == {"id": 424, "protocol": "V_1", "moduleList": [{"module": "ESP32", "version": 12309, "firmwareId": 1683, "len": 1956576, "sign": 6681, "md5": "522cd16977f7d22ce1b721204b9e79b2", "firmwareUrl": "http://192.168.1.250:8123/api/zenmaster/zendure/fw/zenSDK_solarFlow1600AC_esp32c3_3.0.21_20261107_104342.bin"}]}


def test_versions_compare_with_what_the_device_runs() -> None:
    assert versions(ENTRY, 12306) == ("3.0.18", "3.0.21", "ESP32 3.0.21")
    assert versions(ENTRY, 12309)[:2] == ("3.0.21", "3.0.21")
    assert versions(ENTRY, 0)[:2] == (None, "3.0.21")  # zenSDK 2 does not report its version


async def test_install_and_progress() -> None:
    driver = make_driver()
    driver.hass.loop = asyncio.get_running_loop()
    device = make_solarflow(driver)
    device.ip = "192.168.1.24"
    client = Client()
    fetched = []

    async def fetch(entry):
        fetched.append(entry["id"])

    driver.catalog.fetch = fetch
    driver.catalog.entries = {device.sn: ENTRY}
    try:
        # the firmware report brings the running version and the entity
        driver._on_device_message(client, ConnectionType.LOCAL, "/65174u/Cd3wbW9V/firmware/report", "65174u", "Cd3wbW9V", "firmware/report", {"modules": [{"module": "ESP32", "version": 12306}]})
        entity = device.firmware
        assert (entity._attr_installed_version, entity._attr_latest_version) == ("3.0.18", "3.0.21")

        # install: the files fetched, the list at the API, the device told
        await entity.async_install(None, False)
        assert fetched == [424] and entity._attr_in_progress
        assert client.sent[-1][0] == "iot/65174u/Cd3wbW9V/firmware/download"
        status, data = answer(driver, "192.168.1.24", "zen-iot/ota/preFetch", {"deviceKey": "Cd3wbW9V"})
        assert status == 200 and data["data"]["id"] == 424

        # progress, then the end clears the pending update
        driver._on_device_message(client, ConnectionType.LOCAL, "/65174u/Cd3wbW9V/firmware/download/progress", "65174u", "Cd3wbW9V", "firmware/download/progress", {"progress": 40, "success": True})
        assert entity._attr_update_percentage == 40
        driver._on_device_message(client, ConnectionType.LOCAL, "/65174u/Cd3wbW9V/firmware/upgrade/progress", "65174u", "Cd3wbW9V", "firmware/upgrade/progress", {"progress": 100, "success": True, "complete": 1})
        assert not entity._attr_in_progress and device.ota is None
    finally:
        device.unload()
