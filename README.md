# ESP32 Remote Temperature and Humidity Monitor

## Scope

This design uses:

- **1 x controller ESP32** connected to the PC by **USB serial only**
- **N x satellite ESP32 nodes** connected to **1 x SHT85 sensor each**
- **ESP-NOW** for controller ↔ satellite wireless transport
- **No dependency on a router, external Wi-Fi network, or cloud service**

The design is intended to scale from your current **2 satellites** to more nodes later without changing the controller firmware logic. ESP-NOW is suitable here because it supports direct device-to-device communication without a router, small telemetry packets, and a one-to-many / many-to-one topology on ESP32. The practical payload size is limited, and all nodes must operate on the same Wi‑Fi channel. citeturn514375view0turn649005search3turn649005search6

---

## Assumptions

- Your sensor name **Sensirion SHT85**.
- Each satellite has **one SHT85** connected over I2C.
- The controller does **not** read sensors directly.
- The PC software can read newline-delimited serial output from the controller.

The SHT85 is a high-accuracy digital humidity and temperature sensor in the SHT3x family. It uses I2C and is a good fit for distributed sensor nodes. 

---

## Why ESP-NOW instead of Wi-Fi AP or Bluetooth

### Chosen transport: ESP-NOW

ESP-NOW is the recommended baseline for this project because:

- no router or infrastructure is required
- latency is low
- node count can grow without the controller caring about a fixed number of satellites
- controller can still remain USB-only toward the PC
- implementation is simpler and lighter than maintaining a custom Wi‑Fi AP protocol stack or BLE GATT service

### Not chosen as primary transport

#### SoftAP + TCP/UDP

This is viable, but it adds IP addressing, socket handling, reconnection logic, and AP/client management that is unnecessary for small telemetry packets.

#### BLE

BLE is possible, but multi-node collection, discovery, and characteristic design become more complex than needed for a simple many-to-one telemetry bus.

---

## System architecture

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

### Roles

#### Controller node

- fixed ESP-NOW channel
- receives readings from any bound satellite
- stores node registry in flash (`Preferences`)
- exposes events and telemetry to the PC over USB serial
- accepts simple serial commands such as opening a bind window or changing report interval

#### Satellite node

- reads local SHT85 over I2C
- binds to controller on first boot or after reset
- stores controller MAC, assigned node ID, and report interval in flash
- periodically transmits readings and heartbeat frames

---

## Wireless model

### Channel model

All nodes must use the same Wi-Fi channel. In this starter implementation the channel is hardcoded to:

- **Channel 6**

This is a deliberate simplification. If you later need coexistence with an existing Wi‑Fi network, move all project nodes together to a different fixed channel.

### Addressing model

The controller does **not** have a hardcoded list of satellites.

Instead:

- each satellite starts as **unbound**
- it broadcasts a **bind request**
- the controller accepts it only while the **bind window** is open
- the controller assigns a new `nodeId`
- both ends save the relationship in non-volatile storage

This means the controller logic scales to **dynamic node counts**.

---

## Protocol design

All ESP-NOW frames use a packed binary payload with a common header.

### Common header

| Field | Type | Purpose |
|---|---:|---|
| magic | 4 bytes | Protocol signature: `TMON` |
| version | uint16 | Protocol version |
| type | uint8 | Message type |
| sequence | uint32 | Monotonic sender sequence |
| nodeId | uint32 | Logical node ID |
| uptimeMs | uint32 | Sender uptime |

### Message types

| Type | Direction | Purpose |
|---|---|---|
| `MSG_BIND_REQUEST` | satellite → broadcast | Ask a controller to pair |
| `MSG_BIND_ACK` | controller → satellite | Assign node ID and config |
| `MSG_READING` | satellite → controller | Temperature/humidity payload |
| `MSG_HEARTBEAT` | satellite → controller | Keepalive / health |
| `MSG_CONFIG_SET` | controller → satellite | Update interval |
| `MSG_CONFIG_ACK` | satellite → controller | Confirm config applied |
| `MSG_PING` | controller → satellite | Optional probe |
| `MSG_PONG` | satellite → controller | Reserved for future |

### Reading payload

| Field | Type |
|---|---:|
| temperatureC | float |
| humidityPct | float |
| vbat | float |
| sensorOk | uint8 |
| rssiHint | uint8 |

`vbat` is currently reserved and sent as `NaN` in the starter firmware.

---

## Binding flow

```text
Satellite boots unbound
    ↓
Broadcasts MSG_BIND_REQUEST every 3 s
    ↓
Controller bind window is open
    ↓
Controller assigns next nodeId and stores MAC/name
    ↓
Controller replies with MSG_BIND_ACK
    ↓
Satellite stores controller MAC + nodeId + report interval
    ↓
Satellite starts periodic telemetry
```

### Controller behavior

- bind window opens automatically on boot for 120 s
- can be opened again over serial with `BIND`
- can be closed with `BIND OFF`

### Satellite behavior

- if not bound, it only attempts pairing
- if bound, it skips pairing and starts normal operation

---

## USB serial interface to PC

The controller emits **JSON lines** so a Python logger, terminal app, or GUI can parse them easily.

### Example telemetry output

```json
{"event":"reading","node_id":1,"name":"satellite","temperature_c":24.31,"humidity_pct":46.90,"sensor_ok":true,"mac":"A0:B7:65:11:22:33"}
```

