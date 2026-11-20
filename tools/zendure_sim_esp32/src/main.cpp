// Zendure test device on an ESP32: emulates one of several Zendure devices for testing ZenMaster.
//
// Profiles: ACE1500, Hyper2000, Hub1200, Hub2000. The active profile and identity are stored in NVS.
// Configure it in two ways:
//   - serial console (115200), any time before pairing;
//   - a web page at http://zensim.local/ once the device is on your LAN (after the first pairing).
//
// Flow, like a real device:
//   1. First boot (no stored Wi-Fi): BLE pairing (service a002, write c304, notify c305). The ZenMaster
//      onboarding screen sends deviceInfo / token / station; the token carries Wi-Fi + the broker.
//   2. The device stores the Wi-Fi, joins your LAN, and connects to the broker (TLS 8883, client id =
//      deviceId). From then on every boot rejoins the LAN directly and serves the config page there, so
//      changing the device type does not need another BLE pairing (use "Forget Wi-Fi" to pair again).
//
// BLE name follows the legacy/Hyper rule: Zen<product letter><sn[2]>_<sn[-2:]> (letter per product, not
// sn[0]), from solarflow-bt-manager. Reports/commands from recordings (ACE, Hyper) and the Zendure-HA
// reference (Hubs). "assumed" marks anything not seen on real hardware.

#include <Arduino.h>
#include <time.h>
#include <sys/time.h>
#include <Preferences.h>
#include <NimBLEDevice.h>
#include <WiFi.h>
#include <ESPmDNS.h>
#include <WiFiClientSecure.h>
#include <WebServer.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <MD5Builder.h>

enum Style { ST_ACE, ST_HYPER, ST_HUB };

struct Profile {
  const char* name;
  const char* pk;
  const char* product;
  const char* sn;
  const char* did;
  char letter;   // BLE name product letter
  int outMax;
  int inMax;
  float kwh;
  Style style;
};

static const Profile PROFILES[] = {
  {"ace1500",  "8bM93H", "ace",         "FE1HTMCFM2SIM01", "SimAce01", 'F', 800,  900, 1.5f, ST_ACE},
  {"hyper2000","gDa3tb", "solarFlowPro","HO1ANPN3NSIM02",  "SimHyp01", 'E', 1200, 1200, 1.92f, ST_HYPER},
  {"hub1200",  "73bkTV", "solarFlow",   "PO1HLCSIMN00003", "SimHub12", 'P', 1200, 0,   0.96f, ST_HUB},
  {"hub2000",  "a8yh63", "solarFlow",   "WO1BNSIMN000004", "SimHub20", 'H', 1200, 0,   0.96f, ST_HUB},
};
static const int N_PROFILES = sizeof(PROFILES) / sizeof(PROFILES[0]);

static Preferences g_prefs;
static int g_profile = 0;
static String g_sn, g_did, g_pk, g_product, g_bleName;
static char g_letter;
static int g_outMax, g_inMax;
static float g_kwh;
static Style g_style;
static String g_ssid, g_pass, g_iotUrl, g_token;  // Wi-Fi + broker, persisted after pairing
static String g_bUser, g_bPass;                   // MQTT broker login
static bool g_hasWifi = false;
static String g_homeId;              // account home from the BLE token, if the app sends one
static volatile bool g_reRegister = false;  // a new BLE token arrived while connected: re-register on the loop task
static bool g_bound = false;         // register/replay with result 0 succeeded: report bindstate=1 like a real device
static String g_apiUrl, g_userId;    // stored from the register reply, like the real device does

static const char* SVC_UUID   = "0000a002-0000-1000-8000-00805f9b34fb";
static const char* CHAR_WRITE = "0000c304-0000-1000-8000-00805f9b34fb";
static const char* CHAR_READ  = "0000c305-0000-1000-8000-00805f9b34fb";
static const int   MQTT_PORT  = 8883;

static volatile bool g_provisioned = false;

NimBLECharacteristic* g_notify = nullptr;
WiFiClientSecure g_net;   // TLS, for the local broker
WiFiClient g_netPlain;    // plain, for the cloud broker (like zendure_ha)
PubSubClient g_mqtt(g_net);
WebServer g_web(80);
static bool g_webStarted = false;

static float g_soc = 55.0f;
static long  g_target = 0;
static unsigned long g_targetAt = 0;
static unsigned long g_msg = 16000;
static bool g_bleStream = false;  // stream reports over BLE after a read getAll, like the real device
// Wi-Fi networks scanned once at boot (before BLE takes the radio), served on WiFi.get
static const int MAX_AP = 20;
static String g_apSsid[MAX_AP];
static int g_apRssi[MAX_AP], g_apCh[MAX_AP], g_apAuth[MAX_AP], g_apCount = 0;
static uint8_t g_apBssid[MAX_AP][6];  // cached so the join can skip the scan (BLE + WiFi scan coexist badly on the C3)

