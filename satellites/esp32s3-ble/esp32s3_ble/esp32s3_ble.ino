// Prius car computer — BLE test satellite (docs/SATELLITES.md §3, §4).
//
// Mode A (default) "scanner satellite": GAP central. Duty-cycled passive scan for
// the car's service UUID, connect, MTU 247, LE Secure Connections bonding (Just
// Works), subscribe to TX (…0001), write a bare "\n" to RX (…0002) so prius-bled
// registers the link, then the auth handshake exactly like
// cyberpunk_computer/backend/satellite_auth.py SatelliteAuthClient.
// Mode B (-DSAT_MODE_BEACON=1) "beacon satellite": GAP peripheral + GATT server
// with the same UUIDs mirrored (TX notify = sat→car, RX write = car→sat), no
// address prefix; the car connects and runs the same handshake.
//
// Builds for any NimBLE-capable ESP32 (developed on ESP32-C6 and ESP32-S3).

#include <Arduino.h>
#include <NimBLEDevice.h>
#include <ArduinoJson.h>
#include <Ed25519.h>
#include <Preferences.h>
#include <mbedtls/base64.h>
#include <esp_random.h>
#include <freertos/stream_buffer.h>

#if __has_include("provision.h")
#include "provision.h"
#else
#warning "provision.h missing: building with provision.example.h placeholders (car will refuse auth)"
#include "provision.example.h"
#endif

#ifndef SAT_MODE_BEACON
#define SAT_MODE_BEACON 0
#endif
#ifndef SAT_ACTIVE_SCAN          // 1 = active scan (gets the PRIUS name from the scan response)
#define SAT_ACTIVE_SCAN 0
#endif
#ifndef SAT_LED_PIN
#ifdef RGB_BUILTIN
#define SAT_LED_PIN RGB_BUILTIN  // addressable RGB on the variant's PIN_RGB_LED (C6: GPIO8, S3: GPIO48)
#define SAT_LED_RGB 1
#else
#define SAT_LED_PIN -1
#endif
#endif
#ifndef SAT_LED_RGB
#define SAT_LED_RGB 0
#endif

#if CONFIG_IDF_TARGET_ESP32C6
#define FW_VER "esp32c6-0.1"
#elif CONFIG_IDF_TARGET_ESP32S3
#define FW_VER "esp32s3-0.1"
#else
#define FW_VER "esp32-0.1"
#endif

static const char* SERVICE_UUID = "580e04f7-d4c0-458b-b2e0-4ebb01a960c8";
static const char* TX_UUID      = "580e04f7-d4c0-458b-b2e0-4ebb01a90001";  // car→sat (mode A)
static const char* RX_UUID      = "580e04f7-d4c0-458b-b2e0-4ebb01a90002";  // sat→car (mode A)

static const uint32_t SCAN_MS        = 5000;   // scan burst
static const uint32_t SCAN_PERIOD_MS = 15000;  // one burst per period
static const uint32_t STATUS_MS      = 30000;
static const uint32_t AUTH_WAIT_MS   = 90000;  // no auth_ok by then → drop the link
static const uint32_t BACKOFF_MIN_MS = 2000;
static const uint32_t BACKOFF_MAX_MS = 60000;
static const size_t   SAT_LINE_MAX       = 4096;
static const uint16_t WANT_MTU       = 247;

// ─── logging ────────────────────────────────────────────────────────────────
static void logf(const char* fmt, ...) {
  char buf[512];
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(buf, sizeof buf, fmt, ap);
  va_end(ap);
  uint32_t ms = millis();
  Serial.printf("[%6lu.%03lu] %s\n", (unsigned long)(ms / 1000), (unsigned long)(ms % 1000), buf);
}

