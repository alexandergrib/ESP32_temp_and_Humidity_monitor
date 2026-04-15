#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <Preferences.h>
#include <time.h>
#include "protocol.h"

using namespace proto;

Preferences prefs;

#ifndef LED_BUILTIN
static constexpr uint8_t STATUS_LED_PIN = 2;
#else
static constexpr uint8_t STATUS_LED_PIN = LED_BUILTIN;
#endif

static constexpr uint32_t STATUS_LED_PERIOD_MS = 1000;
static constexpr uint32_t STATUS_LED_ON_MS = 80;
static constexpr uint32_t MIN_POLL_GAP_MS = 120;
static constexpr uint32_t OTA_ACK_TIMEOUT_MS = 1500;
static constexpr uint8_t OTA_MAX_RETRIES = 3;
static constexpr uint32_t RSSI_CACHE_MAX_AGE_MS = 1500;
static constexpr size_t RSSI_CACHE_SLOTS = 16;
static constexpr uint8_t ESPNOW_CATEGORY_CODE = 127;
static constexpr uint8_t ESPNOW_ELEMENT_ID = 221;
static constexpr uint8_t ESPNOW_TYPE = 4;
static constexpr uint8_t ESPRESSIF_OUI[3] = {0x18, 0xFE, 0x34};

struct NodeRecord {
    bool used = false;
    uint32_t nodeId = 0;
    uint8_t mac[6] = {0};
    char name[16] = {0};
    uint32_t lastSeenMs = 0;
    uint32_t reportIntervalMs = DEFAULT_REPORT_MS;
    float lastTempC = NAN;
    float lastHumidity = NAN;
    uint8_t sensorOk = 0;
    uint8_t fwMajor = 0;
    uint8_t fwMinor = 0;
    int8_t lastRssiDbm = 0;
    float tempOffsetC = 0.0f;
    uint32_t nextPollDueAtMs = 0;
};

struct RssiSample {
    bool valid = false;
    uint8_t mac[6] = {0};
    int8_t rssiDbm = 0;
    uint32_t seenAtMs = 0;
};

static constexpr size_t MAX_NODES = 32;
NodeRecord nodes[MAX_NODES];
uint32_t nextNodeId = 1;
bool bindWindowOpen = true;
uint32_t bindWindowEndsAt = 0;
uint32_t txSeq = 1;
uint32_t lastLedBeatAt = 0;
bool statusLedOn = false;
uint32_t lastPollSentAt = 0;
bool streamEnabled = false;
bool promptShown = false;
bool controllerTimeValid = false;
time_t controllerBaseEpoch = 0;
uint32_t controllerBaseMillis = 0;
RssiSample rssiSamples[RSSI_CACHE_SLOTS];
size_t nextRssiSampleSlot = 0;

struct OtaSession {
    bool active = false;
    bool awaitingAck = false;
    uint32_t nodeId = 0;
    uint8_t mac[6] = {0};
    uint32_t totalSize = 0;
    uint32_t expectedCrc32 = 0;
    uint32_t nextOffset = 0;
    uint32_t bytesReceived = 0;
    OtaPhase pendingPhase = OTA_PHASE_BEGIN;
    uint32_t pendingOffset = 0;
    uint16_t pendingLen = 0;
    uint8_t pendingData[OTA_CHUNK_BYTES] = {0};
    uint32_t ackDeadlineMs = 0;
    uint8_t retryCount = 0;
} otaSession;

void handleIncomingPacket(const uint8_t* mac, const uint8_t* data, int len, int8_t rssiDbm = 0);
void logSendStatus(esp_now_send_status_t status);
void rebuildPollSchedule();
void handleOtaAck(const uint8_t* mac, const OtaAck& ack);
void retryPendingOtaFrame();
void onPromiscuousPacket(void* buf, wifi_promiscuous_pkt_type_t type);

bool timeReached(uint32_t now, uint32_t target) {
    return static_cast<int32_t>(now - target) >= 0;
}

uint32_t normalizeReportInterval(uint32_t ms) {
    return max(ms, MIN_REPORT_MS);
}

uint8_t rssiToSignalPercent(int8_t rssiDbm) {
    if (rssiDbm >= 0) return 0;
    if (rssiDbm <= -90) return 0;
    if (rssiDbm >= -50) return 100;
    return static_cast<uint8_t>(((static_cast<int16_t>(rssiDbm) + 90) * 100) / 40);
}

const char* otaStatusToString(uint8_t status) {
    switch (status) {
        case OTA_STATUS_OK: return "ok";
        case OTA_STATUS_REJECTED: return "rejected";
        case OTA_STATUS_NOT_ACTIVE: return "not_active";
        case OTA_STATUS_OFFSET_MISMATCH: return "offset_mismatch";
        case OTA_STATUS_WRITE_FAILED: return "write_failed";
        case OTA_STATUS_END_FAILED: return "end_failed";
        case OTA_STATUS_CRC_MISMATCH: return "crc_mismatch";
        case OTA_STATUS_BUSY: return "busy";
        default: return "unknown";
    }
}

