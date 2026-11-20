# Plan: Zendure volledig lokaal in ZenMaster

Vastgelegd 2026-11-20, vervangt de versie van 2026-11-18. Status: fase 1 tot en met 6 gebouwd op branch
`local-control` (niet gepusht, geen PR, niet gedeployed); zie sectie 7.

Bronnen: de drivercode op `main` (c7e1828), `MEMORY.md`, `firmware/zensdk_api.md` (zenSDK 2.0.59), een
decompile van zenSDK 3.0.21 (SolarFlow 2400 Pro) en tests op productie-devices op 18 tot 20 november.

Afkortingen: OTA = firmware-update over het netwerk. RPC = de aanroepen op `POST http://<ip>/rpc` van het
device. NVS = het instellingengeheugen in de flash van het device. HEMS = het energiebeheer in de firmware.
LCN = de lokale cluster van Zendure-devices. TLS = versleutelde verbinding.

## 1. Vastgesteld in de tests

Getest met SF2400_12, SF2400_37, SF800_88 en SF1600_62, gekoppeld via Bluetooth aan de broker op de HA
(192.168.1.250) en met een testeindpunt op `http://192.168.1.250:8199`.

### 1.1 Firmwareversies

- De app toont het pakketnummer (2.0.x), niet de zenSDK-versie. Een pakket bevat de modules MASTER, AC en
  ESP32; de ESP32-module is de zenSDK.
- Gemeten via het firmwarerapport en `Device.GetVer`: SF2400_12 en SF2400_37 draaien zenSDK 3.0.21,
  SF800_88 draait 3.0.18. SF1600_62 draaide zenSDK 2 (geen ESP32 in zijn rapport, `/rpc` eist een
  serienummer) en is in de test naar 3.0.21 gebracht, terug naar 2.0.59 en weer naar 3.0.21.

### 1.2 Registratie en API-adres

- Bij het koppelen via Bluetooth stuurt het device `register` op de broker. Een antwoord met
  `params.apiUrl` wordt in zenSDK 2 en 3 in het geheugen opgeslagen (`mqtt_handle_register_replay`).
- `http://` als API-adres werkt in beide versies.
- Het platte antwoord van ZenMaster (`{"token": .., "result": 0}`, `zendure/driver.py:384`) wijzen de
  zenSDK 3-devices af met `register_failed` / `params_missing`.
- Na een geslaagde registratie herstart zenSDK 3 na ongeveer 2 s.
- Een device dat op wifi zit heeft Bluetooth uit; voor opnieuw koppelen moet de knop 3 s worden ingedrukt.
- De Bluetooth-naamregel van ZenMaster (`Zen{sn0}{sn2}{sn3}_{sn[-1]}`) klopt voor alle vier devices.

### 1.3 Rapportage

- zenSDK 2 haalt na de registratie de platform-config op. `realTimeRate: 2` gaf op SF1600_62 een rapport
  op exact 2,0 s. Daarna rapporteert hij alleen gewijzigde waarden, op tikken van 2 s.
- zenSDK 3 haalt de platform-config nooit op: het startbericht (0x10 in de MQTT-taak) wordt door niets
  verstuurd. Een eerder opgeslagen `realTimeRate` blijft wel gelden; SF1600_62 hield 2 s na de update.
- zenSDK 2 neemt het brokeradres uit `mqttUrl` van de platform-config zodra die er is. Een platform-config
  zonder `mqttUrl` haalde SF1600_62 van de broker; opnieuw koppelen met `mqttUrl` erin herstelde dat.
- Beide versies vragen bij elke verbinding `/zen-iot/hems/base` op, en opnieuw na `function/invoke` met
  `fetchHems`.
- Met een `hemsId` ongelijk aan 0 staat `hemsRunning` aan (zenSDK 3, gemeten op SF800_88 en SF1600_62) en
  komt `/properties/energy` op tikken van 1,5 s; in rust elke 9 s. Het snelle rapport bij een verandering
  van 20 W of meer is nog niet gemeten: geen van de devices veranderde van vermogen.
- zenSDK 2 neemt de `hemsId` alleen over als `mode` en `chargeMode` in hetzelfde antwoord staan
  (decompile 2.0.59); met alleen een `hemsId` kwam er geen `/properties/energy`.