// legacy/Hyper BLE name: Zen<product letter><sn[2]>_<sn[-2:]>
static String bleName(char letter, const String& sn) {
  if (sn.length() < 3) return String("Zen") + letter + "0_00";
  return String("Zen") + letter + sn[2] + "_" + sn.substring(sn.length() - 2);
}

static void loadConfig() {
  g_prefs.begin("zensim", true);
  g_profile = constrain(g_prefs.getInt("profile", 0), 0, N_PROFILES - 1);
  const Profile& p = PROFILES[g_profile];
  g_sn = g_prefs.getString("sn", p.sn);
  g_did = g_prefs.getString("did", p.did);
  g_pk = g_prefs.getString("pk", p.pk);
  g_ssid = g_prefs.getString("wssid", "");
  g_pass = g_prefs.getString("wpass", "");
  g_iotUrl = g_prefs.getString("wiot", "");
  g_bUser = g_prefs.getString("buser", "");
  g_bPass = g_prefs.getString("bpass", "");
  g_prefs.end();
  g_product = p.product; g_letter = p.letter; g_outMax = p.outMax; g_inMax = p.inMax; g_kwh = p.kwh; g_style = p.style;
  g_bleName = bleName(g_letter, g_sn);
  g_hasWifi = g_ssid.length() > 0;
}

static void saveStr(const char* key, const String& v) {
  g_prefs.begin("zensim", false); g_prefs.putString(key, v); g_prefs.end();
}

// ---- serial console ----
static void handleSerial() {
  static String line;
  while (Serial.available()) {
    char ch = Serial.read();
    if (ch != '\n' && ch != '\r') { line += ch; continue; }
    line.trim();
    if (line.length()) {
      int sp = line.indexOf(' ');
      String cmd = sp < 0 ? line : line.substring(0, sp);
      String arg = sp < 0 ? "" : line.substring(sp + 1); arg.trim();
      if (cmd == "show") {
        bool svc = NimBLEDevice::getInitialized() && NimBLEDevice::getServer() && NimBLEDevice::getServer()->getServiceByUUID(SVC_UUID) != nullptr;
        Serial.printf("profile=%s sn=%s deviceId=%s prodkey=%s ble=%s wifi=%s | notify=%s service_a002=%s\n", PROFILES[g_profile].name, g_sn.c_str(), g_did.c_str(), g_pk.c_str(), g_bleName.c_str(), g_ssid.c_str(), g_notify ? "ok" : "null", svc ? "ok" : "MISSING");
      }
      else if (cmd == "profile") {
        int idx = -1; for (int i = 0; i < N_PROFILES; i++) if (arg == PROFILES[i].name) idx = i;
        if (idx < 0) Serial.println("one of: ace1500 hyper2000 hub1200 hub2000");
        else { g_prefs.begin("zensim", false); g_prefs.putInt("profile", idx); g_prefs.remove("sn"); g_prefs.remove("did"); g_prefs.remove("pk"); g_prefs.end(); Serial.println("set; reboot to apply"); }
      } else if (cmd == "sn") { saveStr("sn", arg); Serial.println("set; reboot"); }
      else if (cmd == "deviceid") { saveStr("did", arg); Serial.println("set; reboot"); }
      else if (cmd == "prodkey") { saveStr("pk", arg); Serial.println("set; reboot"); }
      else if (cmd == "wssid") { saveStr("wssid", arg); Serial.println("set; set wpass + reboot"); }
      else if (cmd == "wpass") { saveStr("wpass", arg); Serial.println("set; reboot to join"); }
      else if (cmd == "broker") { saveStr("wiot", arg); Serial.println("set; reboot"); }
      else if (cmd == "forget") { g_prefs.begin("zensim", false); g_prefs.remove("wssid"); g_prefs.remove("wpass"); g_prefs.remove("wiot"); g_prefs.end(); Serial.println("wifi cleared; reboot to pair"); }
      else if (cmd == "reboot") ESP.restart();
      else Serial.println("commands: show | profile <name> | sn <> | deviceid <> | prodkey <> | wssid <> | wpass <> | broker <> | forget | reboot");
    }
    line = "";
  }
}