// ─── LED ────────────────────────────────────────────────────────────────────
static bool ledCmd = false;  // car-controlled ({"a":"led","on":true})
static uint8_t ledState = 0; // 0 idle, 1 linked (auth pending), 2 authenticated
static void ledShow() {
  if (SAT_LED_PIN < 0) return;
#if SAT_LED_RGB
  if (ledCmd)              rgbLedWrite(SAT_LED_PIN, 0, 160, 0);   // bright green: car said on
  else if (ledState == 2)  rgbLedWrite(SAT_LED_PIN, 0, 0, 12);    // dim blue: authenticated
  else if (ledState == 1)  rgbLedWrite(SAT_LED_PIN, 12, 8, 0);    // dim amber: link, no auth yet
  else                     rgbLedWrite(SAT_LED_PIN, 0, 0, 0);
#else
  pinMode(SAT_LED_PIN, OUTPUT);
  digitalWrite(SAT_LED_PIN, ledCmd ? HIGH : LOW);
#endif
}

// ─── crypto helpers ─────────────────────────────────────────────────────────
static String b64e(const uint8_t* data, size_t len) {
  size_t olen = 0;
  unsigned char out[200];
  if (mbedtls_base64_encode(out, sizeof out - 1, &olen, data, len) != 0) return String();
  out[olen] = 0;
  return String((const char*)out);
}
static int b64d(const char* text, uint8_t* out, size_t cap) {
  size_t olen = 0;
  if (!text) return -1;
  if (mbedtls_base64_decode(out, cap, &olen, (const unsigned char*)text, strlen(text)) != 0) return -1;
  return (int)olen;
}

// ─── provisioning / persisted state ─────────────────────────────────────────
static Preferences prefs;
static String certJson;           // compact JSON of our cert ("" = none)
static int32_t satId = PROV_SAT_ID;
static String ownAddr;            // upper-case, colon-separated (as BlueZ reports it)

static void loadState() {
  prefs.begin("prius-sat", false);
  String nvsFp = prefs.getString("fp", "");
  certJson = PROV_CERT_JSON;
  if (nvsFp == PROV_FP) {         // only trust NVS data written for this key
    String c = prefs.getString("cert", "");
    if (c.length()) certJson = c;
    satId = prefs.getInt("sat_id", PROV_SAT_ID);
  }
}
static void storeState(int32_t id, const String& cert) {
  prefs.putString("fp", PROV_FP);
  prefs.putInt("sat_id", id);
  if (cert.length()) prefs.putString("cert", cert);
}

// ─── auth (SatelliteAuthClient) ─────────────────────────────────────────────
static const char SAT_CONTEXT[] = "prius-sat-auth-v1";
static const char CAR_CONTEXT[] = "prius-car-auth-v1";
static uint8_t carNonce[32], satNonce[32];
static bool haveNonces = false;
static bool authenticated = false;

// MSG = "prius-sat-auth-v1" + car_nonce + sat_nonce + car_fp + sat_fp + transport
static size_t satMessage(uint8_t* m) {
  size_t n = 0;
  memcpy(m + n, SAT_CONTEXT, 17); n += 17;
  memcpy(m + n, carNonce, 32); n += 32;
  memcpy(m + n, satNonce, 32); n += 32;
  memcpy(m + n, PROV_CAR_FP, 26); n += 26;
  memcpy(m + n, PROV_FP, 26); n += 26;
  memcpy(m + n, PROV_TRANSPORT, 3); n += 3;
  return n;
}
// MSG2 = "prius-car-auth-v1" + sat_nonce + car_nonce + sat_fp + car_fp
static size_t carMessage(uint8_t* m) {
  size_t n = 0;
  memcpy(m + n, CAR_CONTEXT, 17); n += 17;
  memcpy(m + n, satNonce, 32); n += 32;
  memcpy(m + n, carNonce, 32); n += 32;
  memcpy(m + n, PROV_FP, 26); n += 26;
  memcpy(m + n, PROV_CAR_FP, 26); n += 26;
  return n;
}

// ─── link plumbing (shared by both modes) ───────────────────────────────────
static StreamBufferHandle_t rxStream;   // bytes from the car (BLE task → loop)
static String lineBuf;
static bool linkUp = false;
static uint32_t linkSince = 0, lastStatus = 0;
static bool wantDrop = false;           // loop asks the mode code to disconnect
static uint32_t dropBackoff = 0;        // extra backoff requested with the drop

