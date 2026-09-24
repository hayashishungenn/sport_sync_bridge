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
    BIGRUN_ECG_MODES,
    BIGRUN_ECG_SERVICE_UUID,
    BigRunEcgFrame,
    build_bigrun_ecg_mode_packets,
    set_bigrun_ecg_work_mode,
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

    def test_builds_mode_packets_for_all_supported_modes(self) -> None:
        mode_values = {
            "standard": (0, 0, 0),
            "hrv": (0, 2, 2),
            "ecg": (2, 0, 2),
        }

        self.assertEqual(BIGRUN_ECG_MODES, tuple(mode_values))
        for mode, values in mode_values.items():
            with self.subTest(mode=mode):
                packets = build_bigrun_ecg_mode_packets(mode)
                self.assertEqual(len(packets), 3)
                self.assertEqual(
                    packets,
                    tuple(
                        bytes((0x30, 0x1E, command, 0, 0, 0, 2, value)) + bytes(12)
                        for command, value in zip((0xC0, 0xC4, 0xC2), values, strict=True)
                    ),
                )
                self.assertTrue(all(len(packet) == 20 for packet in packets))

    def test_rejects_unknown_mode(self) -> None:
        with self.assertRaisesRegex(BleError, "standard, hrv, ecg"):
            build_bigrun_ecg_mode_packets("sleep")

    def test_sets_mode_with_three_writes_and_protocol_delays(self) -> None:
        control = SimpleNamespace(uuid=BIGRUN_ECG_CONTROL_UUID, properties=["write"])
        services = [SimpleNamespace(uuid=BIGRUN_ECG_SERVICE_UUID, characteristics=[control])]
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
                calls.append(("disconnect",))

            async def write_gatt_char(self, characteristic, payload, *, response):
                calls.append(("write", characteristic, bytes(payload), response))

        async def fake_sleep(seconds: float) -> None:
            calls.append(("sleep", seconds))

        with patch("sport_sync_bridge.ble_bigrun_ecg.asyncio.sleep", new=fake_sleep):
            asyncio.run(
                set_bigrun_ecg_work_mode(
                    "sensor-id",
                    "hrv",
                    timeout=7,
                    scanner_type=FakeScanner,
                    client_type=FakeClient,
                )
            )

        writes = [call for call in calls if call[0] == "write"]
        self.assertEqual([call[2] for call in writes], list(build_bigrun_ecg_mode_packets("hrv")))
        self.assertTrue(all(call[1] is control and call[3] is True for call in writes))
        self.assertEqual([call for call in calls if call[0] == "sleep"], [("sleep", 0.05)] * 2)
        self.assertEqual(calls[-1], ("disconnect",))

    def test_sets_mode_without_response_when_only_that_write_is_supported(self) -> None:
        control = SimpleNamespace(
            uuid=BIGRUN_ECG_CONTROL_UUID,
            properties=["write-without-response"],
        )
        services = [SimpleNamespace(uuid=BIGRUN_ECG_SERVICE_UUID, characteristics=[control])]
        responses: list[bool] = []

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

            async def write_gatt_char(self, _characteristic, _payload, *, response):
                responses.append(response)

        async def fake_sleep(_seconds: float) -> None:
            return None

        with patch("sport_sync_bridge.ble_bigrun_ecg.asyncio.sleep", new=fake_sleep):
            asyncio.run(
                set_bigrun_ecg_work_mode(
                    "sensor-id",
                    "standard",
                    scanner_type=FakeScanner,
                    client_type=FakeClient,
                )
            )

        self.assertEqual(responses, [False, False, False])

    def test_cli_sets_bigrun_ecg_work_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = SimpleNamespace(
                data_dir=root / ".data",
                db_path=root / ".data" / "state.db",
                log_level="INFO",
                log_path=root / "sync.log",
            )
            calls: list[tuple[object, ...]] = []

            async def fake_set_mode(address: str, mode: str, timeout: float) -> None:
                calls.append((address, mode, timeout))

            stdout = io.StringIO()
            with (
                patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
                patch("sport_sync_bridge.cli.configure_logging"),
                patch("sport_sync_bridge.cli.set_bigrun_ecg_work_mode", side_effect=fake_set_mode),
                contextlib.redirect_stdout(stdout),
            ):
                status = main(["ble", "bigrun-ecg-mode", "sensor-id", "hrv", "--timeout", "7"])

            self.assertEqual(status, 0)
            self.assertEqual(calls, [("sensor-id", "hrv", 7.0)])
            self.assertIn("mode=hrv", stdout.getvalue())

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
