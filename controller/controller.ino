#include <Arduino.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <Preferences.h>
#include "../shared/protocol.h"

using namespace proto;

Preferences prefs;

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
};

static constexpr size_t MAX_NODES = 32;
NodeRecord nodes[MAX_NODES];
uint32_t nextNodeId = 1;
bool bindWindowOpen = true;
uint32_t bindWindowEndsAt = 0;
uint32_t txSeq = 1;

bool macEquals(const uint8_t* a, const uint8_t* b) {
    return memcmp(a, b, 6) == 0;
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
    prefs.putUInt((base + "rpt").c_str(), nodes[i].reportIntervalMs);
}

void loadNodes() {
    prefs.begin("tmon-ctrl", false);
    nextNodeId = prefs.getUInt("nextNodeId", 1);
    for (size_t i = 0; i < MAX_NODES; ++i) {
        String base = "node" + String(i) + "_";
        nodes[i].used = prefs.getBool((base + "used").c_str(), false);
        if (!nodes[i].used) continue;
        nodes[i].nodeId = prefs.getUInt((base + "id").c_str(), 0);
        prefs.getBytes((base + "mac").c_str(), nodes[i].mac, 6);
        String name = prefs.getString((base + "name").c_str(), "node");
        name.toCharArray(nodes[i].name, sizeof(nodes[i].name));
        nodes[i].reportIntervalMs = prefs.getUInt((base + "rpt").c_str(), DEFAULT_REPORT_MS);
    }
}

void printJsonEvent(const String& json) {
    Serial.println(json);
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
    ack.reportIntervalMs = node.reportIntervalMs;
    WiFi.macAddress(ack.controllerMac);
    ack.accepted = 1;
    ensurePeer(mac);
    esp_now_send(mac, reinterpret_cast<const uint8_t*>(&ack), sizeof(ack));
}

void sendConfig(uint32_t nodeId, uint32_t reportIntervalMs) {
    int idx = findNodeById(nodeId);
    if (idx < 0) return;
    ConfigSet cfg{};
    fillHeader(cfg.header, MSG_CONFIG_SET, txSeq++, nodeId, millis());
    cfg.reportIntervalMs = reportIntervalMs;
    ensurePeer(nodes[idx].mac);
    esp_now_send(nodes[idx].mac, reinterpret_cast<const uint8_t*>(&cfg), sizeof(cfg));
}

void handleBindRequest(const uint8_t* mac, const BindRequest& req) {
    if (!bindWindowOpen || millis() > bindWindowEndsAt) return;

    int idx = findNodeByMac(mac);
    if (idx < 0) {
        idx = allocateNodeSlot();
        if (idx < 0) {
            printJsonEvent("{\"event\":\"bind_rejected\",\"reason\":\"node_table_full\"}");
            return;
        }
        nodes[idx].used = true;
        nodes[idx].nodeId = nextNodeId++;
        memcpy(nodes[idx].mac, mac, 6);
        prefs.putUInt("nextNodeId", nextNodeId);
    }

    strncpy(nodes[idx].name, req.nodeName, sizeof(nodes[idx].name) - 1);
    nodes[idx].name[sizeof(nodes[idx].name) - 1] = '\0';
    nodes[idx].reportIntervalMs = DEFAULT_REPORT_MS;
    nodes[idx].lastSeenMs = millis();
    saveNode(idx);
    sendBindAck(mac, nodes[idx]);

    printJsonEvent(
        "{\"event\":\"node_bound\",\"node_id\":" + String(nodes[idx].nodeId) +
        ",\"name\":\"" + String(nodes[idx].name) +
        "\",\"mac\":\"" + macToString(mac) + "\"}"
    );
}

void handleReading(const uint8_t* mac, const Reading& msg) {
    int idx = findNodeByMac(mac);
    if (idx < 0) return;
    nodes[idx].lastSeenMs = millis();
    nodes[idx].lastTempC = msg.temperatureC;
    nodes[idx].lastHumidity = msg.humidityPct;
    nodes[idx].sensorOk = msg.sensorOk;

    printJsonEvent(
        "{\"event\":\"reading\",\"node_id\":" + String(nodes[idx].nodeId) +
        ",\"name\":\"" + String(nodes[idx].name) +
        "\",\"temperature_c\":" + String(msg.temperatureC, 2) +
        ",\"humidity_pct\":" + String(msg.humidityPct, 2) +
        ",\"sensor_ok\":" + String(msg.sensorOk ? "true" : "false") +
        ",\"mac\":\"" + macToString(mac) + "\"}"
    );
}