- Het HEMS-antwoord met `minSoc` en `socSet` zet de HEMS-grenzen gelijk aan die van het device
  (`HEMS.GetConfig` op SF1600_62: `minSoc 100`, `maxSoc 940`).

### 1.4 Firmware via het eigen eindpunt

- Een bericht op `iot/{prodkey}/{deviceId}/firmware/download` laat het device `/zen-iot/ota/preFetch`
  ophalen bij zijn API-adres en daarna de bestanden uit die lijst downloaden.
- Lijst: `{"code": 200, "data": {"id", "protocol", "moduleList": [{"module", "version", "firmwareId",
  "len", "sign", "md5", "firmwareUrl"}]}}`. In zenSDK 2 is `protocol` verplicht (`V_1` werkt).
- Het device installeert alleen als `version` hoger is dan wat er draait. Een oudere versie gaat erin met
  een hoger nummer in de lijst; zo is 2.0.59 met versie 12310 op 3.0.21 gezet.
- `sign` is geen eenvoudige checksum (som-16, vijf CRC-16-varianten en CRC-32 getest). Bij de terugzet werd
  de waarde van het 3.0.21-bestand meegegeven en dat werkte.
- Een update duurt ongeveer 20 s downloaden plus een herstart. Instellingen en registratie blijven staan.

### 1.5 Logupload en lokale API

- zenSDK 3 uploadt logs naar `zendure-device-log-eu.eu-central-1.log.aliyuncs.com` (Alibaba Cloud,
  Frankfurt), los van het API-adres. `Sys.GetZenconfig` meldt `CONFIG_ALIYUN_LOG_UPLOAD_ENABLE=y`.
- `/dev/debug` met `{"enable_log": false}` zet de vlag uit die de upload controleert. Decompile 3.0.21: de
  upload (`FUN_4202c300`) wordt alleen vanuit `FUN_4202c8ee` gestart, en die stopt bij een vlag op 0. De
  vlag staat alleen in het werkgeheugen; registratie, een firmware-update en het cloudvenster in lokale
  modus zetten hem weer aan, en vermoedelijk ook een herstart.
- Niet geverifieerd: of de logmodule de upload-functie ook zelf aanroept, en het netwerkverkeer zelf.
- `Wifi.GetConfig` en `Log.ExportTXT` geven het wifi-wachtwoord in leesbare tekst, zonder authenticatie,
  aan iedereen op het LAN. Het opstartlog bevat ook het brokerwachtwoord en het registratietoken.

### 1.6 Sturen: MQTT tegen zenSDK-HTTP (decompile 3.0.21)

- Beide routes voor `/properties/write` komen uit bij dezelfde functie: MQTT via `mqtt_write`
  (FUN_420216f4), HTTP via `http_server_property_write_handler` (FUN_420237a2). Beide roepen
  `FUN_4201d4a2` aan, dat de opdracht als gebeurtenis 0x1f in dezelfde wachtrij zet. Daarna is de
  verwerking identiek; de firmware geeft geen van beide voorrang.
- De verschillen zitten in de weg ernaartoe: HTTP gaat rechtstreeks van HA naar het device, MQTT via de
  broker en over TLS, en het device verwerkt het in zijn MQTT-taak, die ook rapporten en logs afhandelt.
- MQTT stuurt na het in de wachtrij zetten een antwoord terug met `messageId` en succes
  (`FUN_4202157e`); ZenMaster leest dat nu niet. HTTP geeft 200 of 400. Geen van beide bevestigt dat de
  limiet al geldt.
- MQTT controleert de modbus-vergrendeling, de HTTP-handler niet.
- De metingen van september (zenSDK 0,4 s sneller, minder verlies) waren op zenSDK 2. Het verschil komt
  volgens deze decompile niet uit de verwerking in de firmware, maar vermoedelijk uit de broker, de QoS of
  de MQTT-taak. Of zenSDK 2 intern ook op één wachtrij uitkomt, is niet nagekeken.

