#pragma once

#include <Arduino.h>

namespace proto {

static constexpr uint16_t PROTOCOL_VERSION = 1;
static constexpr uint8_t  RADIO_CHANNEL     = 6;   // must match on all nodes
static constexpr uint32_t DEFAULT_REPORT_MS = 5000;
static constexpr uint32_t HEARTBEAT_MS      = 30000;
static constexpr uint32_t BIND_WINDOW_MS    = 120000;
static constexpr char MAGIC[4]              = {'T', 'M', 'O', 'N'};

enum MessageType : uint8_t {
    MSG_BIND_REQUEST = 1,
    MSG_BIND_ACK     = 2,
    MSG_READING      = 3,
    MSG_HEARTBEAT    = 4,
    MSG_CONFIG_SET   = 5,
    MSG_CONFIG_ACK   = 6,
    MSG_PING         = 7,
    MSG_PONG         = 8,
};

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
    uint8_t controllerMac[6];
    uint8_t accepted;
    uint8_t reserved[3];
};

struct __attribute__((packed)) Reading {
    Header header;
    float temperatureC;
    float humidityPct;
    float vbat;
    uint8_t sensorOk;
    uint8_t rssiHint;
    uint8_t reserved[2];
};

struct __attribute__((packed)) Heartbeat {
    Header header;
    uint8_t sensorOk;
    uint8_t wifiChannel;
    uint16_t reserved;
};

struct __attribute__((packed)) ConfigSet {
    Header header;
    uint32_t reportIntervalMs;
};

struct __attribute__((packed)) ConfigAck {
    Header header;
    uint32_t reportIntervalMs;
    uint8_t applied;
    uint8_t reserved[3];
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
