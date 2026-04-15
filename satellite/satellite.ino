#include <Arduino.h>
#include <WiFi.h>
#include <Wire.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <Preferences.h>
#include <SHT85.h>
#include <Update.h>
#include "protocol.h"

using namespace proto;

// Adjust for your ESP32 board if needed.
static constexpr uint8_t PIN_I2C_SDA = 21;
static constexpr uint8_t PIN_I2C_SCL = 22;
static constexpr char DEFAULT_NODE_NAME[] = "satellite";
static constexpr uint8_t FW_VERSION_MAJOR = 2;
static constexpr uint8_t FW_VERSION_MINOR = 1;

#ifndef LED_BUILTIN
static constexpr uint8_t STATUS_LED_PIN = 2;
#else
static constexpr uint8_t STATUS_LED_PIN = LED_BUILTIN;
#endif

static constexpr uint32_t STATUS_LED_PERIOD_MS = 1000;
static constexpr uint32_t STATUS_LED_ON_MS = 80;

Preferences prefs;
SHT85 sht(SHT_DEFAULT_ADDRESS);

uint8_t controllerMac[6] = {0};
bool isBound = false;
uint32_t nodeId = 0;
uint32_t reportIntervalMs = DEFAULT_REPORT_MS;
uint32_t txSeq = 1;
char nodeName[16] = "satellite";
uint32_t lastLedBeatAt = 0;
bool statusLedOn = false;
float tempOffsetC = 0.0f;

bool sensorPresent = false;
float lastTempC = NAN;
float lastHumidity = NAN;
bool otaRebootPending = false;
uint32_t otaRebootAtMs = 0;

struct SatelliteOtaState {
    bool active = false;
    uint32_t totalSize = 0;
    uint32_t expectedCrc32 = 0;
    uint32_t bytesWritten = 0;
    uint32_t runningCrc32 = 0xFFFFFFFF;
    uint32_t lastChunkOffset = 0;
    uint16_t lastChunkLen = 0;
} otaState;

void handleIncomingPacket(const uint8_t* data, int len);
void logSendStatus(esp_now_send_status_t status);

void initStatusLed() {
    pinMode(STATUS_LED_PIN, OUTPUT);
    digitalWrite(STATUS_LED_PIN, LOW);
}

void updateStatusLed() {
    const uint32_t now = millis();
    if (!statusLedOn && now - lastLedBeatAt >= STATUS_LED_PERIOD_MS) {
        statusLedOn = true;
        lastLedBeatAt = now;
        digitalWrite(STATUS_LED_PIN, HIGH);
    } else if (statusLedOn && now - lastLedBeatAt >= STATUS_LED_ON_MS) {
        statusLedOn = false;
        digitalWrite(STATUS_LED_PIN, LOW);
    }
}

bool macIsZero(const uint8_t* mac) {
    for (int i = 0; i < 6; ++i) if (mac[i] != 0) return false;
    return true;
}

uint32_t normalizeReportInterval(uint32_t ms) {
    return max(ms, MIN_REPORT_MS);
}

uint32_t crc32Update(uint32_t crc, const uint8_t* data, size_t len) {
    uint32_t value = crc;
    for (size_t i = 0; i < len; ++i) {
        value ^= data[i];
        for (uint8_t bit = 0; bit < 8; ++bit) {
            if ((value & 1U) != 0U) {
                value = (value >> 1U) ^ 0xEDB88320U;
            } else {
                value >>= 1U;
            }
        }
    }
    return value;
}

void resetOtaState() {
    otaState = SatelliteOtaState{};
}

void saveConfig() {
    prefs.begin("tmon-node", false);
    prefs.putBool("bound", isBound);
    prefs.putUInt("nodeId", nodeId);
    prefs.putUInt("reportMs", normalizeReportInterval(reportIntervalMs));
    prefs.putFloat("tempOff", tempOffsetC);
    prefs.putBytes("ctrlMac", controllerMac, 6);
    prefs.putString("name", String(nodeName));
    prefs.end();
}

void loadConfig() {
    prefs.begin("tmon-node", false);
    isBound = prefs.getBool("bound", false);
    nodeId = prefs.getUInt("nodeId", 0);
    reportIntervalMs = normalizeReportInterval(prefs.getUInt("reportMs", DEFAULT_REPORT_MS));
    tempOffsetC = prefs.getFloat("tempOff", 0.0f);
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
    Wire.setClock(100000);
    if (!sht.begin()) return false;
    sht.clearStatus();
    sht.heatOff();
    sht.setTemperatureOffset(tempOffsetC);
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
    msg.fwMajor = FW_VERSION_MAJOR;
    msg.fwMinor = FW_VERSION_MINOR;
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
    msg.reserved[0] = FW_VERSION_MAJOR;
    msg.reserved[1] = FW_VERSION_MINOR;
    ensurePeer(controllerMac);
    esp_now_send(controllerMac, reinterpret_cast<const uint8_t*>(&msg), sizeof(msg));
}

