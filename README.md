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
|- tests/                       App and firmware automated tests
|- run_tests.py                 Test runner for app, UI smoke, and firmware checks
|- TESTING.md                   Test workflow and coverage notes
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

```text
+--------------------+       USB Serial       +-------------------------+
|      Computer      | <--------------------> | arduino nano Controller |
|                    |                        | with 8ch mux board      |
| logger / GUI / CLI |                        |  JSON lines over USB    |
+--------------------+                        +------------+------------+
                                                         |
                                                         |  I2C
                                                         |
              +------------------------------------------+------------------+
              |                                          |                  |
              v                                          v                  v
     +-------------------+                     +-------------------+  +-------------------+
     |                   |                     |                   |  |                   |
     |       SHT85       |                     |       SHT85       |  |       SHT85       |
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

If PlatformIO does not auto-detect the correct board, pass a local upload port with `--upload-port`, for example `--upload-port COM6`.

## PC Tools

### Controller Terminal

The controller serial helper is in `pc_logger/controller_terminal.py`.

Run:

```bash
python pc_logger\controller_terminal.py --port COM6
```

It opens the serial port, pushes current PC time to the controller, and provides interactive command entry.

Rename one or more satellites without entering the interactive terminal:

```bash
python pc_logger\controller_terminal.py --port COM6 --rename 3 "Boiler Room"
python pc_logger\controller_terminal.py --port COM6 --rename 3 "Boiler Room" --rename 4 "Outdoor Rack"
```

Interactive helper commands:

- `/sync` resend the current PC time with `TIME SET`
- `/rename <nodeId> <name>` sanitize the name and send `RENAME`
- `/quit` close the terminal

Any other line is forwarded directly to the controller command parser.

### Satellite OTA

The OTA helper is in `pc_logger/ota_satellite.py`.

Build satellite firmware first, then upload through the controller:

```bash
python -m platformio run -e satellite_upload
python pc_logger\ota_satellite.py --port COM6 --node-id 1 --firmware .pio\build\satellite_upload\firmware.bin
python pc_logger\ota_satellite.py --port COM6 --node-id 2 --firmware .pio\build\satellite_upload\firmware.bin
```

Run OTA one satellite at a time.

Optional arguments:

- `--baud 460800` override the controller serial baud rate

The OTA helper automatically enables streaming and uses `OTA PREP <nodeId> ON|OFF` to put the target satellite into OTA-accept mode. In that mode the controller pushes settings that mark the node OTA-ready, force sleep off, switch to a quiet OTA report interval, and suppress normal reading delivery until OTA prep is turned off.

Manual OTA preparation from the controller terminal:

```text
OTA PREP 1 ON
OTA STATUS
OTA BEGIN 1 <size> <crc32hex>
...
OTA END
OTA PREP 1 OFF
```

`OTA PREP` is the high-level command intended for real OTA uploads. `OTA READY` is still available as a lower-level flag toggle, but it does not apply the full quiet-mode profile by itself.

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
- Satellite-aware smoothing and presence/offline handling
- App version shown in the window title and App info dialog

The logger entry point is the `temp_humidity_logger.main` module.

Setup:

```bash
cd Temp_and_HumidityLogger
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python -m temp_humidity_logger.main
```

Packaging instructions are documented in [Temp_and_HumidityLogger/README.md](Temp_and_HumidityLogger/README.md).

## Testing

Run fast local checks:

```bash
python run_tests.py --suite fast
```

Run the full suite, including PlatformIO controller and satellite builds:

```bash
python run_tests.py --suite all
```

Detailed test coverage and suite filters are documented in [TESTING.md](TESTING.md).

## Large Dataset Note

The desktop logger now caches per-channel downsampled chart data before redraw, which keeps pan/zoom and redraws responsive even when session history grows very large. Extremely large session loads can still be limited by database read time rather than chart rendering.

## Controller Commands

Available controller commands:

- `HELP` print the controller command list
- `NODES` print the current node snapshot
- `BIND` open the bind window for pairing new satellites
- `BIND OFF` close the bind window
- `STREAM ON` enable JSON reading/config event output over serial
- `STREAM OFF` disable streaming output
- `SETINT <nodeId> <ms>` set one node report interval in milliseconds
- `SETINT ALL <ms>` set the report interval for every known node
- `SETSAMPLE <nodeId> <hz>` set one node sample rate in Hz (`1` to `5`)
- `SETSAMPLE ALL <hz>` set the sample rate for every known node (`1` to `5`)
- `SLEEP <nodeId> ON|OFF` enable or disable light sleep for one node
- `SLEEP ALL ON|OFF` enable or disable light sleep for every known node
- `SETTOFF <nodeId> <tempOffsetC>` store and apply a temperature offset
- `HEATER <nodeId> ON` enable the SHT85 heater for one node
- `HEATER <nodeId> OFF` disable the SHT85 heater for one node
- `RENAME <nodeId> <name>` rename a node with a sanitized ASCII name
- `TIME STATUS` print controller clock status and ISO timestamp
- `TIME SET <unixSeconds>` set the controller clock from a Unix timestamp
- `OTA PREP <nodeId> ON|OFF` enable or disable OTA preparation mode for one node; this applies the OTA-ready flag, forces sleep off, uses the quiet OTA interval, and pauses normal reading delivery until prep is disabled
- `OTA READY <nodeId> ON|OFF` mark a node ready or not ready for OTA transfer without applying the full OTA prep profile
- `OTA BEGIN <nodeId> <size> <crc32hex>` begin or resume an OTA session
- `OTA CHUNK <offset> <hex>` send one OTA data chunk
- `OTA END` finish the active OTA session
- `OTA STATUS` print the active OTA state, including whether OTA prep is active for the current target
- `OTA ABORT` cancel the active OTA session

## Current Behavior

- Controller stream is off by default at boot
- Controller polls satellites and spaces polls automatically when multiple satellites are present
- Satellites sample SHT85 during each capture window at a configurable target rate up to `5 Hz`
- SHT85 sampling uses high-repeatability single-shot mode with CRC-validated readout
- Poll responses are averaged from stored `10 ms` sample chunks
- Satellite firmware version is reported to the controller
- RSSI is reported as both `rssi_dbm` and `signal_pct`
- Satellite heater state is controlled by the controller and stored in flash

