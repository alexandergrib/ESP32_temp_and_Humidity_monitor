import re
import unittest

from tests.support.path_setup import REPO_ROOT


class FirmwareStaticContractTests(unittest.TestCase):
    def read(self, relative_path):
        return (REPO_ROOT / relative_path).read_text(encoding="utf-8", errors="ignore")

    def test_platformio_environments_exist(self):
        ini = self.read("platformio.ini")
        self.assertIn("[env:controller_upload]", ini)
        self.assertIn("[env:satellite_upload]", ini)
        self.assertIn("monitor_speed = 460800", ini)

    def test_controller_emits_logger_contract_events(self):
        source = self.read("controller/controller.ino")
        for event_name in (
            "controller_ready",
            "node_bound",
            "node_online",
            "node_offline",
            "reading",
            "config_ack",
            "rename_ack",
            "nodes",
        ):
            self.assertIn(event_name, source)

    def test_controller_accepts_logger_commands(self):
        source = self.read("controller/controller.ino")
        for command in ("STREAM", "SETINT", "NODES", "TIME", "RENAME", "SLEEP"):
            self.assertIn(command, source)

    def test_controller_owns_satellite_presence_decisions(self):
        source = self.read("controller/controller.ino")

        self.assertIn("bool online = false", source)
        self.assertIn("nodePresenceTimeoutMs", source)
        self.assertIn("checkNodePresence();", source)
        self.assertIn("\"node_offline\"", source)
        self.assertIn("\"node_online\"", source)

    def test_ota_suppresses_normal_readings(self):
        controller = self.read("controller/controller.ino")
        satellite = self.read("satellite/satellite.ino")

        self.assertIn("suppressNormalReading", controller)
        self.assertIn("effectiveOtaReady(nodes[idx])", controller)
        self.assertIn("effectiveOtaPause(nodes[idx])", controller)
        self.assertIn("clearBufferedReadings();", satellite)
        self.assertRegex(
            satellite,
            re.compile(r"otaState\.active\s*=\s*true;.*clearBufferedReadings\(\);", re.DOTALL),
        )

    def test_satellite_firmware_has_expected_config_fields(self):
        source = self.read("satellite/satellite.ino")
        for token in ("reportIntervalMs", "sleepEnabled", "nodeId", "temperatureC", "humidityPct"):
            self.assertIn(token, source)

    def test_satellite_sensor_capture_has_wake_margin_and_valid_reading_gate(self):
        controller = self.read("controller/controller.ino")
        satellite = self.read("satellite/satellite.ino")
        shared_protocol = self.read("shared/protocol.h")

        self.assertIn("SATELLITE_AVERAGING_WINDOW_MIN_MS = 1000", controller)
        self.assertIn("SATELLITE_WAKE_MARGIN_MS = 500", controller)
        self.assertIn("DEFAULT_SAMPLE_RATE_HZ = 5", shared_protocol)
        self.assertIn("MAX_SAMPLE_RATE_HZ     = 5", shared_protocol)
        self.assertIn("SHT85_FAST_LOW_REPEATABILITY_MODE = false", satellite)
        self.assertIn("SHT85_SKIP_CRC_CHECK = false", satellite)
        self.assertIn("SENSOR_AVERAGING_WINDOW_MIN_MS = 1000", satellite)
        self.assertIn("SENSOR_WAKE_MARGIN_MS = 500", satellite)
        self.assertIn("SENSOR_MIN_SAMPLE_PERIOD_US = 200000", satellite)
        self.assertIn("SENSOR_ADAPTIVE_BACKOFF_MAX_PERIOD_US = 500000", satellite)
        self.assertIn("sht.requestData(SHT85_FAST_LOW_REPEATABILITY_MODE)", satellite)
        self.assertIn("sht.dataReady(SHT85_FAST_LOW_REPEATABILITY_MODE)", satellite)
        self.assertIn("sht.readData(SHT85_SKIP_CRC_CHECK)", satellite)
        self.assertIn("return max(sensorSampler.effectiveSamplePeriodUs, requestedSamplePeriodUs());", satellite)
        self.assertIn("max<uint32_t>(SENSOR_AVERAGING_WINDOW_MIN_MS, scaledWindowMs)", satellite)
        self.assertIn("SENSOR_WAKE_MARGIN_MS + sensorAveragingWindowMs()", satellite)
        self.assertIn("if (!sensorOk)", satellite)
        self.assertIn("SENSOR_FAILED_CAPTURE_RETRY_MS = 1000", satellite)
        self.assertIn("SENSOR_FAILED_CAPTURE_HEARTBEAT_THRESHOLD = 3", satellite)
        self.assertIn("Sensor read failed: retrying before reporting heartbeat", satellite)
        self.assertIn("Sensor read failed repeatedly: sending heartbeat without measurement", satellite)
        self.assertIn("dropBufferedSensorFailureReadings();", satellite)
        self.assertIn("enqueueBufferedReading(temperatureC, humidityPct, sensorOk)", satellite)

    def test_satellite_stays_awake_until_reading_ack(self):
        satellite = self.read("satellite/satellite.ino")

        self.assertIn("READING_ACK_RECOVERY_RETRY_MS = 1000", satellite)
        self.assertIn("READING_ACK_STAY_AWAKE_MS = 2000", satellite)
        self.assertIn("stayAwakeForController(READING_ACK_STAY_AWAKE_MS);", satellite)
        self.assertRegex(
            satellite,
            re.compile(r"if \(BufferedReading\* pending = oldestBufferedReading\(\)\).*?return;", re.DOTALL),
        )

    def test_bound_satellite_rebinds_until_controller_contact_confirmed(self):
        satellite = self.read("satellite/satellite.ino")

        self.assertIn("lastControllerContactAtMs = 0;", satellite)
        self.assertIn("pendingSettingsSync = true;", satellite)
        self.assertIn("lastControllerContactAtMs != 0", satellite)
        self.assertIn("stayAwakeForController(CONTROLLER_AWAKE_WINDOW_MS);", satellite)

    def test_sleep_mode_requires_long_report_interval(self):
        shared_protocol = self.read("shared/protocol.h")
        controller = self.read("controller/controller.ino")
        satellite = self.read("satellite/satellite.ino")

        self.assertIn("MIN_SLEEP_REPORT_INTERVAL_MS = 30000", shared_protocol)
        self.assertIn("sleepAllowedForReportInterval", controller)
        self.assertIn("interval_too_short", controller)
        self.assertIn("sleepAllowedForReportInterval", satellite)
        self.assertIn("effectiveSleepEnabled()", satellite)

    def test_shared_protocol_header_has_message_types(self):
        header = self.read("shared/protocol.h")
        for token in ("MSG_READING", "MSG_CONFIG_SET", "MSG_CONFIG_ACK", "MSG_RENAME_SET", "MSG_RENAME_ACK"):
            self.assertIn(token, header)

    def test_firmware_versions_manifest_matches_sources(self):
        manifest = self.read("FIRMWARE_VERSIONS.md")
        version_header = self.read("shared/firmware_versions.h")
        shared_protocol = self.read("shared/protocol.h")
        controller_protocol = self.read("controller/protocol.h")
        satellite_protocol = self.read("satellite/protocol.h")
        satellite = self.read("satellite/satellite.ino")
        nano = self.read("arduino nano/Temp_Humidity_Sensing/Temp_Humidity_Sensing.ino")

        protocol_match = re.search(r"FIRMWARE_PROTOCOL_VERSION\s*=\s*(\d+)", version_header)
        sat_major_match = re.search(r"SATELLITE_FW_VERSION_MAJOR\s*=\s*(\d+)", version_header)
        sat_minor_match = re.search(r"SATELLITE_FW_VERSION_MINOR\s*=\s*(\d+)", version_header)
        nano_match = re.search(r'ARDUINO_NANO_FW_VERSION\[\]\s*=\s*"([^"]+)"', version_header)

        self.assertIsNotNone(protocol_match)
        self.assertIsNotNone(sat_major_match)
        self.assertIsNotNone(sat_minor_match)
        self.assertIsNotNone(nano_match)
        for protocol in (shared_protocol, controller_protocol, satellite_protocol):
            self.assertIn("firmware_versions.h", protocol)
            self.assertIn("PROTOCOL_VERSION = FIRMWARE_PROTOCOL_VERSION", protocol)
        self.assertIn("SATELLITE_FW_VERSION_MAJOR", satellite)
        self.assertIn("SATELLITE_FW_VERSION_MINOR", satellite)
        self.assertIn("proto::ARDUINO_NANO_FW_VERSION", nano)
        self.assertIn("protocol {0}".format(protocol_match.group(1)), manifest)
        self.assertIn(
            "{0}.{1}".format(sat_major_match.group(1), sat_minor_match.group(1)),
            manifest,
        )
        self.assertIn(nano_match.group(1), manifest)


if __name__ == "__main__":
    unittest.main()