void clearOtaSession() {
    otaSession = OtaSession{};
}

time_t controllerNow() {
    if (!controllerTimeValid) return 0;
    return controllerBaseEpoch + (millis() - controllerBaseMillis) / 1000;
}

void setControllerTime(time_t epochSeconds) {
    controllerTimeValid = true;
    controllerBaseEpoch = epochSeconds;
    controllerBaseMillis = millis();
    prefs.putBool("timeValid", true);
    prefs.putULong64("timeEpoch", static_cast<uint64_t>(epochSeconds));
}

String formatControllerTimeIso(time_t epochSeconds) {
    struct tm tmUtc{};
    gmtime_r(&epochSeconds, &tmUtc);
    char buf[25];
    snprintf(
        buf,
        sizeof(buf),
        "%04d-%02d-%02dT%02d:%02d:%02dZ",
        tmUtc.tm_year + 1900,
        tmUtc.tm_mon + 1,
        tmUtc.tm_mday,
        tmUtc.tm_hour,
        tmUtc.tm_min,
        tmUtc.tm_sec
    );
    return String(buf);
}

String withControllerTime(const String& json) {
    if (!controllerTimeValid) return json;

    const int insertAt = json.lastIndexOf('}');
    if (insertAt < 0) return json;

    const time_t now = controllerNow();
    String enriched = json;
    enriched.remove(insertAt);
    enriched += ",\"controller_unix\":";
    enriched += String(static_cast<unsigned long>(now));
    enriched += ",\"controller_time\":\"";
    enriched += formatControllerTimeIso(now);
    enriched += "\"}";
    return enriched;
}

void printPrompt() {
    Serial.print("> ");
    promptShown = true;
}

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

bool macEquals(const uint8_t* a, const uint8_t* b) {
    return memcmp(a, b, 6) == 0;
}

void recordSniffedRssi(const uint8_t* mac, int8_t rssiDbm) {
    for (size_t i = 0; i < RSSI_CACHE_SLOTS; ++i) {
        if (!rssiSamples[i].valid || !macEquals(rssiSamples[i].mac, mac)) continue;
        memcpy(rssiSamples[i].mac, mac, sizeof(rssiSamples[i].mac));
        rssiSamples[i].rssiDbm = rssiDbm;
        rssiSamples[i].seenAtMs = millis();
        return;
    }

    RssiSample& slot = rssiSamples[nextRssiSampleSlot];
    slot.valid = true;
    memcpy(slot.mac, mac, sizeof(slot.mac));
    slot.rssiDbm = rssiDbm;
    slot.seenAtMs = millis();
    nextRssiSampleSlot = (nextRssiSampleSlot + 1) % RSSI_CACHE_SLOTS;
}

int8_t lookupSniffedRssi(const uint8_t* mac) {
    const uint32_t now = millis();
    for (size_t i = 0; i < RSSI_CACHE_SLOTS; ++i) {
        if (!rssiSamples[i].valid || !macEquals(rssiSamples[i].mac, mac)) continue;
        if (now - rssiSamples[i].seenAtMs > RSSI_CACHE_MAX_AGE_MS) continue;
        return rssiSamples[i].rssiDbm;
    }
    return 0;
}

int findNodeByMac(const uint8_t* mac) {
    for (size_t i = 0; i < MAX_NODES; ++i) {
        if (nodes[i].used && macEquals(nodes[i].mac, mac)) return static_cast<int>(i);
    }
    return -1;
}

int findNodeById(uint32_t nodeId) {
    for (size_t i = 0; i < MAX_NODES; ++i) {
        if (nodes[i].used && nodes[i].nodeId == nodeId) return static_cast<int>(i);
    }
    return -1;
}

int allocateNodeSlot() {
    for (size_t i = 0; i < MAX_NODES; ++i) {
        if (!nodes[i].used) return static_cast<int>(i);
    }
    return -1;
}

void saveNode(size_t i) {
    String base = "node" + String(i) + "_";
    prefs.putBool((base + "used").c_str(), nodes[i].used);
    prefs.putUInt((base + "id").c_str(), nodes[i].nodeId);
    prefs.putBytes((base + "mac").c_str(), nodes[i].mac, 6);
    prefs.putString((base + "name").c_str(), String(nodes[i].name));
    prefs.putUInt((base + "rpt").c_str(), normalizeReportInterval(nodes[i].reportIntervalMs));
    prefs.putUChar((base + "fwmaj").c_str(), nodes[i].fwMajor);
    prefs.putUChar((base + "fwmin").c_str(), nodes[i].fwMinor);
    prefs.putFloat((base + "toff").c_str(), nodes[i].tempOffsetC);
}

