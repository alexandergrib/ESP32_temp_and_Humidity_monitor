#pragma once

#include <Arduino.h>

namespace proto {

static constexpr uint16_t PROTOCOL_VERSION = 8;
static constexpr uint8_t  RADIO_CHANNEL     = 6;   // must match on all nodes
static constexpr uint32_t DEFAULT_REPORT_MS = 1000;
static constexpr uint32_t MIN_REPORT_MS     = 500;
static constexpr uint16_t DEFAULT_SAMPLE_RATE_HZ = 100;
static constexpr uint16_t MIN_SAMPLE_RATE_HZ     = 1;
static constexpr uint16_t MAX_SAMPLE_RATE_HZ     = 200;
static constexpr uint32_t BIND_WINDOW_MS    = 120000;
static constexpr char MAGIC[4]              = {'T', 'M', 'O', 'N'};

enum MessageType : uint8_t {
    MSG_BIND_REQUEST = 1,
    MSG_BIND_ACK     = 2,
    MSG_READING      = 3,
    MSG_READING_ACK  = 4,
    MSG_CONFIG_SET   = 5,
    MSG_CONFIG_ACK   = 6,
    MSG_SAMPLE_REQ   = 7,
    MSG_PING         = 8,
    MSG_PONG         = 9,
    MSG_OTA_BEGIN    = 10,
    MSG_OTA_CHUNK    = 11,
    MSG_OTA_END      = 12,
    MSG_OTA_ACK      = 13,
    MSG_RENAME_SET   = 14,
    MSG_RENAME_ACK   = 15,
};

enum OtaPhase : uint8_t {
    OTA_PHASE_BEGIN = 1,
    OTA_PHASE_CHUNK = 2,
    OTA_PHASE_END   = 3,
};

enum OtaStatus : uint8_t {
    OTA_STATUS_OK              = 0,
    OTA_STATUS_REJECTED        = 1,
    OTA_STATUS_NOT_ACTIVE      = 2,
    OTA_STATUS_OFFSET_MISMATCH = 3,
    OTA_STATUS_WRITE_FAILED    = 4,
    OTA_STATUS_END_FAILED      = 5,
    OTA_STATUS_CRC_MISMATCH    = 6,
    OTA_STATUS_BUSY            = 7,
    OTA_STATUS_NOT_READY       = 8,
};

static constexpr size_t OTA_CHUNK_BYTES = 180;

struct __attribute__((packed)) Header {
    char magic[4];
    uint16_t version;
    uint8_t type;
    uint8_t reserved;
    uint32_t sequence;
    uint32_t nodeId;
    uint32_t uptimeMs;
};

struct __attribute__((packed)) BindRequest {
    Header header;
    char nodeName[16];
    uint8_t fwMajor;
    uint8_t fwMinor;
    uint16_t capabilities; // bit0=SHT85 present
};

struct __attribute__((packed)) BindAck {
    Header header;
    uint32_t assignedNodeId;
    uint32_t reportIntervalMs;
    uint32_t nextReportDelayMs;
    float tempOffsetC;
    uint8_t controllerMac[6];
    uint8_t accepted;
    uint8_t heaterEnabled;
    uint8_t otaReady;
    uint8_t sleepEnabled;
    uint16_t sampleRateHz;
};

struct __attribute__((packed)) Reading {
    Header header;
    float temperatureC;
    float humidityPct;
    float vbat;
    uint8_t sensorOk;
    uint8_t rssiHint;
    uint8_t reserved[2];   // reserved[0]=fwMajor, reserved[1]=fwMinor
};

struct __attribute__((packed)) ReadingAck {
    Header header;
    uint32_t readingSequence;
    uint8_t accepted;
    uint8_t heaterEnabled;
    uint8_t otaReady;
    uint8_t sleepEnabled;
    uint32_t reportIntervalMs;
    uint32_t nextReportDelayMs;
    float tempOffsetC;
    uint16_t sampleRateHz;
};

struct __attribute__((packed)) ConfigSet {
    Header header;
    uint32_t reportIntervalMs;
    uint32_t nextReportDelayMs;
    float tempOffsetC;
    uint8_t heaterEnabled;
    uint8_t otaReady;
    uint8_t sleepEnabled;
    uint16_t sampleRateHz;
};

struct __attribute__((packed)) ConfigAck {
    Header header;
    uint32_t reportIntervalMs;
    uint32_t nextReportDelayMs;
    float tempOffsetC;
    uint8_t applied;
    uint8_t heaterEnabled;
    uint8_t otaReady;
    uint8_t sleepEnabled;
    uint16_t sampleRateHz;
};

struct __attribute__((packed)) SampleRequest {
    Header header;
};

struct __attribute__((packed)) OtaBegin {
    Header header;
    uint32_t totalSize;
    uint32_t expectedCrc32;
};

struct __attribute__((packed)) OtaChunk {
    Header header;
    uint32_t offset;
    uint16_t dataLen;
    uint16_t reserved;
    uint8_t data[OTA_CHUNK_BYTES];
};

struct __attribute__((packed)) OtaEnd {
    Header header;
    uint32_t totalSize;
    uint32_t expectedCrc32;
};

struct __attribute__((packed)) OtaAck {
    Header header;
    uint8_t phase;
    uint8_t status;
    uint16_t detail;
    uint32_t bytesReceived;
};

struct __attribute__((packed)) RenameSet {
    Header header;
    char nodeName[16];
};

struct __attribute__((packed)) RenameAck {
    Header header;
    uint8_t applied;
    uint8_t reserved[3];
    char nodeName[16];
};

inline void fillHeader(Header& h, MessageType type, uint32_t sequence, uint32_t nodeId, uint32_t uptimeMs) {
    memcpy(h.magic, MAGIC, sizeof(MAGIC));
    h.version = PROTOCOL_VERSION;
    h.type = static_cast<uint8_t>(type);
    h.reserved = 0;
    h.sequence = sequence;
    h.nodeId = nodeId;
    h.uptimeMs = uptimeMs;
}

inline bool validHeader(const Header& h) {
    return memcmp(h.magic, MAGIC, sizeof(MAGIC)) == 0 && h.version == PROTOCOL_VERSION;
}

inline String macToString(const uint8_t* mac) {
    char buf[18];
    snprintf(buf, sizeof(buf), "%02X:%02X:%02X:%02X:%02X:%02X",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    return String(buf);
}

} // namespace proto