Meting op zenSDK 3.0.21 (SF2400_37, 2026-11-20): 10 keer 0, 200 en 0 W per route, om en om. Voor beide
routes is via HTTP vier keer per seconde gelezen (`/properties/report`), zodat ze met dezelfde klok
gemeten zijn. MQTT liep via een eigen minimale broker op 192.168.1.11 (`latency_test.py`), rechtstreeks
naar de socket van het device. Alle 40 stappen gemeten, geen gemist. Mediaan (minimum tot maximum):

| | MQTT | HTTP |
|---|---|---|
| naar 200 W: limiet in rapport | 1,16 s (0,89 tot 1,19) | 1,24 s (0,93 tot 1,52) |
| naar 200 W: vermogen 150 W of meer | 2,08 s (1,74 tot 6,62) | 2,36 s (1,79 tot 2,73) |
| naar 0 W: limiet in rapport | 0,04 s | 0,10 s |
| naar 0 W: vermogen 20 W of minder | 0,04 s | 0,10 s |

- MQTT was op zenSDK 3 niet trager dan HTTP, in deze test telkens iets sneller.
- De vertraging zit vooral in het device: ongeveer 1 s tot de limiet geldt, daarna 1 tot 2 s voor het
  vermogen. Naar 0 gaat vrijwel direct.
- De uitschieter van 6,62 s was de eerste stap, direct na de herstart van het omzetten.
- Niet gemeten: de doorgifte via EMQX, andere devices en stapgroottes, en commando's kort na elkaar (het
  verlies van september bij één commando per seconde).
- Omzetten zonder Bluetooth werkte met `Mqtt.SetConfig` (`{"config": {"server": ".."}}`) op `/rpc`; het
  device herstart en verbindt binnen ongeveer 15 s. De melding "platform config is active" had geen
  effect: `Mqtt.SetConfig` schrijft hetzelfde geheugenveld als `mqttUrl` uit de platform-config.

### 1.7 Overig

- Foutcode 40 (`eventId 3`) op SF1600_62 en SF800_88, al vanaf de eerste koppeling. Geen omschrijving in
  de firmware of het app-pakket; de app haalt die vermoedelijk bij de cloud op.
- De lokale-modusschakelaar op de webpagina van het device (`HEMS.SetLocal`) moet op cloud blijven: in
  local regelt het device zelf op een meter en negeert het setpoints.

## 2. Het plan

### Fase 0: netwerk (geen code)

- VLAN of firewall voor de devices: alleen de HA op 8883 (broker) en het API-eindpunt, geen internet.
- Dit is de enige zekere afsluiting van de logupload en dekt ook de open `/rpc` af.
- Overwegen: het wifi-wachtwoord wijzigen, omdat het al geüpload kan zijn.

### Fase 1: register-antwoord

- `zendure/driver.py:381-385`: het antwoord krijgt `params` met `result`, `token`, `apiUrl` en `userId`
  (`USER_ID` bestaat al). `apiUrl` komt van de interne URL van HA.
- Test: één device opnieuw koppelen; geen `register_failed` meer in `/dev/debug`.

### Fase 2: API-eindpunt in ZenMaster

Een HA-view op poort 8123, zonder HA-authenticatie maar alleen voor de eigen devices (besluit 2026-11-20):

- Alleen verzoeken van een ip-adres dat ZenMaster als `ip` van een Zendure-subentry kent; elk ander
  adres krijgt 404. Verzoeken via een tunnel of reverse proxy komen nooit van zo'n adres binnen.
- De `deviceKey` in het verzoek moet bij dat adres horen.
- Verzoeken via Nabu Casa worden geweigerd (`is_cloud_connection(hass)`), zoals bij de webhooks van HA
  met `local_only` (`homeassistant/components/webhook/__init__.py`).

- `/zen-iot/platform/config`: `realTimeRate` en altijd `mqttUrl` (= `mqtt_host` van de driver).
- `/zen-iot/platform/config/reply`: 200.
- `/zen-iot/hems/base`: `hemsId`, plus `minSoc` en `socSet` uit de entiteiten `soc_min` en `soc_max`;
  voor zenSDK 2 ook `mode` en `chargeMode`.
- Al het andere: 404.
- Test: zenSDK 2 rapporteert op `realTimeRate`; zenSDK 3 meldt `hemsRunning` en stuurt
  `/properties/energy`.

### Fase 3: MQTT in de driver