void handleHeartbeat(const uint8_t* mac, const Heartbeat& msg) {
    int idx = findNodeByMac(mac);
    if (idx < 0) return;
    nodes[idx].lastSeenMs = millis();
    nodes[idx].sensorOk = msg.sensorOk;

    printJsonEvent(
        "{\"event\":\"heartbeat\",\"node_id\":" + String(nodes[idx].nodeId) +
        ",\"name\":\"" + String(nodes[idx].name) +
        "\",\"channel\":" + String(msg.wifiChannel) + "}"
    );
}

void handleConfigAck(const uint8_t* mac, const ConfigAck& msg) {
    int idx = findNodeByMac(mac);
    if (idx < 0) return;
    nodes[idx].reportIntervalMs = msg.reportIntervalMs;
    saveNode(idx);
    printJsonEvent(
        "{\"event\":\"config_ack\",\"node_id\":" + String(nodes[idx].nodeId) +
        ",\"report_interval_ms\":" + String(msg.reportIntervalMs) +
        ",\"applied\":" + String(msg.applied ? "true" : "false") + "}"
    );
}

void onDataRecv(const esp_now_recv_info_t* recvInfo, const uint8_t* data, int len) {
    if (len < static_cast<int>(sizeof(Header))) return;
    const Header* h = reinterpret_cast<const Header*>(data);
    if (!validHeader(*h)) return;

    switch (h->type) {
        case MSG_BIND_REQUEST:
            if (len == sizeof(BindRequest)) handleBindRequest(recvInfo->src_addr, *reinterpret_cast<const BindRequest*>(data));
            break;
        case MSG_READING:
            if (len == sizeof(Reading)) handleReading(recvInfo->src_addr, *reinterpret_cast<const Reading*>(data));
            break;
        case MSG_HEARTBEAT:
            if (len == sizeof(Heartbeat)) handleHeartbeat(recvInfo->src_addr, *reinterpret_cast<const Heartbeat*>(data));
            break;
        case MSG_CONFIG_ACK:
            if (len == sizeof(ConfigAck)) handleConfigAck(recvInfo->src_addr, *reinterpret_cast<const ConfigAck*>(data));
            break;
        default:
            break;
    }
}

void onDataSent(const wifi_tx_info_t*, esp_now_send_status_t status) {
    printJsonEvent(String("{\"event\":\"tx_status\",\"ok\":") + (status == ESP_NOW_SEND_SUCCESS ? "true}" : "false}"));
}

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
        Serial.print("}");
    }
    Serial.println("]}");
}

void processSerialCommand(const String& raw) {
    String cmd = raw;
    cmd.trim();
    if (cmd.equalsIgnoreCase("HELP")) {
        Serial.println("Commands: HELP, NODES, BIND, BIND OFF, SETINT <nodeId> <ms>");
    } else if (cmd.equalsIgnoreCase("NODES")) {
        listNodes();
    } else if (cmd.equalsIgnoreCase("BIND")) {
        openBindWindow();
    } else if (cmd.equalsIgnoreCase("BIND OFF")) {
        bindWindowOpen = false;
        Serial.println("{\"event\":\"bind_window\",\"open\":false}");
    } else if (cmd.startsWith("SETINT ")) {
        int sp = cmd.indexOf(' ', 7);
        if (sp > 0) {
            uint32_t nodeId = cmd.substring(7, sp).toInt();
            uint32_t ms = cmd.substring(sp + 1).toInt();
            sendConfig(nodeId, ms);
        }
    }
}

void setup() {
    Serial.begin(115200);
    delay(500);

    loadNodes();

    WiFi.mode(WIFI_STA);
    WiFi.disconnect();
    esp_wifi_set_promiscuous(true);
    esp_wifi_set_channel(RADIO_CHANNEL, WIFI_SECOND_CHAN_NONE);
    esp_wifi_set_promiscuous(false);

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
}

void loop() {
    static String line;
    while (Serial.available()) {
        char c = static_cast<char>(Serial.read());
        if (c == '\n' || c == '\r') {
            if (!line.isEmpty()) processSerialCommand(line);
            line = "";
        } else {
            line += c;
        }
    }

    if (bindWindowOpen && millis() > bindWindowEndsAt) {
        bindWindowOpen = false;
        Serial.println("{\"event\":\"bind_window\",\"open\":false}");
    }
}
