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

static constexpr uint32_t SERIAL_BAUD = 460800;

// Adjust for your ESP32 board if needed.
static constexpr uint8_t PIN_I2C_SDA = 21;
static constexpr uint8_t PIN_I2C_SCL = 22;
static constexpr char DEFAULT_NODE_NAME[] = "satellite";
static constexpr uint32_t SATELLITE_CPU_FREQ_MHZ = 80;
static constexpr bool DEBUG_FORCE_NO_SLEEP = false;

#ifndef LED_BUILTIN
static constexpr uint8_t STATUS_LED_PIN = 2;
#else
static constexpr uint8_t STATUS_LED_PIN = LED_BUILTIN;
#endif

static constexpr uint32_t STATUS_LED_ON_MS = 80;
static constexpr uint32_t STATUS_LED_UNBOUND_PERIOD_MS = 1800;
static constexpr uint32_t STATUS_LED_OTA_PERIOD_MS = 160;
static constexpr uint32_t STATUS_LED_ACTIVITY_MS = 140;
static constexpr uint32_t SENSOR_CHUNK_US = 10000;
static constexpr uint32_t SENSOR_MIN_SAMPLE_PERIOD_US = 6000;
static constexpr uint32_t SENSOR_MAX_SAMPLE_PERIOD_US = 12000;
static constexpr uint32_t SENSOR_MIN_SETTLE_US = 5500;
static constexpr uint32_t SENSOR_RETRY_INTERVAL_US = 250000;
static constexpr uint32_t SENSOR_REQUEST_TIMEOUT_US = 50000;
static constexpr uint32_t SENSOR_I2C_CLOCK_HZ = 400000;
static constexpr uint32_t SENSOR_LAST_GOOD_MAX_AGE_US = 1000000;
static constexpr uint32_t SENSOR_EMPTY_CHUNK_FILL_MAX_AGE_US = 25000;
static constexpr uint32_t SENSOR_ADAPTIVE_BACKOFF_STEP_US = 1000;
static constexpr uint32_t SENSOR_ADAPTIVE_RECOVERY_STEP_US = 500;
static constexpr uint8_t SENSOR_ADAPTIVE_RECOVERY_GOOD_SAMPLES = 8;
static constexpr float SENSOR_MIN_VALID_TEMP_C = -40.0f;
static constexpr float SENSOR_MAX_VALID_TEMP_C = 125.0f;
static constexpr float SENSOR_MIN_VALID_HUMIDITY_PCT = 0.0f;
static constexpr float SENSOR_MAX_VALID_HUMIDITY_PCT = 100.0f;
static constexpr uint8_t SENSOR_AVERAGING_WINDOW_PERCENT = 20;
static constexpr uint32_t SENSOR_AVERAGING_WINDOW_MIN_MS = 1000;
static constexpr uint32_t SENSOR_WAKE_MARGIN_MS = 500;
static constexpr uint32_t READING_ACK_TIMEOUT_MS = 250;
static constexpr uint32_t READING_RETRY_BACKOFF_MS = 150;
static constexpr uint8_t READING_MAX_RETRIES = 3;
static constexpr uint32_t CONTROLLER_AWAKE_WINDOW_MS = 5000;
static constexpr uint32_t SETTINGS_SYNC_WAKE_THRESHOLD_MS = 2000;
static constexpr uint32_t SETTINGS_SYNC_RETRY_MS = 1500;
static constexpr uint32_t SETTINGS_SYNC_AWAKE_WINDOW_MS = 1500;
static constexpr uint32_t UNBOUND_SLEEP_MS = 250;
static constexpr size_t READING_BUFFER_CAPACITY = 8;
static constexpr size_t SENSOR_ROLLING_CHUNK_CAPACITY = 1024;

Preferences prefs;
SHT85 sht(SHT_DEFAULT_ADDRESS);

uint8_t controllerMac[6] = {0};
bool isBound = false;
uint32_t nodeId = 0;
uint32_t reportIntervalMs = DEFAULT_REPORT_MS;
uint16_t sampleRateHz = DEFAULT_SAMPLE_RATE_HZ;
uint32_t txSeq = 1;
char nodeName[16] = "satellite";
bool statusLedOn = false;
float tempOffsetC = 0.0f;
bool heaterEnabled = false;
bool otaReady = false;
bool sleepEnabled = false;
uint32_t activityLedUntilMs = 0;

bool sensorPresent = false;
float lastTempC = NAN;
float lastHumidity = NAN;
uint32_t lastGoodSampleAtUs = 0;
bool otaRebootPending = false;
uint32_t otaRebootAtMs = 0;
uint32_t nextReportDueAtMs = 0;
bool captureWindowActive = false;
uint32_t captureWindowEndsAtMs = 0;
uint32_t controllerAwakeUntilMs = 0;
uint32_t lastControllerContactAtMs = 0;
uint32_t lastRecoveryBindAttemptAtMs = 0;
bool pendingSettingsSync = false;
uint32_t lastSettingsSyncRequestAtMs = 0;

struct SatelliteOtaState {
    bool active = false;
    uint32_t totalSize = 0;
    uint32_t expectedCrc32 = 0;
    uint32_t bytesWritten = 0;
    uint32_t runningCrc32 = 0xFFFFFFFF;
    uint32_t lastChunkOffset = 0;
    uint16_t lastChunkLen = 0;
} otaState;