void loadNodes() {
    prefs.begin("tmon-ctrl", false);
    nextNodeId = prefs.getUInt("nextNodeId", 1);
    controllerTimeValid = prefs.getBool("timeValid", false);
    controllerBaseEpoch = static_cast<time_t>(prefs.getULong64("timeEpoch", 0));
    controllerBaseMillis = millis();
    for (size_t i = 0; i < MAX_NODES; ++i) {
        String base = "node" + String(i) + "_";
        nodes[i].used = prefs.getBool((base + "used").c_str(), false);
        if (!nodes[i].used) continue;
        nodes[i].nodeId = prefs.getUInt((base + "id").c_str(), 0);
        prefs.getBytes((base + "mac").c_str(), nodes[i].mac, 6);
        String name = prefs.getString((base + "name").c_str(), "node");
        name.toCharArray(nodes[i].name, sizeof(nodes[i].name));
        nodes[i].reportIntervalMs = normalizeReportInterval(prefs.getUInt((base + "rpt").c_str(), DEFAULT_REPORT_MS));
        nodes[i].fwMajor = prefs.getUChar((base + "fwmaj").c_str(), 0);
        nodes[i].fwMinor = prefs.getUChar((base + "fwmin").c_str(), 0);
        nodes[i].tempOffsetC = prefs.getFloat((base + "toff").c_str(), 0.0f);
    }
}

void printJsonEvent(const String& json, bool force = false) {
    if (!force && !streamEnabled) return;
    Serial.println(withControllerTime(json));
}

bool ensurePeer(const uint8_t* mac) {
    if (esp_now_is_peer_exist(mac)) return true;
    esp_now_peer_info_t peer{};
    memcpy(peer.peer_addr, mac, 6);
    peer.channel = RADIO_CHANNEL;
    peer.encrypt = false;
    return esp_now_add_peer(&peer) == ESP_OK;
}

void sendBindAck(const uint8_t* mac, const NodeRecord& node) {
    BindAck ack{};
    fillHeader(ack.header, MSG_BIND_ACK, txSeq++, node.nodeId, millis());
    ack.assignedNodeId = node.nodeId;
    ack.reportIntervalMs = normalizeReportInterval(node.reportIntervalMs);
    ack.tempOffsetC = node.tempOffsetC;
    WiFi.macAddress(ack.controllerMac);
    ack.accepted = 1;
    ensurePeer(mac);
    esp_now_send(mac, reinterpret_cast<const uint8_t*>(&ack), sizeof(ack));
}

void sendConfig(uint32_t nodeId, uint32_t reportIntervalMs, float tempOffsetC) {
    int idx = findNodeById(nodeId);
    if (idx < 0) return;
    ConfigSet cfg{};
    fillHeader(cfg.header, MSG_CONFIG_SET, txSeq++, nodeId, millis());
    cfg.reportIntervalMs = normalizeReportInterval(reportIntervalMs);
    cfg.tempOffsetC = tempOffsetC;
    ensurePeer(nodes[idx].mac);
    esp_now_send(nodes[idx].mac, reinterpret_cast<const uint8_t*>(&cfg), sizeof(cfg));
}

void sendSampleRequest(size_t idx) {
    SampleRequest req{};
    fillHeader(req.header, MSG_SAMPLE_REQ, txSeq++, nodes[idx].nodeId, millis());
    ensurePeer(nodes[idx].mac);
    esp_now_send(nodes[idx].mac, reinterpret_cast<const uint8_t*>(&req), sizeof(req));
}

void armOtaAckWait(OtaPhase phase, uint32_t offset = 0, uint16_t len = 0, const uint8_t* data = nullptr) {
    otaSession.awaitingAck = true;
    otaSession.pendingPhase = phase;
    otaSession.pendingOffset = offset;
    otaSession.pendingLen = len;
    if (data != nullptr && len > 0) {
        memcpy(otaSession.pendingData, data, len);
    }
    otaSession.retryCount = 0;
    otaSession.ackDeadlineMs = millis() + OTA_ACK_TIMEOUT_MS;
}

void sendOtaBeginFrame() {
    OtaBegin msg{};
    fillHeader(msg.header, MSG_OTA_BEGIN, txSeq++, otaSession.nodeId, millis());
    msg.totalSize = otaSession.totalSize;
    msg.expectedCrc32 = otaSession.expectedCrc32;
    ensurePeer(otaSession.mac);
    esp_now_send(otaSession.mac, reinterpret_cast<const uint8_t*>(&msg), sizeof(msg));
}

void sendOtaChunkFrame(uint32_t offset, const uint8_t* data, uint16_t len) {
    OtaChunk msg{};
    fillHeader(msg.header, MSG_OTA_CHUNK, txSeq++, otaSession.nodeId, millis());
    msg.offset = offset;
    msg.dataLen = len;
    memcpy(msg.data, data, len);
    ensurePeer(otaSession.mac);
    esp_now_send(otaSession.mac, reinterpret_cast<const uint8_t*>(&msg), sizeof(msg));
}

void sendOtaEndFrame() {
    OtaEnd msg{};
    fillHeader(msg.header, MSG_OTA_END, txSeq++, otaSession.nodeId, millis());
    msg.totalSize = otaSession.totalSize;
    msg.expectedCrc32 = otaSession.expectedCrc32;
    ensurePeer(otaSession.mac);
    esp_now_send(otaSession.mac, reinterpret_cast<const uint8_t*>(&msg), sizeof(msg));
}

