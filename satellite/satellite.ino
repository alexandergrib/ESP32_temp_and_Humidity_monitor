#include <Arduino.h>
#include <WiFi.h>
#include <Wire.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <Preferences.h>
#include <SHT85.h>
#include "../shared/protocol.h"

using namespace proto;

// Adjust for your ESP32 board if needed.
static constexpr uint8_t PIN_I2C_SDA = 21;
static constexpr uint8_t PIN_I2C_SCL = 22;
static constexpr char DEFAULT_NODE_NAME[] = "satellite";

Preferences prefs;
SHT85 sht;

uint8_t controllerMac[6] = {0};
bool isBound = false;
uint32_t nodeId = 0;
uint32_t reportIntervalMs = DEFAULT_REPORT_MS;
uint32_t txSeq = 1;
uint32_t lastReportAt = 0;
uint32_t lastHeartbeatAt = 0;
char nodeName[16] = DEFAULT_NODE_NAME;

bool sensorPresent = false;
float lastTempC = NAN;
float lastHumidity = NAN;

bool macIsZero(const uint8_t* mac) {
    for (int i = 0; i < 6; ++i) if (mac[i] != 0) return false;
    return true;
}

void saveConfig() {
    prefs.begin("tmon-node", false);
    prefs.putBool("bound", isBound);
    prefs.putUInt("nodeId", nodeId);
    prefs.putUInt("reportMs", reportIntervalMs);
    prefs.putBytes("ctrlMac", controllerMac, 6);
    prefs.putString("name", String(nodeName));
    prefs.end();
}

void loadConfig() {
    prefs.begin("tmon-node", false);
    isBound = prefs.getBool("bound", false);
    nodeId = prefs.getUInt("nodeId", 0);
    reportIntervalMs = prefs.getUInt("reportMs", DEFAULT_REPORT_MS);
    prefs.getBytes("ctrlMac", controllerMac, 6);
    String name = prefs.getString("name", DEFAULT_NODE_NAME);
    name.toCharArray(nodeName, sizeof(nodeName));
    prefs.end();
    if (macIsZero(controllerMac)) isBound = false;
}

bool ensurePeer(const uint8_t* mac) {
    if (esp_now_is_peer_exist(mac)) return true;
    esp_now_peer_info_t peer{};
    memcpy(peer.peer_addr, mac, 6);
    peer.channel = RADIO_CHANNEL;
    peer.encrypt = false;
    return esp_now_add_peer(&peer) == ESP_OK;
}

bool initSensor() {
    Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL);
    if (!sht.begin()) return false;
    return true;
}

void sampleSensor() {
    sensorPresent = sht.read(true);
    if (sensorPresent) {
        lastTempC = sht.getTemperature();
        lastHumidity = sht.getHumidity();
    }
}

void sendBindRequest() {
    uint8_t broadcastMac[] = {0xFF,0xFF,0xFF,0xFF,0xFF,0xFF};
    ensurePeer(broadcastMac);

    BindRequest msg{};
    fillHeader(msg.header, MSG_BIND_REQUEST, txSeq++, nodeId, millis());
    strncpy(msg.nodeName, nodeName, sizeof(msg.nodeName) - 1);
    msg.fwMajor = 1;
    msg.fwMinor = 0;
    msg.capabilities = 0x0001;
    esp_now_send(broadcastMac, reinterpret_cast<const uint8_t*>(&msg), sizeof(msg));
}

void sendReading() {
    if (!isBound) return;
    Reading msg{};
    fillHeader(msg.header, MSG_READING, txSeq++, nodeId, millis());
    msg.temperatureC = lastTempC;
    msg.humidityPct = lastHumidity;
    msg.vbat = NAN;
    msg.sensorOk = sensorPresent ? 1 : 0;
    msg.rssiHint = 0;
    ensurePeer(controllerMac);
    esp_now_send(controllerMac, reinterpret_cast<const uint8_t*>(&msg), sizeof(msg));
}

