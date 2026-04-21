#include <Wire.h>
#include <ctype.h>
#include "../../shared/firmware_versions.h"

#define MUX_ADDR 0x70
#define STH85_ADDR 0x44

// SHT85 command: High repeatability, no clock stretching
#define CMD_MEASURE_HIGHREP 0x2400

const char *CH_NAME[8] = {
  "CH0 - Location A",
  "CH1 - Location B",
  "CH2 - Location C",
  "CH3 - Location D",
  "CH4 - Location E",
  "CH5 - Location F",
  "CH6 - Location G",
  "CH7 - Location H"
};

const uint8_t CHANNEL_COUNT = 8;
const uint8_t ACTIVE_CHANNEL_COUNT = 6;
const uint8_t READ_RETRIES = 3;
const uint16_t RETRY_DELAY_MS = 30;
const uint16_t MEASUREMENT_DELAY_MS = 20;
const uint16_t SERIAL_IDLE_DELAY_MS = 5;
const uint32_t I2C_CLOCK_HZ = 50000;
const uint8_t CMD_BUFFER_LEN = 32;
const char *ARDUINO_BOARD = "nano_every";
const char *ARDUINO_FW_VERSION = proto::ARDUINO_NANO_FW_VERSION;

uint8_t handshakeComplete = 0;
char cmdBuffer[CMD_BUFFER_LEN];
uint8_t cmdLength = 0;

// Sensirion CRC-8 (poly 0x31, init 0xFF)
uint8_t sensirionCRC8(const uint8_t *data, uint8_t len) {
  uint8_t crc = 0xFF;
  for (uint8_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (uint8_t b = 0; b < 8; b++) {
      crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x31) : (uint8_t)(crc << 1);
    }
  }
  return crc;
}

void configureI2C() {
  Wire.begin();
  Wire.setClock(I2C_CLOCK_HZ);
}

bool consumeWireTimeout() {
  return false;
}

void recoverI2CBus() {
  Wire.end();

  pinMode(SDA, INPUT_PULLUP);
  pinMode(SCL, INPUT_PULLUP);
  delay(1);

  if (digitalRead(SDA) == LOW) {
    pinMode(SCL, OUTPUT);
    digitalWrite(SCL, HIGH);

    for (uint8_t i = 0; i < 18 && digitalRead(SDA) == LOW; i++) {
      digitalWrite(SCL, LOW);
      delayMicroseconds(5);
      digitalWrite(SCL, HIGH);
      delayMicroseconds(5);
    }

    pinMode(SDA, OUTPUT);
    digitalWrite(SDA, LOW);
    delayMicroseconds(5);
    digitalWrite(SCL, HIGH);
    delayMicroseconds(5);
    digitalWrite(SDA, HIGH);
    delayMicroseconds(5);
  }

  pinMode(SDA, INPUT_PULLUP);
  pinMode(SCL, INPUT_PULLUP);
  delay(1);
}

void reinitI2C() {
  recoverI2CBus();
  configureI2C();
}

bool selectMuxChannel(uint8_t channel) {
  if (channel >= CHANNEL_COUNT) {
    return false;
  }

  Wire.beginTransmission(MUX_ADDR);
  Wire.write((uint8_t)(1U << channel));
  uint8_t status = Wire.endTransmission();

  if (consumeWireTimeout()) {
    reinitI2C();
    return false;
  }

  return status == 0;
}

void clearMuxChannels() {
  Wire.beginTransmission(MUX_ADDR);
  Wire.write((uint8_t)0x00);
  Wire.endTransmission();
  if (consumeWireTimeout()) {
    reinitI2C();
  }
}

bool i2cDevicePresent(uint8_t addr) {
  Wire.beginTransmission(addr);
  uint8_t status = Wire.endTransmission();

  if (consumeWireTimeout()) {
    reinitI2C();
    return false;
  }

  return status == 0;
}