// ---- web config on the LAN (http://zensim.local/) ----
static void handleRoot() {
  String h = "<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
             "<style>body{font-family:sans-serif;margin:24px;max-width:460px}input{width:100%;box-sizing:border-box;padding:6px;margin:3px 0}button{padding:10px;width:100%;margin-top:12px}</style>"
             "<h2>Zendure sim</h2><form method=POST action=/save>";
  for (int i = 0; i < N_PROFILES; i++)
    h += "<label><input type=radio name=profile style='width:auto' value=" + String(i) + (i == g_profile ? " checked" : "") + "> " + PROFILES[i].name + "</label><br>";
  h += "<p>Overrides (blank = profile default):</p>"
       "serial<input name=sn placeholder='" + g_sn + "'>"
       "deviceId<input name=deviceid placeholder='" + g_did + "'>"
       "productKey<input name=prodkey placeholder='" + g_pk + "'>"
       "<p>MQTT broker:</p>"
       "host<input name=broker value='" + g_iotUrl + "'>"
       "username<input name=buser value='" + g_bUser + "'>"
       "password<input name=bpass type=password placeholder='" + String(g_bPass.length() ? "(set)" : "") + "'>"
       "<p>BLE name: <b>" + g_bleName + "</b></p>"
       "<button>Save &amp; reboot</button></form>"
       "<form method=POST action=/forget><button style='background:#eee'>Forget Wi-Fi &amp; re-pair</button></form>";
  g_web.send(200, "text/html", h);
}

static void handleSave() {
  g_prefs.begin("zensim", false);
  g_prefs.putInt("profile", constrain(g_web.arg("profile").toInt(), 0, N_PROFILES - 1));
  const char* keys[][2] = {{"sn", "sn"}, {"did", "deviceid"}, {"pk", "prodkey"}};
  for (auto& k : keys) { String v = g_web.arg(k[1]); v.trim(); if (v.length()) g_prefs.putString(k[0], v); else g_prefs.remove(k[0]); }
  String broker = g_web.arg("broker"); broker.trim();
  if (broker.length()) g_prefs.putString("wiot", broker); else g_prefs.remove("wiot");
  g_prefs.putString("buser", g_web.arg("buser"));           // blank = anonymous
  String bp = g_web.arg("bpass"); if (bp.length()) g_prefs.putString("bpass", bp);  // keep the old password when the field is left blank
  g_prefs.end();
  g_web.send(200, "text/html", "<meta http-equiv=refresh content='4;url=/'>Saved, rebooting...");
  delay(400); ESP.restart();
}

static void handleForget() {
  g_prefs.begin("zensim", false); g_prefs.remove("wssid"); g_prefs.remove("wpass"); g_prefs.remove("wiot"); g_prefs.end();
  g_web.send(200, "text/html", "Wi-Fi cleared, rebooting into pairing...");
  delay(400); ESP.restart();
}

static void startWeb() {
  if (g_webStarted) return;
  g_web.on("/", handleRoot);
  g_web.on("/save", HTTP_POST, handleSave);
  g_web.on("/forget", HTTP_POST, handleForget);
  g_web.begin();
  g_webStarted = true;
}

static void notifyJson(JsonDocument& doc) {
  String out; serializeJson(doc, out);
  if (g_notify) { g_notify->setValue((uint8_t*)out.c_str(), out.length()); g_notify->notify(); }
}

// a telemetry report over BLE (method "report"), the same shape the device streams after a read getAll
static void bleReport(bool full) {
  JsonDocument d; d["method"] = "report"; JsonObject p = d["properties"].to<JsonObject>();
  p["electricLevel"] = (int)roundf(g_soc);
  long t = g_target;
  if (g_style == ST_HYPER) { p["outputPower"] = (int)max(0L, t); p["chargePower"] = (int)max(0L, -t); }
  else if (g_style == ST_ACE) { p["acOutputPower"] = (int)max(0L, t); }
  else { p["outputHomePower"] = (int)max(0L, t); }
  if (full) { p["socSet"] = 1000; p["minSoc"] = 0; p["inverseMaxPower"] = g_outMax; }
  notifyJson(d);
}