bool beginOta(uint32_t nodeId, uint32_t totalSize, uint32_t expectedCrc32) {
    if (otaSession.active) return false;

    int idx = findNodeById(nodeId);
    if (idx < 0) return false;

    otaSession.active = true;
    otaSession.nodeId = nodeId;
    otaSession.totalSize = totalSize;
    otaSession.expectedCrc32 = expectedCrc32;
    memcpy(otaSession.mac, nodes[idx].mac, sizeof(otaSession.mac));

    sendOtaBeginFrame();
    armOtaAckWait(OTA_PHASE_BEGIN);
    printJsonEvent(
        "{\"event\":\"ota_begin_sent\",\"node_id\":" + String(nodeId) +
        ",\"size\":" + String(totalSize) +
        ",\"crc32\":\"" + String(expectedCrc32, HEX) + "\"}"
    );
    return true;
}

bool queueOtaChunk(uint32_t offset, const uint8_t* data, uint16_t len) {
    if (!otaSession.active || otaSession.awaitingAck) return false;
    if (offset != otaSession.nextOffset) return false;
    if (len == 0 || len > OTA_CHUNK_BYTES) return false;

    sendOtaChunkFrame(offset, data, len);
    armOtaAckWait(OTA_PHASE_CHUNK, offset, len, data);
    return true;
}

bool finishOta() {
    if (!otaSession.active || otaSession.awaitingAck) return false;
    if (otaSession.nextOffset != otaSession.totalSize) return false;

    sendOtaEndFrame();
    armOtaAckWait(OTA_PHASE_END);
    return true;
}

void abortOta(const char* reason) {
    printJsonEvent(String("{\"event\":\"ota_aborted\",\"reason\":\"") + reason + "\"}");
    clearOtaSession();
}

void retryPendingOtaFrame() {
    switch (otaSession.pendingPhase) {
        case OTA_PHASE_BEGIN:
            sendOtaBeginFrame();
            break;
        case OTA_PHASE_CHUNK:
            sendOtaChunkFrame(otaSession.pendingOffset, otaSession.pendingData, otaSession.pendingLen);
            break;
        case OTA_PHASE_END:
            sendOtaEndFrame();
            break;
    }
    otaSession.retryCount++;
    otaSession.ackDeadlineMs = millis() + OTA_ACK_TIMEOUT_MS;
}

size_t countActiveNodes() {
    size_t count = 0;
    for (const NodeRecord& node : nodes) {
        if (node.used) ++count;
    }
    return count;
}

void rebuildPollSchedule() {
    const uint32_t now = millis();
    size_t activeCount = countActiveNodes();
    if (activeCount == 0) return;

    uint32_t shortestIntervalMs = UINT32_MAX;
    for (const NodeRecord& node : nodes) {
        if (!node.used) continue;
        shortestIntervalMs = min(shortestIntervalMs, normalizeReportInterval(node.reportIntervalMs));
    }

    const uint32_t spacingMs = max(MIN_POLL_GAP_MS, shortestIntervalMs / activeCount);
    uint32_t offsetMs = 0;
    for (NodeRecord& node : nodes) {
        if (!node.used) continue;
        node.nextPollDueAtMs = now + offsetMs;
        offsetMs += spacingMs;
    }
}

void pollSatellites() {
    if (otaSession.active) return;

    const uint32_t now = millis();
    if (!timeReached(now, lastPollSentAt + MIN_POLL_GAP_MS)) return;

    int dueIdx = -1;
    uint32_t earliestDueAt = 0;
    for (size_t i = 0; i < MAX_NODES; ++i) {
        if (!nodes[i].used) continue;
        if (!timeReached(now, nodes[i].nextPollDueAtMs)) continue;
        if (dueIdx < 0 || timeReached(earliestDueAt, nodes[i].nextPollDueAtMs)) {
            dueIdx = static_cast<int>(i);
            earliestDueAt = nodes[i].nextPollDueAtMs;
        }
    }

    if (dueIdx < 0) return;

    sendSampleRequest(static_cast<size_t>(dueIdx));
    nodes[dueIdx].nextPollDueAtMs = now + normalizeReportInterval(nodes[dueIdx].reportIntervalMs);
    lastPollSentAt = now;
}