static void dropLink(const char* why, uint32_t backoff = 0) {
  logf("dropping link: %s", why);
  wantDrop = true;
  dropBackoff = backoff;
}

static bool sendRaw(const uint8_t* data, size_t len);  // mode-specific

static void sendPayload(JsonDocument& payload) {
  JsonDocument env;
  if (authenticated) env["id"] = satId;
  env["d"] = payload;
  String line;
  serializeJson(env, line);
  logf("TX> %s", line.c_str());
  line += '\n';
  if (!sendRaw((const uint8_t*)line.c_str(), line.length())) logf("send failed");
}

static void sendStatus() {
  JsonDocument p;
  p["a"] = "status";
  p["ver"] = FW_VER;
  p["temp"] = roundf(temperatureRead() * 10) / 10.0f;
  p["up"] = millis() / 1000;
  p["led"] = ledCmd;
  sendPayload(p);
  lastStatus = millis();
}

static void onAuthChallenge(JsonObject d) {
  if ((d["v"] | 0) != 1) { dropLink("challenge version"); return; }
  const char* car = d["car"] | "";
  if (strcmp(car, PROV_CAR_FP) != 0) {
    logf("challenge from a foreign car %s (ours %s)", car, PROV_CAR_FP);
    dropLink("foreign car", BACKOFF_MAX_MS);
    return;
  }
  if (b64d(d["nonce"] | "", carNonce, sizeof carNonce) != 32) { dropLink("bad car nonce"); return; }
  esp_fill_random(satNonce, sizeof satNonce);
  haveNonces = true;
  uint8_t msg[160], sig[64];
  size_t n = satMessage(msg);
  uint32_t t0 = micros();
  Ed25519::sign(sig, PROV_PRIVKEY, PROV_PUBKEY, msg, n);
  logf("challenge from car %s, signed in %lu ms", car, (unsigned long)((micros() - t0) / 1000));
  JsonDocument p;
  p["a"] = "auth";
  p["v"] = 1;
  p["fp"] = PROV_FP;
  p["pubkey"] = PROV_PUBKEY_B64;
  if (certJson.length()) p["cert"] = serialized(certJson);
  else p["cert"] = nullptr;
  p["nonce"] = b64e(satNonce, 32);
  p["sig"] = b64e(sig, 64);
  sendPayload(p);
}

static void onAuthResult(JsonObject d) {
  const char* a = d["a"];
  if (strcmp(a, "auth_fail") == 0) {
    logf("AUTH FAILED: car refused: %s", (const char*)(d["why"] | "?"));
    dropLink("auth_fail", BACKOFF_MAX_MS);
    return;
  }
  if (!haveNonces) { logf("auth_ok before challenge, ignored"); return; }
  uint8_t sig[64], msg[160];
  if (b64d(d["sig"] | "", sig, sizeof sig) != 64) { dropLink("auth_ok: bad sig encoding"); return; }
  size_t n = carMessage(msg);
  if (!Ed25519::verify(sig, PROV_CAR_PUB, msg, n)) {
    logf("AUTH FAILED: car signature invalid (not our car)");
    dropLink("car signature invalid", BACKOFF_MAX_MS);
    return;
  }
  satId = d["sat_id"] | satId;
  String newCert;
  if (!d["cert"].isNull()) { serializeJson(d["cert"], newCert); certJson = newCert; }
  storeState(satId, newCert);
  authenticated = true;
  ledState = 2; ledShow();
  logf("AUTHENTICATED as sat_id %ld (car signature verified)%s", (long)satId,
       newCert.length() ? ", new cert stored" : "");
  sendStatus();
}