// errCode meanings:
// 1 = I2C write fail
// 2 = short read
// 3 = temp CRC fail
// 4 = hum CRC fail
// 5 = I2C timeout
bool readSTH85Once(float &temperature, float &humidity, uint8_t &errCode) {
  errCode = 0;

  Wire.beginTransmission(STH85_ADDR);
  Wire.write((uint8_t)(CMD_MEASURE_HIGHREP >> 8));
  Wire.write((uint8_t)(CMD_MEASURE_HIGHREP & 0xFF));

  uint8_t txStatus = Wire.endTransmission();

  if (consumeWireTimeout()) {
    errCode = 5;
    return false;
  }

  if (txStatus != 0) {
    errCode = 1;
    return false;
  }

  delay(MEASUREMENT_DELAY_MS);

  size_t readCount = Wire.requestFrom((uint8_t)STH85_ADDR, (size_t)6);

  if (consumeWireTimeout()) {
    errCode = 5;
    return false;
  }

  if (readCount != 6 || Wire.available() != 6) {
    while (Wire.available()) {
      Wire.read();
    }
    errCode = 2;
    return false;
  }

  uint8_t buf[6];
  for (uint8_t i = 0; i < 6; i++) {
    buf[i] = Wire.read();
  }

  if (sensirionCRC8(&buf[0], 2) != buf[2]) {
    errCode = 3;
    return false;
  }

  if (sensirionCRC8(&buf[3], 2) != buf[5]) {
    errCode = 4;
    return false;
  }

  uint16_t t_raw = ((uint16_t)buf[0] << 8) | buf[1];
  uint16_t h_raw = ((uint16_t)buf[3] << 8) | buf[4];

  temperature = -45.0f + (175.0f * (float)t_raw / 65535.0f);
  humidity = 100.0f * (float)h_raw / 65535.0f;

  return true;
}

bool readSTH85Robust(float &t, float &h, uint8_t &lastErr) {
  for (uint8_t attempt = 1; attempt <= READ_RETRIES; attempt++) {
    uint8_t err = 0;
    if (readSTH85Once(t, h, err)) {
      lastErr = 0;
      return true;
    }

    lastErr = err;
    delay(RETRY_DELAY_MS);

    if (err == 5 || attempt < READ_RETRIES) {
      reinitI2C();
      delay(5);
    }
  }

  return false;
}

const char *errToText(uint8_t err) {
  switch (err) {
    case 1: return "I2C write fail";
    case 2: return "Short read";
    case 3: return "Temp CRC fail";
    case 4: return "Hum CRC fail";
    case 5: return "I2C timeout";
    default: return "Unknown";
  }
}

void printJsonString(const char *value) {
  Serial.write('\"');
  while (*value != '\0') {
    char c = *value++;
    if (c == '\"' || c == '\\') {
      Serial.write('\\');
    }
    Serial.write(c);
  }
  Serial.write('\"');
}

void emitReadyEvent() {
  Serial.print("{\"event\":\"arduino_ready\",\"protocol\":\"json\",\"fw_version\":\"");
  Serial.print(ARDUINO_FW_VERSION);
  Serial.print("\",\"board\":\"");
  Serial.print(ARDUINO_BOARD);
  Serial.print("\",\"channel_count\":");
  Serial.print(ACTIVE_CHANNEL_COUNT);
  Serial.print(",\"mux_addr\":\"0x70\",\"sensor_addr\":\"0x44\",\"i2c_clock_hz\":");
  Serial.print(I2C_CLOCK_HZ);
  Serial.print(",\"retries\":");
  Serial.print(READ_RETRIES);
  Serial.println(",\"timeout_enabled\":false}");
}

void emitBatchReadingItem(uint8_t channel, float temperature, float humidity) {
  Serial.print("{\"channel\":");
  Serial.print(channel);
  Serial.print(",\"name\":");
  printJsonString(CH_NAME[channel]);
  Serial.print(",\"temperature_c\":");
  Serial.print(temperature, 2);
  Serial.print(",\"humidity_pct\":");
  Serial.print(humidity, 2);
  Serial.print(",\"sensor_ok\":true}");
}