void handleBindRequest(const uint8_t* mac, const BindRequest& req) {
    if (!bindWindowOpen || millis() > bindWindowEndsAt) return;

    int idx = findNodeByMac(mac);
    bool isNewNode = false;
    if (idx < 0) {
        idx = allocateNodeSlot();
        if (idx < 0) {
            printJsonEvent("{\"event\":\"bind_rejected\",\"reason\":\"node_table_full\"}");
            return;
        }
        isNewNode = true;
        nodes[idx].used = true;
        nodes[idx].nodeId = nextNodeId++;
        memcpy(nodes[idx].mac, mac, 6);
        prefs.putUInt("nextNodeId", nextNodeId);
    }

    strncpy(nodes[idx].name, req.nodeName, sizeof(nodes[idx].name) - 1);
    nodes[idx].name[sizeof(nodes[idx].name) - 1] = '\0';
    nodes[idx].fwMajor = req.fwMajor;
    nodes[idx].fwMinor = req.fwMinor;
    if (isNewNode) {
        nodes[idx].reportIntervalMs = DEFAULT_REPORT_MS;
    }
    nodes[idx].lastSeenMs = millis();
    saveNode(idx);
    rebuildPollSchedule();
    sendBindAck(mac, nodes[idx]);

    printJsonEvent(
        "{\"event\":\"node_bound\",\"node_id\":" + String(nodes[idx].nodeId) +
        ",\"name\":\"" + String(nodes[idx].name) +
        "\",\"mac\":\"" + macToString(mac) + "\"}"
    );
}

void handleReading(const uint8_t* mac, const Reading& msg, int8_t rssiDbm) {
    int idx = findNodeByMac(mac);
    if (idx < 0) return;
    nodes[idx].lastSeenMs = millis();
    nodes[idx].lastTempC = msg.temperatureC;
    nodes[idx].lastHumidity = msg.humidityPct;
    nodes[idx].sensorOk = msg.sensorOk;
    nodes[idx].fwMajor = msg.reserved[0];
    nodes[idx].fwMinor = msg.reserved[1];
    nodes[idx].lastRssiDbm = rssiDbm;

    printJsonEvent(
        "{\"event\":\"reading\",\"node_id\":" + String(nodes[idx].nodeId) +
        ",\"name\":\"" + String(nodes[idx].name) +
        "\",\"temperature_c\":" + String(msg.temperatureC, 2) +
        ",\"humidity_pct\":" + String(msg.humidityPct, 2) +
        ",\"sensor_ok\":" + String(msg.sensorOk ? "true" : "false") +
        ",\"fw_version\":\"" + String(nodes[idx].fwMajor) + "." + String(nodes[idx].fwMinor) + "\"" +
        ",\"rssi_dbm\":" + String(nodes[idx].lastRssiDbm) +
        ",\"signal_pct\":" + String(rssiToSignalPercent(nodes[idx].lastRssiDbm)) +
        ",\"mac\":\"" + macToString(mac) + "\"}"
    );
}

void handleHeartbeat(const uint8_t* mac, const Heartbeat& msg, int8_t rssiDbm) {
    int idx = findNodeByMac(mac);
    if (idx < 0) return;
    nodes[idx].lastSeenMs = millis();
    nodes[idx].sensorOk = msg.sensorOk;
    nodes[idx].fwMajor = static_cast<uint8_t>(msg.reserved & 0xFF);
    nodes[idx].fwMinor = static_cast<uint8_t>((msg.reserved >> 8) & 0xFF);
    nodes[idx].lastRssiDbm = rssiDbm;

    printJsonEvent(
        "{\"event\":\"heartbeat\",\"node_id\":" + String(nodes[idx].nodeId) +
        ",\"name\":\"" + String(nodes[idx].name) +
        "\",\"channel\":" + String(msg.wifiChannel) +
        ",\"fw_version\":\"" + String(nodes[idx].fwMajor) + "." + String(nodes[idx].fwMinor) + "\"" +
        ",\"rssi_dbm\":" + String(nodes[idx].lastRssiDbm) +
        ",\"signal_pct\":" + String(rssiToSignalPercent(nodes[idx].lastRssiDbm)) + "}"
    );
}

void handleConfigAck(const uint8_t* mac, const ConfigAck& msg) {
    int idx = findNodeByMac(mac);
    if (idx < 0) return;
    nodes[idx].reportIntervalMs = normalizeReportInterval(msg.reportIntervalMs);
    nodes[idx].tempOffsetC = msg.tempOffsetC;
    saveNode(idx);
    rebuildPollSchedule();
    printJsonEvent(
        "{\"event\":\"config_ack\",\"node_id\":" + String(nodes[idx].nodeId) +
        ",\"report_interval_ms\":" + String(nodes[idx].reportIntervalMs) +
        ",\"temp_offset_c\":" + String(nodes[idx].tempOffsetC, 2) +
        ",\"applied\":" + String(msg.applied ? "true" : "false") + "}"
    );
}

void handleOtaAck(const uint8_t* mac, const OtaAck& ack) {
    if (!otaSession.active || !macEquals(mac, otaSession.mac)) return;

    otaSession.awaitingAck = false;
    otaSession.bytesReceived = ack.bytesReceived;

    printJsonEvent(
        "{\"event\":\"ota_ack\",\"node_id\":" + String(otaSession.nodeId) +
        ",\"phase\":" + String(ack.phase) +
        ",\"status\":\"" + String(otaStatusToString(ack.status)) +
        "\",\"bytes_received\":" + String(ack.bytesReceived) +
        ",\"detail\":" + String(ack.detail) + "}"
    );

    if (ack.status != OTA_STATUS_OK) {
        if (ack.status == OTA_STATUS_OFFSET_MISMATCH) {
            otaSession.nextOffset = ack.bytesReceived;
            return;
        }
        if (ack.phase == OTA_PHASE_BEGIN) {
            clearOtaSession();
        }
        if (ack.phase == OTA_PHASE_END) {
            clearOtaSession();
        }
        return;
    }

    switch (ack.phase) {
        case OTA_PHASE_BEGIN:
            otaSession.nextOffset = ack.bytesReceived;
            break;
        case OTA_PHASE_CHUNK:
            otaSession.nextOffset = ack.bytesReceived;
            break;
        case OTA_PHASE_END:
            printJsonEvent(
                "{\"event\":\"ota_complete\",\"node_id\":" + String(otaSession.nodeId) +
                ",\"bytes\":" + String(ack.bytesReceived) + "}"
            );
            clearOtaSession();
            break;
    }
}