// ---- BLE pairing (only until Wi-Fi is stored) ----
class WriteCb : public NimBLECharacteristicCallbacks {
  void onRead(NimBLECharacteristic*) override { Serial.println("BLE read c304"); }
  void onWrite(NimBLECharacteristic* c) override {
    std::string v = c->getValue();
    JsonDocument in;
    if (deserializeJson(in, v.c_str(), v.size())) return;
    const char* method = in["method"] | "";
    Serial.printf("BLE <- %s\n", v.c_str());
    if (!strcmp(method, "deviceInfo")) {
      // real device reply: method deviceInfo, success 1, deviceId, slaveSn (from a phone btsnoop capture)
      JsonDocument out; out["method"] = "deviceInfo"; out["timestamp"] = (uint32_t)time(nullptr); out["messageId"] = in["messageId"];
      out["success"] = 1; out["deviceId"] = g_did; out["slaveSn"] = g_sn; notifyJson(out);
    } else if (!strcmp(method, "getInfo")) {
      // real device reply: method getInfo-rsp, with wifi/mac/modules
      JsonDocument out; out["method"] = "getInfo-rsp"; out["messageId"] = in["messageId"]; out["timestamp"] = (uint32_t)time(nullptr);
      out["deviceId"] = g_did; out["sn"] = g_sn; out["ip"] = WiFi.localIP().toString(); out["wifiName"] = g_ssid;
      out["mac"] = WiFi.macAddress(); out["blemac"] = NimBLEDevice::getAddress().toString().c_str();
      JsonArray mods = out["modules"].to<JsonArray>();
      { JsonObject m = mods.add<JsonObject>(); m["module"] = "MASTER"; m["version"] = 4133; }
      { JsonObject m = mods.add<JsonObject>(); m["module"] = "ESP32"; m["version"] = 12309; }
      notifyJson(out);
    } else if (!strcmp(method, "token")) {
      // store only the fields present, so the real two-phase token (ssid/password, then iotUrl/token) works as well as a single message
      g_prefs.begin("zensim", false);
      if (in["ssid"].is<const char*>()) { g_ssid = (const char*)in["ssid"]; g_prefs.putString("wssid", g_ssid); }
      if (in["password"].is<const char*>()) { g_pass = (const char*)in["password"]; g_prefs.putString("wpass", g_pass); }
      if (in["iotUrl"].is<const char*>() && strlen(in["iotUrl"] | "")) { g_iotUrl = (const char*)in["iotUrl"]; g_prefs.putString("wiot", g_iotUrl); }
      if (in["token"].is<const char*>()) g_token = (const char*)in["token"];
      if (!in["homeId"].isNull()) g_homeId = in["homeId"].as<String>();
      g_prefs.end();
      bool complete = g_ssid.length() && g_iotUrl.length() && g_token.length();
      Serial.printf("token: ssid=%s iotUrl=%s homeId=%s complete=%d\n", g_ssid.c_str(), g_iotUrl.c_str(), g_homeId.c_str(), complete);
      // the app sends no "station"; the real device's token handler starts the connect itself
      if (complete) { if (g_provisioned) g_reRegister = true; else { g_provisioned = true; g_hasWifi = true; } }
      // real device reply: method "Token" (capital), with productModel, result 0 (from a phone btsnoop capture)
      JsonDocument out; out["messageId"] = in["messageId"]; out["productModel"] = g_product; out["method"] = "Token"; out["result"] = 0; notifyJson(out);
    } else if (!strcmp(method, "read")) {
      // getAll: stream reports over BLE, like the real device's method_read
      for (JsonVariant v : in["properties"].as<JsonArray>()) if (!strcmp(v | "", "getAll")) { g_bleStream = true; bleReport(true); }
    } else if (!strcmp(method, "write")) {
      JsonObject p = in["properties"];
      if (p["outputLimit"].is<int>() || p["inputLimit"].is<int>())
        g_target = constrain((long)(p["outputLimit"] | 0) - (long)(p["inputLimit"] | 0), (long)-g_inMax, (long)g_outMax);
      JsonDocument out; out["method"] = "write"; out["messageId"] = in["messageId"]; out["success"] = 1; notifyJson(out);
    } else if (!strcmp(method, "WiFi.get")) {
      // one WiFi.set batch of up to 5 networks, keys like the real device (ssid/signal/Primary/AM); separate notifies get dropped
      int msgId = in["messageId"] | 0;
      Serial.printf("WiFi.get: %d cached networks\n", g_apCount);
      JsonDocument out; out["messageId"] = msgId; out["method"] = "WiFi.set";
      JsonArray arr = out["WiFi.list"].to<JsonArray>();
      for (int i = 0; i < g_apCount && i < 5; i++) {
        JsonObject ap = arr.add<JsonObject>();
        ap["SSID"] = g_apSsid[i]; ap["signal"] = g_apRssi[i]; ap["Primary"] = g_apCh[i]; ap["AM"] = g_apAuth[i];
      }
      String s; serializeJson(out, s);
      Serial.printf("WiFi.set out (%d bytes): %s\n", s.length(), s.c_str());
      g_notify->setValue((uint8_t*)s.c_str(), s.length()); g_notify->notify();
    } else if (!strcmp(method, "station")) {
      g_provisioned = true;
    } else if (!strcmp(method, "BLEBind")) {
      // the app's final pairing step (method_bind): confirm and reboot, like the real device
      JsonDocument out; out["method"] = "BLEBind"; out["messageId"] = in["messageId"]; out["result"] = 0; notifyJson(out);
      Serial.println("BLEBind: bind confirmed, rebooting");
      delay(200); ESP.restart();
    }
  }
};

