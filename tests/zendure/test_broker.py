"""The own broker: a known device gets TLS and its replies, a stranger is closed before TLS, a device speaks only for itself."""

import asyncio
import json
import ssl
import struct
from types import SimpleNamespace

import pytest

from conftest import fake_hass
from custom_components.zenmaster.distribution import Distribution
from custom_components.zenmaster.zendure.broker import CIPHERS, ZendureBroker, masked, packet, text
from custom_components.zenmaster.zendure.driver import ZendureDriver


def make_driver(devices: dict) -> ZendureDriver:
    driver = ZendureDriver(fake_hass(), "entry", [], Distribution(fake_hass(), "entry"))
    driver.devices.update(devices)

    async def probe(hass, ip):
        return None  # nothing on the network answers as a Zendure device

    driver.probe = probe
    return driver


async def start(driver: ZendureDriver) -> tuple[ZendureBroker, int]:
    broker = ZendureBroker(driver)
    assert await broker.start("127.0.0.1", 0)
    return broker, broker.server.sockets[0].getsockname()[1]


async def connect(port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    # a device: TLS 1.2 without a certificate check, as the firmware only checks the root
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    return await asyncio.open_connection("127.0.0.1", port, ssl=ctx)


async def read(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    head, size = (await reader.readexactly(1))[0], (await reader.readexactly(1))[0]
    return head, await reader.readexactly(size)


def published(body: bytes) -> tuple[str, dict]:
    n = struct.unpack_from(">H", body)[0]
    return body[2:2 + n].decode(), json.loads(body[2 + n:])


async def test_known_device_gets_tls_and_its_replies() -> None:
    device = SimpleNamespace(ip="127.0.0.1", device_key="Cd3wbW9V", prodkey="65174u", connection=0, mqtt=None)
    broker, port = await start(make_driver({"HED4NENBN470562": device}))
    try:
        reader, writer = await connect(port)
        assert writer.get_extra_info("cipher")[0] in CIPHERS.split(":")

        # connect and subscribe: the reporting is kicked off right after
        writer.write(packet(0x10, text("MQTT") + b"\x04\x02\x00\x1e" + text("Cd3wbW9V")))
        assert await read(reader) == (0x20, b"\0\0")
        writer.write(packet(0x82, b"\0\x01" + text("iot/65174u/Cd3wbW9V/#") + b"\x01"))
        assert await read(reader) == (0x90, b"\0\x01\0")
        assert published((await read(reader))[1]) == ("iot/65174u/Cd3wbW9V/properties/read", {"properties": ["getAll"]})

        # a message for another device id is dropped; its own time request is answered over this connection
        writer.write(packet(0x30, text("/65174u/other/time-sync") + b'{"messageId": 1}'))
        writer.write(packet(0x30, text("/65174u/Cd3wbW9V/time-sync") + b'{"messageId": 2}'))
        topic, reply = published((await read(reader))[1])
        assert topic == "iot/65174u/Cd3wbW9V/time-sync/reply" and reply["messageId"] == 2
        assert published((await read(reader))[1]) == ("iot/65174u/Cd3wbW9V/dev/debug", {"enable_log": False})
        assert device.mqtt is not None

        # the connection closing leaves the device without a client
        writer.write(packet(0xE0, b""))
        await writer.drain()
        writer.close()
        for _ in range(50):
            if device.mqtt is None:
                break
            await asyncio.sleep(0.01)
        assert device.mqtt is None
    finally:
        broker.close()


async def test_stranger_is_closed_before_tls_and_pairing_opens_the_door() -> None:
    broker, port = await start(make_driver({}))
    try:
        with pytest.raises((ssl.SSLError, ConnectionError, OSError)):
            await connect(port)
        broker.pair("Cd3wbW9V")
        reader, writer = await connect(port)
        writer.write(packet(0x10, text("MQTT") + b"\x04\x02\x00\x1e" + text("x")))
        assert await read(reader) == (0x20, b"\0\0")
        writer.close()
    finally:
        broker.close()


def test_raw_log_masks_secrets() -> None:
    assert masked('{"token": "abc", "password":"p", "sn": "X"}') == '{"token": "***", "password":"***", "sn": "X"}'