void handleIncomingPacket(const uint8_t* mac, const uint8_t* data, int len, int8_t rssiDbm) {
    if (len < static_cast<int>(sizeof(Header))) return;
    if (rssiDbm == 0) {
        rssiDbm = lookupSniffedRssi(mac);
    }
    const Header* h = reinterpret_cast<const Header*>(data);
    if (!validHeader(*h)) return;

    switch (h->type) {
        case MSG_BIND_REQUEST:
            if (len == sizeof(BindRequest)) handleBindRequest(mac, *reinterpret_cast<const BindRequest*>(data));
            break;
        case MSG_READING:
            if (len == sizeof(Reading)) handleReading(mac, *reinterpret_cast<const Reading*>(data), rssiDbm);
            break;
        case MSG_HEARTBEAT:
            if (len == sizeof(Heartbeat)) handleHeartbeat(mac, *reinterpret_cast<const Heartbeat*>(data), rssiDbm);
            break;
        case MSG_CONFIG_ACK:
            if (len == sizeof(ConfigAck)) handleConfigAck(mac, *reinterpret_cast<const ConfigAck*>(data));
            break;
        case MSG_OTA_ACK:
            if (len == sizeof(OtaAck)) handleOtaAck(mac, *reinterpret_cast<const OtaAck*>(data));
            break;
        default:
            break;
    }
}

void logSendStatus(esp_now_send_status_t status) {
    if (status != ESP_NOW_SEND_SUCCESS) {
        printJsonEvent(String("{\"event\":\"tx_status\",\"ok\":false}"), true);
    }
}

void onPromiscuousPacket(void* buf, wifi_promiscuous_pkt_type_t type) {
    if (type != WIFI_PKT_MGMT || buf == nullptr) return;

    const auto* pkt = static_cast<const wifi_promiscuous_pkt_t*>(buf);
    const uint8_t* frame = pkt->payload;
    const uint16_t len = pkt->rx_ctrl.sig_len;

    if (len < 39) return;
    if (frame[24] != ESPNOW_CATEGORY_CODE) return;
    if (memcmp(frame + 25, ESPRESSIF_OUI, sizeof(ESPRESSIF_OUI)) != 0) return;
    if (frame[32] != ESPNOW_ELEMENT_ID) return;
    if (memcmp(frame + 34, ESPRESSIF_OUI, sizeof(ESPRESSIF_OUI)) != 0) return;
    if (frame[37] != ESPNOW_TYPE) return;

    recordSniffedRssi(frame + 10, pkt->rx_ctrl.rssi);
}

#if ESP_ARDUINO_VERSION >= ESP_ARDUINO_VERSION_VAL(3, 0, 0)
void onDataRecv(const esp_now_recv_info_t* recvInfo, const uint8_t* data, int len) {
    const int8_t rssiDbm =
        (recvInfo != nullptr && recvInfo->rx_ctrl != nullptr) ? recvInfo->rx_ctrl->rssi : 0;
    handleIncomingPacket(recvInfo->src_addr, data, len, rssiDbm);
}

void onDataSent(const wifi_tx_info_t*, esp_now_send_status_t status) {
    logSendStatus(status);
}
#else
void onDataRecv(const uint8_t* mac, const uint8_t* data, int len) {
    handleIncomingPacket(mac, data, len, 0);
}

void onDataSent(const uint8_t*, esp_now_send_status_t status) {
    logSendStatus(status);
}
#endif

void openBindWindow(uint32_t durationMs = BIND_WINDOW_MS) {
    bindWindowOpen = true;
    bindWindowEndsAt = millis() + durationMs;
    printJsonEvent(String("{\"event\":\"bind_window\",\"open\":true,\"duration_ms\":") + durationMs + "}");
}