- `/properties/energy` verwerken in `update_properties` (`chargePower` als `gridInputPower`,
  `outputPower` als `outputHomePower`). Nu leest de driver alleen `properties/report` (`driver.py:378`).
- Bij elke verbinding `/dev/debug` met `enable_log: false`, zoals `time-sync` nu al beantwoord wordt.
- Na een gewijzigde `hemsId` of SoC-grens een `function/invoke` met `fetchHems`.
- Sturen: open keuze 7 (zie 1.6). Het besluit van 21-09 was zenSDK-HTTP.

### Fase 3b: eigen MQTT-broker in ZenMaster

ZenMaster wordt zelf de broker voor de Zendure-devices, in plaats van EMQX. Proef: `latency_test.py`
(sectie 1.6). Besluiten voor een security review, 2026-11-20:

1. **Certificaat: meeleveren in de integratie.** Het EMQX-testcertificaat en de sleutel (tag v5.0.0) met
   de Apache 2.0-vermelding. De sleutel is openbaar; iedereen op het LAN kan zich als broker voordoen. Dat
   komt door de vaste EMQ-root in de firmware en geldt voor elke broker. Documenteren, met het VLAN uit
   fase 0 als maatregel. Het certificaat verloopt op 6 mei 2030.
2. **TLS: alleen 1.2, met ECDHE-RSA-AES-128-GCM-SHA256 en ECDHE-RSA-AES-256-GCM-SHA384.** Beide staan in
   de images van zenSDK 2.0.59 en 3.0.21 (58 suites, allemaal TLS 1.2, geen TLS 1.3). Eerst bevestigen met
   één verbinding per firmwarevariant, waarbij de broker de gekozen suite logt.
3. **Wie er mag verbinden: gecontroleerd bij het aannemen van de verbinding, vóór TLS.**
   - Bekend ip-adres (`ip` van een Zendure-subentry): door; de client-id moet de `deviceId` van die
     subentry zijn.
   - Onbekend adres: alleen door tijdens een koppelvenster (bij koppelen via Bluetooth zet de broker de
     `deviceId` uit de handdruk vooraf op de lijst, 10 minuten), of als `http://<ip>/properties/report` een
     Zendure-device teruggeeft. Daarna de bestaande ontdekking, die Jan bevestigt.
   - Al het andere: verbinding direct dicht.
4. **Parser: eenvoudig.** Door punt 3 ziet de parser alleen echte devices. Vangnet: maximale
   pakketgrootte 16 kB en een time-out op het lezen. Ter vergelijking: de firmware knipt rapporten op
   756 bytes (`mqtt_report_publish`) en logregels op 1024 tekens.
5. **Bereikbaarheid: alleen op het LAN-adres van HA**, niet op de interne bridges. Poort 8883 ligt vast in
   de firmware. Is 8883 bezet (EMQX), dan een duidelijke melding en geen broker.
6. **Logging: geen ruwe inhoud in het gewone log**, alleen topic, device en grootte. Ruwe inhoud alleen via
   de bestaande schakelaar `mqtt_log`, met `token` en wachtwoordvelden gemaskeerd vóór het wegschrijven.

**Als EMQX poort 8883 blijft hosten met het Zendure-certificaat** (het EMQ-testcertificaat):

- Andere TLS-clients die het certificaat controleren, kunnen daar niet verbinden: het valt niet onder een
  publieke CA en de naam (`CN=Server`) klopt niet met het adres. Alleen clients met uitgezette
  certificaatcontrole, of die de EMQ-root expliciet vertrouwen, verbinden wel.
- Poort 1883 (zonder TLS) staat daar los van.
- Voor andere apparaten met TLS: een tweede TLS-listener in EMQX op een andere poort (bijvoorbeeld 8884)
  met een eigen, geldig certificaat. Poort 8883 blijft dan alleen voor de Zendure-devices.
- Wie bij EMQX verbindt, hangt af van de login- en ACL-instellingen, niet van het certificaat. De
  handleiding `docs/emqx6-zendure-setup.md` zet `No Match Action` op `allow`, en de devices loggen in met
  een vaste login die in hun opstartlog staat.

### Fase 4: firmware via het account

