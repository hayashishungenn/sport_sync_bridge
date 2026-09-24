from __future__ import annotations

import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sport_sync_bridge.ble_sensors import (
    BleDeviceRegistry,
    BleError,
    BleScanResult,
    HeartRateSample,
    classify_ble_device,
    decode_heart_rate_measurement,
    read_battery_level,
    scan_ble_devices,
    stream_heart_rate,
)
from sport_sync_bridge.cli import main


class BleSensorTests(unittest.TestCase):
    def test_classifies_advertised_services(self) -> None:
        self.assertEqual(classify_ble_device(("0000180d-0000-1000-8000-00805f9b34fb",)), "heart_rate")
        self.assertEqual(classify_ble_device(("1818",)), "cycling_power")
        self.assertEqual(classify_ble_device(("abcd",)), "other")

    def test_decodes_heart_rate_formats_optional_energy_and_rr_intervals(self) -> None:
        one_byte = decode_heart_rate_measurement(bytes([0x00, 152]))
        self.assertEqual(one_byte.heart_rate_bpm, 152)
        self.assertIsNone(one_byte.sensor_contact)

        two_byte = decode_heart_rate_measurement(bytes([0x1F, 0x2C, 0x01, 0x2A, 0x00, 0x04, 0x00]))
        self.assertEqual(two_byte.heart_rate_bpm, 300)
        self.assertTrue(two_byte.sensor_contact)
        self.assertEqual(two_byte.energy_expended_kj, 42)
        self.assertEqual(two_byte.rr_intervals_ms, (3.906,))

    def test_rejects_truncated_heart_rate_values(self) -> None:
        for payload in (b"", bytes([0x01, 0x2C]), bytes([0x08, 90])):
            with self.subTest(payload=payload), self.assertRaises(BleError):
                decode_heart_rate_measurement(payload)

    def test_scans_and_classifies_ble_advertisements(self) -> None:
        device = SimpleNamespace(address="sensor-id", name=None)
        advertisement = SimpleNamespace(
            local_name="HR Sensor",
            service_uuids=["0000180d-0000-1000-8000-00805f9b34fb"],
            rssi=-51,
        )

        class FakeScanner:
            @staticmethod
            async def discover(*, timeout: float, return_adv: bool):
                self.assertEqual(timeout, 3.0)
                self.assertTrue(return_adv)
                return {"sensor-id": (device, advertisement)}

        results = asyncio.run(scan_ble_devices(3.0, scanner_type=FakeScanner))

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].address, "sensor-id")
        self.assertEqual(results[0].name, "HR Sensor")
        self.assertEqual(results[0].rssi, -51)
        self.assertEqual(results[0].device_type, "heart_rate")

    def test_reads_battery_service_percentage(self) -> None:
        characteristic = SimpleNamespace(uuid="2a19")
        service = SimpleNamespace(uuid="180f", characteristics=[characteristic])

        class FakeScanner:
            @staticmethod
            async def find_device_by_address(address: str, *, timeout: float):
                self.assertEqual(address, "sensor-id")
                return SimpleNamespace(address=address)

        class FakeClient:
            def __init__(self, device, *, timeout: float):
                self.services = [service]

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def read_gatt_char(self, value):
                if value is not characteristic:
                    raise AssertionError("Unexpected battery characteristic")
                return bytes([83])

        level = asyncio.run(
            read_battery_level(
                "sensor-id",
                scanner_type=FakeScanner,
                client_type=FakeClient,
            )
        )
        self.assertEqual(level, 83)

    def test_registry_persists_names_battery_and_preferred_device(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = BleDeviceRegistry(Path(directory) / "ble_devices.json")
            result = BleScanResult("sensor-id", "HR Sensor", -51, ("180d", "180f"), "heart_rate")

            registry.add_scan_result(result)
            registry.rename_device("sensor-id", "Chest strap")
            registry.update_battery_level("sensor-id", 83)
            registry.set_preferred_device("sensor-id", "heart_rate")

            device = registry.list_devices()[0]
            saved_json = json.loads(registry.path.read_text(encoding="utf-8"))
            self.assertEqual(device["name"], "Chest strap")
            self.assertEqual(device["original_name"], "HR Sensor")
            self.assertEqual(device["battery_level"], 83)
            self.assertEqual(saved_json["preferred_devices"], {"heart_rate": "sensor-id"})

            registry.remove_device("sensor-id")
            self.assertEqual(registry.list_devices(), ())
            self.assertEqual(json.loads(registry.path.read_text(encoding="utf-8"))["preferred_devices"], {})

    def test_registry_rejects_malformed_device_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ble_devices.json"
            path.write_text(json.dumps({"devices": [{"address": "sensor-id"}]}), encoding="utf-8")

            with self.assertRaises(BleError):
                BleDeviceRegistry(path).list_devices()

    def test_registry_removes_temporary_file_when_atomic_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = BleDeviceRegistry(root / "ble_devices.json")
            result = BleScanResult("sensor-id", "HR Sensor", -51, ("180d",), "heart_rate")

            with patch.object(Path, "replace", side_effect=OSError("replace blocked")):
                with self.assertRaises(BleError):
                    registry.add_scan_result(result)

            self.assertEqual(list(root.iterdir()), [])

    def test_streams_heart_rate_and_stops_notifications_when_closed(self) -> None:
        characteristic = SimpleNamespace(uuid="2a37")
        service = SimpleNamespace(uuid="180d", characteristics=[characteristic])
        client_state = {"stopped": False}

        class FakeScanner:
            @staticmethod
            async def find_device_by_address(address: str, *, timeout: float):
                return SimpleNamespace(address=address)

        class FakeClient:
            services = [service]

            def __init__(self, device, *, timeout: float):
                self.device = device

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def start_notify(self, value, callback):
                self.assert_characteristic(value)
                callback(value, bytearray([0x00, 147]))

            async def stop_notify(self, value):
                self.assert_characteristic(value)
                client_state["stopped"] = True

            @staticmethod
            def assert_characteristic(value):
                if value is not characteristic:
                    raise AssertionError("Unexpected heart rate characteristic")

        async def capture_one_sample():
            stream = stream_heart_rate(
                "sensor-id",
                10,
                scanner_type=FakeScanner,
                client_type=FakeClient,
            )
            sample = await anext(stream)
            await stream.aclose()
            return sample

        sample = asyncio.run(capture_one_sample())

        self.assertEqual(sample.heart_rate_bpm, 147)
        self.assertTrue(client_state["stopped"])

    def test_cli_scan_saves_devices_to_local_registry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = SimpleNamespace(
                data_dir=root / ".data",
                db_path=root / ".data" / "state.db",
                log_level="INFO",
                log_path=root / "sync.log",
            )
            result = BleScanResult("sensor-id", "HR Sensor", -51, ("180d",), "heart_rate")
            stdout = io.StringIO()
            with (
                patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
                patch("sport_sync_bridge.cli.configure_logging"),
                patch("sport_sync_bridge.cli.scan_ble_devices", new=AsyncMock(return_value=(result,))),
                contextlib.redirect_stdout(stdout),
            ):
                status = main(["ble", "scan", "--save"])

            self.assertEqual(status, 0)
            self.assertIn('"saved": true', stdout.getvalue())
            self.assertIn("saved=1", stdout.getvalue())
            registry = BleDeviceRegistry(config.data_dir / "ble_devices.json")
            self.assertEqual(registry.list_devices()[0]["address"], "sensor-id")

    def test_cli_records_heart_rate_samples_to_csv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = SimpleNamespace(
                data_dir=root / ".data",
                db_path=root / ".data" / "state.db",
                log_level="INFO",
                log_path=root / "sync.log",
            )
            output = root / "capture.csv"

            async def fake_stream(*_args, **_kwargs):
                yield HeartRateSample("2026-09-24T12:00:00+00:00", 140, True, None, (857.422,))

            stdout = io.StringIO()
            with (
                patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
                patch("sport_sync_bridge.cli.configure_logging"),
                patch("sport_sync_bridge.cli.stream_heart_rate", side_effect=fake_stream),
                contextlib.redirect_stdout(stdout),
            ):
                status = main(["ble", "heart-rate", "sensor-id", "--duration", "1", "--output", str(output)])

            self.assertEqual(status, 0)
            self.assertIn("samples=1", stdout.getvalue())
            rows = output.read_text(encoding="utf-8").splitlines()
            self.assertIn("heart_rate_bpm", rows[0])
            self.assertIn(",140,True,", rows[1])
            self.assertIn("857.422", rows[1])


if __name__ == "__main__":
    unittest.main()
