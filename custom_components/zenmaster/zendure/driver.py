"""ZendureDriver: connects to MQTT, discovers devices, routes telemetry."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import random
import time
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from types import SimpleNamespace
from typing import Any, NamedTuple

import paho.mqtt.client as mqtt
from aiohttp import ClientTimeout
from bleak import BleakClient
from bleak_retry_connector import establish_connection
from homeassistant.components import bluetooth, network
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval

from ..config import ConfigField
from ..const import DOMAIN
from ..device import DeviceState, DistributionDevice
from ..driver import Driver
from ..entity import EntityDevice
from ..update import HaUpdate
from .battery import ZendureBattery
from .broker import ZendureBroker, masked
from .device import COMMAND_CHAR, READ_CHAR, ConnectionType, ZendureDevice
from .firmware import FirmwareCatalog, manifest, versions
from .local_api import API_PATH, ZendureApiView
from .smartmeter import ZendureSmartMeter
from .solarflow import ZendureHub, ZendureHyper, ZendureLegacy, ZendureSolarFlow

_LOGGER = logging.getLogger(__name__)

SF_HAKEY = "C*dafwArEOXK"
USER_ID = 38555  # the cloud sends this account id in its replies (captured 2026-08-24); the device seems to expect it


class Product(NamedTuple):
    cls: type
    family: str  # names the device (family_<serial tail>)
    pwr_max: int = 0  # the two powers belong to a battery device; a smart meter has none
    pwr_min: int = 0
    prodkeys: tuple[str, ...] = ()  # the cloud and the BLE handshake name no product, only one of these keys


# one table for every product; the key is the product string the device reports over zenSDK, mDNS or MQTT
PRODUCTS: dict[str, Product] = {
    "solarflow2400ac": Product(ZendureSolarFlow, "SF2400", 2400, -2400, ("bc8b7f",)),
    "solarflow2400ac+": Product(ZendureSolarFlow, "SF2400", 2400, -2400, ("5fg27j",)),
    "solarflow2400pro": Product(ZendureSolarFlow, "SF2400", 2400, -2400, ("2qe7c9",)),
    "solarflow1600ac+": Product(ZendureSolarFlow, "SF1600", 1600, -1600, ("65174u",)),
    "solarflow800": Product(ZendureSolarFlow, "SF800", 800, -1000, ("a4ss5p", "b1nhmc", "n8sky9")),
    "solarflow800pro": Product(ZendureSolarFlow, "SF800", 800, -1000, ("r3mn8u",)),
    "solarflow800pro2": Product(ZendureSolarFlow, "SF800", 800, -1000, ("nvyeqm",)),
    "solarflow800plus": Product(ZendureSolarFlow, "SF800", 800, -800, ("8n77v3",)),
    "solarflow4000mixac+": Product(ZendureSolarFlow, "SF4000", 3600, -3600, ("gvvy0c",)),
    "solarflow4000mixpro": Product(ZendureSolarFlow, "SF4000", 3600, -3600, ("k50hwq",)),
    "solarflow3000mixac+": Product(ZendureSolarFlow, "SF3000", 3000, -3000, ("g0wyvd",)),
    "hyper2000": Product(ZendureHyper, "Hyper2000", 1200, -1200, ("gda3tb", "b3dxda", "ja72u0")),
    "solarflowpro": Product(ZendureHyper, "Hyper2000", 1200, -1200),  # what a Hyper 2000 calls itself on the broker
    "hub1200": Product(ZendureHub, "Hub1200", 1200, 0, ("73bktv",)),
    "hub2000": Product(ZendureHub, "Hub2000", 1200, 0, ("a8yh63",)),
    "aio2400": Product(ZendureLegacy, "AIO2400", 1200, 0, ("ywf7hv",)),
    "ace1500": Product(ZendureLegacy, "Ace1500", 800, -900, ("8bm93h",)),
    "smartmeter3ct": Product(ZendureSmartMeter, "Meter3CT", 0, 0, ("c3yt68", "14yyd8")),
    "smartmeter3ct-s": Product(ZendureSmartMeter, "Meter3CTS", 0, 0, ("y6hvtw",)),
    "smartmeterp1": Product(ZendureSmartMeter, "MeterP1", 0, 0, ("q331b1",)),
    "smartplug": Product(ZendureSmartMeter, "Plug", 0, 0, ("1dmcr8",)),
    "smartct": Product(ZendureSmartMeter, "MeterCT", 0, 0, ("vv1wd7",)),
}
PRODKEYS: dict[str, str] = {key: product for product, row in PRODUCTS.items() for key in row.prodkeys}


def find_product(product: str) -> Product | None:
    # the product string is written differently per route ("SolarFlow 800 Pro" from the cloud, "solarFlow800Pro" locally)
    return PRODUCTS.get(product.lower().replace(" ", ""))


class ZendureDriver(Driver):
    """Connects to MQTT, discovers devices, routes telemetry; the coordinator turns its finds into subentries."""

    id = "zendure"  # the settings section and the subentry type of the devices
    manufacturer = "Zendure"
    LOGO = "zendure_ha"
    ZEROCONF = [{"type": "_http._tcp.local.", "name": "zendure-*"}, {"type": "_zendure._tcp.local.", "name": "zendure-*"}]
    BLUETOOTH = [{"local_name": "Zen*", "connectable": True}]
    CONFIG = [
        ConfigField("id", {"text": {}}, cast=str, is_key=True, readonly=True),
        ConfigField("apptoken", {"text": {"type": "password"}}, cast=str, required=False, section_key="cloud"),
        ConfigField("cloud_url", {"text": {}}, cast=str, required=False, expert=True, section_key="cloud"),
        ConfigField("mqtt_host", {"text": {}}, cast=str, required=False, section_key="local_broker"),
        ConfigField("mqtt_port", {"number": {"min": 1, "max": 65535, "mode": "box"}}, cast=int, required=False, section_key="local_broker"),
        ConfigField("mqtt_user", {"text": {}}, cast=str, required=False, section_key="local_broker"),
        ConfigField("mqtt_psw", {"text": {"type": "password"}}, cast=str, required=False, section_key="local_broker"),
        ConfigField("mqtt_log", {"boolean": {}}, cast=bool, section_key="local_broker"),
        ConfigField("wifi_ssid", {"text": {}}, cast=str, required=False, section_key="wifi"),
        ConfigField("wifi_psw", {"text": {"type": "password"}}, cast=str, required=False, section_key="wifi"),
        ConfigField("account", {"text": {}}, cast=str, required=False, section_key="account"),
        ConfigField("account_psw", {"text": {"type": "password"}}, cast=str, required=False, section_key="account"),
        ConfigField("account_env", {"select": {"options": ["app", "pre"]}}, cast=str, required=False, expert=True, section_key="account"),
    ]
    ADD = [  # a device that is never found: its identity by hand, the addresses it is reached on
        ConfigField("sn", {"text": {}}, cast=str),
        ConfigField("product", {"text": {}}, cast=str),
        *(ConfigField(k, {"text": {}}, cast=str, required=False) for k in ("device_id", "prodkey", "ip", "blemac")),
    ]

    def __init__(self, hass, entry_id: str, all_devices: list[EntityDevice], distribution) -> None:
        super().__init__(hass, entry_id, all_devices, distribution)
        self._ips: dict[str, str] = {}  # serial or MQTT address -> ip from mDNS, for a device that is not built yet
        self._mqtt_clients: dict[ConnectionType, mqtt.Client] = {}
        self._mqtt_log = logging.getLogger(f"{DOMAIN}.mqtt")  # raw payload log; the file is opened on first use
        self.mqtt_log = False  # write every raw MQTT payload to zenmaster_mqtt.jsonl
        self._ble_probed: set[str] = set()  # addresses asked over Bluetooth who they are; once per run
        self.broker = ZendureBroker(self)  # the own broker, used when no other broker is configured
        self.catalog = FirmwareCatalog(self)
        self._catalog_unsub: Any = None

        self.apptoken: str = ""
        self.cloud_url: str = ""
        self.mqtt_host: str = ""
        self.mqtt_port: int = 1883
        self.mqtt_user: str = ""
        self.mqtt_psw: str = ""
        self.wifi_ssid: str = ""
        self.wifi_psw: str = ""
        self.account = ""  # the Zendure app login, only for the firmware catalog
        self.account_psw = ""
        self.account_env = "app"  # app = production, pre = the test environment with the betas
        self.local_ip = "127.0.0.1"  # HA's own LAN address: the devices reach the API and the own broker there
        self.api_url = ""  # the API address a device gets at registration
        self.broker_host = ""  # the broker a device is sent to: the configured one, else HA itself
        self.firmware_dir = hass.config.path(f"{DOMAIN}_firmware") if hasattr(getattr(hass, "config", None), "path") else ""

    async def _lan_address(self) -> tuple[str, int]:
        # HA's address and HTTP port as HA knows them; before http is up, the source address of the host
        api = getattr(getattr(self.hass, "config", None), "api", None)
        if (ip := getattr(api, "local_ip", None)) is None:
            try:
                ip = await network.async_get_source_ip(self.hass)
            except Exception:  # no network component (tests)
                ip = "127.0.0.1"
        return str(ip), getattr(api, "port", None) or 8123

    async def connect(self) -> None:
        """connect to local MQTT and list the devices the cloud API knows."""

        # disconnect first
        await self.unload()

        # the addresses the devices get: the API at registration, the broker in the platform config and at pairing
        self.local_ip, port = await self._lan_address()
        self.api_url = f"http://{self.local_ip}:{port}{API_PATH}"
        self.broker_host = self.mqtt_host or self.local_ip

        # the API view is registered once per HA run; it finds the driver of the running coordinator itself
        if (data := getattr(self.hass, "data", None)) is not None and hasattr(self.hass, "http") and not data.get(f"{DOMAIN}_zendure_api"):
            data[f"{DOMAIN}_zendure_api"] = True
            self.hass.http.register_view(ZendureApiView(self.hass))

        # a configured broker is used as before; without one, ZenMaster is the broker on its own LAN address
        if self.mqtt_host != "":
            self._add_mqtt_client(ConnectionType.LOCAL, self.mqtt_host, self.mqtt_port, self.mqtt_user, self.mqtt_psw)
        else:
            await self.broker.start(self.local_ip)

        # the firmware catalog of the account: once now, then weekly
        if self.account and self.account_psw:
            self.hass.async_create_task(self._firmware_check())
            self._catalog_unsub = async_track_time_interval(self.hass, lambda _now: self.hass.async_create_task(self._firmware_check()), timedelta(days=7))

        if not self.apptoken:
            return
        try:
            base64_url = base64.b64decode(self.apptoken).decode("utf-8")
            api_url, app_key = base64_url.rsplit(".", 1)
            timestamp = int(time.time())
            nonce = str(random.randint(10000, 99999))
            sign_params = {"appKey": app_key, "timestamp": timestamp, "nonce": nonce}
            body_str = "".join(f"{k}{v}" for k, v in sorted(sign_params.items()))
            sign = hashlib.sha1(f"{SF_HAKEY}{body_str}{SF_HAKEY}".encode()).hexdigest().upper()

            headers = {"Content-Type": "application/json", "timestamp": str(timestamp), "nonce": nonce, "clientid": "zenHa", "sign": sign}
            session = async_get_clientsession(self.hass)  # HA's shared session, never closed here
            async with session.post(f"{api_url}/api/ha/deviceList", json={"appKey": app_key}, headers=headers, timeout=ClientTimeout(total=10)) as resp:
                data = await resp.json()

            code = data.get("code")
            if code != 200 or not data.get("success"):
                _LOGGER.warning("Cloud: apikey %s... rejected, code=%s msg=%s", self.apptoken[:10], code, data.get("msg", ""))
                return
            result = data["data"]
            device_list = result.get("deviceList", []) or []
            _LOGGER.info("Cloud: apikey %s... -> %d device(s), code=%s", self.apptoken[:10], len(device_list), code)
            for dev in device_list:
                if dev_id := dev.get("deviceKey", ""):
                    self.discover(dev.get("snNumber", ""), dev.get("product", ""), dev_id, dev.get("productKey", ""), dev.get("ip", "") or "")

            # the cloud broker address, only as a target for BLE onboarding; we do not connect to it
            if (mqtt_cfg := result.get("mqtt", {})) and mqtt_cfg.get("url"):
                self.cloud_url = mqtt_cfg["url"].split(":")[0]
        except Exception as err:
            _LOGGER.warning("Cloud API failed for key %s... (%s)", self.apptoken[:10], err)

    async def unload(self) -> None:
        """Disconnect all MQTT clients."""
        clients = list(self._mqtt_clients.values())
        for client in clients:
            client.disconnect()
        for client in clients:
            # loop_stop joins the paho thread; off the event loop, or a paho callback blocking on run_coroutine_threadsafe deadlocks HA
            await asyncio.get_running_loop().run_in_executor(None, client.loop_stop)
        self._mqtt_clients.clear()
        self.broker.close()
        if self._catalog_unsub is not None:
            self._catalog_unsub()
            self._catalog_unsub = None
        ZendureBattery._registry.clear()  # class-level registry must not hand out stale instances after a reload

    def discover(self, sn: str, product: str, device_id: str = "", prodkey: str = "", ip: str = "", blemac: str = "") -> None:
        # a device the cloud, the broker, mDNS or Bluetooth told us about; the coordinator turns it into a subentry, which comes back through init_device
        # without a serial there is nothing to identify: a meter reader reports the meter it reads, not itself
        if self.on_discovered is None or not sn or sn in self.devices:
            return
        product = product or PRODKEYS.get(prodkey.lower(), "")  # the cloud hands over a product key, never a product
        if not (row := find_product(product)):
            _LOGGER.warning("Zendure: skipping %s: unknown product %s", sn, product)
            return
        _LOGGER.info("Found Zendure device: %s (%s)", sn, product)
        ip = ip or self._ips.pop(sn, "") or self._ips.pop(device_id, "")  # an address mDNS saw before the cloud or the broker named the device
        self.on_discovered({"sn": sn, "product": product, "device_id": device_id, "prodkey": prodkey, "ip": ip, "blemac": blemac, "family": row.family})

    @classmethod
    def product_row(cls, data: dict[str, Any]) -> Product | None:
        return find_product(str(data.get("product", "")))  # the product string differs per route

    def build_device(self, subentry_id: str, name: str, data: dict[str, Any], row: Product) -> EntityDevice:
        # an address mDNS saw before the device was built; the coordinator writes it to the subentry right after the build
        if not data.get("ip") and (ip := self._ips.pop(data["sn"], None) or self._ips.pop(data.get("device_id", ""), None)):
            data = {**data, "ip": ip}
        powers = (row.pwr_max, row.pwr_min) if issubclass(row.cls, DistributionDevice) else ()
        return row.cls(self.hass, self, subentry_id, name, data, data["product"], *powers)

    @classmethod
    def match_zeroconf(cls, info: Any) -> dict[str, Any] | None:
        # the hostname is zendure-{productModel}-{sn}; the serial links the find to a device, the ip is what we learn
        parts = str(info.name).split("._")[0].split("-")
        if len(parts) < 3 or parts[0].lower() != "zendure":
            return None
        return {"sn": parts[-1], "model": "-".join(parts[1:-1]), "ip": str(info.host)}

    @classmethod
    async def first_find(cls, hass: Any, info: Any) -> dict[str, Any] | None:
        # before the entry exists: a Bluetooth advertisement is asked over Bluetooth, an mDNS find over its local API
        if hasattr(info, "address"):
            return await cls.probe_ble(hass, str(info.address))
        return await cls.probe(hass, match["ip"]) if (match := cls.match_zeroconf(info)) else None

    def bluetooth_found(self, info: Any, _change: Any) -> None:
        # HA's callback for a Zen* advertisement: one of our devices learns its address; a stranger is asked who it is, once
        if (device := self.match_bluetooth(info)) is not None:
            device.blemac = str(info.address)
            device.config_changed()
        elif (address := str(info.address)) not in self._ble_probed and not any(getattr(d, "blemac", "") == address for d in self.devices.values()):
            self._ble_probed.add(address)
            self.hass.async_create_task(self._bluetooth_found(address))

    async def _bluetooth_found(self, address: str) -> None:
        if (find := await self.probe_ble(self.hass, address)) is not None:
            self.discover(find["sn"], find.get("product", ""), find["device_id"], find["prodkey"], blemac=address)

    @classmethod
    async def probe_ble(cls, hass: Any, address: str) -> dict[str, Any] | None:
        # the start of the provisioning handshake and nothing more: connect, ack the BLESPP, and read the identity the device sends
        try:
            device = bluetooth.async_ble_device_from_address(hass, address, True)
        except Exception as err:  # no bluetooth stack in this HA
            _LOGGER.debug("Zendure BLE probe of %s: %s", address, err)
            device = None
        if device is None:
            return None
        replies: asyncio.Queue[dict] = asyncio.Queue()

        def on_notify(_sender: Any, data: bytearray) -> None:
            try:
                replies.put_nowait(json.loads(data.decode()))
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass

        found: dict[str, Any] = {}
        client = None
        try:
            client = await establish_connection(BleakClient, device, address)
            await client.start_notify(READ_CHAR, on_notify)
            async with asyncio.timeout(10):
                while len(found) < 3:
                    message = await replies.get()
                    if message.get("method") == "BLESPP":
                        await client.write_gatt_char(COMMAND_CHAR, b'{"method": "BLESPP_OK"}', response=False)
                    for source in (message, message.get("properties") or {}):
                        found.update({key: str(source[key]) for key in ("productKey", "sn", "deviceId") if source.get(key)})
        except Exception as err:  # a timeout too: the device did not hand over its identity
            _LOGGER.debug("Zendure BLE probe of %s: %s", address, err)
        finally:
            if client is not None:
                await client.disconnect()
        if len(found) < 3:
            _LOGGER.warning("Zendure BLE probe of %s: no identity in the handshake, got %s", address, sorted(found))
            return None
        return {"prodkey": found["productKey"], "product": PRODKEYS.get(found["productKey"].lower(), ""), "device_id": found["deviceId"], "sn": found["sn"], "blemac": address}

    async def zeroconf_found(self, name: str, ip: str, _properties: dict | None = None) -> None:
        # the device tells us who it is over its local API; a meter reader reports the meter it reads, not itself
        if not name.lower().startswith("zendure-"):
            return  # _http._tcp carries every web server on the network
        if not (find := await self.probe(self.hass, ip)) or not (find["sn"] or find["device_id"]):
            if (host := self.match_zeroconf(SimpleNamespace(name=name, host=ip))) is None:
                return
            find = {"prodkey": "", "device_id": "", "sn": host["sn"], "product": host["model"], "ip": ip}
        elif not find["sn"] and (host := self.match_zeroconf(SimpleNamespace(name=name, host=ip))) is not None:
            find["reader_sn"] = host["sn"]
        # a device we know follows its address; one that is not built yet gets it when it is
        for d in self.devices.values():
            if d.sn == find["sn"] or (find["device_id"] and d.device_key == find["device_id"]):
                if (reader := find.get("reader_sn")) and getattr(d, "reader_sn", "") != reader:
                    d.reader_sn = reader
                d.ip = ip
                d.config_changed()  # a no-op for the subentry when nothing changed
                return
        self._ips[find["sn"] or find["device_id"]] = ip
        self.discover(find["sn"], find["product"], find["device_id"], find["prodkey"], ip)

    @classmethod
    async def probe(cls, hass: Any, ip: str) -> dict[str, Any] | None:
        # the zenSDK report identifies the device: a SolarFlow gives sn and product, a meter reader the deviceId of its meter
        try:
            session = async_get_clientsession(hass)
            async with session.get(f"http://{ip}/properties/report", timeout=ClientTimeout(total=4)) as response:
                report = await response.json(content_type=None)
        except Exception as err:
            _LOGGER.debug("Zendure probe of %s failed: %s", ip, err)
            return None
        return {"prodkey": str(report.get("productKey") or ""), "device_id": str(report.get("deviceId") or ""), "sn": str(report.get("sn") or ""), "product": str(report.get("product") or ""), "ip": ip}

    def match_bluetooth(self, info: Any) -> EntityDevice | None:
        # the advertised name is the BLE name of one of our devices; the manufacturer data carries a serial tail that settles ties
        for d in self.devices.values():
            if not d.ble_name or not (info.name or "").startswith(d.ble_name):
                continue
            tails = [m.decode("utf-8", "ignore")[:-1] for m in (getattr(info, "manufacturer_data", None) or {}).values() if m and len(m) > 1]
            if tails and not any(t and d.sn.endswith(t) for t in tails):
                continue
            return d
        return None

    def _add_mqtt_client(self, conn: ConnectionType, host: str, port: int, username: str, password: str, client_id: str | None = None) -> None:
        if conn in self._mqtt_clients:
            return  # already connected; a config-save reload must not stack a second client
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id, userdata=conn)
            client.on_connect = self._on_mqtt_connect
            client.on_disconnect = self._on_mqtt_disconnect
            client.on_message = self._on_mqtt_message
            client.username_pw_set(username, password)
            client.connect_async(host, port)
            client.loop_start()
            self._mqtt_clients[conn] = client
            _LOGGER.info("MQTT connecting to %s:%s", host, port)
        except (ConnectionRefusedError, OSError):
            _LOGGER.warning("MQTT connection failed to %s:%s", host, port)

    def _on_mqtt_connect(self, client: Any, userdata: Any, _flags: Any, rc: Any, _props: Any) -> None:
        _LOGGER.info("MQTT [%s] connected, rc=%s", userdata, rc)
        client.subscribe("/#")  # local broker: everything, discovery needs it
        for d in list(self.devices.values()):
            # without an MQTT address there is no topic to talk to; that device is steered over zenSDK
            if not d.device_key:
                continue
            # kick off the reporting, the same read BLE sends right after connecting
            client.publish(f"iot/{d.prodkey}/{d.device_key}/properties/read", json.dumps({"properties": ["getAll"]}))

    def _on_mqtt_disconnect(self, client: Any, userdata: Any, _flags: Any, rc: Any, _props: Any) -> None:
        _LOGGER.info("MQTT [%s] disconnected, rc=%s", userdata, rc)

    def _on_mqtt_message(self, client: Any, userdata: Any, msg: Any) -> None:
        """Paho callback on its own network thread: hop onto the HA event loop, where all downstream work must run."""
        try:
            if not msg.payload:
                return  # empty

            if self.mqtt_log:
                self.log_raw(msg.topic, masked(msg.payload.decode("utf-8", "ignore")))

            _, prodkey, deviceid, topic = msg.topic.split("/", 3)
            payload = json.loads(msg.payload.decode("utf-8", "ignore"))
            # the device side on the event loop, where all downstream work must run
            self.hass.loop.call_soon_threadsafe(self._on_device_message, client, userdata, msg.topic, prodkey, deviceid, topic, payload)

        except RuntimeError:
            pass  # loop closed during shutdown, drop the message
        except Exception as ex:
            _LOGGER.warning("MQTT message %s: %s", msg.topic, ex)

    def log_raw(self, topic: str, payload: str) -> None:
        # one raw payload in zenmaster_mqtt.jsonl; called off the event loop (paho thread or executor), so the file never blocks it
        if not self._mqtt_log.handlers:
            handler = RotatingFileHandler(self.hass.config.path(f"{DOMAIN}_mqtt.jsonl"), maxBytes=20_000_000, backupCount=3)
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._mqtt_log.addHandler(handler)
            self._mqtt_log.setLevel(logging.INFO)
            self._mqtt_log.propagate = False
        self._mqtt_log.info(json.dumps({"t": time.time(), "topic": topic, "payload": payload}))

    def _on_device_message(self, client: Any, userdata: Any, msg_topic: str, prodkey: str, deviceid: str, topic: str, payload: dict) -> None:
        """A broker message on the event loop: the device it belongs to handles it, an unknown one is discovered."""
        try:
            # a registration is answered for every device, also one not known yet: the API address rides along
            if topic == "register":
                self._register_reply(client, userdata, msg_topic, payload)
                payload = {"deviceSn": (payload.get("params") or {}).get("deviceSn")}  # its serial, for the discovery below
            if (d := next((d for d in self.devices.values() if d.device_key == deviceid), None)) is not None:
                # a device on zenSDK stays there: the broker still brings its reports, the commands go over HTTP;
                # otherwise the client that brought the report sends the commands, also after a reconnect made a new one.
                # The client is kept either way: the broker messages (dev/debug, fetchHems, firmware) use it
                d.mqtt = client
                if d.connection != ConnectionType.ZENSDK:
                    d.connection = userdata

                match topic:
                    case "properties/report":
                        d.update_properties(payload, time.time())

                    case "properties/energy":
                        # the 1.5 s report of a device with a hemsId: its two powers under the names of the full report
                        p = payload.get("properties") or {}
                        d.update_properties({"properties": {k: p[s] for s, k in (("chargePower", "gridInputPower"), ("outputPower", "outputHomePower")) if s in p}}, time.time())

                    case "firmware/report":
                        d.firmware_report(payload)
                        self._firmware_show(d)

                    case "firmware/download/progress" | "firmware/upgrade/progress":
                        # the download and then the install, each 0 to 100; a failure or the end clears the pending update
                        busy = bool(payload.get("success", True)) and not payload.get("complete")
                        if not busy:
                            _LOGGER.info("Zendure firmware %s on %s: %s", topic, d.name, {k: payload.get(k) for k in ("module", "success", "complete", "errorReason")})
                            d.ota = None
                        self._firmware_show(d, payload.get("progress") if busy else None, busy)

                    case "function/invoke/reply":
                        if isinstance(d, ZendureSolarFlow) and payload.get("messageId") == d._msg_id:
                            d._msg_update = time.time()
                            d.set_state(DeviceState.CONFIRM)

                    case "time-sync":
                        z = datetime.now().astimezone().strftime("%z")
                        reply = json.dumps({"zoneOffset": f"{z[:3]}:{z[3:]}", "timestamp": int(time.time()), "userId": USER_ID, "messageId": payload.get("messageId", "none")})
                        client.publish(f"iot{msg_topic}/reply", reply)
                        # a device asks the time at every connect and after each restart: the log upload to the cloud goes off again
                        client.publish(f"iot/{prodkey}/{deviceid}/dev/debug", json.dumps({"enable_log": False}))
            else:
                if sn := payload.get("log", {}).get("sn") or payload.get("slaveSN") or payload.get("properties", {}).get("slaveSn") or payload.get("deviceSn"):
                    if (known := self.devices.get(sn)) is None:
                        self.discover(sn, str(payload.get("product", "")), deviceid, prodkey)
                    elif known.device_key != deviceid:
                        # the topic carries the MQTT address; a device that came from mDNS learns it here
                        known.device_key = deviceid
                        known.prodkey = prodkey
                        known.config_changed()
        except Exception as ex:
            _LOGGER.warning("MQTT message %s: %s", msg_topic, ex)

    async def _firmware_check(self) -> None:
        try:
            await self.catalog.refresh()
        except Exception as err:
            _LOGGER.warning("Zendure firmware catalog: %s", err)
            return
        for d in self.devices.values():
            self._firmware_show(d)

    def _firmware_show(self, d: Any, progress: int | None = None, busy: bool = False) -> None:
        # a device the account lists gets an update entity: zenSDK running versus zenSDK on offer
        if not hasattr(d, "mqtt_publish") or (entry := self.catalog.entries.get(d.sn)) is None:
            return
        if getattr(d, "firmware", None) is None:
            d.firmware = HaUpdate(d, "firmware", self._firmware_install)
        d.firmware.show(*versions(entry, d.esp_version), progress, busy)

    async def _firmware_install(self, entity: HaUpdate) -> None:
        # the files to the HA storage, the list to the local API, then the device is told to fetch it
        d = entity.device
        if (entry := self.catalog.entries.get(d.sn)) is None or d.mqtt is None:
            raise HomeAssistantError(f"{d.name}: no firmware on offer, or not on the broker")
        await self.catalog.fetch(entry)
        d.ota = manifest(entry, self.api_url)
        d.mqtt_publish("firmware/download", {})
        self._firmware_show(d, None, True)

    def _register_reply(self, client: Any, userdata: Any, msg_topic: str, payload: dict) -> None:
        # zenSDK 3 rejects a reply without params; both versions store the API address from it and leave the cloud API
        _LOGGER.info("Register on %s", msg_topic)
        if userdata != ConnectionType.LOCAL:
            return
        token = (payload.get("params") or {}).get("token")
        reply = {"token": token, "result": 0, "params": {"result": 0, "token": token, "apiUrl": self.api_url, "userId": USER_ID}}
        client.publish(f"iot{msg_topic}/replay", json.dumps(reply))  # no retain: a retained replay reboot-loops the device