// the notify characteristic (c305): greet with BLESPP only once the client subscribes, so it is delivered
class NotifyCb : public NimBLECharacteristicCallbacks {
  void onSubscribe(NimBLECharacteristic*, ble_gap_conn_desc*, uint16_t subValue) override {
    Serial.printf("BLE subscribe c305: subValue=%d\n", subValue);
    if (subValue & 0x01) { JsonDocument out; out["method"] = "BLESPP"; notifyJson(out); }
  }
  void onRead(NimBLECharacteristic*) override { Serial.println("BLE read c305"); }
};

class ServerCb : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer*, ble_gap_conn_desc* d) override {
    Serial.printf("BLE connect: conn=%d peer=%s\n", d->conn_handle, NimBLEAddress(d->peer_id_addr).toString().c_str());
  }
  void onDisconnect(NimBLEServer*, ble_gap_conn_desc* d) override {
    Serial.printf("BLE disconnect: conn=%d\n", d->conn_handle);
  }
  void onMTUChange(uint16_t mtu, ble_gap_conn_desc* d) override {
    Serial.printf("BLE mtu=%d conn=%d\n", mtu, d->conn_handle);
  }
};

static void startBle() {
  NimBLEDevice::init(g_bleName.c_str());
  NimBLEServer* server = NimBLEDevice::createServer();
  server->setCallbacks(new ServerCb());
  NimBLEService* svc = server->createService(SVC_UUID);
  g_notify = svc->createCharacteristic(CHAR_READ, NIMBLE_PROPERTY::NOTIFY);
  g_notify->setCallbacks(new NotifyCb());
  NimBLECharacteristic* w = svc->createCharacteristic(CHAR_WRITE, NIMBLE_PROPERTY::WRITE | NIMBLE_PROPERTY::WRITE_NR);
  w->setCallbacks(new WriteCb());
  svc->start();
  NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
  // like the real device (update_adv_data): manufacturer data 0xFF = the serial (+1 byte), name in the scan response.
  // ZenMaster/HA identify the device by that serial tail, so it must be advertised.
  std::string mfr(g_sn.c_str(), g_sn.length()); mfr.push_back((char)0x16);  // product/type byte like the real device (SF1600 advertises 0x16); test value
  adv->setManufacturerData(mfr);
  adv->addServiceUUID(SVC_UUID);  // 16-bit a002, so Windows/Chrome do a full GATT discovery; fits beside the manufacturer data
  adv->setScanResponse(true);  // the name goes in the scan response
  adv->start();
  Serial.printf("BLE advertising as %s (profile %s), mfr=serial\n", g_bleName.c_str(), PROFILES[g_profile].name);
}

// ---- MQTT ----
static String topic(const char* sub) { return String("/") + g_pk + "/" + g_did + "/" + sub; }

static void publish(const char* sub, JsonDocument& body) {
  body["messageId"] = ++g_msg; body["deviceId"] = g_did; body["timestamp"] = (uint32_t)time(nullptr);
  String out; serializeJson(body, out);
  Serial.printf("MQTT -> %s %s\n", topic(sub).c_str(), out.c_str());
  g_mqtt.publish(topic(sub).c_str(), out.c_str());
}
static void reportProps(JsonObject props) { JsonDocument b; b["product"] = g_product; b["properties"] = props; publish("properties/report", b); }

static void sendFirmware() {
  JsonDocument fw; JsonArray mods = fw["modules"].to<JsonArray>();
  if (g_style == ST_ACE) { const char* n[] = {"MASTER","BMS","DC","AC","MPPT"}; int v[] = {4112,8223,4104,4102,4118}; for (int i=0;i<5;i++){ JsonObject m=mods.add<JsonObject>(); m["module"]=n[i]; m["version"]=v[i]; } }
  else { const char* n[] = {"MASTER","ESP32"}; int v[] = {4133,12309}; for (int i=0;i<2;i++){ JsonObject m=mods.add<JsonObject>(); m["module"]=n[i]; m["version"]=v[i]; } }
  fw["deviceSn"] = g_sn; publish("firmware/report", fw);
}

static void sendFullReport() {
  JsonDocument d; JsonObject p = d.to<JsonObject>();
  p["electricLevel"] = (int)roundf(g_soc); p["socSet"] = 1000; p["minSoc"] = 0;
  p["gridInputPower"] = 0; p["outputHomePower"] = 0; p["solarInputPower"] = 0;
  if (g_style == ST_ACE) { p["acOutputPower"] = 0; p["inputLimit"] = g_inMax; p["acMode"] = 2; p["autoModel"] = 0; }
  else { p["inverseMaxPower"] = g_outMax; p["chargeMaxLimit"] = g_inMax; p["acMode"] = 2; p["gridState"] = 1; }
  reportProps(p);
}

static void getAll(int messageId) {
  g_mqtt.publish(topic("properties/read/reply").c_str(), (String("{\"messageId\":") + messageId + ",\"success\":1,\"properties\":{\"getAll\":1}}").c_str());
  sendFirmware(); sendFullReport();
}