static void handleLine(String line) {
#if !SAT_MODE_BEACON
  // TX address prefix "<17-char ADDR> <line>" (docs §4). Lines without a valid
  // prefix are taken as addressed to us (compat).
  if (line.length() >= 18 && line[17] == ' ' && line[2] == ':' && line[5] == ':' && line[14] == ':') {
    String dst = line.substring(0, 17);
    dst.toUpperCase();
    if (dst != ownAddr) { logf("RX (for %s, dropped)", dst.c_str()); return; }
    line = line.substring(18);
  }
#endif
  logf("RX< %s", line.c_str());
  JsonDocument doc;
  if (deserializeJson(doc, line)) { logf("non-JSON line ignored"); return; }
  JsonObject d = doc["d"].is<JsonObject>() ? doc["d"].as<JsonObject>() : doc.as<JsonObject>();
  const char* a = d["a"] | "";
  if (!authenticated) {
    if (strcmp(a, "auth") == 0) onAuthChallenge(d);
    else if (strcmp(a, "auth_ok") == 0 || strcmp(a, "auth_fail") == 0) onAuthResult(d);
    else logf("ignoring pre-auth '%s'", a);
    return;
  }
  if (strcmp(a, "ping") == 0) { JsonDocument p; p["a"] = "pong"; sendPayload(p); return; }
  if (strcmp(a, "pong") == 0) return;
  if (strcmp(a, "auth") == 0) { logf("re-auth requested"); authenticated = false; onAuthChallenge(d); return; }
  if (strcmp(a, "led") == 0) {
    ledCmd = d["on"] | false;
    ledShow();
    logf("LED %s (car command)", ledCmd ? "ON" : "OFF");
  }
  if (strcmp(a, "status") == 0) sendStatus();
  JsonDocument p;
  p["a"] = "ack";
  p["cmd"] = a;
  sendPayload(p);
}

static void pumpRx() {
  uint8_t buf[256];
  size_t n;
  while ((n = xStreamBufferReceive(rxStream, buf, sizeof buf, 0)) > 0) {
    for (size_t i = 0; i < n; i++) {
      char c = (char)buf[i];
      if (c == '\n') {
        String line = lineBuf;
        lineBuf = "";
        line.trim();
        if (line.length()) handleLine(line);
      } else if (lineBuf.length() < SAT_LINE_MAX) {
        lineBuf += c;
      } else {
        lineBuf = "";
        dropLink("line too long");
      }
    }
  }
}

static void resetSession() {
  authenticated = false;
  haveNonces = false;
  lineBuf = "";
  xStreamBufferReset(rxStream);
  ledState = 0;
  ledShow();
}

static void sessionTick() {
  pumpRx();
  if (!linkUp) return;
  uint32_t now = millis();
  if (authenticated && now - lastStatus >= STATUS_MS) sendStatus();
  if (!authenticated && now - linkSince >= AUTH_WAIT_MS) dropLink("no auth_ok within 90 s");
}

#if !SAT_MODE_BEACON
// ═══ Mode A: scanner satellite (GAP central, car = GATT server) ═════════════
static NimBLEClient* client = nullptr;
static NimBLERemoteCharacteristic* rxChr = nullptr;
static NimBLEAddress target;
static bool haveTarget = false;
static volatile bool disconnected = false;
static volatile int discReason = 0;
static uint32_t nextScanAt = 0;
static uint32_t backoff = BACKOFF_MIN_MS;

class ScanCb : public NimBLEScanCallbacks {
  void onResult(const NimBLEAdvertisedDevice* dev) override {
    if (!dev->isAdvertisingService(NimBLEUUID(SERVICE_UUID))) return;
    String addr = dev->getAddress().toString().c_str();
    addr.toUpperCase();
    std::string name = dev->getName();
    bool exact = strlen(PROV_CAR_ADDR) && addr == PROV_CAR_ADDR;
    bool any = !strlen(PROV_CAR_ADDR);
    logf("seen car candidate %s name='%s' rssi %d%s", addr.c_str(), name.c_str(), dev->getRSSI(),
         exact ? " (provisioned car_addr)" : (any || name == "PRIUS") ? "" : " (not our car_addr, skipped)");
    if (exact || ((any || name == "PRIUS") && !haveTarget)) {
      target = dev->getAddress();
      haveTarget = true;
      if (exact || name == "PRIUS") NimBLEDevice::getScan()->stop();
    }
  }
  void onScanEnd(const NimBLEScanResults&, int reason) override {}
};

