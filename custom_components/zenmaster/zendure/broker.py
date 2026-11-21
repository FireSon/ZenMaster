"""The own MQTT broker for the Zendure devices: TLS on 8883 with the certificate their firmware trusts, only for devices of the fleet."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import ssl
import struct
import time
from typing import TYPE_CHECKING

from homeassistant.components import persistent_notification

from .device import ConnectionType

if TYPE_CHECKING:
    from .driver import ZendureDriver

_LOGGER = logging.getLogger(__name__)

PORT = 8883  # fixed in the firmware
MAX_PACKET = 16384  # the firmware cuts its reports at 756 bytes
READ_TIMEOUT = 120  # the firmware pings every 30 s
PAIRING_S = 600
CIPHERS = "ECDHE-RSA-AES128-GCM-SHA256:ECDHE-RSA-AES256-GCM-SHA384"
CERTS = os.path.join(os.path.dirname(__file__), "certs")
SECRET = re.compile(r'("(?:token|password|psw|pwd|passwd|wifiPsw)"\s*:\s*)"[^"]*"', re.I)


def masked(text: str) -> str:
    # tokens and passwords never reach the raw log
    return SECRET.sub(r'\1"***"', text)


def tls_context() -> ssl.SSLContext:
    # TLS 1.2 only, with the two GCM suites both zenSDK 2 and 3 offer; file I/O, so off the event loop
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    ctx.set_ciphers(CIPHERS)
    ctx.load_cert_chain(os.path.join(CERTS, "cert.pem"), os.path.join(CERTS, "key.pem"))
    return ctx


def text(s: str) -> bytes:
    return struct.pack(">H", len(s.encode())) + s.encode()


def packet(kind: int, body: bytes) -> bytes:
    # fixed header with the remaining length as a varint
    size, head = len(body), bytearray([kind])
    while True:
        b, size = size & 0x7F, size >> 7
        head.append(b | (0x80 if size else 0))
        if not size:
            return bytes(head) + body


class Connection:
    """One device on the broker; the driver keeps it as the device's MQTT client."""

    def __init__(self, writer: asyncio.StreamWriter, ip: str, keys: set[str]) -> None:
        self.writer = writer
        self.ip = ip
        self.keys = keys  # the device ids this connection may speak for; empty = the first topic decides
        self.client_id = ""

    def publish(self, topic: str, payload: str) -> None:
        if not self.writer.is_closing():
            self.writer.write(packet(0x30, text(topic) + payload.encode()))


