# ESP32 Temperature Monitor

ESP32 controller + one or more ESP32 satellites with SHT85 sensors.

- Controller talks to the PC over USB serial.
- Satellites talk to the controller over ESP-NOW on channel `6`.
- Satellites only send readings when the controller requests them.
- Satellite OTA is performed through the controller.

#wiring
```text
SHT85 - ESP32
1 WHITE SCL -> P22
2 RED VCC -> 3v3
3 BLACK GND - GND
4 GREEN SDA -> P21
```
```text
+--------------------+       USB Serial       +-------------------------+
|      Computer      | <--------------------> |    ESP32 Controller     |
| logger / GUI / CLI |                        |  JSON lines over USB    |
+--------------------+                        +------------+------------+
                                                          |
                                                  ESP-NOW |
                                                          |
               +------------------------------------------+----------------------------------+
               |                                          |                                  |
               v                                          v                                  v
      +-------------------+                     +-------------------+               +-------------------+
      | ESP32 Satellite 1 |                     | ESP32 Satellite 2 |               | ESP32 Satellite N |
      | + 1 x SHT85       |                     | + 1 x SHT85       |               | + 1 x SHT85       |
      +-------------------+                     +-------------------+               +-------------------+
```

## Repository Layout

```text
esp32_temp_monitor/
|- controller/
|  |- controller.ino
|  |- protocol.h
|- satellite/
|  |- satellite.ino
|  |- protocol.h
|- shared/
|  |- protocol.h
|- src/
|  |- main.cpp
|  |- satellite_main.cpp
|- pc_logger/
|  |- controller_terminal.py
|  |- ota_satellite.py
|- platformio.ini
```

## Hardware

### Controller

- ESP32 board connected to the PC by USB

### Satellite

- ESP32 board
- SHT85 sensor on I2C

Default I2C pins used by the satellite firmware:

- `GPIO21` = SDA
- `GPIO22` = SCL

Keep the SHT85 physically away from the ESP32 module and regulator if you want realistic temperature readings.

## Requirements

- Python 3
- PlatformIO
- `pyserial`

Install the Python tools:

```bash
pip install platformio pyserial
```

## Build

Build controller firmware:

```bash
python -m platformio run -e controller_upload
```

Build satellite firmware:

```bash
python -m platformio run -e satellite_upload
```

## Flash Over USB

Flash the controller on `COM6`:

```bash
python -m platformio run -e controller_upload -t upload
```

Flash a satellite on `COM6`:

```bash
python -m platformio run -e satellite_upload -t upload
```

`platformio.ini` already contains the upload environments used above.

## OTA Update For Satellites

Satellite OTA goes through the controller serial port. The controller itself is still updated over USB flash, not OTA.

Build the satellite firmware first:

```bash
python -m platformio run -e satellite_upload
```

Then upload to a satellite node through the controller:

```bash
#python pc_logger\ota_satellite.py --port COM6 --node-id 1 --firmware .pio\build\satellite_upload\firmware.bin
python pc_logger\ota_satellite.py --port COM6 --node-id 1 --firmware .pio\build\satellite_upload\firmware.bin
python pc_logger\ota_satellite.py --port COM6 --node-id 2 --firmware .pio\build\satellite_upload\firmware.bin
```

The OTA helper now quiets other known satellites first by temporarily moving them to a long poll interval, then restores their previous interval after a successful transfer. Use OTA one satellite at a time.

## Terminal / Serial Use

Controller serial settings:

- Port: `COM6`
- Baud: `115200`
- Data bits: `8`
- Stop bits: `1`
- Parity: `None`
- Flow control: `None`

The controller boots with streaming disabled, so you get a prompt first.

Recommended terminal helper:

```bash
python pc_logger\controller_terminal.py --port COM6
```

That helper:

- opens the controller serial port
- pushes current PC time into the controller
- lets you type controller commands interactively

You can also use PuTTY or another serial terminal.

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

## Command Notes

### `NODES`

Prints stored node information, including:

- `node_id`
- `name`
- `mac`
- `report_interval_ms`
- `fw_version`
- `rssi_dbm`
- `signal_pct`
- `temp_offset_c`
- `heater_enabled`
- `sample_rate_hz`

### `SETINT`

Changes how often the controller polls a satellite.

Example:

```text
SETINT 1 1000
```

### `SETSAMPLE`

Changes the satellite background sample rate and stores it in flash on the satellite.
The satellite keeps sampling continuously, collapses those samples into `10 ms` chunk
averages, and returns the average of the stored chunks when the controller polls it.

Examples:

```text
SETSAMPLE 1 200
SETSAMPLE ALL 100
```

### `SETTOFF`

Applies a temperature offset on the satellite.

Example:

```text
SETTOFF 1 -1.50
```

### `HEATER`

Turns the SHT85 heater on or off on a satellite.

Examples:

```text
HEATER 1 ON
HEATER 1 OFF
```

### `RENAME`

Renames a satellite from the controller and stores the new name in flash on the satellite,
so it survives power loss and reboot.

Example:

```text
RENAME 1 greenhouse
```

### `STREAM`

- `STREAM ON` enables live JSON telemetry
- `STREAM OFF` stops live telemetry and leaves the prompt quiet

## Example Workflow

### First-time programming

1. Flash the controller over USB.
2. Flash each satellite over USB once.
3. Power all devices.
4. Open the controller terminal.
5. Use `BIND` if needed.
6. Check `NODES`.

### Normal use

1. Connect to the controller.
2. Use `NODES` to inspect satellites.
3. Use `STREAM ON` to watch live data.
4. Use `SETINT`, `SETTOFF`, or `HEATER` as needed.

### Satellite firmware rollout

1. Build `satellite_upload`.
2. OTA one satellite at a time through the controller with `ota_satellite.py`.
3. If the controller firmware changed too, flash the controller over USB last.

If the satellite protocol changed, the safe order is:

1. OTA satellites first while the old controller can still talk to them.
2. Flash the controller last.

If an OTA transfer is interrupted, reset that satellite before retrying.

## Satellite LED Status

The built-in LED on each satellite is used as a simple state indicator:

- unbound: double blink
- bound and idle: short heartbeat pulse
- radio activity such as bind/config/sample/OTA ack traffic: brief solid pulse
- OTA in progress: fast blink
- OTA complete, waiting to reboot: solid on

## Output Format

Example reading:

```json
{"event":"reading","node_id":1,"name":"satellite","temperature_c":22.54,"humidity_pct":47.16,"sensor_ok":true,"fw_version":"2.1","rssi_dbm":-50,"signal_pct":100,"mac":"1C:C3:AB:C2:1E:7C"}
```

Example nodes response:

```json
{"event":"nodes","items":[{"node_id":1,"name":"satellite","mac":"1C:C3:AB:C2:1E:7C","last_seen_ms":12638,"report_interval_ms":1000,"fw_version":"2.1","rssi_dbm":-51,"signal_pct":97,"temp_offset_c":0.00,"heater_enabled":false,"next_poll_in_ms":955}]}
```

## Current Behavior

- Controller stream is off by default at boot.
- Controller polls satellites and spaces polls automatically when multiple satellites are present.
- Satellites sample the SHT85 continuously at a configurable target rate up to `200 Hz`.
- Poll responses are averaged from stored `10 ms` sample chunks, not a single instant read.
- Time can be pushed in from the connected host terminal.
- Satellite firmware version is reported to the controller.
- RSSI is reported as both `rssi_dbm` and `signal_pct`.
- Satellite heater state is controlled by the controller and stored in flash.