void listNodes() {
    Serial.println("{\"event\":\"nodes\",\"items\":[");
    bool first = true;
    for (size_t i = 0; i < MAX_NODES; ++i) {
        if (!nodes[i].used) continue;
        if (!first) Serial.println(",");
        first = false;
        Serial.print("{\"node_id\":"); Serial.print(nodes[i].nodeId);
        Serial.print(",\"name\":\""); Serial.print(nodes[i].name);
        Serial.print("\",\"mac\":\""); Serial.print(macToString(nodes[i].mac));
        Serial.print("\",\"last_seen_ms\":"); Serial.print(nodes[i].lastSeenMs);
        Serial.print(",\"report_interval_ms\":"); Serial.print(nodes[i].reportIntervalMs);
        Serial.print(",\"fw_version\":\""); Serial.print(nodes[i].fwMajor); Serial.print("."); Serial.print(nodes[i].fwMinor); Serial.print("\"");
        Serial.print(",\"rssi_dbm\":"); Serial.print(nodes[i].lastRssiDbm);
        Serial.print(",\"signal_pct\":"); Serial.print(rssiToSignalPercent(nodes[i].lastRssiDbm));
        Serial.print(",\"temp_offset_c\":"); Serial.print(nodes[i].tempOffsetC, 2);
        Serial.print(",\"next_poll_in_ms\":"); Serial.print(static_cast<int32_t>(nodes[i].nextPollDueAtMs - millis()));
        Serial.print("}");
    }
    Serial.println("]}");
}

int hexNibble(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return 10 + (c - 'a');
    if (c >= 'A' && c <= 'F') return 10 + (c - 'A');
    return -1;
}

bool decodeHexPayload(const String& hex, uint8_t* out, uint16_t& outLen) {
    if ((hex.length() % 2) != 0) return false;
    const size_t bytes = hex.length() / 2;
    if (bytes == 0 || bytes > OTA_CHUNK_BYTES) return false;

    for (size_t i = 0; i < bytes; ++i) {
        int hi = hexNibble(hex[2 * i]);
        int lo = hexNibble(hex[2 * i + 1]);
        if (hi < 0 || lo < 0) return false;
        out[i] = static_cast<uint8_t>((hi << 4) | lo);
    }
    outLen = static_cast<uint16_t>(bytes);
    return true;
}

void processSerialCommand(const String& raw) {
    String cmd = raw;
    cmd.trim();
    promptShown = false;
    if (cmd.equalsIgnoreCase("HELP")) {
        Serial.println("Commands: HELP, NODES, BIND, BIND OFF, SETINT <nodeId> <ms>, SETTOFF <nodeId> <tempOffsetC>, STREAM OFF, STREAM ON, TIME STATUS, TIME SET <unixSeconds>, OTA BEGIN <nodeId> <size> <crc32hex>, OTA CHUNK <offset> <hex>, OTA END, OTA STATUS, OTA ABORT");
    } else if (cmd.equalsIgnoreCase("NODES")) {
        listNodes();
    } else if (cmd.equalsIgnoreCase("BIND")) {
        openBindWindow();
    } else if (cmd.equalsIgnoreCase("BIND OFF")) {
        bindWindowOpen = false;
        Serial.println("{\"event\":\"bind_window\",\"open\":false}");
    } else if (cmd.equalsIgnoreCase("STREAM OFF")) {
        streamEnabled = false;
        printJsonEvent("{\"event\":\"stream\",\"enabled\":false}", true);
    } else if (cmd.equalsIgnoreCase("STREAM ON")) {
        streamEnabled = true;
        printJsonEvent("{\"event\":\"stream\",\"enabled\":true}", true);
    } else if (cmd.equalsIgnoreCase("TIME STATUS")) {
        if (controllerTimeValid) {
            const time_t now = controllerNow();
            printJsonEvent(
                "{\"event\":\"time_status\",\"valid\":true,\"unix\":" +
                String(static_cast<unsigned long>(now)) +
                ",\"iso\":\"" + formatControllerTimeIso(now) + "\"}",
                true
            );
        } else {
            printJsonEvent("{\"event\":\"time_status\",\"valid\":false}", true);
        }
    } else if (cmd.startsWith("TIME SET ")) {
        const time_t epochSeconds = static_cast<time_t>(strtoul(cmd.substring(9).c_str(), nullptr, 10));
        if (epochSeconds == 0) {
            printJsonEvent("{\"event\":\"time_error\",\"reason\":\"invalid_epoch\"}", true);
        } else {
            setControllerTime(epochSeconds);
            const time_t now = controllerNow();
            printJsonEvent(
                "{\"event\":\"time_set\",\"unix\":" +
                String(static_cast<unsigned long>(now)) +
                ",\"iso\":\"" + formatControllerTimeIso(now) + "\"}",
                true
            );
        }
    } else if (cmd.startsWith("SETINT ")) {
        int sp = cmd.indexOf(' ', 7);
        if (sp > 0) {
            uint32_t nodeId = cmd.substring(7, sp).toInt();
            uint32_t ms = cmd.substring(sp + 1).toInt();
            int idx = findNodeById(nodeId);
            if (idx >= 0) sendConfig(nodeId, ms, nodes[idx].tempOffsetC);
        }
    } else if (cmd.startsWith("SETTOFF ")) {
        int sp = cmd.indexOf(' ', 8);
        if (sp > 0) {
            uint32_t nodeId = cmd.substring(8, sp).toInt();
            float offsetC = cmd.substring(sp + 1).toFloat();
            int idx = findNodeById(nodeId);
            if (idx >= 0) {
                nodes[idx].tempOffsetC = offsetC;
                saveNode(static_cast<size_t>(idx));
                sendConfig(nodeId, nodes[idx].reportIntervalMs, nodes[idx].tempOffsetC);
            }
        }
    } else if (cmd.equalsIgnoreCase("OTA STATUS")) {
        printJsonEvent(
            "{\"event\":\"ota_status\",\"active\":" + String(otaSession.active ? "true" : "false") +
            ",\"awaiting_ack\":" + String(otaSession.awaitingAck ? "true" : "false") +
            ",\"node_id\":" + String(otaSession.nodeId) +
            ",\"next_offset\":" + String(otaSession.nextOffset) +
            ",\"bytes_received\":" + String(otaSession.bytesReceived) + "}"
        );
    } else if (cmd.equalsIgnoreCase("OTA ABORT")) {
        abortOta("host_request");
    } else if (cmd.startsWith("OTA BEGIN ")) {
        int sp1 = cmd.indexOf(' ', 10);
        int sp2 = sp1 > 0 ? cmd.indexOf(' ', sp1 + 1) : -1;
        if (sp1 > 0 && sp2 > 0) {
            uint32_t nodeId = cmd.substring(10, sp1).toInt();
            uint32_t totalSize = cmd.substring(sp1 + 1, sp2).toInt();
            uint32_t expectedCrc32 = strtoul(cmd.substring(sp2 + 1).c_str(), nullptr, 16);
            if (!beginOta(nodeId, totalSize, expectedCrc32)) {
                printJsonEvent("{\"event\":\"ota_error\",\"reason\":\"begin_rejected\"}");
            }
        }
    } else if (cmd.startsWith("OTA CHUNK ")) {
        int sp1 = cmd.indexOf(' ', 10);
        if (sp1 > 0) {
            uint32_t offset = cmd.substring(10, sp1).toInt();
            String hex = cmd.substring(sp1 + 1);
            uint8_t data[OTA_CHUNK_BYTES];
            uint16_t len = 0;
            if (!decodeHexPayload(hex, data, len)) {
                printJsonEvent("{\"event\":\"ota_error\",\"reason\":\"invalid_hex\"}");
                return;
            }
            if (!queueOtaChunk(offset, data, len)) {
                printJsonEvent(
                    "{\"event\":\"ota_error\",\"reason\":\"chunk_rejected\",\"expected_offset\":" +
                    String(otaSession.nextOffset) + "}"
                );
            }
        }
    } else if (cmd.equalsIgnoreCase("OTA END")) {
        if (!finishOta()) {
            printJsonEvent("{\"event\":\"ota_error\",\"reason\":\"end_rejected\"}");
        }
    }
    printPrompt();
}