struct SensorSamplerState {
    bool requestInFlight = false;
    uint32_t requestIssuedAtUs = 0;
    uint32_t nextRequestAtUs = 0;
    uint32_t chunkStartedAtUs = 0;
    uint32_t effectiveSamplePeriodUs = SENSOR_MIN_SAMPLE_PERIOD_US;
    uint8_t consecutiveGoodSamples = 0;
    uint8_t consecutiveBadSamples = 0;
    float chunkTempSum = 0.0f;
    float chunkHumiditySum = 0.0f;
    uint16_t chunkSampleCount = 0;
} sensorSampler;

struct RollingChunk {
    uint32_t endedAtUs = 0;
    float temperatureC = NAN;
    float humidityPct = NAN;
};

struct BufferedReading {
    bool used = false;
    uint32_t sequence = 0;
    uint32_t sampledAtMs = 0;
    uint32_t lastSendAtMs = 0;
    uint8_t retryCount = 0;
    float temperatureC = NAN;
    float humidityPct = NAN;
    uint8_t sensorOk = 0;
};

BufferedReading readingBuffer[READING_BUFFER_CAPACITY];
size_t readingBufferHead = 0;
size_t readingBufferCount = 0;
RollingChunk rollingChunks[SENSOR_ROLLING_CHUNK_CAPACITY];
size_t rollingChunkHead = 0;
size_t rollingChunkCount = 0;

void handleIncomingPacket(const uint8_t* data, int len);
void logSendStatus(esp_now_send_status_t status);
void pumpSensorSampling();
void maintainControllerLink();
void maybeRequestSettingsSync();
bool initEspNowRadio();
bool resetEspNowRadioAfterWake();
void applyControllerState(
    uint32_t reportMs,
    uint32_t nextDelayMs,
    float tempOffset,
    bool heaterOn,
    bool otaReadyEnabled,
    bool sleepEnabledState,
    uint16_t sampleRate,
    bool persistConfig
);
void beginCaptureWindow();
void completeCaptureWindow();
bool captureScheduledReading();
void saveConfig();
#if ESP_ARDUINO_VERSION >= ESP_ARDUINO_VERSION_VAL(3, 0, 0)
void onDataRecv(const esp_now_recv_info_t*, const uint8_t* data, int len);
void onDataSent(const wifi_tx_info_t*, esp_now_send_status_t status);
#else
void onDataRecv(const uint8_t*, const uint8_t* data, int len);
void onDataSent(const uint8_t*, esp_now_send_status_t status);
#endif

void noteActivity() {
    activityLedUntilMs = millis() + STATUS_LED_ACTIVITY_MS;
}

void stayAwakeForController(uint32_t durationMs = CONTROLLER_AWAKE_WINDOW_MS) {
    const uint32_t deadlineMs = millis() + durationMs;
    controllerAwakeUntilMs = controllerAwakeUntilMs > deadlineMs ? controllerAwakeUntilMs : deadlineMs;
}

bool timeReachedUs(uint32_t now, uint32_t target) {
    return static_cast<int32_t>(now - target) >= 0;
}

bool timeReachedMs(uint32_t now, uint32_t target) {
    return static_cast<int32_t>(now - target) >= 0;
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
    if (sanitized.length() == 0) sanitized = DEFAULT_NODE_NAME;
    return sanitized;
}

void applySensorConfig() {
    sht.setTemperatureOffset(tempOffsetC);
    if (heaterEnabled) {
        sht.heatOn();
    } else {
        sht.heatOff();
    }
}

void initStatusLed() {
    pinMode(STATUS_LED_PIN, OUTPUT);
    digitalWrite(STATUS_LED_PIN, LOW);
}

void updateStatusLed() {
    const uint32_t now = millis();
    bool ledHigh = false;

    if (otaRebootPending) {
        ledHigh = true;
    } else if (otaState.active) {
        ledHigh = ((now / STATUS_LED_OTA_PERIOD_MS) % 2U) == 0U;
    } else if (static_cast<int32_t>(activityLedUntilMs - now) > 0) {
        ledHigh = true;
    } else if (!isBound) {
        const uint32_t phase = now % STATUS_LED_UNBOUND_PERIOD_MS;
        ledHigh = phase < STATUS_LED_ON_MS || (phase >= 220 && phase < 220 + STATUS_LED_ON_MS);
    } else {
        ledHigh = false;
    }

    if (ledHigh != statusLedOn) {
        statusLedOn = ledHigh;
        digitalWrite(STATUS_LED_PIN, ledHigh ? HIGH : LOW);
    }
}

bool macIsZero(const uint8_t* mac) {
    for (int i = 0; i < 6; ++i) if (mac[i] != 0) return false;
    return true;
}

uint32_t normalizeReportInterval(uint32_t ms) {
    return max(ms, MIN_REPORT_MS);
}

bool sleepAllowedForReportInterval(uint32_t reportMs) {
    return normalizeReportInterval(reportMs) >= MIN_SLEEP_REPORT_INTERVAL_MS;
}

bool effectiveSleepEnabled() {
    return sleepEnabled && sleepAllowedForReportInterval(reportIntervalMs);
}

uint16_t normalizeSampleRateHz(uint32_t hz) {
    return static_cast<uint16_t>(constrain(hz, MIN_SAMPLE_RATE_HZ, MAX_SAMPLE_RATE_HZ));
}

uint32_t requestedSamplePeriodUs() {
    return max<uint32_t>(1000000UL / normalizeSampleRateHz(sampleRateHz), SENSOR_MIN_SAMPLE_PERIOD_US);
}

