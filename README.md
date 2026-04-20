# ESP32 Temperature Monitor

ESP32 controller and satellite firmware with PC-side tools for logging, graphing, terminal control, and OTA updates.

## Overview

The system is split into three parts:

- An ESP32 controller connected to the PC over USB serial
- One or more ESP32 satellite nodes with SHT85 sensors
- Python tools for live logging, charts, terminal control, and satellite OTA

Satellites communicate with the controller over ESP-NOW on channel `6`. The controller polls satellites and returns JSON events to the PC.

## Repository Layout

```text
esp32_temp_monitor/
|- controller/                  Arduino controller sketch
|- satellite/                   Arduino satellite sketch
|- src/                         PlatformIO controller/satellite entry points
|- shared/                      Shared protocol header
|- pio_controller_src/          Alternate PlatformIO controller source
|- pc_logger/                   CLI helpers for controller terminal and OTA
|- Temp_and_HumidityLogger/     Tkinter + Matplotlib desktop logger
|- platformio.ini               PlatformIO environments
```

## Hardware

### Controller

- ESP32 development board connected to the PC by USB

### Satellite

- ESP32 development board
- SHT85 sensor over I2C

Default satellite I2C pins:

- `GPIO21` = SDA
- `GPIO22` = SCL

Wiring:

```text
SHT85 -> ESP32
SCL   -> GPIO22
VCC   -> 3V3
GND   -> GND
SDA   -> GPIO21
```

System view:

```text
+--------------------+       USB Serial       +-------------------------+
|      Computer      | <--------------------> |    ESP32 Controller     |
| logger / GUI / CLI |                        |  JSON lines over USB    |
+--------------------+                        +------------+------------+
                                                         |
                                                 ESP-NOW |
                                                         |
              +------------------------------------------+------------------+
              |                                          |                  |
              v                                          v                  v
     +-------------------+                     +-------------------+  +-------------------+
     | ESP32 Satellite 1 |                     | ESP32 Satellite 2 |  | ESP32 Satellite N |
     | + 1 x SHT85       |                     | + 1 x SHT85       |  | + 1 x SHT85       |
     +-------------------+                     +-------------------+  +-------------------+
```

## Firmware Build

Requirements:

- Python 3
- PlatformIO

Install PlatformIO if needed:

```bash
pip install platformio
```

Build controller firmware:

```bash
python -m platformio run -e controller_upload
```

Build satellite firmware:

```bash
python -m platformio run -e satellite_upload
```

Flash controller over USB:

```bash
python -m platformio run -e controller_upload -t upload
```

Flash satellite over USB:

```bash
python -m platformio run -e satellite_upload -t upload
```

`platformio.ini` currently uses `COM6` as the default upload port. Change that locally if your board is on a different port.

## PC Tools

### Controller Terminal

The controller serial helper is in `pc_logger/controller_terminal.py`.

Run:

```bash
python pc_logger\controller_terminal.py --port COM6
```

It opens the serial port, pushes current PC time to the controller, and provides interactive command entry.

### Satellite OTA

The OTA helper is in `pc_logger/ota_satellite.py`.

Build satellite firmware first, then upload through the controller:

```bash
python -m platformio run -e satellite_upload
python pc_logger\ota_satellite.py --port COM6 --node-id 1 --firmware .pio\build\satellite_upload\firmware.bin
python pc_logger\ota_satellite.py --port COM6 --node-id 2 --firmware .pio\build\satellite_upload\firmware.bin
```

Run OTA one satellite at a time.

### Desktop Logger

The desktop GUI lives in `Temp_and_HumidityLogger/`.

It provides:

- Live temperature and humidity graphs
- SQLite-backed session history
- CSV export
- Channel naming, colors, and calibration
- Marker annotations
- Split temperature and humidity tabs
- COM-port control for Arduino and ESP controller sources

Setup:

```bash
cd Temp_and_HumidityLogger
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python arduino_logger_v72.py
```

Packaging instructions are documented in [Temp_and_HumidityLogger/README.md](/C:/Users/LENOVO/Downloads/esp32_temp_monitor-20260414T164103Z-3-001/esp32_temp_monitor/Temp_and_HumidityLogger/README.md).

## Large Dataset Note

The desktop logger now caches per-channel downsampled chart data before redraw, which keeps pan/zoom and redraws responsive even when session history grows very large. Extremely large session loads can still be limited by database read time rather than chart rendering.

## Controller Commands

Available controller commands:

- `HELP`
- `NODES`
- `BIND`
- `BIND OFF`
- `STREAM ON`
- `STREAM OFF`
- `SETINT <nodeId> <ms>`
- `SETINT ALL <ms>`
- `SETSAMPLE <nodeId> <hz>`
- `SETSAMPLE ALL <hz>`
- `SETTOFF <nodeId> <tempOffsetC>`
- `HEATER <nodeId> ON`
- `HEATER <nodeId> OFF`
- `RENAME <nodeId> <name>`
- `TIME STATUS`
- `TIME SET <unixSeconds>`
- `OTA BEGIN <nodeId> <size> <crc32hex>`
- `OTA CHUNK <offset> <hex>`
- `OTA END`
- `OTA STATUS`
- `OTA ABORT`

## Current Behavior

- Controller stream is off by default at boot
- Controller polls satellites and spaces polls automatically when multiple satellites are present
- Satellites sample SHT85 continuously at a configurable target rate up to `200 Hz`
- Poll responses are averaged from stored `10 ms` sample chunks
- Satellite firmware version is reported to the controller
- RSSI is reported as both `rssi_dbm` and `signal_pct`
- Satellite heater state is controlled by the controller and stored in flash

