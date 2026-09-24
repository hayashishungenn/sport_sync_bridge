from __future__ import annotations

import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sport_sync_bridge.ble_bigrun_ecg import (
    BIGRUN_ECG_CONTROL_UUID,
    BIGRUN_ECG_DATA_UUID,
    BIGRUN_ECG_ENABLE_COMMAND,
    BIGRUN_ECG_SERVICE_UUID,
    BigRunEcgFrame,
    stream_bigrun_ecg,
    validate_bigrun_ecg_options,
)
from sport_sync_bridge.ble_sensors import BLE_TYPES, BleError, classify_ble_device
from sport_sync_bridge.cli import main


class BigRunEcgTests(unittest.TestCase):
    def test_identifies_bigrun_service_for_ble_registry(self) -> None:
        self.assertIn("bigrun_ecg", BLE_TYPES)
        self.assertEqual(
            classify_ble_device(("F000EFE0-0451-4000-0000-00000000B000",)),
            "bigrun_ecg",
        )

    def test_rejects_invalid_recording_options(self) -> None:
        for duration, timeout in ((0, 15), (86401, 15), (1, 0)):
            with self.subTest(duration=duration, timeout=timeout), self.assertRaises(BleError):
                validate_bigrun_ecg_options(duration, timeout)

    def test_streams_raw_frames_and_stops_notifications(self) -> None:
        control = SimpleNamespace(uuid=BIGRUN_ECG_CONTROL_UUID, properties=["write"])
        data = SimpleNamespace(uuid=BIGRUN_ECG_DATA_UUID)
        services = [SimpleNamespace(uuid=BIGRUN_ECG_SERVICE_UUID, characteristics=[control, data])]
        calls: list[tuple[object, ...]] = []

        class FakeScanner:
            @staticmethod
            async def find_device_by_address(address: str, *, timeout: float):
                calls.append(("scan", address, timeout))
                return SimpleNamespace(address=address)

        class FakeClient:
            def __init__(self, _device: object, *, timeout: float):
                calls.append(("connect", timeout))
                self.services = services

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def write_gatt_char(self, characteristic, payload, *, response):
                calls.append(("write", characteristic, bytes(payload), response))

            async def start_notify(self, characteristic, callback):
                calls.append(("start", characteristic))
                callback(characteristic, bytearray((0xA1, 0x00, 0x7F, 0xFF)))

            async def stop_notify(self, characteristic):
                calls.append(("stop", characteristic))

        async def capture():
            stream = stream_bigrun_ecg(
                "sensor-id",
                10,
                scanner_type=FakeScanner,
                client_type=FakeClient,
            )
            frame = await anext(stream)
            await stream.aclose()
            return frame

        frame = asyncio.run(capture())

        self.assertEqual(frame.payload_length, 4)
        self.assertEqual(frame.payload_hex, "a1007fff")
        self.assertEqual(calls[2], ("write", control, BIGRUN_ECG_ENABLE_COMMAND, True))
        self.assertEqual(calls[3], ("start", data))
        self.assertEqual(calls[4], ("stop", data))

    def test_rejects_nonwritable_control_characteristic(self) -> None:
        control = SimpleNamespace(uuid=BIGRUN_ECG_CONTROL_UUID, properties=["read"])
        data = SimpleNamespace(uuid=BIGRUN_ECG_DATA_UUID)
        services = [SimpleNamespace(uuid=BIGRUN_ECG_SERVICE_UUID, characteristics=[control, data])]

        class FakeScanner:
            @staticmethod
            async def find_device_by_address(_address: str, *, timeout: float):
                return SimpleNamespace(address="sensor-id")

        class FakeClient:
            def __init__(self, _device: object, *, timeout: float):
                self.services = services

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

        async def capture():
            stream = stream_bigrun_ecg(
                "sensor-id",
                1,
                scanner_type=FakeScanner,
                client_type=FakeClient,
            )
            with self.assertRaises(BleError):
                await anext(stream)

        asyncio.run(capture())

    def test_cli_records_raw_frames_as_json_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = SimpleNamespace(
                data_dir=root / ".data",
                db_path=root / ".data" / "state.db",
                log_level="INFO",
                log_path=root / "sync.log",
            )
            output = root / "ecg.jsonl"

            async def fake_stream(*_args, **_kwargs):
                yield BigRunEcgFrame("2026-09-24T12:00:00+00:00", 3, "010203")

            stdout = io.StringIO()
            with (
                patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
                patch("sport_sync_bridge.cli.configure_logging"),
                patch("sport_sync_bridge.cli.stream_bigrun_ecg", side_effect=fake_stream),
                contextlib.redirect_stdout(stdout),
            ):
                status = main(
                    ["ble", "bigrun-ecg", "sensor-id", "--duration", "1", "--output", str(output)]
                )

            self.assertEqual(status, 0)
            self.assertIn("frames=1", stdout.getvalue())
            record = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(record["payload_hex"], "010203")
            self.assertEqual(record["payload_length"], 3)


if __name__ == "__main__":
    unittest.main()