uint32_t samplePeriodUs() {
    return constrain(
        max(sensorSampler.effectiveSamplePeriodUs, requestedSamplePeriodUs()),
        requestedSamplePeriodUs(),
        SENSOR_MAX_SAMPLE_PERIOD_US
    );
}

bool isValidSensorSample(float temperatureC, float humidityPct) {
    return isfinite(temperatureC) &&
           isfinite(humidityPct) &&
           temperatureC >= SENSOR_MIN_VALID_TEMP_C &&
           temperatureC <= SENSOR_MAX_VALID_TEMP_C &&
           humidityPct >= SENSOR_MIN_VALID_HUMIDITY_PCT &&
           humidityPct <= SENSOR_MAX_VALID_HUMIDITY_PCT;
}

bool hasRecentGoodSample(uint32_t nowUs) {
    return lastGoodSampleAtUs != 0 &&
           timeReachedUs(nowUs, lastGoodSampleAtUs) &&
           (nowUs - lastGoodSampleAtUs) <= SENSOR_LAST_GOOD_MAX_AGE_US &&
           isValidSensorSample(lastTempC, lastHumidity);
}

bool canReuseLastSampleForChunk(uint32_t nowUs) {
    return hasRecentGoodSample(nowUs) &&
           (nowUs - lastGoodSampleAtUs) <= max(SENSOR_EMPTY_CHUNK_FILL_MAX_AGE_US, samplePeriodUs() * 2U);
}

uint32_t sensorAveragingWindowMs() {
    const uint32_t normalizedReportMs = normalizeReportInterval(reportIntervalMs);
    const uint32_t scaledWindowMs = (normalizedReportMs * SENSOR_AVERAGING_WINDOW_PERCENT) / 100U;
    return max<uint32_t>(SENSOR_AVERAGING_WINDOW_MIN_MS, scaledWindowMs);
}

uint32_t captureWindowMs() {
    return SENSOR_WAKE_MARGIN_MS + sensorAveragingWindowMs();
}

uint32_t averagingWindowMs() {
    return sensorAveragingWindowMs();
}

uint32_t averagingWindowUs() {
    return averagingWindowMs() * 1000UL;
}

void clearBufferedChunks() {
    rollingChunkHead = 0;
    rollingChunkCount = 0;
}

void clearBufferedReadings() {
    for (BufferedReading& item : readingBuffer) {
        item = BufferedReading{};
    }
    readingBufferHead = 0;
    readingBufferCount = 0;
}

void trimRollingChunks(uint32_t nowUs) {
    const uint32_t windowUs = averagingWindowUs();
    while (rollingChunkCount > 0) {
        const RollingChunk& chunk = rollingChunks[rollingChunkHead];
        if (!timeReachedUs(nowUs, chunk.endedAtUs)) {
            break;
        }
        if ((nowUs - chunk.endedAtUs) <= windowUs) {
            break;
        }
        rollingChunks[rollingChunkHead] = RollingChunk{};
        rollingChunkHead = (rollingChunkHead + 1U) % SENSOR_ROLLING_CHUNK_CAPACITY;
        rollingChunkCount--;
    }
}

void pushRollingChunk(float temperatureC, float humidityPct, uint32_t endedAtUs) {
    trimRollingChunks(endedAtUs);
    if (rollingChunkCount >= SENSOR_ROLLING_CHUNK_CAPACITY) {
        rollingChunks[rollingChunkHead] = RollingChunk{};
        rollingChunkHead = (rollingChunkHead + 1U) % SENSOR_ROLLING_CHUNK_CAPACITY;
        rollingChunkCount--;
    }
    const size_t insertIndex = (rollingChunkHead + rollingChunkCount) % SENSOR_ROLLING_CHUNK_CAPACITY;
    rollingChunks[insertIndex].endedAtUs = endedAtUs;
    rollingChunks[insertIndex].temperatureC = temperatureC;
    rollingChunks[insertIndex].humidityPct = humidityPct;
    rollingChunkCount++;
}

void resetCurrentChunk() {
    sensorSampler.chunkTempSum = 0.0f;
    sensorSampler.chunkHumiditySum = 0.0f;
    sensorSampler.chunkSampleCount = 0;
}

void restartSensorSampler(bool clearHistory = false) {
    const uint32_t nowUs = micros();
    sensorSampler.requestInFlight = false;
    sensorSampler.requestIssuedAtUs = 0;
    sensorSampler.nextRequestAtUs = nowUs;
    sensorSampler.chunkStartedAtUs = nowUs;
    sensorSampler.effectiveSamplePeriodUs = requestedSamplePeriodUs();
    sensorSampler.consecutiveGoodSamples = 0;
    sensorSampler.consecutiveBadSamples = 0;
    resetCurrentChunk();
    if (clearHistory) {
        clearBufferedChunks();
        lastTempC = NAN;
        lastHumidity = NAN;
        lastGoodSampleAtUs = 0;
    }
}

void noteSamplingFailure() {
    sensorSampler.consecutiveBadSamples = min<uint8_t>(sensorSampler.consecutiveBadSamples + 1, 32);
    sensorSampler.consecutiveGoodSamples = 0;
    const uint32_t backoffStepUs = SENSOR_ADAPTIVE_BACKOFF_STEP_US *
                                   min<uint8_t>(sensorSampler.consecutiveBadSamples, 4);
    sensorSampler.effectiveSamplePeriodUs = min(
        SENSOR_MAX_SAMPLE_PERIOD_US,
        max(sensorSampler.effectiveSamplePeriodUs, requestedSamplePeriodUs()) + backoffStepUs
    );
}