// register with the cloud and ask the time, like a device at every connect; also re-run after a new BLE token
static void sendRegister() {
  JsonDocument reg; JsonObject params = reg["params"].to<JsonObject>(); params["token"] = g_token; params["deviceSn"] = g_sn;
  publish("register", reg);
  JsonDocument tsreq; publish("time-sync", tsreq);
}

static void applyCommand(const String& sub, JsonDocument& in) {
  long target = g_target;
  if (sub.endsWith("/function/invoke")) {
    const char* fn = in["function"] | "";
    if (!strcmp(fn, "deviceAutomation")) {
      JsonObject arg = in["arguments"][0]; int model = arg["autoModel"] | 0; JsonVariant val = arg["autoModelValue"];
      if (model == 0) target = 0;
      else if (val.is<JsonObject>()) { long charge = ((int)(val["chargingType"] | 0) == 1) ? (long)(val["chargingPower"] | 0) : 0; target = (long)(val["outPower"] | 0) - charge; }
      else target = (long)(val | 0);
    } else if (!strcmp(fn, "hemsEP")) { JsonObject a = in["arguments"]; target = (long)(a["outputPower"] | 0) - (long)(a["chargePower"] | 0); }
  } else if (sub.endsWith("/properties/write")) {
    JsonObject props = in["properties"];
    if (props["outputLimit"].is<int>() || props["inputLimit"].is<int>()) target = (long)(props["outputLimit"] | 0) - (long)(props["inputLimit"] | 0);
  }
  g_target = constrain(target, (long)-g_inMax, (long)g_outMax);
  g_targetAt = millis() + 2000;
}

static void onMqtt(char* t, byte* payload, unsigned int len) {
  Serial.printf("MQTT <- %s %.*s\n", t, (int)len, (char*)payload);
  JsonDocument in; if (deserializeJson(in, payload, len)) return;
  String ts(t);
  if (ts.endsWith("/properties/read")) getAll(in["messageId"] | 0);
  else if (ts.endsWith("/function/invoke") || ts.endsWith("/properties/write")) { applyCommand(ts, in); g_mqtt.publish((ts + "/reply").c_str(), "{\"success\":1}"); }
  else if (ts.endsWith("/register/replay")) {
    // result 0 = success (real firmware: result != 0 is a failure); store apiUrl/userId and set bindstate, then report it
    JsonObject pr = in["params"];
    int result = pr["result"] | -1;
    if (result == 0) { g_bound = true; g_apiUrl = (const char*)(pr["apiUrl"] | ""); g_userId = pr["userId"].as<String>(); }  // store the cloud's apiUrl/userId like a real device
    Serial.printf("registered, result=%d apiUrl=%s userId=%s bound=%d\n", result, g_apiUrl.c_str(), g_userId.c_str(), g_bound);
  }
  else if (ts.endsWith("/time-sync/reply")) {
    // the controller returns the time; set the clock so later timestamps are real, like a device at every connect
    long t = in["timestamp"] | 0L;
    if (t > 1700000000L) { struct timeval tv = {.tv_sec = t, .tv_usec = 0}; settimeofday(&tv, nullptr); }
    Serial.printf("time-sync: timestamp=%ld zoneOffset=%s\n", t, (const char*)(in["zoneOffset"] | ""));
  }
}