void sendHeartbeat() {
    if (!isBound) return;
    Heartbeat msg{};
    fillHeader(msg.header, MSG_HEARTBEAT, txSeq++, nodeId, millis());
    msg.sensorOk = sensorPresent ? 1 : 0;
    msg.wifiChannel = RADIO_CHANNEL;
    ensurePeer(controllerMac);
    esp_now_send(controllerMac, reinterpret_cast<const uint8_t*>(&msg), sizeof(msg));
}

void sendConfigAck(bool applied) {
    if (!isBound) return;
    ConfigAck msg{};
    fillHeader(msg.header, MSG_CONFIG_ACK, txSeq++, nodeId, millis());
    msg.reportIntervalMs = reportIntervalMs;
    msg.applied = applied ? 1 : 0;
    ensurePeer(controllerMac);
    esp_now_send(controllerMac, reinterpret_cast<const uint8_t*>(&msg), sizeof(msg));
}

void onDataRecv(const esp_now_recv_info_t* recvInfo, const uint8_t* data, int len) {
    if (len < static_cast<int>(sizeof(Header))) return;
    const Header* h = reinterpret_cast<const Header*>(data);
    if (!validHeader(*h)) return;

    if (h->type == MSG_BIND_ACK && len == sizeof(BindAck)) {
        const BindAck* ack = reinterpret_cast<const BindAck*>(data);
        if (!ack->accepted) return;
        memcpy(controllerMac, ack->controllerMac, 6);
        nodeId = ack->assignedNodeId;
        reportIntervalMs = ack->reportIntervalMs;
        isBound = true;
        ensurePeer(controllerMac);
        saveConfig();
        Serial.printf("Bound to controller %s as node %lu\n", macToString(controllerMac).c_str(), static_cast<unsigned long>(nodeId));
    } else if (h->type == MSG_CONFIG_SET && len == sizeof(ConfigSet)) {
        const ConfigSet* cfg = reinterpret_cast<const ConfigSet*>(data);
        reportIntervalMs = max<uint32_t>(1000, cfg->reportIntervalMs);
        saveConfig();
        sendConfigAck(true);
    } else if (h->type == MSG_PING && isBound) {
        sendHeartbeat();
    }
}

void onDataSent(const wifi_tx_info_t*, esp_now_send_status_t status) {
    Serial.printf("Last send: %s\n", status == ESP_NOW_SEND_SUCCESS ? "ok" : "fail");
}

void setup() {
    Serial.begin(115200);
    delay(500);

    loadConfig();
    sensorPresent = initSensor();
    sampleSensor();

    WiFi.mode(WIFI_STA);
    WiFi.disconnect();
    esp_wifi_set_promiscuous(true);
    esp_wifi_set_channel(RADIO_CHANNEL, WIFI_SECOND_CHAN_NONE);
    esp_wifi_set_promiscuous(false);

    if (esp_now_init() != ESP_OK) {
        Serial.println("ESP-NOW init failed");
        return;
    }

    esp_now_register_recv_cb(onDataRecv);
    esp_now_register_send_cb(onDataSent);

    if (isBound) {
        ensurePeer(controllerMac);
        Serial.printf("Loaded binding: node %lu controller %s\n", static_cast<unsigned long>(nodeId), macToString(controllerMac).c_str());
    } else {
        Serial.println("Unbound. Waiting to pair with controller...");
    }
}

void loop() {
    if (!isBound) {
        static uint32_t lastBindAttemptAt = 0;
        if (millis() - lastBindAttemptAt >= 3000) {
            lastBindAttemptAt = millis();
            sendBindRequest();
            Serial.println("Bind request sent");
        }
        delay(10);
        return;
    }

    if (millis() - lastReportAt >= reportIntervalMs) {
        lastReportAt = millis();
        sampleSensor();
        sendReading();
    }

    if (millis() - lastHeartbeatAt >= HEARTBEAT_MS) {
        lastHeartbeatAt = millis();
        sendHeartbeat();
    }

    delay(10);
}