void noteSamplingSuccess() {
    sensorSampler.consecutiveBadSamples = 0;
    sensorSampler.consecutiveGoodSamples = min<uint8_t>(sensorSampler.consecutiveGoodSamples + 1, 32);
    const uint32_t targetPeriodUs = requestedSamplePeriodUs();
    if (sensorSampler.effectiveSamplePeriodUs > targetPeriodUs &&
        sensorSampler.consecutiveGoodSamples >= SENSOR_ADAPTIVE_RECOVERY_GOOD_SAMPLES) {
        sensorSampler.effectiveSamplePeriodUs = max(
            targetPeriodUs,
            sensorSampler.effectiveSamplePeriodUs - SENSOR_ADAPTIVE_RECOVERY_STEP_US
        );
        sensorSampler.consecutiveGoodSamples = 0;
    }
}

void finalizeCurrentChunk() {
    const uint32_t chunkEndedAtUs = sensorSampler.chunkStartedAtUs + SENSOR_CHUNK_US;
    if (sensorSampler.chunkSampleCount > 0) {
        pushRollingChunk(
            sensorSampler.chunkTempSum / sensorSampler.chunkSampleCount,
            sensorSampler.chunkHumiditySum / sensorSampler.chunkSampleCount,
            chunkEndedAtUs
        );
    } else if (canReuseLastSampleForChunk(chunkEndedAtUs)) {
        pushRollingChunk(lastTempC, lastHumidity, chunkEndedAtUs);
    }
    resetCurrentChunk();
}

void rollChunkWindows(uint32_t nowUs) {
    if (sensorSampler.chunkStartedAtUs == 0) {
        sensorSampler.chunkStartedAtUs = nowUs;
    }

    while (timeReachedUs(nowUs, sensorSampler.chunkStartedAtUs + SENSOR_CHUNK_US)) {
        finalizeCurrentChunk();
        sensorSampler.chunkStartedAtUs += SENSOR_CHUNK_US;
    }
}

void recordSensorSample(float temperatureC, float humidityPct) {
    if (!isValidSensorSample(temperatureC, humidityPct)) {
        return;
    }
    lastTempC = temperatureC;
    lastHumidity = humidityPct;
    lastGoodSampleAtUs = micros();
    sensorSampler.chunkTempSum += temperatureC;
    sensorSampler.chunkHumiditySum += humidityPct;
    sensorSampler.chunkSampleCount++;
}

bool prepareReadingSnapshot(float& temperatureC, float& humidityPct, bool& sensorOk) {
    const uint32_t nowUs = micros();
    rollChunkWindows(nowUs);
    trimRollingChunks(nowUs);

    if (rollingChunkCount > 0) {
        float rollingTemperatureSum = 0.0f;
        float rollingHumiditySum = 0.0f;
        size_t rollingCount = 0;
        for (size_t i = 0; i < rollingChunkCount; ++i) {
            const RollingChunk& chunk = rollingChunks[(rollingChunkHead + i) % SENSOR_ROLLING_CHUNK_CAPACITY];
            if (!isValidSensorSample(chunk.temperatureC, chunk.humidityPct)) {
                continue;
            }
            rollingTemperatureSum += chunk.temperatureC;
            rollingHumiditySum += chunk.humidityPct;
            rollingCount++;
        }
        if (rollingCount > 0) {
            const float rollingTemperatureC = rollingTemperatureSum / rollingCount;
            const float rollingHumidityPct = rollingHumiditySum / rollingCount;
            if (isValidSensorSample(rollingTemperatureC, rollingHumidityPct)) {
                temperatureC = rollingTemperatureC;
                humidityPct = rollingHumidityPct;
                sensorOk = true;
                return true;
            }
        }
    }

    if (hasRecentGoodSample(nowUs)) {
        temperatureC = lastTempC;
        humidityPct = lastHumidity;
        sensorOk = true;
        return true;
    }

    temperatureC = NAN;
    humidityPct = NAN;
    sensorOk = false;
    return false;
}

BufferedReading* oldestBufferedReading() {
    if (readingBufferCount == 0) {
        return nullptr;
    }
    return &readingBuffer[readingBufferHead];
}

void dropOldestBufferedReading() {
    if (readingBufferCount == 0) {
        return;
    }
    readingBuffer[readingBufferHead] = BufferedReading{};
    readingBufferHead = (readingBufferHead + 1U) % READING_BUFFER_CAPACITY;
    readingBufferCount--;
}

bool enqueueBufferedReading(float temperatureC, float humidityPct, bool sensorOk) {
    if (readingBufferCount >= READING_BUFFER_CAPACITY) {
        return false;
    }
    const size_t insertIndex = (readingBufferHead + readingBufferCount) % READING_BUFFER_CAPACITY;
    BufferedReading& item = readingBuffer[insertIndex];
    item = BufferedReading{};
    item.used = true;
    item.sequence = txSeq++;
    item.sampledAtMs = millis();
    item.temperatureC = temperatureC;
    item.humidityPct = humidityPct;
    item.sensorOk = sensorOk ? 1 : 0;
    readingBufferCount++;
    return true;
}