void emitBatchReadFailItem(uint8_t channel, uint8_t errCode) {
  Serial.print("{\"channel\":");
  Serial.print(channel);
  Serial.print(",\"name\":");
  printJsonString(CH_NAME[channel]);
  Serial.print(",\"sensor_ok\":false,\"error\":");
  printJsonString(errToText(errCode));
  Serial.print("}");
}

void printHandshake() {
  emitReadyEvent();
}

void normalizeCommand() {
  uint8_t start = 0;
  while (start < cmdLength && isspace((unsigned char)cmdBuffer[start])) {
    start++;
  }

  uint8_t end = cmdLength;
  while (end > start && isspace((unsigned char)cmdBuffer[end - 1])) {
    end--;
  }

  uint8_t out = 0;
  for (uint8_t i = start; i < end && out < (CMD_BUFFER_LEN - 1); i++) {
    cmdBuffer[out++] = (char)toupper((unsigned char)cmdBuffer[i]);
  }
  cmdBuffer[out] = '\0';
  cmdLength = out;
}

void resetCommandBuffer() {
  cmdLength = 0;
  cmdBuffer[0] = '\0';
}

void readAllSensors() {
  bool foundAnySensor = false;
  bool firstItem = true;
  const char *batchStatus = "ok";
  const char *batchMessage = NULL;

  Serial.print("{\"event\":\"arduino_batch\",\"items\":[");

  for (uint8_t ch = 0; ch < ACTIVE_CHANNEL_COUNT; ch++) {
    if (!selectMuxChannel(ch)) {
      batchStatus = "mux_select_fail";
      batchMessage = "check wiring/power";
      break;
    }

    if (!i2cDevicePresent(STH85_ADDR)) {
      continue;
    }

    foundAnySensor = true;

    float t = 0.0f;
    float h = 0.0f;
    uint8_t lastErr = 0;

    if (!firstItem) {
      Serial.write(',');
    }
    firstItem = false;

    if (readSTH85Robust(t, h, lastErr)) {
      emitBatchReadingItem(ch, t, h);
    } else {
      emitBatchReadFailItem(ch, lastErr);
    }
  }

  clearMuxChannels();

  if (!foundAnySensor) {
    batchStatus = "no_sensors";
    batchMessage = "No sensors found on CH0..CH5.";
  }

  Serial.print("],\"status\":\"");
  Serial.print(batchStatus);
  Serial.write('\"');
  if (batchMessage != NULL && batchMessage[0] != '\0') {
    Serial.print(",\"message\":");
    printJsonString(batchMessage);
  }
  Serial.println("}");
}

void processCommand() {
  normalizeCommand();
  if (cmdLength == 0) {
    return;
  }

  if (strcmp(cmdBuffer, "HANDSHAKE?") == 0) {
    printHandshake();
    handshakeComplete = 1;
    return;
  }

  if (strcmp(cmdBuffer, "DISCONNECT") == 0) {
    handshakeComplete = 0;
    return;
  }

  if (!handshakeComplete) {
    return;
  }

  if (strcmp(cmdBuffer, "READ") == 0) {
    readAllSensors();
  }
}

void pollSerial() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();

    if (c == '\n' || c == '\r') {
      processCommand();
      resetCommandBuffer();
      continue;
    }

    if (cmdLength < (CMD_BUFFER_LEN - 1)) {
      cmdBuffer[cmdLength++] = c;
      cmdBuffer[cmdLength] = '\0';
    }
  }
}

void setup() {
  Serial.begin(9600);
  resetCommandBuffer();
  configureI2C();
  clearMuxChannels();
}

void loop() {
  pollSerial();
  delay(SERIAL_IDLE_DELAY_MS);
}
