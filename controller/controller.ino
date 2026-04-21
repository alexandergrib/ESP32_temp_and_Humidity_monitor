#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <Preferences.h>
#include <time.h>
#include "protocol.h"

using namespace proto;

static constexpr uint32_t SERIAL_BAUD = 460800;

Preferences prefs;

#ifndef LED_BUILTIN
static constexpr uint8_t STATUS_LED_PIN = 2;
#else
static constexpr uint8_t STATUS_LED_PIN = LED_BUILTIN;
#endif

static constexpr uint32_t STATUS_LED_PERIOD_MS = 1000;
static constexpr uint32_t STATUS_LED_ON_MS = 80;
static constexpr uint32_t MIN_REPORT_SLOT_GAP_MS = 250;
static constexpr uint8_t SATELLITE_AVERAGING_WINDOW_PERCENT = 20;
static constexpr uint32_t SATELLITE_AVERAGING_WINDOW_MAX_MS = 1000;
static constexpr uint32_t OFFLINE_REPROBE_MS = 2000;
static constexpr uint32_t OTA_ACK_TIMEOUT_MS = 1500;
static constexpr uint8_t OTA_MAX_RETRIES = 3;
static constexpr uint32_t OTA_PREP_REPORT_INTERVAL_MS = 30000;
static constexpr bool DEFAULT_SLEEP_ENABLED = false;
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
    bool heaterEnabled = false;
    bool otaReady = false;
    bool sleepEnabled = DEFAULT_SLEEP_ENABLED;
    bool otaPrepActive = false;
    uint16_t sampleRateHz = DEFAULT_SAMPLE_RATE_HZ;
    float tempOffsetC = 0.0f;
    uint32_t nextReportDelayMs = 0;
    uint32_t nextScheduledReportAtMs = 0;
    uint32_t lastDeliveredReadingSeq = 0;
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
void rebuildReportSchedule();
void pushSchedulesToAllNodes();
uint32_t nextWakeDelayForNode(size_t idx, uint32_t controllerUptimeMs);
void handleOtaAck(const uint8_t* mac, const OtaAck& ack);
void handleRenameAck(const uint8_t* mac, const RenameAck& ack);
void retryPendingOtaFrame();
void onPromiscuousPacket(void* buf, wifi_promiscuous_pkt_type_t type);
uint32_t effectiveReportIntervalMs(const NodeRecord& node);
bool effectiveOtaReady(const NodeRecord& node);
bool effectiveOtaPause(const NodeRecord& node);
bool effectiveSleepEnabled(const NodeRecord& node);
void setNodeOtaPrep(size_t idx, bool enabled, bool pushConfig = true);

bool timeReached(uint32_t now, uint32_t target) {
    return static_cast<int32_t>(now - target) >= 0;
}

uint32_t normalizeReportInterval(uint32_t ms) {
    return max(ms, MIN_REPORT_MS);
}

bool sleepAllowedForReportInterval(uint32_t reportIntervalMs) {
    return normalizeReportInterval(reportIntervalMs) >= MIN_SLEEP_REPORT_INTERVAL_MS;
}

uint16_t normalizeSampleRateHz(uint32_t hz) {
    return static_cast<uint16_t>(constrain(hz, MIN_SAMPLE_RATE_HZ, MAX_SAMPLE_RATE_HZ));
}

uint32_t satelliteCaptureWindowMs(uint32_t reportIntervalMs) {
    const uint32_t normalizedReportMs = normalizeReportInterval(reportIntervalMs);
    const uint32_t scaledWindowMs = (normalizedReportMs * SATELLITE_AVERAGING_WINDOW_PERCENT) / 100U;
    return min<uint32_t>(normalizedReportMs, max<uint32_t>(1, min<uint32_t>(scaledWindowMs, SATELLITE_AVERAGING_WINDOW_MAX_MS)));
}

String sanitizeNodeName(String name) {
    name.trim();
    String sanitized;
    sanitized.reserve(15);
    for (size_t i = 0; i < name.length() && sanitized.length() < 15; ++i) {
        const char c = name[i];
        if ((c >= 'A' && c <= 'Z') ||
            (c >= 'a' && c <= 'z') ||
            (c >= '0' && c <= '9') ||
            c == '_' || c == '-' || c == ' ') {
            sanitized += c;
        }
    }
    sanitized.trim();
    if (sanitized.length() == 0) sanitized = "satellite";
    return sanitized;
}