void scheduleNextReport(uint32_t delayMs) {
    nextReportDueAtMs = millis() + max<uint32_t>(delayMs, 25);
}

void applyControllerState(
    uint32_t reportMs,
    uint32_t nextDelayMs,
    float tempOffset,
    bool heaterOn,
    bool otaReadyEnabled,
    bool sleepEnabledState,
    uint16_t sampleRate,
    bool persistConfig
) {
    const uint32_t normalizedReportMs = normalizeReportInterval(reportMs);
    const uint16_t normalizedSampleRate = normalizeSampleRateHz(sampleRate);
    const bool normalizedSleepEnabled = sleepEnabledState && sleepAllowedForReportInterval(normalizedReportMs);
    const bool configChanged =
        reportIntervalMs != normalizedReportMs ||
        sampleRateHz != normalizedSampleRate ||
        tempOffsetC != tempOffset ||
        heaterEnabled != heaterOn ||
        otaReady != otaReadyEnabled ||
        sleepEnabled != normalizedSleepEnabled;

    reportIntervalMs = normalizedReportMs;
    sampleRateHz = normalizedSampleRate;
    tempOffsetC = tempOffset;
    heaterEnabled = heaterOn;
    otaReady = otaReadyEnabled;
    sleepEnabled = normalizedSleepEnabled;
    applySensorConfig();
    restartSensorSampler(true);
    captureWindowActive = false;
    captureWindowEndsAtMs = 0;
    if (otaReadyEnabled) {
        clearBufferedReadings();
    }
    scheduleNextReport(nextDelayMs);
    lastControllerContactAtMs = millis();
    lastRecoveryBindAttemptAtMs = 0;
    pendingSettingsSync = false;
    lastSettingsSyncRequestAtMs = 0;
    if (persistConfig || configChanged) {
        saveConfig();
    }
}

void beginCaptureWindow() {
    captureWindowActive = true;
    captureWindowEndsAtMs = millis() + captureWindowMs();
    restartSensorSampler(true);
    stayAwakeForController(captureWindowMs() + READING_ACK_TIMEOUT_MS + READING_RETRY_BACKOFF_MS);
}

void completeCaptureWindow() {
    captureWindowActive = false;
    captureWindowEndsAtMs = 0;
    captureScheduledReading();
    scheduleNextReport(normalizeReportInterval(reportIntervalMs));
    stayAwakeForController(READING_ACK_TIMEOUT_MS + READING_RETRY_BACKOFF_MS + 500);
}

bool captureScheduledReading() {
    float temperatureC = NAN;
    float humidityPct = NAN;
    bool sensorOk = false;
    prepareReadingSnapshot(temperatureC, humidityPct, sensorOk);
    if (!sensorOk) {
        Serial.println("Sensor read failed: sending heartbeat without measurement");
    }
    if (!enqueueBufferedReading(temperatureC, humidityPct, sensorOk)) {
        Serial.println("Reading buffer full, keeping oldest unsent data");
        return false;
    }
    return true;
}

void markSampleRequestScheduled(uint32_t nowUs) {
    if (sensorSampler.nextRequestAtUs == 0) {
        sensorSampler.nextRequestAtUs = nowUs;
    }
    while (timeReachedUs(nowUs, sensorSampler.nextRequestAtUs)) {
        sensorSampler.nextRequestAtUs += samplePeriodUs();
    }
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
    prefs.putUShort("sampleHz", normalizeSampleRateHz(sampleRateHz));
    prefs.putFloat("tempOff", tempOffsetC);
    prefs.putBool("heaterOn", heaterEnabled);
    prefs.putBool("sleepEn", effectiveSleepEnabled());
    prefs.putBytes("ctrlMac", controllerMac, 6);
    prefs.putString("name", String(nodeName));
    prefs.end();
}

void loadConfig() {
    prefs.begin("tmon-node", false);
    isBound = prefs.getBool("bound", false);
    nodeId = prefs.getUInt("nodeId", 0);
    reportIntervalMs = normalizeReportInterval(prefs.getUInt("reportMs", DEFAULT_REPORT_MS));
    sampleRateHz = normalizeSampleRateHz(prefs.getUShort("sampleHz", DEFAULT_SAMPLE_RATE_HZ));
    tempOffsetC = prefs.getFloat("tempOff", 0.0f);
    heaterEnabled = prefs.getBool("heaterOn", false);
    sleepEnabled = prefs.getBool("sleepEn", false);
    if (!sleepAllowedForReportInterval(reportIntervalMs)) {
        sleepEnabled = false;
    }
    prefs.getBytes("ctrlMac", controllerMac, 6);
    String name = prefs.getString("name", DEFAULT_NODE_NAME);
    sanitizeNodeName(name).toCharArray(nodeName, sizeof(nodeName));
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

bool initEspNowRadio() {
    WiFi.mode(WIFI_STA);
    WiFi.disconnect();
    esp_wifi_set_ps(WIFI_PS_NONE);
    esp_wifi_set_promiscuous(true);
    esp_wifi_set_channel(RADIO_CHANNEL, WIFI_SECOND_CHAN_NONE);
    esp_wifi_set_promiscuous(false);

    if (esp_now_init() != ESP_OK) {
        return false;
    }

    esp_now_register_recv_cb(onDataRecv);
    esp_now_register_send_cb(onDataSent);
    return true;
}

bool resetEspNowRadioAfterWake() {
    esp_now_deinit();
    WiFi.mode(WIFI_OFF);
    delay(2);

    if (!initEspNowRadio()) {
        Serial.println("ESP-NOW reinit failed after wake");
        return false;
    }

    if (isBound) {
        ensurePeer(controllerMac);
    }
    return true;
}

bool initSensor() {
    Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL);
    Wire.setClock(SENSOR_I2C_CLOCK_HZ);
    if (!sht.begin()) return false;
    sht.clearStatus();
    applySensorConfig();
    restartSensorSampler(true);
    return true;
}