class ZendureBroker:
    """Accepts a device before TLS on its address, then speaks just enough MQTT 3.1.1 for one device per connection."""

    def __init__(self, driver: ZendureDriver) -> None:
        self.driver = driver
        self.server: asyncio.Server | None = None
        self.ctx: ssl.SSLContext | None = None
        self.pairing: dict[str, float] = {}  # device id from the Bluetooth handshake -> end of its window

    async def start(self, host: str, port: int = PORT) -> bool:
        self.ctx = await asyncio.get_running_loop().run_in_executor(None, tls_context)
        try:
            self.server = await asyncio.start_server(self._accept, host, port)
        except OSError as err:
            # the port is taken, most likely by the EMQX add-on: no broker, and the user hears why
            _LOGGER.warning("Zendure broker: %s:%s not available (%s)", host, port, err)
            persistent_notification.async_create(self.driver.hass, f"ZenMaster cannot start its Zendure broker on {host}:{port} ({err}). Another broker (EMQX?) holds the port: stop it there, or enter that broker in the Zendure settings.", "ZenMaster", "zenmaster_zendure_broker")
            return False
        _LOGGER.info("Zendure broker on %s:%s", host, port)
        return True

    def close(self) -> None:
        if self.server is not None:
            self.server.close()
            self.server = None

    def pair(self, device_id: str) -> None:
        # a device being paired over Bluetooth may connect from an address the fleet does not know yet
        self.pairing[device_id] = time.time() + PAIRING_S

    async def _admit(self, ip: str) -> set[str] | None:
        # the device ids a connection from this address may use, or None: a stranger is closed before TLS
        if keys := {d.device_key for d in self.driver.devices.values() if getattr(d, "ip", None) == ip}:
            return {k for k in keys if k}
        now = time.time()
        self.pairing = {k: t for k, t in self.pairing.items() if t > now}
        if self.pairing:
            return set(self.pairing)
        if (find := await self.driver.probe(self.driver.hass, ip)) and find["device_id"]:
            return {find["device_id"]}
        return None

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        ip = (writer.get_extra_info("peername") or ("",))[0]
        conn = None
        try:
            if (keys := await self._admit(ip)) is None:
                _LOGGER.debug("Zendure broker: refused %s", ip)
                return
            await writer.start_tls(self.ctx)
            conn = Connection(writer, ip, keys)
            while await self._packet(conn, reader):
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, TimeoutError, ssl.SSLError, OSError) as err:
            _LOGGER.debug("Zendure broker: %s closed (%r)", ip, err)
        except Exception:
            _LOGGER.warning("Zendure broker: %s dropped", ip, exc_info=True)
        finally:
            writer.close()
            # the device's client is gone: its commands wait for the reconnect
            for d in self.driver.devices.values():
                if conn is not None and getattr(d, "mqtt", None) is conn:
                    d.mqtt = None

    async def _packet(self, conn: Connection, reader: asyncio.StreamReader) -> bool:
        # one packet: the fixed header, the length, the body; False ends the connection
        head = (await asyncio.wait_for(reader.readexactly(1), READ_TIMEOUT))[0]
        size = shift = 0
        while (b := (await reader.readexactly(1))[0]) & 0x80:
            size, shift = size | (b & 0x7F) << shift, shift + 7
            if shift > 21:
                return False
        if (size := size | b << shift) > MAX_PACKET:
            _LOGGER.warning("Zendure broker: %s sent %d bytes in one packet", conn.ip, size)
            return False
        body = await asyncio.wait_for(reader.readexactly(size), READ_TIMEOUT)
        kind, flags = head >> 4, head & 0x0F
        match kind:
            case 1:  # CONNECT: protocol name, level, flags, keepalive, client id
                at = 2 + struct.unpack_from(">H", body)[0] + 4
                conn.client_id = body[at + 2:at + 2 + struct.unpack_from(">H", body, at)[0]].decode("utf-8", "replace")
                conn.writer.write(packet(0x20, b"\0\0"))
                cipher = conn.writer.get_extra_info("cipher")
                _LOGGER.info("Zendure broker: %s connected as %s, %s", conn.ip, conn.client_id, cipher[0] if cipher else "?")
            case 3:  # PUBLISH, QoS 0 to 2
                at = 2 + struct.unpack_from(">H", body)[0]
                topic, qos = body[2:at].decode("utf-8", "replace"), flags >> 1 & 3
                if qos:
                    conn.writer.write(packet(0x40 if qos == 1 else 0x50, body[at:at + 2]))
                    at += 2
                self._message(conn, topic, body[at:])
            case 6:  # PUBREL of a QoS 2 publish
                conn.writer.write(packet(0x70, body[:2]))
            case 8:  # SUBSCRIBE: every filter granted at QoS 0, then the reporting kicked off
                conn.writer.write(packet(0x90, body[:2] + b"\0" * self._filters(body)))
                for d in self.driver.devices.values():
                    if d.device_key and d.device_key in conn.keys:
                        conn.publish(f"iot/{d.prodkey}/{d.device_key}/properties/read", json.dumps({"properties": ["getAll"]}))
            case 10:  # UNSUBSCRIBE
                conn.writer.write(packet(0xB0, body[:2]))
            case 12:  # PINGREQ
                conn.writer.write(packet(0xD0, b""))
            case 14:  # DISCONNECT
                return False
        return True

    @staticmethod
    def _filters(body: bytes) -> int:
        # the number of topic filters in a SUBSCRIBE, each a string and a QoS byte
        at, n = 2, 0
        while at < len(body):
            at, n = at + 2 + struct.unpack_from(">H", body, at)[0] + 1, n + 1
        return n

    def _message(self, conn: Connection, topic: str, data: bytes) -> None:
        # a device speaks only for itself: the device id in the topic must be one this connection was admitted for
        if len(parts := topic.split("/", 3)) < 4 or not data:
            return
        _, prodkey, deviceid, sub = parts
        if not conn.keys:
            conn.keys = {deviceid}
        if deviceid not in conn.keys:
            _LOGGER.warning("Zendure broker: %s (%s) sent for %s, dropped", conn.ip, conn.client_id, deviceid)
            return
        payload_text = data.decode("utf-8", "replace")
        _LOGGER.debug("Zendure broker: %s %s, %d bytes", deviceid, sub, len(data))
        if self.driver.mqtt_log:
            self.driver.hass.async_add_executor_job(self.driver.log_raw, topic, masked(payload_text))
        try:
            payload = json.loads(payload_text)
        except ValueError:
            return
        if isinstance(payload, dict):
            self.driver._on_device_message(conn, ConnectionType.LOCAL, topic, prodkey, deviceid, sub, payload)