Besloten 2026-11-20: ja, met het account in de instellingen. Reden: met de devices lokaal is de app niet
meer bruikbaar voor updates, en zonder account zijn nieuwe versies niet te vinden (de bucket weigert een
lijst, de adressen bevatten de bouwtijd tot op de seconde). De catalogus werkt ook terwijl de devices
lokaal zijn (gemeten op 19 november).

- Accountlogin (gebruikersnaam en wachtwoord van de Zendure-app) als instelling van de Zendure-driver,
  het wachtwoord als wachtwoordveld. Het staat onversleuteld in de opslag van HA (`.storage`).
- Eén keer per week de catalogus via `ota/batch/firmware` (werkwijze in `tools/zendure_firmware.py`).
- De cloud kent de werkelijke versie niet meer (toont 2.0.28 voor devices op 3.0.21); ZenMaster vergelijkt
  met de versie die het device lokaal meldt (`Device.GetVer` of het firmwarerapport over MQTT).
- Alleen voor eigen gebruik; geen openbare lijst met downloadadressen en geen verspreiding van de
  bestanden.
- Bestanden met md5-controle in de HA-opslag, geserveerd door het eindpunt met `Range`.
- Per device een HA-update-entiteit; installeren = melding op `/firmware/download`; voortgang uit
  `firmware/upgrade/progress`.

### Fase 5: koppelen en opruimen

- Het koppelen via Bluetooth blijft nodig voor nieuwe devices, een wifi-wijziging, zenSDK 2, de Hyper en
  de oude modellen, en om het API-adres te zetten. Een zenSDK 3-device op wifi kan zonder Bluetooth naar
  een andere broker (`Mqtt.SetConfig`).
- De apptoken en `/api/ha/deviceList` gaan uit de driver (`zendure/driver.py:134-165`).
- `cloud_url` wordt nog gelezen door het koppelscherm (`ui/src/components/zm-ble-onboard.ts:26`).

Instellingen van de Zendure-driver (besloten 2026-11-20), nu `zendure/driver.py:89-100`:

- Sectie cloud (`apptoken`, `cloud_url`): vervalt.
- Sectie lokale broker (`mqtt_host`, `mqtt_port`, `mqtt_user`, `mqtt_psw`): vervalt met de eigen broker
  (fase 3b); het adres is dat van HA zelf. `mqtt_log` blijft, als expertinstelling.
- Sectie wifi (`wifi_ssid`, `wifi_psw`): vervalt. Het koppelscherm vraagt netwerknaam en wachtwoord bij
  het koppelen en bewaart ze niet; de browser mag de netwerknaam onthouden. `wifi_psw` wordt nu nergens
  gebruikt.
- Nieuw: sectie account met gebruikersnaam en wachtwoord (fase 4).
- Niet getest, maar zou het koppelen verder vereenvoudigen: de netwerken in de buurt via Bluetooth
  (`WiFi.get`), en of zenSDK 3 de koppeling zonder netwerknaam en wachtwoord accepteert.

### Fase 6: documentatie

- `firmware/zensdk_api.md` corrigeren: sectie 16 zegt dat alleen RS485 het API-adres zet; het
  register-antwoord doet dat ook.

## 3. Risico's

- Ontbreekt `mqttUrl` in de platform-config, dan verliest zenSDK 2 zijn broker.
- Met een `hemsId` moet de lokale modus op cloud blijven staan.
- `enable_log` springt na een herstart terug op aan; fase 3 vangt dat op.
- Het eindpunt heeft geen HA-authenticatie; de bescherming is de controle op device-ip en `deviceKey`
  (fase 2). Een apparaat op het LAN dat het ip-adres van een device overneemt, komt erdoor.
- Verandert het ip-adres van een device, dan weigert het eindpunt het tot mDNS het nieuwe adres in de
  subentry heeft gezet.
- Niet getest: Hyper 2000 en de oude Hub/AIO/Ace.
- Een eigen broker (fase 3b) en de EMQX-add-on kunnen niet allebei poort 8883 gebruiken.

## 4. Open keuzes (voor Jan)

1. ~~De waarde van `realTimeRate`.~~ Besloten 2026-11-20: 2.
2. ~~Het schema voor de `hemsId` per device.~~ Besloten 2026-11-20: afgeleid van het serienummer
   (100000 + crc32 % 900000).