void pumpSensorSampling() {
    const uint32_t nowUs = micros();
    rollChunkWindows(nowUs);

    if (otaState.active) return;

    if (sensorSampler.requestInFlight) {
        if (timeReachedUs(nowUs, sensorSampler.requestIssuedAtUs + SENSOR_MIN_SETTLE_US) && sht.dataReady(true)) {
            sensorPresent = sht.readData(false);
            if (sensorPresent) {
                const float temperatureC = sht.getTemperature();
                const float humidityPct = sht.getHumidity();
                if (isValidSensorSample(temperatureC, humidityPct)) {
                    recordSensorSample(temperatureC, humidityPct);
                    noteSamplingSuccess();
                } else {
                    sensorPresent = false;
                    noteSamplingFailure();
                }
            } else {
                noteSamplingFailure();
            }
            sensorSampler.requestInFlight = false;
            markSampleRequestScheduled(nowUs);
        } else if (timeReachedUs(nowUs, sensorSampler.requestIssuedAtUs + SENSOR_REQUEST_TIMEOUT_US)) {
            sensorPresent = false;
            sensorSampler.requestInFlight = false;
            noteSamplingFailure();
            sensorSampler.nextRequestAtUs = nowUs + SENSOR_RETRY_INTERVAL_US;
        }
        return;
    }

    if (!timeReachedUs(nowUs, sensorSampler.nextRequestAtUs)) return;

    if (sht.requestData(true)) {
        sensorSampler.requestInFlight = true;
        sensorSampler.requestIssuedAtUs = nowUs;
    } else {
        sensorPresent = false;
        noteSamplingFailure();
        sensorSampler.nextRequestAtUs = nowUs + SENSOR_RETRY_INTERVAL_US;
    }
}

void sendBindRequest() {
    uint8_t broadcastMac[] = {0xFF,0xFF,0xFF,0xFF,0xFF,0xFF};
    ensurePeer(broadcastMac);

    BindRequest msg{};
    fillHeader(msg.header, MSG_BIND_REQUEST, txSeq++, nodeId, millis());
    strncpy(msg.nodeName, nodeName, sizeof(msg.nodeName) - 1);
    msg.fwMajor = SATELLITE_FW_VERSION_MAJOR;
    msg.fwMinor = SATELLITE_FW_VERSION_MINOR;
    msg.capabilities = 0x0001;
    esp_now_send(broadcastMac, reinterpret_cast<const uint8_t*>(&msg), sizeof(msg));
    noteActivity();
}

bool sendReading() {
    if (!isBound) return false;
    BufferedReading* pending = oldestBufferedReading();
    if (pending == nullptr) {
        return false;
    }

    Reading msg{};
    fillHeader(msg.header, MSG_READING, pending->sequence, nodeId, millis());
    msg.temperatureC = pending->temperatureC;
    msg.humidityPct = pending->humidityPct;
    msg.vbat = NAN;
    msg.sensorOk = pending->sensorOk;
    msg.rssiHint = 0;
    msg.reserved[0] = SATELLITE_FW_VERSION_MAJOR;
    msg.reserved[1] = SATELLITE_FW_VERSION_MINOR;
    ensurePeer(controllerMac);
    esp_now_send(controllerMac, reinterpret_cast<const uint8_t*>(&msg), sizeof(msg));
    pending->lastSendAtMs = millis();
    pending->retryCount = min<uint8_t>(pending->retryCount + 1, 255);
    noteActivity();
    stayAwakeForController();
    return true;
}

void sendConfigAck(bool applied) {
    if (!isBound) return;
    ConfigAck msg{};
    fillHeader(msg.header, MSG_CONFIG_ACK, txSeq++, nodeId, millis());
    msg.reportIntervalMs = reportIntervalMs;
    msg.nextReportDelayMs = nextReportDueAtMs > millis() ? nextReportDueAtMs - millis() : 0;
    msg.tempOffsetC = tempOffsetC;
    msg.applied = applied ? 1 : 0;
    msg.heaterEnabled = heaterEnabled ? 1 : 0;
    msg.otaReady = otaReady ? 1 : 0;
    msg.sleepEnabled = effectiveSleepEnabled() ? 1 : 0;
    msg.sampleRateHz = normalizeSampleRateHz(sampleRateHz);
    ensurePeer(controllerMac);
    esp_now_send(controllerMac, reinterpret_cast<const uint8_t*>(&msg), sizeof(msg));
    noteActivity();
}

void sendRenameAck(bool applied) {
    if (!isBound) return;
    RenameAck msg{};
    fillHeader(msg.header, MSG_RENAME_ACK, txSeq++, nodeId, millis());
    msg.applied = applied ? 1 : 0;
    strncpy(msg.nodeName, nodeName, sizeof(msg.nodeName) - 1);
    ensurePeer(controllerMac);
    esp_now_send(controllerMac, reinterpret_cast<const uint8_t*>(&msg), sizeof(msg));
    noteActivity();
}