bool nodeNeedsFastProbe(const NodeRecord& node, uint32_t now) {
    if (!node.used) return false;
    if (node.lastSeenMs == 0) return true;

    const uint32_t staleAfterMs = max<uint32_t>(OFFLINE_REPROBE_MS * 2, normalizeReportInterval(node.reportIntervalMs) * 3);
    return now - node.lastSeenMs >= staleAfterMs;
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
        case OTA_STATUS_NOT_READY: return "not_ready";
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
    prefs.putBool((base + "heat").c_str(), nodes[i].heaterEnabled);
    prefs.putBool((base + "sleep").c_str(), nodes[i].sleepEnabled);
    prefs.putUShort((base + "srhz").c_str(), nodes[i].sampleRateHz);
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
        nodes[i].heaterEnabled = prefs.getBool((base + "heat").c_str(), false);
        nodes[i].sleepEnabled = prefs.getBool((base + "sleep").c_str(), DEFAULT_SLEEP_ENABLED);
        nodes[i].sampleRateHz = normalizeSampleRateHz(prefs.getUShort((base + "srhz").c_str(), DEFAULT_SAMPLE_RATE_HZ));
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

uint32_t effectiveReportIntervalMs(const NodeRecord& node) {
    return normalizeReportInterval(node.otaPrepActive ? OTA_PREP_REPORT_INTERVAL_MS : node.reportIntervalMs);
}

bool effectiveOtaReady(const NodeRecord& node) {
    return node.otaPrepActive || node.otaReady;
}

bool effectiveOtaPause(const NodeRecord& node) {
    return otaSession.active && node.used && node.nodeId != otaSession.nodeId;
}

bool effectiveSleepEnabled(const NodeRecord& node) {
    return node.otaPrepActive ? false : (node.sleepEnabled && sleepAllowedForReportInterval(effectiveReportIntervalMs(node)));
}

void alignNodeSchedule(NodeRecord& node, uint32_t nowMs, uint32_t minFutureMs = MIN_REPORT_SLOT_GAP_MS) {
    const uint32_t cycleMs = effectiveReportIntervalMs(node);
    const uint32_t earliestSlotAtMs = nowMs + max<uint32_t>(minFutureMs, satelliteCaptureWindowMs(cycleMs));
    if (node.nextScheduledReportAtMs == 0) {
        node.nextScheduledReportAtMs = earliestSlotAtMs;
        return;
    }
    while (timeReached(earliestSlotAtMs, node.nextScheduledReportAtMs)) {
        node.nextScheduledReportAtMs += cycleMs;
    }
}

void sendBindAck(const uint8_t* mac, size_t idx) {
    NodeRecord& node = nodes[idx];
    BindAck ack{};
    fillHeader(ack.header, MSG_BIND_ACK, txSeq++, node.nodeId, millis());
    ack.assignedNodeId = node.nodeId;
    ack.reportIntervalMs = effectiveReportIntervalMs(node);
    ack.nextReportDelayMs = nextWakeDelayForNode(idx, ack.header.uptimeMs);
    ack.tempOffsetC = node.tempOffsetC;
    ack.heaterEnabled = node.heaterEnabled ? 1 : 0;
    ack.otaReady = effectiveOtaReady(node) ? 1 : 0;
    ack.otaPause = effectiveOtaPause(node) ? 1 : 0;
    ack.sleepEnabled = effectiveSleepEnabled(node) ? 1 : 0;
    ack.sampleRateHz = normalizeSampleRateHz(node.sampleRateHz);
    WiFi.macAddress(ack.controllerMac);
    ack.accepted = 1;
    ensurePeer(mac);
    esp_now_send(mac, reinterpret_cast<const uint8_t*>(&ack), sizeof(ack));
}

void sendConfig(
    uint32_t nodeId,
    uint32_t reportIntervalMs,
    uint32_t nextReportDelayMs,
    float tempOffsetC,
    bool heaterEnabled,
    bool otaReady,
    bool otaPause,
    bool sleepEnabled,
    uint16_t sampleRateHz
) {
    int idx = findNodeById(nodeId);
    if (idx < 0) return;
    ConfigSet cfg{};
    fillHeader(cfg.header, MSG_CONFIG_SET, txSeq++, nodeId, millis());
    cfg.reportIntervalMs = normalizeReportInterval(reportIntervalMs);
    cfg.nextReportDelayMs = nextWakeDelayForNode(static_cast<size_t>(idx), cfg.header.uptimeMs);
    cfg.tempOffsetC = tempOffsetC;
    cfg.heaterEnabled = heaterEnabled ? 1 : 0;
    cfg.otaReady = otaReady ? 1 : 0;
    cfg.otaPause = otaPause ? 1 : 0;
    cfg.sleepEnabled = (sleepEnabled && sleepAllowedForReportInterval(cfg.reportIntervalMs)) ? 1 : 0;
    cfg.sampleRateHz = normalizeSampleRateHz(sampleRateHz);
    ensurePeer(nodes[idx].mac);
    esp_now_send(nodes[idx].mac, reinterpret_cast<const uint8_t*>(&cfg), sizeof(cfg));
}

void sendNodeConfig(size_t idx) {
    if (idx >= MAX_NODES || !nodes[idx].used) return;
    sendConfig(
        nodes[idx].nodeId,
        effectiveReportIntervalMs(nodes[idx]),
        nodes[idx].nextReportDelayMs,
        nodes[idx].tempOffsetC,
        nodes[idx].heaterEnabled,
        effectiveOtaReady(nodes[idx]),
        effectiveOtaPause(nodes[idx]),
        effectiveSleepEnabled(nodes[idx]),
        nodes[idx].sampleRateHz
    );
}

void setNodeOtaReady(size_t idx, bool enabled, bool pushConfig = true) {
    if (idx >= MAX_NODES || !nodes[idx].used) return;
    if (nodes[idx].otaPrepActive && !enabled) {
        setNodeOtaPrep(idx, false, pushConfig);
        return;
    }
    nodes[idx].otaReady = enabled;
    if (pushConfig) {
        sendNodeConfig(idx);
    }
    printJsonEvent(
        "{\"event\":\"ota_ready\",\"node_id\":" + String(nodes[idx].nodeId) +
        ",\"enabled\":" + String(enabled ? "true" : "false") + "}",
        true
    );
}

void setNodeOtaPrep(size_t idx, bool enabled, bool pushConfig) {
    if (idx >= MAX_NODES || !nodes[idx].used) return;
    nodes[idx].otaPrepActive = enabled;
    if (pushConfig) {
        sendNodeConfig(idx);
    }
    printJsonEvent(
        "{\"event\":\"ota_prep\",\"node_id\":" + String(nodes[idx].nodeId) +
        ",\"enabled\":" + String(enabled ? "true" : "false") +
        ",\"ota_ready\":" + String(effectiveOtaReady(nodes[idx]) ? "true" : "false") +
        ",\"sleep_enabled\":" + String(effectiveSleepEnabled(nodes[idx]) ? "true" : "false") +
        ",\"report_interval_ms\":" + String(effectiveReportIntervalMs(nodes[idx])) + "}",
        true
    );
}

void sendRename(uint32_t nodeId, const String& requestedName) {
    int idx = findNodeById(nodeId);
    if (idx < 0) return;

    const String sanitizedName = sanitizeNodeName(requestedName);
    RenameSet msg{};
    fillHeader(msg.header, MSG_RENAME_SET, txSeq++, nodeId, millis());
    sanitizedName.toCharArray(msg.nodeName, sizeof(msg.nodeName));
    ensurePeer(nodes[idx].mac);
    esp_now_send(nodes[idx].mac, reinterpret_cast<const uint8_t*>(&msg), sizeof(msg));
}

uint32_t sendReadingAck(size_t idx, uint32_t readingSequence) {
    if (idx >= MAX_NODES || !nodes[idx].used) return MIN_REPORT_SLOT_GAP_MS;
    ReadingAck ack{};
    fillHeader(ack.header, MSG_READING_ACK, txSeq++, nodes[idx].nodeId, millis());
    ack.readingSequence = readingSequence;
    ack.accepted = 1;
    ack.heaterEnabled = nodes[idx].heaterEnabled ? 1 : 0;
    ack.otaReady = effectiveOtaReady(nodes[idx]) ? 1 : 0;
    ack.otaPause = effectiveOtaPause(nodes[idx]) ? 1 : 0;
    ack.sleepEnabled = effectiveSleepEnabled(nodes[idx]) ? 1 : 0;
    ack.reportIntervalMs = effectiveReportIntervalMs(nodes[idx]);
    ack.nextReportDelayMs = nextWakeDelayForNode(idx, ack.header.uptimeMs);
    ack.tempOffsetC = nodes[idx].tempOffsetC;
    ack.sampleRateHz = normalizeSampleRateHz(nodes[idx].sampleRateHz);
    ensurePeer(nodes[idx].mac);
    esp_now_send(nodes[idx].mac, reinterpret_cast<const uint8_t*>(&ack), sizeof(ack));
    return ack.nextReportDelayMs;
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
    if (!effectiveOtaReady(nodes[idx])) return false;

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

void refreshOtaPauseState() {
    pushSchedulesToAllNodes();
}

void abortOta(const char* reason) {
    if (otaSession.active) {
        int idx = findNodeById(otaSession.nodeId);
        if (idx >= 0) {
            if (nodes[idx].otaPrepActive) {
                setNodeOtaPrep(static_cast<size_t>(idx), false);
            } else {
                setNodeOtaReady(static_cast<size_t>(idx), false);
            }
        }
    }
    printJsonEvent(String("{\"event\":\"ota_aborted\",\"reason\":\"") + reason + "\"}");
    clearOtaSession();
    refreshOtaPauseState();
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

uint32_t nextWakeDelayForNode(size_t idx, uint32_t controllerUptimeMs) {
    if (idx >= MAX_NODES || !nodes[idx].used) {
        return MIN_REPORT_SLOT_GAP_MS;
    }

    NodeRecord& node = nodes[idx];
    const uint32_t cycleMs = effectiveReportIntervalMs(node);
    if (cycleMs == 0) {
        return MIN_REPORT_SLOT_GAP_MS;
    }

    const uint32_t captureLeadMs = satelliteCaptureWindowMs(cycleMs);
    alignNodeSchedule(node, controllerUptimeMs);

    if (timeReached(controllerUptimeMs + captureLeadMs, node.nextScheduledReportAtMs)) {
        return 0;
    }
    return node.nextScheduledReportAtMs - controllerUptimeMs - captureLeadMs;
}

void rebuildReportSchedule() {
    size_t activeCount = countActiveNodes();
    if (activeCount == 0) return;
    uint32_t cycleMs = activeCount * MIN_REPORT_SLOT_GAP_MS;
    for (const NodeRecord& node : nodes) {
        if (!node.used) continue;
        cycleMs = max(cycleMs, effectiveReportIntervalMs(node));
    }
    const uint32_t spacingMs = max(MIN_REPORT_SLOT_GAP_MS, cycleMs / activeCount);
    const uint32_t nowMs = millis();
    uint32_t offsetMs = 0;
    for (NodeRecord& node : nodes) {
        if (!node.used) continue;
        node.reportIntervalMs = cycleMs;
        node.nextReportDelayMs = offsetMs;
        node.nextScheduledReportAtMs = nowMs + spacingMs + offsetMs;
        offsetMs += spacingMs;
    }
}

void pushSchedulesToAllNodes() {
    for (size_t i = 0; i < MAX_NODES; ++i) {
        if (!nodes[i].used) continue;
        sendNodeConfig(i);
    }
}

void handleBindRequest(const uint8_t* mac, const BindRequest& req) {
    int idx = findNodeByMac(mac);
    bool isNewNode = false;
    const uint32_t nowMs = millis();
    if (idx < 0) {
        if (!bindWindowOpen || nowMs > bindWindowEndsAt) {
            return;
        }
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
    bool shouldRebuildSchedule = isNewNode || nodeNeedsFastProbe(nodes[idx], nowMs);

    sanitizeNodeName(String(req.nodeName)).toCharArray(nodes[idx].name, sizeof(nodes[idx].name));
    nodes[idx].fwMajor = req.fwMajor;
    nodes[idx].fwMinor = req.fwMinor;
    if (isNewNode) {
        nodes[idx].reportIntervalMs = DEFAULT_REPORT_MS;
        nodes[idx].sampleRateHz = DEFAULT_SAMPLE_RATE_HZ;
        nodes[idx].sleepEnabled = DEFAULT_SLEEP_ENABLED;
    }
    nodes[idx].lastSeenMs = nowMs;
    saveNode(idx);
    if (shouldRebuildSchedule) {
        rebuildReportSchedule();
    }
    sendBindAck(mac, static_cast<size_t>(idx));
    if (shouldRebuildSchedule) {
        pushSchedulesToAllNodes();
    }

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
    const bool suppressNormalReading =
        effectiveOtaReady(nodes[idx]) ||
        effectiveOtaPause(nodes[idx]) ||
        (otaSession.active && nodes[idx].nodeId == otaSession.nodeId);
    if (nodes[idx].lastDeliveredReadingSeq == msg.header.sequence) {
        sendReadingAck(static_cast<size_t>(idx), msg.header.sequence);
        return;
    }
    if (suppressNormalReading) {
        sendReadingAck(static_cast<size_t>(idx), msg.header.sequence);
        return;
    }
    nodes[idx].lastTempC = msg.temperatureC;
    nodes[idx].lastHumidity = msg.humidityPct;
    nodes[idx].sensorOk = msg.sensorOk;
    nodes[idx].fwMajor = msg.reserved[0];
    nodes[idx].fwMinor = msg.reserved[1];
    nodes[idx].lastRssiDbm = rssiDbm;
    nodes[idx].lastDeliveredReadingSeq = msg.header.sequence;
    const uint32_t nextReportDelayMs = sendReadingAck(static_cast<size_t>(idx), msg.header.sequence);

    printJsonEvent(
        "{\"event\":\"reading\",\"node_id\":" + String(nodes[idx].nodeId) +
        ",\"name\":\"" + String(nodes[idx].name) +
        "\",\"temperature_c\":" + String(msg.temperatureC, 2) +
        ",\"humidity_pct\":" + String(msg.humidityPct, 2) +
        ",\"sensor_ok\":" + String(msg.sensorOk ? "true" : "false") +
        ",\"report_interval_ms\":" + String(effectiveReportIntervalMs(nodes[idx])) +
        ",\"next_report_delay_ms\":" + String(nextReportDelayMs) +
        ",\"fw_version\":\"" + String(nodes[idx].fwMajor) + "." + String(nodes[idx].fwMinor) + "\"" +
        ",\"rssi_dbm\":" + String(nodes[idx].lastRssiDbm) +
        ",\"signal_pct\":" + String(rssiToSignalPercent(nodes[idx].lastRssiDbm)) +
        ",\"mac\":\"" + macToString(mac) + "\"}"
    );
}

void handleConfigAck(const uint8_t* mac, const ConfigAck& msg) {
    int idx = findNodeByMac(mac);
    if (idx < 0) return;
    nodes[idx].lastSeenMs = millis();
    const uint32_t reportedIntervalMs = normalizeReportInterval(msg.reportIntervalMs);
    const bool reportedOtaReady = msg.otaReady != 0;
    const bool reportedSleepEnabled = msg.sleepEnabled != 0;
    if (!nodes[idx].otaPrepActive) {
        nodes[idx].reportIntervalMs = reportedIntervalMs;
        nodes[idx].otaReady = reportedOtaReady;
        nodes[idx].sleepEnabled = reportedSleepEnabled;
    }
    nodes[idx].nextReportDelayMs = msg.nextReportDelayMs;
    nodes[idx].tempOffsetC = msg.tempOffsetC;
    nodes[idx].heaterEnabled = msg.heaterEnabled != 0;
    nodes[idx].sampleRateHz = normalizeSampleRateHz(msg.sampleRateHz);
    saveNode(idx);
    printJsonEvent(
        "{\"event\":\"config_ack\",\"node_id\":" + String(nodes[idx].nodeId) +
        ",\"report_interval_ms\":" + String(reportedIntervalMs) +
        ",\"next_report_delay_ms\":" + String(nodes[idx].nextReportDelayMs) +
        ",\"temp_offset_c\":" + String(nodes[idx].tempOffsetC, 2) +
        ",\"heater_enabled\":" + String(nodes[idx].heaterEnabled ? "true" : "false") +
        ",\"ota_ready\":" + String(reportedOtaReady ? "true" : "false") +
        ",\"sleep_enabled\":" + String(reportedSleepEnabled ? "true" : "false") +
        ",\"ota_prep_active\":" + String(nodes[idx].otaPrepActive ? "true" : "false") +
        ",\"sample_rate_hz\":" + String(nodes[idx].sampleRateHz) +
        ",\"applied\":" + String(msg.applied ? "true" : "false") + "}"
    );
}

void handleRenameAck(const uint8_t* mac, const RenameAck& ack) {
    int idx = findNodeByMac(mac);
    if (idx < 0) return;

    const String sanitizedName = sanitizeNodeName(String(ack.nodeName));
    if (ack.applied) {
        sanitizedName.toCharArray(nodes[idx].name, sizeof(nodes[idx].name));
        nodes[idx].lastSeenMs = millis();
        saveNode(static_cast<size_t>(idx));
    }

    printJsonEvent(
        "{\"event\":\"rename_ack\",\"node_id\":" + String(nodes[idx].nodeId) +
        ",\"name\":\"" + sanitizedName +
        "\",\"applied\":" + String(ack.applied ? "true" : "false") + "}",
        true
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

    if (ack.phase == OTA_PHASE_BEGIN && ack.status == OTA_STATUS_BUSY) {
        otaSession.awaitingAck = false;
        otaSession.nextOffset = ack.bytesReceived;
        otaSession.bytesReceived = ack.bytesReceived;
        return;
    }

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
            {
                int idx = findNodeById(otaSession.nodeId);
                if (idx >= 0) {
                    if (nodes[idx].otaPrepActive) {
                        setNodeOtaPrep(static_cast<size_t>(idx), false, false);
                    } else {
                        nodes[idx].otaReady = false;
                    }
                }
            }
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
        case MSG_CONFIG_ACK:
            if (len == sizeof(ConfigAck)) handleConfigAck(mac, *reinterpret_cast<const ConfigAck*>(data));
            break;
        case MSG_RENAME_ACK:
            if (len == sizeof(RenameAck)) handleRenameAck(mac, *reinterpret_cast<const RenameAck*>(data));
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
        const uint32_t effectiveReportMs = effectiveReportIntervalMs(nodes[i]);
        const bool effectiveReady = effectiveOtaReady(nodes[i]);
        const bool effectiveSleep = effectiveSleepEnabled(nodes[i]);
        Serial.print("{\"node_id\":"); Serial.print(nodes[i].nodeId);
        Serial.print(",\"name\":\""); Serial.print(nodes[i].name);
        Serial.print("\",\"mac\":\""); Serial.print(macToString(nodes[i].mac));
        Serial.print("\",\"last_seen_ms\":"); Serial.print(nodes[i].lastSeenMs);
        Serial.print(",\"report_interval_ms\":"); Serial.print(effectiveReportMs);
        Serial.print(",\"fw_version\":\""); Serial.print(nodes[i].fwMajor); Serial.print("."); Serial.print(nodes[i].fwMinor); Serial.print("\"");
        Serial.print(",\"rssi_dbm\":"); Serial.print(nodes[i].lastRssiDbm);
        Serial.print(",\"signal_pct\":"); Serial.print(rssiToSignalPercent(nodes[i].lastRssiDbm));
        Serial.print(",\"temp_offset_c\":"); Serial.print(nodes[i].tempOffsetC, 2);
        Serial.print(",\"heater_enabled\":"); Serial.print(nodes[i].heaterEnabled ? "true" : "false");
        Serial.print(",\"ota_ready\":"); Serial.print(effectiveReady ? "true" : "false");
        Serial.print(",\"sleep_enabled\":"); Serial.print(effectiveSleep ? "true" : "false");
        Serial.print(",\"ota_prep_active\":"); Serial.print(nodes[i].otaPrepActive ? "true" : "false");
        Serial.print(",\"sample_rate_hz\":"); Serial.print(nodes[i].sampleRateHz);
        Serial.print(",\"next_report_delay_ms\":"); Serial.print(nodes[i].nextReportDelayMs);
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
        Serial.println("Commands: HELP, NODES, BIND, BIND OFF, SETINT <nodeId> <ms>, SETINT ALL <ms>, SETSAMPLE <nodeId> <hz>, SETSAMPLE ALL <hz>, SLEEP <nodeId> ON|OFF, SLEEP ALL ON|OFF, SETTOFF <nodeId> <tempOffsetC>, HEATER <nodeId> ON|OFF, RENAME <nodeId> <name>, STREAM OFF, STREAM ON, TIME STATUS, TIME SET <unixSeconds>, OTA PREP <nodeId> ON|OFF, OTA READY <nodeId> ON|OFF, OTA BEGIN <nodeId> <size> <crc32hex>, OTA CHUNK <offset> <hex>, OTA END, OTA STATUS, OTA ABORT");
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
        if (cmd.startsWith("SETINT ALL ")) {
            uint32_t ms = normalizeReportInterval(cmd.substring(11).toInt());
            uint32_t appliedCount = 0;
            uint32_t sleepDisabledCount = 0;
            for (size_t i = 0; i < MAX_NODES; ++i) {
                if (!nodes[i].used) continue;
                nodes[i].reportIntervalMs = ms;
                if (!sleepAllowedForReportInterval(ms) && nodes[i].sleepEnabled) {
                    nodes[i].sleepEnabled = false;
                    sleepDisabledCount++;
                }
                saveNode(i);
                appliedCount++;
            }
            rebuildReportSchedule();
            pushSchedulesToAllNodes();
            printJsonEvent(
                "{\"event\":\"setint_all\",\"report_interval_ms\":" + String(ms) +
                ",\"targets\":" + String(appliedCount) +
                ",\"sleep_disabled\":" + String(sleepDisabledCount) + "}",
                true
            );
        } else {
            int sp = cmd.indexOf(' ', 7);
            if (sp > 0) {
                uint32_t nodeId = cmd.substring(7, sp).toInt();
                uint32_t ms = normalizeReportInterval(cmd.substring(sp + 1).toInt());
                int idx = findNodeById(nodeId);
                if (idx >= 0) {
                    nodes[idx].reportIntervalMs = ms;
                    if (!sleepAllowedForReportInterval(ms)) {
                        nodes[idx].sleepEnabled = false;
                    }
                    saveNode(static_cast<size_t>(idx));
                    rebuildReportSchedule();
                    pushSchedulesToAllNodes();
                }
            }
        }
    } else if (cmd.startsWith("SETSAMPLE ")) {
        if (cmd.startsWith("SETSAMPLE ALL ")) {
            uint16_t hz = normalizeSampleRateHz(cmd.substring(14).toInt());
            uint32_t appliedCount = 0;
            for (size_t i = 0; i < MAX_NODES; ++i) {
                if (!nodes[i].used) continue;
                nodes[i].sampleRateHz = hz;
                saveNode(i);
                sendNodeConfig(i);
                appliedCount++;
            }
            printJsonEvent(
                "{\"event\":\"setsample_all\",\"sample_rate_hz\":" + String(hz) +
                ",\"targets\":" + String(appliedCount) + "}",
                true
            );
        } else {
            int sp = cmd.indexOf(' ', 10);
            if (sp > 0) {
                uint32_t nodeId = cmd.substring(10, sp).toInt();
                uint16_t hz = normalizeSampleRateHz(cmd.substring(sp + 1).toInt());
                int idx = findNodeById(nodeId);
                if (idx >= 0) {
                    nodes[idx].sampleRateHz = hz;
                    saveNode(static_cast<size_t>(idx));
                    sendNodeConfig(static_cast<size_t>(idx));
                }
            }
        }
    } else if (cmd.startsWith("SLEEP ")) {
        if (cmd.startsWith("SLEEP ALL ")) {
            String value = cmd.substring(10);
            value.trim();
            bool enabled = false;
            if (value.equalsIgnoreCase("ON")) {
                enabled = true;
            } else if (!value.equalsIgnoreCase("OFF")) {
                printJsonEvent("{\"event\":\"sleep_error\",\"reason\":\"invalid_value\"}", true);
                printPrompt();
                return;
            }
            if (enabled) {
                for (size_t i = 0; i < MAX_NODES; ++i) {
                    if (!nodes[i].used) continue;
                    if (!sleepAllowedForReportInterval(effectiveReportIntervalMs(nodes[i]))) {
                        printJsonEvent(
                            "{\"event\":\"sleep_error\",\"reason\":\"interval_too_short\",\"min_report_interval_ms\":" +
                            String(MIN_SLEEP_REPORT_INTERVAL_MS) + "}",
                            true
                        );
                        printPrompt();
                        return;
                    }
                }
            }
            uint32_t appliedCount = 0;
            for (size_t i = 0; i < MAX_NODES; ++i) {
                if (!nodes[i].used) continue;
                nodes[i].sleepEnabled = enabled;
                saveNode(i);
                sendNodeConfig(i);
                appliedCount++;
            }
            printJsonEvent(
                "{\"event\":\"sleep_all\",\"sleep_enabled\":" + String(enabled ? "true" : "false") +
                ",\"targets\":" + String(appliedCount) + "}",
                true
            );
        } else {
            int sp = cmd.indexOf(' ', 6);
            if (sp > 0) {
                uint32_t nodeId = cmd.substring(6, sp).toInt();
                String value = cmd.substring(sp + 1);
                value.trim();
                int idx = findNodeById(nodeId);
                if (idx >= 0) {
                    if (value.equalsIgnoreCase("ON")) {
                        if (!sleepAllowedForReportInterval(effectiveReportIntervalMs(nodes[idx]))) {
                            printJsonEvent(
                                "{\"event\":\"sleep_error\",\"reason\":\"interval_too_short\",\"node_id\":" +
                                String(nodeId) +
                                ",\"min_report_interval_ms\":" + String(MIN_SLEEP_REPORT_INTERVAL_MS) + "}",
                                true
                            );
                            printPrompt();
                            return;
                        }
                        nodes[idx].sleepEnabled = true;
                    } else if (value.equalsIgnoreCase("OFF")) {
                        nodes[idx].sleepEnabled = false;
                    } else {
                        printJsonEvent("{\"event\":\"sleep_error\",\"reason\":\"invalid_value\"}", true);
                        printPrompt();
                        return;
                    }
                    saveNode(static_cast<size_t>(idx));
                    sendNodeConfig(static_cast<size_t>(idx));
                    printJsonEvent(
                        "{\"event\":\"sleep_set\",\"node_id\":" + String(nodeId) +
                        ",\"sleep_enabled\":" + String(nodes[idx].sleepEnabled ? "true" : "false") + "}",
                        true
                    );
                }
            }
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
                sendNodeConfig(static_cast<size_t>(idx));
            }
        }
    } else if (cmd.startsWith("HEATER ")) {
        int sp = cmd.indexOf(' ', 7);
        if (sp > 0) {
            uint32_t nodeId = cmd.substring(7, sp).toInt();
            String value = cmd.substring(sp + 1);
            value.trim();
            int idx = findNodeById(nodeId);
            if (idx >= 0) {
                if (value.equalsIgnoreCase("ON")) {
                    nodes[idx].heaterEnabled = true;
                } else if (value.equalsIgnoreCase("OFF")) {
                    nodes[idx].heaterEnabled = false;
                } else {
                    printJsonEvent("{\"event\":\"heater_error\",\"reason\":\"invalid_value\"}", true);
                    printPrompt();
                    return;
                }
                saveNode(static_cast<size_t>(idx));
                sendNodeConfig(static_cast<size_t>(idx));
            }
        }
    } else if (cmd.startsWith("RENAME ")) {
        int sp = cmd.indexOf(' ', 7);
        if (sp > 0) {
            uint32_t nodeId = cmd.substring(7, sp).toInt();
            const String name = sanitizeNodeName(cmd.substring(sp + 1));
            int idx = findNodeById(nodeId);
            if (idx >= 0) {
                sendRename(nodeId, name);
                printJsonEvent(
                    "{\"event\":\"rename_sent\",\"node_id\":" + String(nodeId) +
                    ",\"name\":\"" + name + "\"}",
                    true
                );
            }
        }
    } else if (cmd.startsWith("OTA PREP ")) {
        int sp = cmd.indexOf(' ', 9);
        if (sp > 0) {
            uint32_t nodeId = cmd.substring(9, sp).toInt();
            String value = cmd.substring(sp + 1);
            value.trim();
            int idx = findNodeById(nodeId);
            if (idx >= 0) {
                if (value.equalsIgnoreCase("ON")) {
                    setNodeOtaPrep(static_cast<size_t>(idx), true);
                } else if (value.equalsIgnoreCase("OFF")) {
                    setNodeOtaPrep(static_cast<size_t>(idx), false);
                } else {
                    printJsonEvent("{\"event\":\"ota_error\",\"reason\":\"invalid_prep_value\"}", true);
                    printPrompt();
                    return;
                }
            }
        }
    } else if (cmd.startsWith("OTA READY ")) {
        int sp = cmd.indexOf(' ', 10);
        if (sp > 0) {
            uint32_t nodeId = cmd.substring(10, sp).toInt();
            String value = cmd.substring(sp + 1);
            value.trim();
            int idx = findNodeById(nodeId);
            if (idx >= 0) {
                if (value.equalsIgnoreCase("ON")) {
                    setNodeOtaReady(static_cast<size_t>(idx), true);
                } else if (value.equalsIgnoreCase("OFF")) {
                    setNodeOtaReady(static_cast<size_t>(idx), false);
                } else {
                    printJsonEvent("{\"event\":\"ota_error\",\"reason\":\"invalid_ready_value\"}", true);
                    printPrompt();
                    return;
                }
            }
        }
    } else if (cmd.equalsIgnoreCase("OTA STATUS")) {
        bool otaReady = false;
        bool otaPrepActive = false;
        if (otaSession.nodeId != 0) {
            int idx = findNodeById(otaSession.nodeId);
            if (idx >= 0) {
                otaReady = effectiveOtaReady(nodes[idx]);
                otaPrepActive = nodes[idx].otaPrepActive;
            }
        }
        printJsonEvent(
            "{\"event\":\"ota_status\",\"active\":" + String(otaSession.active ? "true" : "false") +
            ",\"awaiting_ack\":" + String(otaSession.awaitingAck ? "true" : "false") +
            ",\"node_id\":" + String(otaSession.nodeId) +
            ",\"next_offset\":" + String(otaSession.nextOffset) +
            ",\"bytes_received\":" + String(otaSession.bytesReceived) +
            ",\"ota_ready\":" + String(otaReady ? "true" : "false") +
            ",\"ota_prep_active\":" + String(otaPrepActive ? "true" : "false") + "}"
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
                int idx = findNodeById(nodeId);
                const char* reason = (idx >= 0 && !effectiveOtaReady(nodes[idx])) ? "target_not_ready" : "begin_rejected";
                printJsonEvent(
                    "{\"event\":\"ota_error\",\"reason\":\"" + String(reason) + "\",\"node_id\":" + String(nodeId) + "}",
                    true
                );
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
    if (!cmd.startsWith("OTA CHUNK ")) {
        printPrompt();
    }
}

void setup() {
    Serial.begin(SERIAL_BAUD);
    delay(500);
    initStatusLed();

    loadNodes();
    rebuildReportSchedule();

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
                processSerialCommand(line);
            } else if (!promptShown) {
                printPrompt();
            }
            line = "";
        } else if (c == '\b' || c == 127) {
            if (!line.isEmpty()) {
                line.remove(line.length() - 1);
            }
        } else {
            line += c;
        }
    }

    if (bindWindowOpen && millis() > bindWindowEndsAt) {
        bindWindowOpen = false;
        Serial.println("{\"event\":\"bind_window\",\"open\":false}");
    }

    if (otaSession.active && otaSession.awaitingAck && timeReached(millis(), otaSession.ackDeadlineMs)) {
        if (otaSession.retryCount >= OTA_MAX_RETRIES) {
            abortOta("ack_timeout");
        } else {
            retryPendingOtaFrame();
        }
    }
}