3. ~~Het eindpunt op poort 8123 of een eigen poort.~~ Besloten 2026-11-20: 8123, alleen eigen device-ip's
   (zie fase 2).
4. ~~Firmware-updates via de integratie: ja of nee.~~ Besloten 2026-11-20: ja, met het account in de
   instellingen (zie fase 4).
5. ~~De knop Cloud bij het koppelen.~~ Besloten 2026-11-20: weg.
6. ~~Voor zenSDK 2: `mode` en `chargeMode` in het HEMS-antwoord.~~ Besloten 2026-11-20: zenSDK 2 krijgt
   geen `hemsId`.
7. Sturen over MQTT of over zenSDK-HTTP: blijft zenSDK-HTTP (besluit 21-09); MQTT pas na een test.

## 5. Stand van de devices (20 november)

| device | ip | zenSDK | broker | API-adres | hemsId |
|---|---|---|---|---|---|
| SF2400_12 | 192.168.1.102 | 3.0.21 | HA | testeindpunt | geen |
| SF2400_37 | 192.168.1.70 | 3.0.21 | HA | testeindpunt | geen |
| SF800_88 | 192.168.1.86 | 3.0.18 | HA | testeindpunt | 880001 |
| SF1600_62 | 192.168.1.24 | 3.0.21 | HA | testeindpunt | 160001 |

- Het testeindpunt (`http://192.168.1.250:8199`) draait niet meer. De verzoeken van de devices mislukken
  daardoor; volgens de decompile leidt dat niet tot een herstart.