// EMQX stock RootCA: the Zendure cloud broker's chain validates against it (name not checked), like ZenMaster v0.1.12
static const char* EMQX_ROOTCA = R"(-----BEGIN CERTIFICATE-----
MIIDUTCCAjmgAwIBAgIJAPPYCjTmxdt/MA0GCSqGSIb3DQEBCwUAMD8xCzAJBgNV
BAYTAkNOMREwDwYDVQQIDAhoYW5nemhvdTEMMAoGA1UECgwDRU1RMQ8wDQYDVQQD
DAZSb290Q0EwHhcNMjAwNTA4MDgwNjUyWhcNMzAwNTA2MDgwNjUyWjA/MQswCQYD
VQQGEwJDTjERMA8GA1UECAwIaGFuZ3pob3UxDDAKBgNVBAoMA0VNUTEPMA0GA1UE
AwwGUm9vdENBMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAzcgVLex1
EZ9ON64EX8v+wcSjzOZpiEOsAOuSXOEN3wb8FKUxCdsGrsJYB7a5VM/Jot25Mod2
juS3OBMg6r85k2TWjdxUoUs+HiUB/pP/ARaaW6VntpAEokpij/przWMPgJnBF3Ur
MjtbLayH9hGmpQrI5c2vmHQ2reRZnSFbY+2b8SXZ+3lZZgz9+BaQYWdQWfaUWEHZ
uDaNiViVO0OT8DRjCuiDp3yYDj3iLWbTA/gDL6Tf5XuHuEwcOQUrd+h0hyIphO8D
tsrsHZ14j4AWYLk1CPA6pq1HIUvEl2rANx2lVUNv+nt64K/Mr3RnVQd9s8bK+TXQ
KGHd2Lv/PALYuwIDAQABo1AwTjAdBgNVHQ4EFgQUGBmW+iDzxctWAWxmhgdlE8Pj
EbQwHwYDVR0jBBgwFoAUGBmW+iDzxctWAWxmhgdlE8PjEbQwDAYDVR0TBAUwAwEB
/zANBgkqhkiG9w0BAQsFAAOCAQEAGbhRUjpIred4cFAFJ7bbYD9hKu/yzWPWkMRa
ErlCKHmuYsYk+5d16JQhJaFy6MGXfLgo3KV2itl0d+OWNH0U9ULXcglTxy6+njo5
CFqdUBPwN1jxhzo9yteDMKF4+AHIxbvCAJa17qcwUKR5MKNvv09C6pvQDJLzid7y
E2dkgSuggik3oa0427KvctFf8uhOV94RvEDyqvT5+pgNYZ2Yfga9pD/jjpoHEUlo
88IGU8/wJCx3Ds2yc8+oBg/ynxG8f/HmCC1ET6EHHoe2jlo8FpU/SgGtghS1YL30
IWxNsPrUP+XsZpBJy/mvOhE5QXo6Y35zDqqj8tI7AGmAWu22jg==
-----END CERTIFICATE-----)";

// the cloud-broker password a device uses, derived from its deviceId (zendure_ha api.py); never printed
static String brokerPass() {
  MD5Builder md5; md5.begin(); md5.add(g_did); md5.calculate();
  String h = md5.toString(); h.toUpperCase();
  return h.substring(8, 24);
}
static bool isIp(const String& h) {
  for (unsigned i = 0; i < h.length(); i++) if (!isdigit(h[i]) && h[i] != '.') return false;
  return h.length() > 0;
}

static void mqttConnect() {
  int port = 8883;  // Zendure devices use TLS 8883 (cloud and local alike; plain 1883 is refused)
  g_net.setInsecure(); g_mqtt.setClient(g_net);  // skip cert check: the cloud cert chains to a public CA, not EMQX RootCA
  g_mqtt.setServer(g_iotUrl.c_str(), port); g_mqtt.setBufferSize(1024); g_mqtt.setCallback(onMqtt);
  String user = g_bUser.length() ? g_bUser : g_did;              // cloud: username = deviceId
  String pass = g_bUser.length() ? g_bPass : brokerPass();        // cloud: password = MD5(deviceId)[8:24]
  int tries = 0;
  while (!g_mqtt.connected() && tries++ < 3) {
    Serial.printf("MQTT connecting to %s:%d user=%s\n", g_iotUrl.c_str(), port, user.c_str());
    bool conn = g_mqtt.connect(g_did.c_str(), user.c_str(), pass.c_str());
    Serial.printf("MQTT connect result=%d state=%d\n", conn, g_mqtt.state());
    if (conn) {
      g_mqtt.subscribe((String("iot/") + g_pk + "/" + g_did + "/#").c_str());
      sendRegister();
      sendFirmware(); sendFullReport(); Serial.println("MQTT connected");
    } else { g_web.handleClient(); delay(1500); }
  }
}

static void emitPower() {
  long t = g_target; if ((g_soc <= 0 && t > 0) || (g_soc >= 100 && t < 0)) t = 0;
  JsonDocument d; JsonObject p = d.to<JsonObject>();
  // the real solarFlowPro (Hyper) energy report is just these four fields (verified on the live broker)
  if (g_style == ST_HYPER) { p["chargePower"] = (int)max(0L, -t); p["outputPower"] = (int)max(0L, t); p["mode"] = 11; p["LCNState"] = 0; JsonDocument b; b["product"] = g_product; b["properties"] = p; publish("properties/energy", b); }
  else if (g_style == ST_ACE) { p["acOutputPower"] = (int)max(0L, t); p["gridInputPower"] = (int)max(0L, -t); p["packInputPower"] = (int)max(0L, t); p["outputPackPower"] = (int)max(0L, -t); reportProps(p); }
  else { p["outputHomePower"] = (int)max(0L, t); p["gridInputPower"] = (int)max(0L, -t); reportProps(p); }
}