void handleReadingAck(const ReadingAck& ack) {
    BufferedReading* pending = oldestBufferedReading();
    if (pending == nullptr) {
        return;
    }
    if (!ack.accepted || ack.readingSequence != pending->sequence) {
        return;
    }
    dropOldestBufferedReading();
    applyControllerState(
        ack.reportIntervalMs,
        ack.nextReportDelayMs,
        ack.tempOffsetC,
        ack.heaterEnabled != 0,
        ack.otaReady != 0,
        ack.sleepEnabled != 0,
        ack.sampleRateHz,
        false
    );
    stayAwakeForController();
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
    noteActivity();
}

void handleOtaBegin(const OtaBegin& msg) {
    if (!otaReady) {
        sendOtaAck(OTA_PHASE_BEGIN, OTA_STATUS_NOT_READY, otaState.bytesWritten);
        return;
    }

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
    captureWindowActive = false;
    captureWindowEndsAtMs = 0;
    clearBufferedReadings();
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
    lastControllerContactAtMs = millis();
    stayAwakeForController();

    if (h->type == MSG_BIND_ACK && len == sizeof(BindAck)) {
        const BindAck* ack = reinterpret_cast<const BindAck*>(data);
        if (!ack->accepted) return;
        memcpy(controllerMac, ack->controllerMac, 6);
        nodeId = ack->assignedNodeId;
        isBound = true;
        ensurePeer(controllerMac);
        applyControllerState(
            ack->reportIntervalMs,
            ack->nextReportDelayMs,
            ack->tempOffsetC,
            ack->heaterEnabled != 0,
            ack->otaReady != 0,
            ack->sleepEnabled != 0,
            ack->sampleRateHz,
            true
        );
        Serial.printf(
            "Bound to controller %s as node %lu fw %u.%u heater %s sample %uHz\n",
            macToString(controllerMac).c_str(),
            static_cast<unsigned long>(nodeId),
            SATELLITE_FW_VERSION_MAJOR,
            SATELLITE_FW_VERSION_MINOR,
            heaterEnabled ? "on" : "off",
            static_cast<unsigned>(sampleRateHz)
        );
        noteActivity();
    } else if (h->type == MSG_CONFIG_SET && len == sizeof(ConfigSet)) {
        const ConfigSet* cfg = reinterpret_cast<const ConfigSet*>(data);
        applyControllerState(
            cfg->reportIntervalMs,
            cfg->nextReportDelayMs,
            cfg->tempOffsetC,
            cfg->heaterEnabled != 0,
            cfg->otaReady != 0,
            cfg->sleepEnabled != 0,
            cfg->sampleRateHz,
            true
        );
        sendConfigAck(true);
        noteActivity();
    } else if (h->type == MSG_READING_ACK && len == sizeof(ReadingAck) && isBound) {
        handleReadingAck(*reinterpret_cast<const ReadingAck*>(data));
        noteActivity();
    } else if (h->type == MSG_RENAME_SET && len == sizeof(RenameSet) && isBound) {
        const RenameSet* rename = reinterpret_cast<const RenameSet*>(data);
        sanitizeNodeName(String(rename->nodeName)).toCharArray(nodeName, sizeof(nodeName));
        saveConfig();
        sendRenameAck(true);
        noteActivity();
    } else if (h->type == MSG_OTA_BEGIN && len == sizeof(OtaBegin) && isBound) {
        handleOtaBegin(*reinterpret_cast<const OtaBegin*>(data));
        noteActivity();
    } else if (h->type == MSG_OTA_CHUNK && len == sizeof(OtaChunk) && isBound) {
        handleOtaChunk(*reinterpret_cast<const OtaChunk*>(data));
        noteActivity();
    } else if (h->type == MSG_OTA_END && len == sizeof(OtaEnd) && isBound) {
        handleOtaEnd(*reinterpret_cast<const OtaEnd*>(data));
        noteActivity();
    }
}

void maintainControllerLink() {
    if (!isBound || otaState.active || otaReady) {
        return;
    }

    const uint32_t nowMs = millis();
    const uint32_t staleAfterMs = max<uint32_t>(15000, normalizeReportInterval(reportIntervalMs) * 3U);
    if (!timeReachedMs(nowMs, lastControllerContactAtMs + staleAfterMs)) {
        return;
    }

    stayAwakeForController();
    if (!timeReachedMs(nowMs, lastRecoveryBindAttemptAtMs + 3000)) {
        return;
    }

    lastRecoveryBindAttemptAtMs = nowMs;
    sendBindRequest();
    Serial.println("Controller contact stale, rebinding...");
}

void maybeRequestSettingsSync() {
    if (!isBound || otaState.active || otaReady) {
        return;
    }
    if (!pendingSettingsSync) {
        return;
    }

    const uint32_t nowMs = millis();
    if (!timeReachedMs(nowMs, lastSettingsSyncRequestAtMs + SETTINGS_SYNC_RETRY_MS)) {
        return;
    }

    lastSettingsSyncRequestAtMs = nowMs;
    sendBindRequest();
    stayAwakeForController(SETTINGS_SYNC_AWAKE_WINDOW_MS);
}

void logSendStatus(esp_now_send_status_t status) {
    if (status != ESP_NOW_SEND_SUCCESS) {
        Serial.println("Last send: fail");
    }
}