void setup() {
    Serial.begin(115200);
    delay(500);
    initStatusLed();

    loadNodes();
    rebuildPollSchedule();

    WiFi.mode(WIFI_STA);
    WiFi.disconnect();
    esp_wifi_set_promiscuous(true);
    esp_wifi_set_channel(RADIO_CHANNEL, WIFI_SECOND_CHAN_NONE);
    wifi_promiscuous_filter_t filter{};
    filter.filter_mask = WIFI_PROMIS_FILTER_MASK_MGMT;
    esp_wifi_set_promiscuous_filter(&filter);
    esp_wifi_set_promiscuous_rx_cb(onPromiscuousPacket);

    if (esp_now_init() != ESP_OK) {
        Serial.println("{\"event\":\"fatal\",\"reason\":\"esp_now_init_failed\"}");
        return;
    }

    esp_now_register_recv_cb(onDataRecv);
    esp_now_register_send_cb(onDataSent);

    uint8_t broadcastMac[] = {0xFF,0xFF,0xFF,0xFF,0xFF,0xFF};
    ensurePeer(broadcastMac);

    openBindWindow();
    Serial.println("{\"event\":\"controller_ready\",\"channel\":6}");
    printPrompt();
}

void loop() {
    static String line;
    updateStatusLed();

    while (Serial.available()) {
        char c = static_cast<char>(Serial.read());
        if (c == '\n' || c == '\r') {
            if (!line.isEmpty()) {
                Serial.println();
                processSerialCommand(line);
            } else if (!promptShown) {
                printPrompt();
            }
            line = "";
        } else if (c == '\b' || c == 127) {
            if (!line.isEmpty()) {
                line.remove(line.length() - 1);
                Serial.print("\b \b");
            }
        } else {
            line += c;
            Serial.print(c);
        }
    }

    if (bindWindowOpen && millis() > bindWindowEndsAt) {
        bindWindowOpen = false;
        Serial.println("{\"event\":\"bind_window\",\"open\":false}");
    }

    pollSatellites();

    if (otaSession.active && otaSession.awaitingAck && timeReached(millis(), otaSession.ackDeadlineMs)) {
        if (otaSession.retryCount >= OTA_MAX_RETRIES) {
            abortOta("ack_timeout");
        } else {
            retryPendingOtaFrame();
        }
    }
}