static void reportTick() {
  static unsigned long last = 0;
  unsigned long interval = (g_style == ST_HYPER) ? 3000 : 2000;
  if (millis() - last < interval) return;
  float dt = (millis() - last) / 1000.0f; last = millis();
  if (g_targetAt && millis() >= g_targetAt) { emitPower(); g_targetAt = 0; }
  g_soc = constrain(g_soc - g_target * dt / 3600.0f / (g_kwh * 1000.0f) * 100.0f, 0.0f, 100.0f);
  JsonDocument d; JsonObject p = d.to<JsonObject>(); p["electricLevel"] = (int)roundf(g_soc); reportProps(p);
}

static void joinLan() {
  // legacy devices keep BLE up alongside Wi-Fi; do not deinit it here
  // connect on the scanned channel (not a pinned BSSID: that breaks the WPA2/WPA3 auth) so there is no full scan
  static int fails = 0;
  int ch = 0;
  for (int i = 0; i < g_apCount; i++) if (g_apSsid[i] == g_ssid) { ch = g_apCh[i]; break; }
  Serial.printf("Wi-Fi joining %s (pw len %d, ch %d)\n", g_ssid.c_str(), g_pass.length(), ch);
  WiFi.mode(WIFI_STA);
  if (ch) WiFi.begin(g_ssid.c_str(), g_pass.c_str(), ch);
  else WiFi.begin(g_ssid.c_str(), g_pass.c_str());
  int t = 0;
  while (WiFi.status() != WL_CONNECTED && t++ < 20) { delay(500); }
  if (WiFi.status() != WL_CONNECTED) {
    Serial.printf("Wi-Fi failed, status=%d (try %d)\n", WiFi.status(), fails + 1);  // 4=auth/wrong pw, 1=no ssid, 6=disconnected
    if (++fails >= 3) { fails = 0; g_provisioned = false; WiFi.mode(WIFI_OFF); Serial.println("back to BLE pairing so the app can re-provision"); }
    return;
  }
  fails = 0;
  configTime(0, 0, "pool.ntp.org");
  MDNS.begin("zensim");  // http://zensim.local/
  startWeb();
  Serial.printf("\nWi-Fi %s  config at http://zensim.local/ or http://%s/\n", WiFi.localIP().toString().c_str(), WiFi.localIP().toString().c_str());
}

// scan the Wi-Fi networks once, before BLE starts, so WiFi.get can answer instantly during the connection
static void scanWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  delay(200);
  int n = WiFi.scanNetworks(false, true, false, 350);  // sync, show hidden, active, 350 ms/channel
  g_apCount = 0;
  for (int i = 0; i < n && g_apCount < MAX_AP; i++) {
    if (WiFi.SSID(i).length() == 0) continue;  // skip entries without a name
    g_apSsid[g_apCount] = WiFi.SSID(i);
    g_apRssi[g_apCount] = WiFi.RSSI(i);
    g_apCh[g_apCount] = WiFi.channel(i);
    memcpy(g_apBssid[g_apCount], WiFi.BSSID(i), 6);
    g_apAuth[g_apCount] = (int)WiFi.encryptionType(i);
    Serial.printf("  AP %s rssi=%d ch=%d\n", g_apSsid[g_apCount].c_str(), g_apRssi[g_apCount], g_apCh[g_apCount]);
    g_apCount++;
  }
  WiFi.scanDelete();
  WiFi.mode(WIFI_OFF);
  Serial.printf("WiFi pre-scan: found=%d cached=%d\n", n, g_apCount);
}

void setup() {
  Serial.begin(115200); delay(200);
  loadConfig();
  Serial.printf("Zendure sim: profile %s, ble %s, wifi %s\n", PROFILES[g_profile].name, g_bleName.c_str(), g_hasWifi ? g_ssid.c_str() : "(none, pairing)");
  Serial.println("serial: show | profile <name> | sn <> | deviceid <> | prodkey <> | forget | reboot");
  scanWifi();  // always cache the networks at boot (radio free, before join and BLE) so WiFi.get answers even when provisioned
  if (g_hasWifi) { g_provisioned = true; joinLan(); }  // known Wi-Fi: straight onto the LAN, no pairing
  startBle();  // legacy devices (ACE/Hyper/Hub) keep BLE advertising always, also on wifi; only zenSDK turns it off
}

void loop() {
  if (g_webStarted) g_web.handleClient();
  if (!g_provisioned) {
    handleSerial();
    static unsigned long lastBle = 0;
    if (g_bleStream && millis() - lastBle > 2000) { lastBle = millis(); bleReport(false); }  // stream after a BLE read getAll
    delay(20); return;
  }
  if (WiFi.status() != WL_CONNECTED) joinLan();
  if (WiFi.status() == WL_CONNECTED && g_iotUrl.length() && !g_mqtt.connected()) mqttConnect();  // only once on the LAN, so no DNS spam
  g_mqtt.loop();
  if (g_reRegister && g_mqtt.connected()) { g_reRegister = false; sendRegister(); Serial.println("re-register after new BLE token"); }
  reportTick();
}