void queueScheduledReadingsIfDue() {
    if (!isBound || otaState.active || otaReady) {
        return;
    }
    if (captureWindowActive || readingBufferCount >= READING_BUFFER_CAPACITY) {
        return;
    }
    if (timeReachedMs(millis(), nextReportDueAtMs)) {
        beginCaptureWindow();
    }
}

void serviceReadingDelivery() {
    if (!isBound || otaState.active || otaReady) {
        return;
    }
    BufferedReading* pending = oldestBufferedReading();
    if (pending == nullptr) {
        return;
    }

    const uint32_t nowMs = millis();
    if (pending->lastSendAtMs == 0) {
        sendReading();
        return;
    }

    uint32_t nextRetryAtMs = pending->lastSendAtMs + READING_ACK_TIMEOUT_MS;
    if (pending->retryCount >= READING_MAX_RETRIES) {
        nextRetryAtMs = pending->lastSendAtMs + max<uint32_t>(normalizeReportInterval(reportIntervalMs), READING_ACK_TIMEOUT_MS);
        if (timeReachedMs(nowMs, nextRetryAtMs)) {
            pending->retryCount = 0;
        }
    } else {
        nextRetryAtMs += READING_RETRY_BACKOFF_MS;
    }

    if (timeReachedMs(nowMs, nextRetryAtMs)) {
        sendReading();
    }
}

void lightSleepForMs(uint32_t sleepMs) {
    if (sleepMs < 20) {
        return;
    }
    esp_sleep_enable_timer_wakeup(static_cast<uint64_t>(sleepMs) * 1000ULL);
    esp_light_sleep_start();
    esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_TIMER);
    resetEspNowRadioAfterWake();
    stayAwakeForController(200);
    if (isBound && sleepMs >= SETTINGS_SYNC_WAKE_THRESHOLD_MS) {
        pendingSettingsSync = true;
    }
}

void maybeSleep() {
    if (DEBUG_FORCE_NO_SLEEP || !effectiveSleepEnabled()) {
        return;
    }

    if (otaState.active || otaRebootPending || otaReady) {
        return;
    }

    const uint32_t nowMs = millis();
    if (!timeReachedMs(nowMs, controllerAwakeUntilMs)) {
        return;
    }

    if (!isBound) {
        lightSleepForMs(UNBOUND_SLEEP_MS);
        return;
    }

    if (captureWindowActive) {
        return;
    }

    uint32_t nextWakeAtMs = nextReportDueAtMs;
    if (BufferedReading* pending = oldestBufferedReading()) {
        const uint32_t staleAfterMs = max<uint32_t>(15000, normalizeReportInterval(reportIntervalMs) * 3U);
        if (!timeReachedMs(nowMs, lastControllerContactAtMs + staleAfterMs)) {
            return;
        }
        const uint32_t fallbackWakeAtMs = pending->lastSendAtMs == 0
            ? (nowMs + MIN_REPORT_MS)
            : (pending->lastSendAtMs + normalizeReportInterval(reportIntervalMs));
        if (timeReachedMs(nextWakeAtMs, fallbackWakeAtMs)) {
            nextWakeAtMs = fallbackWakeAtMs;
        }
    }

    if (timeReachedMs(nowMs + 20, nextWakeAtMs)) {
        return;
    }
    lightSleepForMs(nextWakeAtMs - nowMs);
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
    const bool cpuFreqApplied = setCpuFrequencyMhz(SATELLITE_CPU_FREQ_MHZ);
    Serial.begin(SERIAL_BAUD);
    delay(500);
    loadConfig();
    Serial.printf(
        "Satellite CPU frequency: %u MHz%s\n",
        static_cast<unsigned>(getCpuFrequencyMhz()),
        cpuFreqApplied ? "" : " (requested change failed)"
    );
    if (DEBUG_FORCE_NO_SLEEP) {
        Serial.println("DEBUG_FORCE_NO_SLEEP enabled: light sleep disabled");
    } else {
        Serial.printf(
            "Sleep mode: %s\n",
            sleepEnabled ? "enabled" : "disabled"
        );
    }
    initStatusLed();
    sensorPresent = initSensor();

    if (!initEspNowRadio()) {
        Serial.println("ESP-NOW init failed");
        return;
    }

    if (isBound) {
        ensurePeer(controllerMac);
        scheduleNextReport(reportIntervalMs);
        stayAwakeForController();
        lastControllerContactAtMs = millis();
        lastRecoveryBindAttemptAtMs = 0;
        Serial.printf(
            "Loaded binding: node %lu controller %s fw %u.%u\n",
            static_cast<unsigned long>(nodeId),
            macToString(controllerMac).c_str(),
            SATELLITE_FW_VERSION_MAJOR,
            SATELLITE_FW_VERSION_MINOR
        );
        sendBindRequest();
        Serial.println("Rebinding with controller...");
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
        pumpSensorSampling();
        static uint32_t lastBindAttemptAt = 0;
        if (millis() - lastBindAttemptAt >= 3000) {
            lastBindAttemptAt = millis();
            sendBindRequest();
            Serial.println("Bind request sent");
        }
        maybeSleep();
        delay(1);
        return;
    }

    maintainControllerLink();
    maybeRequestSettingsSync();
    if (captureWindowActive) {
        pumpSensorSampling();
        if (timeReachedMs(millis(), captureWindowEndsAtMs)) {
            completeCaptureWindow();
        }
    }
    queueScheduledReadingsIfDue();
    serviceReadingDelivery();
    maybeSleep();
    delay(1);
}
