from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sport_sync_bridge.cli import main


class BlePermissionGuideTests(unittest.TestCase):
    def _run_guide(self, *arguments: str) -> str:
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_dir = Path(temporary_directory) / ".data"
            output = io.StringIO()
            with (
                patch.dict(os.environ, {"SYNC_DATA_DIR": str(data_dir)}),
                patch("sport_sync_bridge.cli.configure_logging"),
                patch("sport_sync_bridge.cli.BleDeviceRegistry", side_effect=AssertionError("registry access")),
                patch("sport_sync_bridge.cli.scan_ble_devices", side_effect=AssertionError("BLE scan")),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(main(["ble", "guide", *arguments]), 0)
            self.assertFalse((data_dir / "ble_devices.json").exists())
            return output.getvalue()

    def test_chinese_guide_explains_sensor_use_permissions_and_skip(self) -> None:
        output = self._run_guide()

        self.assertIn("蓝牙传感器权限", output)
        self.assertIn("心率、功率和踏频", output)
        self.assertIn("系统设置", output)
        self.assertIn("可以跳过", output)

    def test_english_guide_is_available_without_touching_ble(self) -> None:
        output = self._run_guide("--locale", "en")

        self.assertIn("BLE sensor permissions", output)
        self.assertIn("heart rate, power, and cadence", output)
        self.assertIn("Skip this setup", output)
        self.assertIn("cannot open an Android permission prompt", output)


if __name__ == "__main__":
    unittest.main()
