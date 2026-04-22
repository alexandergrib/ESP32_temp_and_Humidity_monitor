# Firmware Versions

Track firmware-facing versions here whenever firmware or protocol behavior changes.

| Component | Version | Source | Notes |
| --- | --- | --- | --- |
| Shared ESP-NOW protocol | 9 | `shared/firmware_versions.h` (`FIRMWARE_PROTOCOL_VERSION`) | Must match controller and satellite protocol headers. |
| ESP32 controller firmware | protocol 9 | `controller/controller.ino`, `controller/protocol.h` | Controller uses protocol version 9 and reports satellite firmware versions in JSON. |
| ESP32 satellite firmware | 2.8 | `shared/firmware_versions.h` (`SATELLITE_FW_VERSION_MAJOR`, `SATELLITE_FW_VERSION_MINOR`) | Retries transient failed captures before reporting a sensor-failure heartbeat. |
| Arduino Nano JSON firmware | nano-sth85-json-1.0 | `shared/firmware_versions.h` (`ARDUINO_NANO_FW_VERSION`) | Reported in `arduino_ready` JSON. |

Update this file in the same commit as firmware version bumps.
