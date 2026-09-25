from __future__ import annotations

import asyncio
import contextlib
import io
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fit_tool.fit_file import FitFile

from sport_sync_bridge.ble_sensors import BleError
from sport_sync_bridge.ble_trainer import RideRunResult, run_trainer_course
from sport_sync_bridge.cli import main
from sport_sync_bridge.virtual_ride import PowerSegment, RideCourse


class BleTrainerCourseTests(unittest.TestCase):
    def _fake_ble(self, *, response_result: int = 0x01, indoor_data_payload: bytes | None = None):
        control_point = SimpleNamespace(uuid="2ad9", properties=["write", "indicate"])
        indoor_data = SimpleNamespace(uuid="2ad2", properties=["notify"])
        characteristics = [control_point]
        if indoor_data_payload is not None:
            characteristics.append(indoor_data)
        service = SimpleNamespace(uuid="1826", characteristics=characteristics)
        client_state = SimpleNamespace(writes=[], stop_count=0, targets=[])

        class FakeScanner:
            @staticmethod
            async def find_device_by_address(address: str, *, timeout: float):
                return SimpleNamespace(address=address)

        class FakeClient:
            def __init__(self, _device, *, timeout: float):
                self.services = [service]
                self._callbacks = {}
                self.is_connected = True

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def start_notify(self, characteristic, callback):
                self._callbacks[characteristic.uuid] = callback
                if characteristic is indoor_data and indoor_data_payload is not None:
                    callback(None, bytearray(indoor_data_payload))

            async def stop_notify(self, _characteristic):
                client_state.stop_count += 1

            async def write_gatt_char(self, _characteristic, payload: bytes, *, response: bool):
                self.assert_ack_write(response)
                command = bytes(payload)
                client_state.writes.append(command)
                result = response_result if command == b"\x00" else 0x01
                self._callbacks[control_point.uuid](None, bytearray((0x80, command[0], result)))

            @staticmethod
            def assert_ack_write(response: bool):
                if not response:
                    raise AssertionError("FTMS control point writes must be acknowledged")

        return FakeScanner, FakeClient, client_state

    def test_runs_course_with_acknowledged_ftms_power_and_resets_target(self) -> None:
        scanner, client, state = self._fake_ble()
        course = RideCourse((PowerSegment(0, 0.02, 150, 150, "steady"),))
        reported: list[tuple[float, int, int]] = []

        asyncio.run(
            run_trainer_course(
                "trainer-id",
                course,
                scanner_type=scanner,
                client_type=client,
                on_target=lambda *values: reported.append(values),
            )
        )

        self.assertEqual(state.writes, [b"\x00", b"\x05\x96\x00", b"\x05\x00\x00"])
        self.assertEqual(state.stop_count, 1)
        self.assertEqual(reported, [(0.0, 0, 150)])

    def test_rejected_control_request_stops_notifications(self) -> None:
        scanner, client, state = self._fake_ble(response_result=0x02)
        course = RideCourse((PowerSegment(0, 0.01, 100, 100),))

        with self.assertRaisesRegex(BleError, "operation not supported"):
            asyncio.run(
                run_trainer_course(
                    "trainer-id",
                    course,
                    scanner_type=scanner,
                    client_type=client,
                )
            )

        self.assertEqual(state.writes, [b"\x00"])
        self.assertEqual(state.stop_count, 1)

    def test_zero_watt_start_is_sent_as_the_initial_course_target(self) -> None:
        scanner, client, state = self._fake_ble()
        course = RideCourse((PowerSegment(0, 0.01, 0, 0),))

        asyncio.run(
            run_trainer_course(
                "trainer-id",
                course,
                scanner_type=scanner,
                client_type=client,
            )
        )

        self.assertEqual(state.writes, [b"\x00", b"\x05\x00\x00"])
        self.assertEqual(state.stop_count, 1)

    def test_live_intensity_control_changes_the_next_ftms_target(self) -> None:
        scanner, client, state = self._fake_ble()
        course = RideCourse((PowerSegment(0, 0.01, 100, 100),))

        asyncio.run(
            run_trainer_course(
                "trainer-id",
                course,
                scanner_type=scanner,
                client_type=client,
                on_control=lambda: "increase",
            )
        )

        self.assertEqual(state.writes, [b"\x00", b"\x05\x69\x00", b"\x05\x00\x00"])

    def test_live_skip_control_moves_to_the_next_segment(self) -> None:
        scanner, client, state = self._fake_ble()
        course = RideCourse(
            (PowerSegment(0, 0.01, 100, 100), PowerSegment(0.01, 0.02, 200, 200))
        )
        targets: list[tuple[float, int, int]] = []
        controls = iter(("skip", None))

        asyncio.run(
            run_trainer_course(
                "trainer-id",
                course,
                scanner_type=scanner,
                client_type=client,
                on_target=lambda *values: targets.append(values),
                on_control=lambda: next(controls),
            )
        )

        self.assertEqual(state.writes, [b"\x00", b"\x05\xc8\x00", b"\x05\x00\x00"])
        self.assertEqual(targets[0][1:], (1, 200))

    def test_pause_resume_freezes_course_timer_and_reduces_trainer_target(self) -> None:
        scanner, client, state = self._fake_ble()
        course = RideCourse((PowerSegment(0, 1.01, 100, 100),))
        controls = iter((None, "pause", None, "pause"))
        pause_states: list[bool] = []

        result = asyncio.run(
            run_trainer_course(
                "trainer-id",
                course,
                scanner_type=scanner,
                client_type=client,
                on_control=lambda: next(controls),
                on_pause=pause_states.append,
            )
        )

        self.assertEqual(
            state.writes,
            [b"\x00", b"\x05\x64\x00", b"\x05\x00\x00", b"\x05\x64\x00", b"\x05\x00\x00"],
        )
        self.assertEqual(pause_states, [True, False])
        self.assertGreater(result.elapsed_time_s, result.timer_time_s + 0.05)

    def test_streams_standard_ftms_indoor_bike_measurements(self) -> None:
        flags = (1 << 2) | (1 << 4) | (1 << 6) | (1 << 9) | (1 << 11)
        payload = struct.pack("<HHH", flags, 250, 180)
        payload += (123456).to_bytes(3, "little") + struct.pack("<hBH", 250, 145, 321)
        scanner, client, state = self._fake_ble(indoor_data_payload=payload)
        course = RideCourse((PowerSegment(0, 0.01, 100, 100),))
        samples: list[dict[str, object]] = []

        asyncio.run(
            run_trainer_course(
                "trainer-id",
                course,
                scanner_type=scanner,
                client_type=client,
                on_measurement=samples.append,
            )
        )

        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["power_w"], 250)
        self.assertEqual(samples[0]["heart_rate_bpm"], 145)
        self.assertEqual(samples[0]["cadence_rpm"], 90.0)
        self.assertEqual(state.stop_count, 2)

    def test_cli_ride_saves_recorded_trainer_data_as_fit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "ride.fit"
            config = SimpleNamespace(
                data_dir=root / ".data",
                log_level="INFO",
                log_path=root / "sync.log",
            )

            async def fake_ride(_address, _course, *, on_tick, on_measurement, **_kwargs):
                on_tick(0)
                on_measurement(
                    {
                        "distance_m": 250,
                        "speed_mps": 7.2,
                        "heart_rate_bpm": 138,
                        "cadence_rpm": 88,
                        "power_w": 175,
                    }
                )
                on_tick(1)
                return RideRunResult(600.0, 600.0)

            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
                patch("sport_sync_bridge.cli.configure_logging"),
                patch("sport_sync_bridge.cli.run_trainer_course", side_effect=fake_ride),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                status = main(
                    ["ble", "trainer", "ride", "trainer-id", "--output", str(output)]
                )

            self.assertEqual(status, 0)
            self.assertTrue(output.is_file())
            self.assertIn("fit_output=", stdout.getvalue())
            decoded = FitFile.from_file(str(output))
            records = [
                record.message
                for record in decoded.records
                if not record.is_definition and record.message.name == "record"
            ]
            telemetry_records = [record for record in records if record.power is not None]
            self.assertEqual(len(telemetry_records), 1)
            self.assertEqual(telemetry_records[0].power, 175)
            self.assertEqual(telemetry_records[0].heart_rate, 138)
            self.assertEqual(telemetry_records[0].cadence, 88)

    def test_rejects_unrepresentable_power_before_scanning(self) -> None:
        scanner, client, state = self._fake_ble()
        scanner_called = False

        async def unexpected_scan(*_args, **_kwargs):
            nonlocal scanner_called
            scanner_called = True
            return SimpleNamespace(address="trainer-id")

        scanner.find_device_by_address = unexpected_scan
        course = RideCourse((PowerSegment(0, 1, 20_000, 20_000),))

        with self.assertRaisesRegex(ValueError, "exceeds 32767"):
            asyncio.run(
                run_trainer_course(
                    "trainer-id",
                    course,
                    intensity=2,
                    scanner_type=scanner,
                    client_type=client,
                )
            )

        self.assertFalse(scanner_called)
        self.assertEqual(state.writes, [])


if __name__ == "__main__":
    unittest.main()