void sendHeartbeat() {
    if (!isBound) return;
    Heartbeat msg{};
    fillHeader(msg.header, MSG_HEARTBEAT, txSeq++, nodeId, millis());
    msg.sensorOk = sensorPresent ? 1 : 0;
    msg.wifiChannel = RADIO_CHANNEL;
    msg.reserved = static_cast<uint16_t>(FW_VERSION_MAJOR) | (static_cast<uint16_t>(FW_VERSION_MINOR) << 8);
    ensurePeer(controllerMac);
    esp_now_send(controllerMac, reinterpret_cast<const uint8_t*>(&msg), sizeof(msg));
}

void sendConfigAck(bool applied) {
    if (!isBound) return;
    ConfigAck msg{};
    fillHeader(msg.header, MSG_CONFIG_ACK, txSeq++, nodeId, millis());
    msg.reportIntervalMs = reportIntervalMs;
    msg.tempOffsetC = tempOffsetC;
    msg.applied = applied ? 1 : 0;
    ensurePeer(controllerMac);
    esp_now_send(controllerMac, reinterpret_cast<const uint8_t*>(&msg), sizeof(msg));
}

void sendOtaAck(OtaPhase phase, OtaStatus status, uint32_t bytesReceived, uint16_t detail = 0) {
    if (!isBound) return;
    OtaAck msg{};
    fillHeader(msg.header, MSG_OTA_ACK, txSeq++, nodeId, millis());
    msg.phase = phase;
    msg.status = status;
    msg.detail = detail;
    msg.bytesReceived = bytesReceived;
    ensurePeer(controllerMac);
    esp_now_send(controllerMac, reinterpret_cast<const uint8_t*>(&msg), sizeof(msg));
}

void handleOtaBegin(const OtaBegin& msg) {
    if (otaState.active) {
        sendOtaAck(OTA_PHASE_BEGIN, OTA_STATUS_BUSY, otaState.bytesWritten);
        return;
    }

    if (!Update.begin(msg.totalSize, U_FLASH)) {
        sendOtaAck(OTA_PHASE_BEGIN, OTA_STATUS_REJECTED, 0, static_cast<uint16_t>(Update.getError()));
        return;
    }

    resetOtaState();
    otaState.active = true;
    otaState.totalSize = msg.totalSize;
    otaState.expectedCrc32 = msg.expectedCrc32;
    otaState.runningCrc32 = 0xFFFFFFFF;
    sendOtaAck(OTA_PHASE_BEGIN, OTA_STATUS_OK, 0);
}

void handleOtaChunk(const OtaChunk& msg) {
    if (!otaState.active) {
        sendOtaAck(OTA_PHASE_CHUNK, OTA_STATUS_NOT_ACTIVE, otaState.bytesWritten);
        return;
    }

    if (msg.dataLen == 0 || msg.dataLen > OTA_CHUNK_BYTES) {
        sendOtaAck(OTA_PHASE_CHUNK, OTA_STATUS_WRITE_FAILED, otaState.bytesWritten, 0);
        return;
    }

    const bool isDuplicate =
        otaState.bytesWritten == (otaState.lastChunkOffset + otaState.lastChunkLen) &&
        msg.offset == otaState.lastChunkOffset &&
        msg.dataLen == otaState.lastChunkLen;

    if (isDuplicate) {
        sendOtaAck(OTA_PHASE_CHUNK, OTA_STATUS_OK, otaState.bytesWritten);
        return;
    }

    if (msg.offset != otaState.bytesWritten) {
        sendOtaAck(OTA_PHASE_CHUNK, OTA_STATUS_OFFSET_MISMATCH, otaState.bytesWritten);
        return;
    }

    const size_t written = Update.write(const_cast<uint8_t*>(msg.data), msg.dataLen);
    if (written != msg.dataLen) {
        sendOtaAck(OTA_PHASE_CHUNK, OTA_STATUS_WRITE_FAILED, otaState.bytesWritten, static_cast<uint16_t>(Update.getError()));
        return;
    }

    otaState.runningCrc32 = crc32Update(otaState.runningCrc32, msg.data, msg.dataLen);
    otaState.lastChunkOffset = msg.offset;
    otaState.lastChunkLen = msg.dataLen;
    otaState.bytesWritten += msg.dataLen;
    sendOtaAck(OTA_PHASE_CHUNK, OTA_STATUS_OK, otaState.bytesWritten);
}