class ClientCb : public NimBLEClientCallbacks {
  void onConnect(NimBLEClient* c) override { logf("connected to %s", c->getPeerAddress().toString().c_str()); }
  void onConnectFail(NimBLEClient* c, int reason) override { logf("connect failed, reason %d", reason); }
  void onDisconnect(NimBLEClient* c, int reason) override {
    discReason = reason;
    disconnected = true;
  }
  void onAuthenticationComplete(NimBLEConnInfo& ci) override {
    logf("link security: encrypted=%d bonded=%d authenticated=%d keysize=%d", ci.isEncrypted(),
         ci.isBonded(), ci.isAuthenticated(), ci.getSecKeySize());
  }
  void onMTUChange(NimBLEClient* c, uint16_t mtu) override { logf("ATT MTU %u", mtu); }
  void onConfirmPasskey(NimBLEConnInfo& ci, uint32_t pin) override { NimBLEDevice::injectConfirmPasskey(ci, true); }
};

static void onNotify(NimBLERemoteCharacteristic*, uint8_t* data, size_t len, bool) {
  xStreamBufferSend(rxStream, data, len, 0);
}

static bool sendRaw(const uint8_t* data, size_t len) {
  if (!client || !client->isConnected() || !rxChr) return false;
  size_t chunk = client->getMTU() > 3 ? client->getMTU() - 3 : 20;
  for (size_t off = 0; off < len; off += chunk) {
    size_t n = min(chunk, len - off);
    // Write Request (with response): reliable, and NimBLE re-secures on
    // insufficient-encryption; write commands would be dropped silently.
    if (!rxChr->writeValue(data + off, n, true)) return false;
  }
  return true;
}

static void scheduleRetry(uint32_t extra) {
  uint32_t wait = max(backoff, extra);
  nextScanAt = millis() + wait;
  logf("retry in %lu s", (unsigned long)(wait / 1000));
  backoff = min(backoff * 2, BACKOFF_MAX_MS);
}

static void teardown(const char* why) {
  if (client && client->isConnected()) client->disconnect();
  uint32_t t0 = millis();
  while (client && client->isConnected() && millis() - t0 < 3000) delay(10);
  if (linkUp) logf("link down (%s)", why);
  linkUp = false;
  rxChr = nullptr;
  resetSession();
}

static bool connectCar() {
  logf("connecting to %s (bonded: %s)", target.toString().c_str(), NimBLEDevice::isBonded(target) ? "yes" : "no");
  disconnected = false;
  if (!client->connect(target, true, false, true)) {
    logf("connect failed (err %d)", client->getLastError());
    return false;
  }
  if (client->getMTU() < WANT_MTU) client->exchangeMTU();
  logf("ATT MTU %u", client->getMTU());
  // The car's characteristics are encrypt-only: secure the link up front. The
  // car accepts a *new* bond only while its pairing window is open; a stored
  // bond the car has lost (PIN/key missing) is deleted by NimBLE, so the next
  // attempt pairs afresh.
  if (!client->secureConnection()) {
    logf("pairing/encryption failed (err %d): car pairing window closed, or bond lost on one side",
         client->getLastError());
    return false;
  }
  NimBLEConnInfo ci = client->getConnInfo();
  logf("encrypted=%d bonded=%d", ci.isEncrypted(), ci.isBonded());
  NimBLERemoteService* svc = client->getService(SERVICE_UUID);
  if (!svc) { logf("car service not found"); return false; }
  NimBLERemoteCharacteristic* tx = svc->getCharacteristic(TX_UUID);
  rxChr = svc->getCharacteristic(RX_UUID);
  if (!tx || !rxChr) { logf("TX/RX characteristic missing"); return false; }
  if (!tx->subscribe(true, onNotify, true)) { logf("subscribe to TX failed"); return false; }
  logf("subscribed to TX");
  resetSession();
  linkUp = true;
  linkSince = millis();
  ledState = 1; ledShow();
  // prius-bled counts a central as a satellite link at its first RX write.
  if (!sendRaw((const uint8_t*)"\n", 1)) { logf("hello write failed"); return false; }
  logf("link up (hello written), waiting for the car's auth challenge");
  return true;
}