### Example controller events

```json
{"event":"controller_ready","channel":6}
{"event":"node_bound","node_id":1,"name":"satellite","mac":"A0:B7:65:11:22:33"}
{"event":"heartbeat","node_id":1,"name":"satellite","channel":6}
{"event":"config_ack","node_id":1,"report_interval_ms":5000,"applied":true}
```

### Serial commands accepted by controller

| Command | Function |
|---|---|
| `HELP` | Print available commands |
| `NODES` | List known nodes |
| `BIND` | Open bind window for 120 s |
| `BIND OFF` | Close bind window |
| `SETINT <nodeId> <ms>` | Set report interval on a node |

---

## Wiring

## Satellite ESP32 ↔ SHT85

This starter firmware assumes the common ESP32 I2C pins:

- `GPIO21` = SDA
- `GPIO22` = SCL

### Basic wiring

| SHT85 | ESP32 |
|---|---|
| VCC | 3V3 |
| GND | GND |
| SDA | GPIO21 |
| SCL | GPIO22 |

### Notes

- The SHT85 uses I2C and, in common usage, is on address `0x44` with this library family.
- Use **3.3 V**, not 5 V, unless your specific breakout explicitly supports level shifting and 5 V input.
- Keep wires short for clean measurements.
- Put the sensor away from ESP32 heat if you want more accurate temperature readings.

The SHT85 belongs to the SHT3x-compatible family, and Arduino support exists for SHT85/SHT3x devices. citeturn649005search2turn649005search5turn649005search19

---

## Firmware files

```text
esp32_temp_monitor/
├── README.md
├── shared/
│   └── protocol.h
├── controller/
│   └── controller.ino
└── satellite/
    └── satellite.ino
```

### `shared/protocol.h`

Contains:

- protocol constants
- packed message structs
- common helper functions

### `controller/controller.ino`

Implements:

- ESP-NOW receiver
- node registry in flash
- dynamic binding
- serial JSON output
- serial command handling

### `satellite/satellite.ino`

Implements:

- SHT85 reading
- first-time binding
- periodic reading transmit
- heartbeat transmit
- flash-stored binding/configuration

---

## Software dependencies

### Arduino IDE / ESP32 core

You need the ESP32 Arduino core installed. ESP-NOW support is part of the ESP32 Arduino environment. citeturn649005search0turn649005search3turn649005search12

### Arduino libraries

Install:

- **SHT85** by Rob Tillaart, or another compatible SHT85/SHT3x Arduino library

This starter firmware is written against the `SHT85.h` library API. The Arduino library index shows current SHT85 support. citeturn649005search2turn649005search8

---

## Setup procedure

## 1. Install libraries

In Arduino IDE:

- install ESP32 board support
- install the `SHT85` library

## 2. Open firmware

- flash `controller/controller.ino` to the controller ESP32
- flash `satellite/satellite.ino` to each satellite ESP32

## 3. Power and connect

- connect controller ESP32 to the computer over USB
- power satellites over USB or stable 5 V input to their boards
- connect one SHT85 to each satellite board

## 4. Pair satellites

- power controller first
- within the initial 120 s bind window, power a satellite
- satellite broadcasts bind requests until controller accepts
- repeat for each additional satellite

If you miss the window:

- open serial monitor on controller
- send `BIND`
- power or reset the satellite

## 5. Verify operation

On controller serial output you should see:

- `controller_ready`
- `node_bound`
- repeated `reading` events
- periodic `heartbeat` events

---

## Rebinding and replacement

### Replace a satellite board

If a satellite ESP32 is replaced, it will have a different MAC address and should be treated as a new node.

### Force a satellite to forget binding

Current starter firmware does not expose a serial reset command on the satellite. For now, erase flash or modify firmware to clear `Preferences`.

A future extension can add:

- long-press GPIO button to clear binding
- serial command on satellite to factory reset
- controller-side delete-node command

---

## Functions implemented

### Controller functions

- dynamic node registration
- node persistence across power cycles
- JSON serial telemetry export
- simple runtime commands
- bind window control

### Satellite functions

- automatic first-time pairing
- periodic temperature/humidity reporting
- heartbeat reporting
- persistent config storage
- remote report interval update

---

## Known limitations of this starter design

- no encryption enabled on ESP-NOW yet
- fixed channel only
- no battery measurement yet
- no RTC timestamp in the node payload
- no packet retry queue beyond ESP-NOW delivery behavior
- no PC-side logger application included yet
- assumes one SHT85 per satellite

---

## Recommended next upgrades

### Priority 1

- add CRC or application-level integrity field
- add battery voltage measurement on satellites
- add node delete/reset commands
- add PC-side Python logger that stores CSV and plots data

### Priority 2

- add ESP-NOW encryption and key provisioning
- add optional OLED status screen on controller
- add watchdog and sensor fault counters
- add time synchronization from controller to nodes

### Priority 3

- optional Wi‑Fi AP on controller for browser dashboard
- optional PC tray app or desktop GUI
- OTA firmware updates over Wi‑Fi for satellites

---

## Build notes

This is **starter firmware and project infrastructure**, not a finished production product. It is designed to give you:

- a scalable topology
- a concrete protocol
- pairable nodes
- a clean path to logging software on the PC

For your stated requirement, this is the most direct structure:

- **USB only** from controller to computer
- **wireless only** from controller to satellites
- **controller independent of satellite count**