void handleOtaEnd(const OtaEnd& msg) {
    if (!otaState.active) {
        sendOtaAck(OTA_PHASE_END, OTA_STATUS_NOT_ACTIVE, otaState.bytesWritten);
        return;
    }

    if (msg.totalSize != otaState.totalSize || otaState.bytesWritten != otaState.totalSize) {
        sendOtaAck(OTA_PHASE_END, OTA_STATUS_OFFSET_MISMATCH, otaState.bytesWritten);
        return;
    }

    const uint32_t finalCrc32 = otaState.runningCrc32 ^ 0xFFFFFFFFU;
    if (finalCrc32 != otaState.expectedCrc32 || msg.expectedCrc32 != otaState.expectedCrc32) {
        Update.abort();
        sendOtaAck(OTA_PHASE_END, OTA_STATUS_CRC_MISMATCH, otaState.bytesWritten);
        resetOtaState();
        return;
    }

    if (!Update.end(true)) {
        sendOtaAck(OTA_PHASE_END, OTA_STATUS_END_FAILED, otaState.bytesWritten, static_cast<uint16_t>(Update.getError()));
        resetOtaState();
        return;
    }

    sendOtaAck(OTA_PHASE_END, OTA_STATUS_OK, otaState.bytesWritten);
    resetOtaState();
    otaRebootPending = true;
    otaRebootAtMs = millis() + 750;
}

void handleIncomingPacket(const uint8_t* data, int len) {
    if (len < static_cast<int>(sizeof(Header))) return;
    const Header* h = reinterpret_cast<const Header*>(data);
    if (!validHeader(*h)) return;

    if (h->type == MSG_BIND_ACK && len == sizeof(BindAck)) {
        const BindAck* ack = reinterpret_cast<const BindAck*>(data);
        if (!ack->accepted) return;
        memcpy(controllerMac, ack->controllerMac, 6);
        nodeId = ack->assignedNodeId;
        reportIntervalMs = normalizeReportInterval(ack->reportIntervalMs);
        tempOffsetC = ack->tempOffsetC;
        sht.setTemperatureOffset(tempOffsetC);
        isBound = true;
        ensurePeer(controllerMac);
        saveConfig();
        Serial.printf(
            "Bound to controller %s as node %lu fw %u.%u\n",
            macToString(controllerMac).c_str(),
            static_cast<unsigned long>(nodeId),
            FW_VERSION_MAJOR,
            FW_VERSION_MINOR
        );
    } else if (h->type == MSG_CONFIG_SET && len == sizeof(ConfigSet)) {
        const ConfigSet* cfg = reinterpret_cast<const ConfigSet*>(data);
        reportIntervalMs = normalizeReportInterval(cfg->reportIntervalMs);
        tempOffsetC = cfg->tempOffsetC;
        sht.setTemperatureOffset(tempOffsetC);
        saveConfig();
        sendConfigAck(true);
    } else if (h->type == MSG_OTA_BEGIN && len == sizeof(OtaBegin) && isBound) {
        handleOtaBegin(*reinterpret_cast<const OtaBegin*>(data));
    } else if (h->type == MSG_OTA_CHUNK && len == sizeof(OtaChunk) && isBound) {
        handleOtaChunk(*reinterpret_cast<const OtaChunk*>(data));
    } else if (h->type == MSG_OTA_END && len == sizeof(OtaEnd) && isBound) {
        handleOtaEnd(*reinterpret_cast<const OtaEnd*>(data));
    } else if (h->type == MSG_SAMPLE_REQ && len == sizeof(SampleRequest) && isBound) {
        sampleSensor();
        sendReading();
    } else if (h->type == MSG_PING && isBound) {
        sendHeartbeat();
    }
}

void logSendStatus(esp_now_send_status_t status) {
    Serial.printf("Last send: %s\n", status == ESP_NOW_SEND_SUCCESS ? "ok" : "fail");
}

#if ESP_ARDUINO_VERSION >= ESP_ARDUINO_VERSION_VAL(3, 0, 0)
void onDataRecv(const esp_now_recv_info_t*, const uint8_t* data, int len) {
    handleIncomingPacket(data, len);
}

void onDataSent(const wifi_tx_info_t*, esp_now_send_status_t status) {
    logSendStatus(status);
}
#else
void onDataRecv(const uint8_t*, const uint8_t* data, int len) {
    handleIncomingPacket(data, len);
}

void onDataSent(const uint8_t*, esp_now_send_status_t status) {
    logSendStatus(status);
}
#endif

void setup() {
    Serial.begin(115200);
    delay(500);
    initStatusLed();

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
        Serial.printf(
            "Loaded binding: node %lu controller %s fw %u.%u\n",
            static_cast<unsigned long>(nodeId),
            macToString(controllerMac).c_str(),
            FW_VERSION_MAJOR,
            FW_VERSION_MINOR
        );
    } else {
        Serial.println("Unbound. Waiting to pair with controller...");
    }
}

void loop() {
    updateStatusLed();

    if (otaRebootPending && static_cast<int32_t>(millis() - otaRebootAtMs) >= 0) {
        ESP.restart();
    }

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

    delay(10);
}