static void modeSetup() {
  NimBLEScan* scan = NimBLEDevice::getScan();
  scan->setScanCallbacks(new ScanCb(), false);
  scan->setActiveScan(SAT_ACTIVE_SCAN);
  scan->setInterval(100);
  scan->setWindow(100);   // 100 % duty inside the burst; the burst itself is duty-cycled
  scan->setDuplicateFilter(1);
  client = NimBLEDevice::createClient();
  client->setClientCallbacks(new ClientCb(), true);
  // The car advertises every 10–10.24 s with ACC off: an initiator only gets a
  // chance at the next ADV_IND, so the connect timeout must exceed that interval.
  client->setConnectTimeout(15000);
  client->setConnectionParams(24, 40, 0, 400);  // 30–50 ms, 4 s supervision
}

static void modeLoop() {
  if (linkUp || client->isConnected()) {
    if (disconnected) {
      logf("disconnected, reason 0x%x", discReason);
      teardown("peer");
      scheduleRetry(0);
      return;
    }
    if (wantDrop) {
      wantDrop = false;
      teardown("dropped");
      scheduleRetry(dropBackoff);
      return;
    }
    sessionTick();
    if (authenticated) backoff = BACKOFF_MIN_MS;
    return;
  }
  if ((int32_t)(millis() - nextScanAt) < 0) { delay(20); return; }
  haveTarget = false;
  logf("scanning %lu ms (%s) for %s%s%s", (unsigned long)SCAN_MS, SAT_ACTIVE_SCAN ? "active" : "passive",
       SERVICE_UUID, strlen(PROV_CAR_ADDR) ? " car_addr " : "", PROV_CAR_ADDR);
  NimBLEDevice::getScan()->clearResults();
  NimBLEDevice::getScan()->getResults(SCAN_MS, false);
  NimBLEDevice::getScan()->clearResults();
  if (!haveTarget) {
    nextScanAt = millis() + (SCAN_PERIOD_MS - SCAN_MS);
    return;
  }
  if (!connectCar()) {
    teardown("setup failed");
    scheduleRetry(0);
  }
}

#else
// ═══ Mode B: beacon satellite (GAP peripheral, satellite = GATT server) ═════
// Mirrored roles: TX (…0001, notify) = sat→car, RX (…0002, write) = car→sat.
static NimBLEServer* server = nullptr;
static NimBLECharacteristic* txChr = nullptr;
static volatile bool connEvent = false, discEvent = false;
static volatile uint16_t connHandle = BLE_HS_CONN_HANDLE_NONE;
static uint16_t peerMtu = 23;

class ServerCb : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer*, NimBLEConnInfo& ci) override {
    connHandle = ci.getConnHandle();
    connEvent = true;
  }
  void onDisconnect(NimBLEServer*, NimBLEConnInfo& ci, int reason) override { discEvent = true; }
  void onMTUChange(uint16_t mtu, NimBLEConnInfo&) override { peerMtu = mtu; }
  void onAuthenticationComplete(NimBLEConnInfo& ci) override {
    logf("link security: encrypted=%d bonded=%d", ci.isEncrypted(), ci.isBonded());
  }
  void onConfirmPassKey(NimBLEConnInfo& ci, uint32_t) override { NimBLEDevice::injectConfirmPasskey(ci, true); }
};
class RxCb : public NimBLECharacteristicCallbacks {
  void onWrite(NimBLECharacteristic* c, NimBLEConnInfo&) override {
    NimBLEAttValue v = c->getValue();
    xStreamBufferSend(rxStream, v.data(), v.length(), 0);
  }
};

static bool sendRaw(const uint8_t* data, size_t len) {
  if (!linkUp || !txChr) return false;
  size_t chunk = peerMtu > 3 ? peerMtu - 3 : 20;
  for (size_t off = 0; off < len; off += chunk) {
    size_t n = min(chunk, len - off);
    txChr->setValue(data + off, n);
    if (!txChr->notify(connHandle)) return false;
    delay(5);
  }
  return true;
}

