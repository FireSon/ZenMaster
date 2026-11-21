# Zendure test device on an ESP32

Turns an ESP32 (ESP32-C3) into a fake Zendure device for testing the ZenMaster onboarding and control
path on real hardware. It does the BLE pairing the Python sim (`tools/zendure_sim.py`) cannot.

## Profiles

One firmware, four device types, selected and configured over the serial console (stored in NVS, no
reflash to switch):

| profile | product key | reports | command |
|---|---|---|---|
| `ace1500` | 8bM93H | `ace` report, discharge as `acOutputPower` | `deviceAutomation` autoModel 8/0 |
| `hyper2000` | gDa3tb | fast `properties/energy` | `hemsEP` (ZenMaster fork) and `deviceAutomation` |
| `hub1200` | 73bkTV | home power, discharge only | `deviceAutomation` (autoModelValue = number) |
| `hub2000` | a8yh63 | home power, discharge only | `deviceAutomation` (autoModelValue = number) |

ACE and Hyper are modelled on recordings of the real devices (test broker, 2026-11-20). **Hub1200 and
Hub2000 are modelled on the Zendure-HA reference only** (`devices/hub1200.py`, `hub2000.py`), not on
recorded traffic, so their telemetry is approximate. SuperBase is not included (ZenMaster has no support
for it yet).

## Flashing

Board is ESP32-C3 (`env:esp32-c3`, confirmed from the device registry). Flash over USB/serial:

```
pio run -e esp32-c3 -t upload
pio device monitor
```

## Configuring

Two ways: the web page at `http://zensim.local/` once the device is on your LAN, or the serial console
(115200) before pairing. Serial commands (then `reboot`):

```
show                     current profile, identity and stored Wi-Fi
profile hyper2000        switch device type (resets identity to that profile's defaults)
sn <serial>              override the serial
deviceid <id>            override the deviceId / MQTT address
prodkey <key>            override the product key
forget                   clear the stored Wi-Fi (boot back into BLE pairing)
reboot                   apply and restart
```

The BLE name follows the legacy/Hyper rule: `Zen{product letter}{sn[2]}_{sn[-2:]}` (letter per product:
ACE F, Hyper E, Hub1200 P, Hub2000 H), from solarflow-bt-manager. So the Hyper default advertises
`ZenE1_02`, Hub1200 `ZenP1_03`, regardless of the serial's first character.

After the first pairing the device stores the Wi-Fi and, on every later boot, rejoins your LAN directly
(no pairing) and serves the same config page at **http://zensim.local/**. Changing the device type there
and rebooting rejoins the LAN as the new type. Use **Forget Wi-Fi** (web button or serial `forget`) to go
back to BLE pairing.

## Testing

Open the ZenMaster pairing screen; it finds the device (e.g. `ZenE1_02` for the Hyper default, `ZenP1_03`
for Hub1200), you
enter the Wi-Fi, and it provisions. The device joins Wi-Fi, connects to the broker (client id =
deviceId), registers, and reports. The screen opens the broker's pairing window via
`zenmaster/zendure_pair`, so the device is admitted from its new address.

**The broker the device is sent to is the driver's `broker_host`.** Run the pairing screen from a
ZenMaster whose `broker_host` is the instance you want to watch (e.g. this PC's branch broker), or the
device goes to that ZenMaster's own broker.

## Assumptions / caveats (verify against real hardware)

- Reply formats to `properties/write`, `function/invoke` and the BLE `token`/`deviceInfo` commands.
- The mapping of `deviceAutomation`/`hemsEP` to charge/discharge power.
- Hub1200/Hub2000 telemetry is from the reference, not recorded.
- The device connects to the broker **without credentials**. A real device is provisioned with
  `iotUserName`/`iotPassword` (broker login) and `authmode` in the BLE token step; ZenMaster's
  onboarding omits those. For ZenMaster's own broker (no auth) this is fine; for an authenticated broker
  the firmware would need to send user/pass in `mqttConnect`.
