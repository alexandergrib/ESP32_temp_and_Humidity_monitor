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