static void startAdvertising() {
  NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
  adv->reset();
  adv->addServiceUUID(SERVICE_UUID);
  NimBLEAdvertisementData sr;
  sr.setName(PROV_NAME);
  adv->setScanResponseData(sr);
  adv->enableScanResponse(true);
  adv->setMinInterval(1600);  // 1 s (0.625 ms units)
  adv->setMaxInterval(1760);
  adv->start();
  logf("advertising %s as '%s'", SERVICE_UUID, PROV_NAME);
}

static void modeSetup() {
  server = NimBLEDevice::createServer();
  server->setCallbacks(new ServerCb());
  server->advertiseOnDisconnect(false);
  NimBLEService* svc = server->createService(SERVICE_UUID);
  txChr = svc->createCharacteristic(TX_UUID, NIMBLE_PROPERTY::NOTIFY | NIMBLE_PROPERTY::READ |
                                                 NIMBLE_PROPERTY::READ_ENC);
  NimBLECharacteristic* rx = svc->createCharacteristic(
      RX_UUID, NIMBLE_PROPERTY::WRITE | NIMBLE_PROPERTY::WRITE_NR | NIMBLE_PROPERTY::WRITE_ENC);
  rx->setCallbacks(new RxCb());
  svc->start();
  startAdvertising();
}

static void modeLoop() {
  if (connEvent) {
    connEvent = false;
    resetSession();
    linkUp = true;
    linkSince = millis();
    peerMtu = server->getPeerMTU(connHandle);
    ledState = 1; ledShow();
    logf("car connected (handle %u, MTU %u), waiting for the auth challenge", connHandle, peerMtu);
  }
  if (discEvent) {
    discEvent = false;
    logf("link down");
    linkUp = false;
    resetSession();
    startAdvertising();
  }
  if (wantDrop) {
    wantDrop = false;
    server->disconnect(connHandle);
  }
  sessionTick();
}
#endif

// ─── setup / loop ───────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 1500) delay(10);
  rxStream = xStreamBufferCreate(8192, 1);
  ledShow();
  logf("prius BLE satellite %s, mode %s", FW_VER, SAT_MODE_BEACON ? "B (beacon)" : "A (scanner)");
  loadState();
  uint8_t derived[32];
  Ed25519::derivePublicKey(derived, PROV_PRIVKEY);
  bool keyOk = memcmp(derived, PROV_PUBKEY, 32) == 0;
  logf("name %s sat_id %ld fp %s car %s cert %s key %s", PROV_NAME, (long)satId, PROV_FP, PROV_CAR_FP,
       certJson.length() ? "yes" : "NO", keyOk ? "ok" : "MISMATCH (bad provision.h)");

  NimBLEDevice::init(SAT_MODE_BEACON ? PROV_NAME : "");
  NimBLEDevice::setOwnAddrType(BLE_OWN_ADDR_PUBLIC);
  NimBLEDevice::setMTU(WANT_MTU);
  NimBLEDevice::setSecurityAuth(true, false, true);           // bond, no MITM, LE Secure Connections
  NimBLEDevice::setSecurityIOCap(BLE_HS_IO_NO_INPUT_OUTPUT);  // Just Works
  NimBLEDevice::setSecurityInitKey(BLE_SM_PAIR_KEY_DIST_ENC | BLE_SM_PAIR_KEY_DIST_ID);
  NimBLEDevice::setSecurityRespKey(BLE_SM_PAIR_KEY_DIST_ENC | BLE_SM_PAIR_KEY_DIST_ID);
  ownAddr = NimBLEDevice::getAddress().toString().c_str();
  ownAddr.toUpperCase();
  logf("own BLE address %s (public), %d bond(s) in NVS", ownAddr.c_str(), NimBLEDevice::getNumBonds());
  modeSetup();
}

void loop() {
  modeLoop();
  delay(5);
}
