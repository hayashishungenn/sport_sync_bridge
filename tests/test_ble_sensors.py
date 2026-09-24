from __future__ import annotations

import asyncio
import contextlib
import io
import json
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sport_sync_bridge.ble_sensors import (
    BleDeviceRegistry,
    BleError,
    BleScanResult,
    BleSensorSample,
    HeartRateSample,
    SensorMeasurementDecoder,
    classify_ble_device,
    decode_csc_measurement,
    decode_cycling_power_measurement,
    decode_cycling_power_vector,
    decode_heart_rate_measurement,
    decode_indoor_bike_data,
    decode_rsc_measurement,
    read_battery_level,
    scan_ble_devices,
    stream_heart_rate,
    stream_sensor_data,
    validate_sensor_recording_options,
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

    def test_decodes_running_speed_cadence_fields_and_rejects_truncation(self) -> None:
        payload = bytes([0x07, 0x00, 0x01, 180, 120, 0, 0x39, 0x30, 0, 0])

        measurement = decode_rsc_measurement(payload)

        self.assertEqual(measurement["speed_mps"], 1.0)
        self.assertEqual(measurement["cadence_spm"], 180)
        self.assertEqual(measurement["stride_length_m"], 1.2)
        self.assertEqual(measurement["distance_m"], 1234.5)
        self.assertTrue(measurement["running"])
        with self.assertRaises(ValueError):
            decode_rsc_measurement(bytes([0x01, 0, 1, 180]))

    def test_decodes_csc_counts_and_derives_cadence_speed_and_distance(self) -> None:
        decoder = SensorMeasurementDecoder(wheel_circumference_m=2.1)
        first = bytes([0x03]) + struct.pack("<IHHH", 100, 1024, 50, 2048)
        second = bytes([0x03]) + struct.pack("<IHHH", 102, 3072, 51, 3072)

        with self.assertRaises(ValueError):
            decode_csc_measurement(bytes([0x01, 1]))

        self.assertEqual(decoder.decode("cycling_speed_cadence", first)["distance_m"], 210.0)
        measurement = decoder.decode("cycling_speed_cadence", second)

        self.assertEqual(measurement["speed_mps"], 2.1)
        self.assertEqual(measurement["cadence_rpm"], 60.0)
        self.assertEqual(measurement["distance_m"], 214.2)

    def test_decodes_cycling_power_and_optional_revolution_fields(self) -> None:
        flags = (1 << 0) | (1 << 4) | (1 << 5) | (1 << 11)
        payload = struct.pack("<HhBIHHHH", flags, 250, 80, 1000, 1024, 90, 2048, 12)

        measurement = decode_cycling_power_measurement(payload)

        self.assertEqual(measurement["power_w"], 250)
        self.assertEqual(measurement["pedal_power_balance_percent"], 40.0)
        self.assertEqual(measurement["cumulative_wheel_revolutions"], 1000)
        self.assertEqual(measurement["cumulative_crank_revolutions"], 90)
        self.assertEqual(measurement["accumulated_energy_kj"], 12)
        with self.assertRaises(ValueError):
            decode_cycling_power_measurement(bytes([0x10, 0, 0, 0]))

    def test_decodes_all_cycling_power_optional_field_lengths(self) -> None:
        flags = sum(1 << bit for bit in (2, 6, 7, 8, 9, 10, 12))
        payload = struct.pack("<HhHhhhh", flags, -20, 100, -1, 1, -2, 2)
        payload += bytes([0x34, 0x12, 0x56]) + struct.pack("<HH", 90, 180)

        measurement = decode_cycling_power_measurement(payload)

        self.assertEqual(measurement["power_w"], -20)
        self.assertEqual(measurement["accumulated_torque_raw"], 100)
        self.assertEqual(measurement["extreme_force_maximum_raw"], -1)
        self.assertEqual(measurement["extreme_force_minimum_raw"], 1)
        self.assertEqual(measurement["extreme_torque_maximum_raw"], -2)
        self.assertEqual(measurement["extreme_torque_minimum_raw"], 2)
        self.assertEqual(measurement["extreme_angles_packed_raw"], 0x561234)
        self.assertEqual(measurement["top_dead_spot_angle_raw"], 90)
        self.assertEqual(measurement["bottom_dead_spot_angle_raw"], 180)

    def test_decodes_cycling_power_vector_arrays_and_crank_data(self) -> None:
        flags = 0x01 | 0x02 | 0x08 | 0x10
        payload = struct.pack("<BHHHhh", flags, 100, 1024, 90, -32, 32)

        measurement = decode_cycling_power_vector(payload)
        decoder = SensorMeasurementDecoder()
        decoder.decode("cycling_power_vector", payload)
        next_measurement = decoder.decode(
            "cycling_power_vector",
            struct.pack("<BHHHhh", flags, 101, 2048, 91, -64, 64),
        )

        self.assertEqual(measurement["measurement_direction"], "tangential")
        self.assertEqual(measurement["first_crank_measurement_angle_deg"], 90)
        self.assertEqual(measurement["torque_magnitudes_raw"], [-32, 32])
        self.assertEqual(measurement["torque_magnitudes_nm"], [-1.0, 1.0])
        self.assertEqual(next_measurement["cadence_rpm"], 60.0)

        force_values = decode_cycling_power_vector(bytes([0x04, 100, 0, 0xCE, 0xFF]))
        self.assertEqual(force_values["force_magnitudes_n"], [100, -50])
        self.assertEqual(decode_cycling_power_vector(bytes([0x0C]))["force_magnitudes_n"], [])
        self.assertEqual(decode_cycling_power_vector(bytes([0x0C]))["torque_magnitudes_nm"], [])
        for invalid_payload in (bytes([0x08, 1]), bytes([0x0C, 1, 0]), bytes([0x00, 1, 0])):
            with self.subTest(payload=invalid_payload), self.assertRaises(ValueError):
                decode_cycling_power_vector(invalid_payload)

    def test_decodes_indoor_bike_data_in_flag_order(self) -> None:
        flags = (1 << 2) | (1 << 4) | (1 << 6) | (1 << 9) | (1 << 11)
        payload = struct.pack("<HHH", flags, 250, 180) + (123456).to_bytes(3, "little")
        payload += struct.pack("<hBH", 250, 145, 321)

        measurement = decode_indoor_bike_data(payload)

        self.assertEqual(measurement["speed_mps"], round(250 / 360.0, 6))
        self.assertEqual(measurement["cadence_rpm"], 90.0)
        self.assertEqual(measurement["distance_m"], 123456)
        self.assertEqual(measurement["power_w"], 250)
        self.assertEqual(measurement["heart_rate_bpm"], 145)
        self.assertEqual(measurement["elapsed_time_s"], 321)

        more_data = decode_indoor_bike_data(struct.pack("<Hh", 0x0041, 200))
        self.assertTrue(more_data["more_data"])
        self.assertEqual(more_data["power_w"], 200)
        self.assertNotIn("speed_mps", more_data)
        with self.assertRaises(ValueError):
            decode_indoor_bike_data(bytes([0, 0]))

    def test_decodes_all_optional_indoor_bike_fields(self) -> None:
        flags = sum(1 << bit for bit in range(1, 13))
        payload = struct.pack("<HHHHH", flags, 3600, 720, 180, 160)
        payload += (1000).to_bytes(3, "little")
        payload += struct.pack("<hhhHHBBBHH", -12, 200, 190, 500, 600, 10, 145, 25, 3600, 1800)

        measurement = decode_indoor_bike_data(payload)

        self.assertEqual(measurement["average_speed_mps"], 2.0)
        self.assertEqual(measurement["average_cadence_rpm"], 80.0)
        self.assertEqual(measurement["resistance_level"], -1.2)
        self.assertEqual(measurement["average_power_w"], 190)
        self.assertEqual(measurement["energy_kcal"], 500)
        self.assertEqual(measurement["energy_per_hour_kcal"], 600)
        self.assertEqual(measurement["energy_per_minute_kcal"], 10)
        self.assertEqual(measurement["metabolic_equivalent"], 2.5)
        self.assertEqual(measurement["remaining_time_s"], 1800)

    def test_rejects_invalid_recording_options(self) -> None:
        with self.assertRaises(BleError):
            validate_sensor_recording_options(0, 15, None)
        with self.assertRaises(BleError):
            validate_sensor_recording_options(1, 0, None)
        with self.assertRaises(BleError):
            validate_sensor_recording_options(1, 15, 0)

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

    def test_streams_standard_sensor_measurements_and_stops_all_notifications(self) -> None:
        rsc_characteristic = SimpleNamespace(uuid="2a53")
        power_characteristic = SimpleNamespace(uuid="2a63")
        vector_characteristic = SimpleNamespace(uuid="2a64")
        services = [
            SimpleNamespace(uuid="1814", characteristics=[rsc_characteristic]),
            SimpleNamespace(uuid="1818", characteristics=[power_characteristic, vector_characteristic]),
        ]
        stopped: list[object] = []

        class FakeScanner:
            @staticmethod
            async def find_device_by_address(address: str, *, timeout: float):
                return SimpleNamespace(address=address)

        class FakeClient:
            def __init__(self, device, *, timeout: float):
                self.services = services

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def start_notify(self, characteristic, callback):
                if characteristic is rsc_characteristic:
                    callback(characteristic, bytearray([0, 0, 1, 150]))
                elif characteristic is power_characteristic:
                    callback(characteristic, bytearray(struct.pack("<Hh", 0, 220)))
                elif characteristic is vector_characteristic:
                    callback(characteristic, bytearray([0x04, 100, 0]))
                else:
                    raise AssertionError("Unexpected characteristic")

            async def stop_notify(self, characteristic):
                stopped.append(characteristic)

        async def capture_samples():
            stream = stream_sensor_data(
                "sensor-id",
                10,
                scanner_type=FakeScanner,
                client_type=FakeClient,
            )
            samples = [await anext(stream), await anext(stream), await anext(stream)]
            await stream.aclose()
            return samples

        samples = asyncio.run(capture_samples())

        self.assertEqual(
            [sample.sensor_type for sample in samples],
            ["running_speed_cadence", "cycling_power", "cycling_power_vector"],
        )
        self.assertEqual(samples[0].measurement["cadence_spm"], 150)
        self.assertEqual(samples[1].measurement["power_w"], 220)
        self.assertEqual(stopped, [vector_characteristic, power_characteristic, rsc_characteristic])

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

    def test_cli_records_sensor_measurements_as_json_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = SimpleNamespace(
                data_dir=root / ".data",
                db_path=root / ".data" / "state.db",
                log_level="INFO",
                log_path=root / "sync.log",
            )
            output = root / "sensor.jsonl"

            async def fake_stream(*_args, **_kwargs):
                yield BleSensorSample(
                    "2026-09-24T12:00:00+00:00",
                    "cycling_power",
                    {"power_w": 220},
                )

            stdout = io.StringIO()
            with (
                patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
                patch("sport_sync_bridge.cli.configure_logging"),
                patch("sport_sync_bridge.cli.stream_sensor_data", side_effect=fake_stream),
                contextlib.redirect_stdout(stdout),
            ):
                status = main(
                    ["ble", "record", "sensor-id", "--duration", "1", "--output", str(output)]
                )

            self.assertEqual(status, 0)
            self.assertIn("samples=1", stdout.getvalue())
            record = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(record["sensor_type"], "cycling_power")
            self.assertEqual(record["measurement"]["power_w"], 220)


if __name__ == "__main__":
    unittest.main()