- Op SF1600_62 staat de logupload uit tot de volgende herstart; op de andere drie niet.
- SF800_88 heeft geen accu (`packNum 0`). SF1600_62 was op 19 november aan het kalibreren.
- De andere devices (SF800_90, de SF4000's, de Hyper) zijn niet aangeraakt.

## 6. Gereedschap en waar het staat

- Testscripts, in de scratchpad van sessie f317644e
  (`C:\Users\jbrandsma\AppData\Local\Temp\claude\adac00e93d66ca5d\f317644e-0043-4b87-be17-326e00d340a8\scratchpad\rtr\`):
  `zm_rtr_test.py` (eindpunt en MQTT-luisteraar, draait op de HA), `run_remote.py` (kopieert en start dat
  script over SSH), `provision.py` (koppelt via Bluetooth van deze machine; wifi en broker uit de store van
  ZenMaster, nooit getoond) en `readlog.py` (leest het testlog).
- Eigen broker en latentietest, in `...\scratchpad\probe\`: `latency_test.py` (164 regels, waarvan 72 voor
  de broker), met `latency_log.txt` en `latency_result.json`; `mqtt_probe.py` is de proefbroker die alleen
  logt.
- Ghidra-scripts in `...\scratchpad\gh\`: `StrSub.java`, `RefFn.java`, `CallLines.java`, `GpFile.java`.
  Eerdere scripts en het project `zs3021` staan in de scratchpad van sessie dfb82366 (`...\ghidra\`), de
  ELF in `C:\Users\jbrandsma\AppData\Local\Temp\zs3\`.
- Ghidra 12.1.4 in `C:\Tools\ghidra_12.1.4_PUBLIC`, JDK 21 als `JAVA_HOME`.
- Deze mappen zijn tijdelijk. De `.bin`-bestanden in `firmware/` en `mkelf.py` zijn genoeg om het project
  opnieuw op te bouwen.

## 7. Gebouwd op `local-control` (20 november)

| fase | commit | inhoud |
|---|---|---|
| 1 en 2 | fcb92ca | register-antwoord met `apiUrl`, eindpunt `/api/zenmaster/zendure/...` op 8123 |
| 3 | 5a17c7f | `properties/energy`, `enable_log: false` na `time-sync`, `fetchHems` na een SoC-grens |
| 3b | c4b3107 | eigen broker (`zendure/broker.py`), certificaat in `zendure/certs/` met NOTICE |
| 4 | 2ff9ec9 | account in de instellingen, wekelijkse catalogus, update-entiteit per device |
| 5 | 6976fa3 | apptoken, cloud en wifi weg, broker-velden onder Expert, koppelscherm zonder Cloud |
| 6 | (deze) | dit plan; `firmware/zensdk_api.md` sectie 16, 17 en 21 bijgewerkt, maar `firmware/` staat in `.gitignore` en is dus alleen lokaal |

Alleen met unittests getest (327 geslaagd); niets op een device of op de productie-HA. Afwijkingen en
open punten:

- **Client-id (besluit 3b.3) niet als controle gebouwd.** Bij het bouwen was niet vastgesteld welke
  client-id de firmware stuurt (in de decompile een pointer, geen naam); de livetest hieronder laat zien dat
  het de `deviceId` is. De broker controleert in plaats daarvan het
  `deviceId` in elk topic: een verbinding mag alleen berichten sturen voor de device-id's waarvoor ze is
  toegelaten (bekend ip, koppelvenster of HTTP-controle); de rest wordt gelogd en weggegooid. De
  client-id wordt bij elke verbinding gelogd, zodat dit bij de eerste devicetest vast te stellen is.
- **TLS-suite per firmware (besluit 3b.2):** zenSDK 3.0.21 kiest ECDHE-RSA-AES128-GCM-SHA256 (livetest
  hieronder). zenSDK 2 nog niet bevestigd.

**Livetest 20 november, 12:26 tot 12:31:** de broker van deze branch (echte driver en broker, nep-HA, geen
device-loop en dus geen setpoints) op deze pc (192.168.1.99), SF2400_37 erheen gezet met `Mqtt.SetConfig`
en daarna terug naar 192.168.1.250 (op de HA-host gecontroleerd: verbinding weer open op 8883).

- Verbinding binnen 18 s na `SetConfig`, toegelaten op bekend ip, TLS 1.2 met ECDHE-RSA-AES128-GCM-SHA256.
- **Client-id = de `deviceId`** (`MPEt6K1P`). De controle uit besluit 3b.3 kan dus alsnog gebouwd worden.
- In 4,7 minuten 105 rapporten, 3 firmwarerapporten, 3 `log`-berichten (ongeveer elke 78 s), 4
  `event/error` (eventId 3); geen verbroken verbinding, niets weggegooid.
- `time-sync` beantwoord en `enable_log: false` meegestuurd; het firmwarerapport zette de versie op 3.0.21.
- De broker stuurde `properties/read` twee keer: het device stuurt twee SUBSCRIBE-pakketten.
- Niet getest: registratie, API-adres en `hemsId`. `Mqtt.SetConfig` leidt niet tot een `register`, en het
  device roept de API aan op zijn oude adres (het gestopte testeindpunt); op deze pc kwam geen API-aanroep
  binnen. Daarvoor is opnieuw koppelen via Bluetooth nodig.
- De foutmelding `'NoneType' object is not callable` bij rapporten met accu's kwam uit het harnas
  (`on_invalidate` zet de coordinator in HA), niet uit de branch.
- Harnas: `...\scratchpad\live\broker_live.py`, log `broker_live.log`.
- **Voortgang van een firmware-update** komt uit de decompile van 2.0.59 (`firmware/download/progress`,
  `firmware/upgrade/progress`, velden `progress`, `success`, `complete`); niet live gezien.
- **De update-entiteit vergelijkt alleen zenSDK (ESP32).** Andere modules (AC, MASTER, BMS) staan in de
  samenvatting en gaan mee bij installeren; het device installeert alleen wat nieuwer is. Een zenSDK
  2-device meldt zijn versie niet; de geïnstalleerde versie is dan onbekend.
- **De firmwarebestanden** worden pas bij installeren gedownload, uit de adressen van de laatste
  catalogus.
- **Koppelvenster**: het koppelscherm meldt de `deviceId` uit de Bluetooth-handdruk via
  `zenmaster/zendure_pair`; daarna mag een onbekend adres 10 minuten verbinden.
- **Overstap**: zolang `mqtt_host` is ingevuld, blijft de driver op die broker (EMQX). Pas met een leeg
  `mqtt_host` start de eigen broker, en die moet 8883 vrij hebben. De devices staan nu op EMQX en
  hebben als API-adres het gestopte testeindpunt; na de deploy zet de eerste registratie (opnieuw
  koppelen) het echte adres.
