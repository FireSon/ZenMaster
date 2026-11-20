"""Local control: the register reply with the API address, and the API ZenMaster answers for its own devices."""

import json
from types import SimpleNamespace

from conftest import fake_hass
from custom_components.zenmaster.binary_sensor import HaBinarySensor
from custom_components.zenmaster.distribution import Distribution
from custom_components.zenmaster.number import HaNumber
from custom_components.zenmaster.select import HaSelect
from custom_components.zenmaster.sensor import HaSensor
from custom_components.zenmaster.zendure.device import ConnectionType
from custom_components.zenmaster.zendure.driver import ZendureDriver
from custom_components.zenmaster.zendure.local_api import answer, esp_version, firmware_file, hems_id

for cls in (HaSensor, HaNumber, HaBinarySensor, HaSelect):
    cls.add = staticmethod(lambda *a, **k: None)  # no HA platform in tests


def make_driver() -> ZendureDriver:
    hass = fake_hass()
    driver = ZendureDriver(hass, "entry", [], Distribution(hass, "entry"))
    driver.api_url = "http://192.168.1.250:8123/api/zenmaster/zendure"
    driver.broker_host = "192.168.1.250"
    return driver


def make_device(**kw) -> SimpleNamespace:
    # what the API reads of a device: address, keys, firmware and the two SoC knobs in percent
    knobs = {"minSoc": SimpleNamespace(_attr_native_value=10, factor=0.1), "socSet": SimpleNamespace(_attr_native_value=94, factor=0.1)}
    return SimpleNamespace(**{"ip": "192.168.1.24", "device_key": "Cd3wbW9V", "sn": "HED4NENBN470562", "esp_version": 12309, "ota": None, "entities": knobs, **kw})


class Client:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    def publish(self, topic: str, payload: str) -> None:
        self.sent.append((topic, json.loads(payload)))


def test_register_reply_carries_the_api_address_and_discovers() -> None:
    driver = make_driver()
    found: list = []
    driver.on_discovered = found.append
    client = Client()
    driver._on_device_message(client, ConnectionType.LOCAL, "/65174u/Cd3wbW9V/register", "65174u", "Cd3wbW9V", "register", {"params": {"deviceSn": "HED4NENBN470562", "token": "tok"}})
    topic, reply = client.sent[0]
    assert topic == "iot/65174u/Cd3wbW9V/register/replay"
    assert reply["result"] == 0 and reply["token"] == "tok"
    assert reply["params"] == {"result": 0, "token": "tok", "apiUrl": driver.api_url, "userId": 38555}
    assert found[0]["sn"] == "HED4NENBN470562" and found[0]["product"] == "solarflow1600ac+"


def test_api_answers_only_known_devices() -> None:
    driver = make_driver()
    driver.devices["HED4NENBN470562"] = make_device()
    assert answer(driver, "192.168.1.99", "zen-iot/platform/config", {"deviceKey": "Cd3wbW9V"})[0] == 404
    assert answer(driver, "192.168.1.24", "zen-iot/platform/config", {"deviceKey": "other"})[0] == 404
    status, data = answer(driver, "192.168.1.24", "zen-iot/platform/config", {"deviceKey": "Cd3wbW9V"})
    assert status == 200 and data["data"]["configs"] == {"realTimeRate": 2, "mqttUrl": "192.168.1.250"}
    assert answer(driver, "192.168.1.24", "tdengine/zen-iot/report/energy", {"deviceKey": "Cd3wbW9V"})[0] == 404


def test_hems_base_for_zensdk3_with_the_device_limits() -> None:
    driver = make_driver()
    driver.devices["a"] = make_device()
    status, data = answer(driver, "192.168.1.24", "zen-iot/hems/base", {"deviceKey": "Cd3wbW9V"})
    assert status == 200 and data["data"] == {"hemsId": hems_id("HED4NENBN470562"), "minSoc": 100, "socSet": 940}
    # zenSDK 2 gets no hemsId: it needs mode and chargeMode, not tested there
    driver.devices["a"] = make_device(esp_version=0)
    assert answer(driver, "192.168.1.24", "zen-iot/hems/base", {"deviceKey": "Cd3wbW9V"})[0] == 404


def test_hems_id_and_version() -> None:
    assert hems_id("HED4NENBN470562") == hems_id("HED4NENBN470562")
    assert 100000 <= hems_id("WOB1NHMCN032288") < 1000000 and hems_id("WOB1NHMCN032288") != hems_id("WOB1NHMCN032390")
    assert esp_version(12309) == "3.0.21" and esp_version(12306) == "3.0.18"


def test_ota_list_and_files_only_for_the_pending_device(tmp_path) -> None:
    driver = make_driver()
    driver.firmware_dir = str(tmp_path)
    (tmp_path / "fw.bin").write_bytes(b"x")
    (tmp_path / "other.bin").write_bytes(b"y")
    ota = {"id": 424, "protocol": "V_1", "moduleList": [{"module": "ESP32", "firmwareUrl": "http://h/api/zenmaster/zendure/fw/fw.bin"}]}
    driver.devices["a"] = make_device(ota=ota)
    assert answer(driver, "192.168.1.24", "zen-iot/ota/preFetch", {"deviceKey": "Cd3wbW9V"})[1]["data"] == ota
    assert firmware_file(driver, "192.168.1.24", "fw.bin") == str(tmp_path / "fw.bin")
    assert firmware_file(driver, "192.168.1.24", "other.bin") is None
    assert firmware_file(driver, "192.168.1.24", "../secrets.bin") is None
    assert firmware_file(driver, "192.168.1.99", "fw.bin") is None
