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

    def test_satellite_firmware_has_expected_config_fields(self):
        source = self.read("satellite/satellite.ino")
        for token in ("reportIntervalMs", "sleepEnabled", "nodeId", "temperatureC", "humidityPct"):
            self.assertIn(token, source)

    def test_shared_protocol_header_has_message_types(self):
        header = self.read("shared/protocol.h")
        for token in ("MSG_READING", "MSG_CONFIG_SET", "MSG_CONFIG_ACK", "MSG_RENAME_SET", "MSG_RENAME_ACK"):
            self.assertIn(token, header)


if __name__ == "__main__":
    unittest.main()
