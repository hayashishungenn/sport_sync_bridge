from __future__ import annotations

import asyncio
import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from .ble_measurements import (
    SENSOR_CHARACTERISTICS,
    BleMeasurementError,
    BleSensorSample,
    SensorMeasurementDecoder,
    decode_csc_measurement,
    decode_cycling_power_measurement,
    decode_cycling_power_vector,
    decode_indoor_bike_data,
    decode_rsc_measurement,
)


BLE_TYPES = {
    "heart_rate",
    "running_speed_cadence",
    "cycling_speed_cadence",
    "cycling_power",
    "fitness_machine",
    "other",
}

_SERVICE_TYPES = {
    "180d": "heart_rate",
    "1814": "running_speed_cadence",
    "1816": "cycling_speed_cadence",
    "1818": "cycling_power",
    "1826": "fitness_machine",
}
_BATTERY_SERVICE_UUID = "180f"
_BATTERY_LEVEL_UUID = "2a19"
_HEART_RATE_SERVICE_UUID = "180d"
_HEART_RATE_MEASUREMENT_UUID = "2a37"


class BleError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class BleScanResult:
    address: str
    name: str
    rssi: int | None
    service_uuids: tuple[str, ...]
    device_type: str


@dataclass(frozen=True, slots=True)
class HeartRateMeasurement:
    heart_rate_bpm: int
    sensor_contact: bool | None
    energy_expended_kj: int | None
    rr_intervals_ms: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class HeartRateSample:
    timestamp: str
    heart_rate_bpm: int
    sensor_contact: bool | None
    energy_expended_kj: int | None
    rr_intervals_ms: tuple[float, ...]


class BleDeviceRegistry:
    def __init__(self, path: Path):
        self.path = path

    def list_devices(self) -> tuple[dict[str, object], ...]:
        devices, _ = self._read()
        return tuple(sorted(devices.values(), key=lambda item: str(item["name"]).casefold()))

    def add_scan_result(self, result: BleScanResult) -> dict[str, object]:
        devices, preferred = self._read()
        existing = devices.get(result.address, {})
        original_name = result.name or "Unknown"
        item = {
            "address": result.address,
            "name": existing.get("name") or original_name,
            "original_name": existing.get("original_name") or original_name,
            "type": existing.get("type") or result.device_type,
            "service_uuids": list(result.service_uuids),
            "last_connected": existing.get("last_connected"),
            "battery_level": existing.get("battery_level"),
        }
        devices[result.address] = item
        self._write(devices, preferred)
        return item

    def rename_device(self, address: str, name: str) -> None:
        normalized_name = name.strip()
        if not normalized_name or any(ord(character) < 32 for character in normalized_name):
            raise BleError("Device name must be non-empty and contain no control characters")
        devices, preferred = self._read()
        if address not in devices:
            raise BleError(f"Saved BLE device was not found: {address}")
        devices[address]["name"] = normalized_name
        self._write(devices, preferred)

    def remove_device(self, address: str) -> None:
        devices, preferred = self._read()
        if address not in devices:
            raise BleError(f"Saved BLE device was not found: {address}")
        del devices[address]
        preferred = {device_type: device_id for device_type, device_id in preferred.items() if device_id != address}
        self._write(devices, preferred)

    def set_preferred_device(self, address: str, device_type: str) -> None:
        if device_type not in BLE_TYPES:
            raise BleError(f"Unsupported BLE device type: {device_type}")
        devices, preferred = self._read()
        if address not in devices:
            raise BleError(f"Saved BLE device was not found: {address}")
        preferred[device_type] = address
        self._write(devices, preferred)

    def update_battery_level(self, address: str, battery_level: int) -> None:
        if not 0 <= battery_level <= 100:
            raise BleError("Battery level must be between 0 and 100 percent")
        self._update_existing(address, "battery_level", battery_level)

    def update_last_connected(self, address: str) -> None:
        self._update_existing(address, "last_connected", datetime.now(timezone.utc).isoformat())

    def _update_existing(self, address: str, key: str, value: object) -> None:
        devices, preferred = self._read()
        if address not in devices:
            return
        devices[address][key] = value
        self._write(devices, preferred)

    def _read(self) -> tuple[dict[str, dict[str, object]], dict[str, str]]:
        if not self.path.exists():
            return {}, {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise BleError(f"Could not read saved BLE devices ({type(exc).__name__})") from exc
        if not isinstance(payload, dict):
            raise BleError("Saved BLE device file has an invalid structure")
        raw_devices = payload.get("devices", [])
        raw_preferred = payload.get("preferred_devices", {})
        if not isinstance(raw_devices, list) or not isinstance(raw_preferred, dict):
            raise BleError("Saved BLE device file has an invalid structure")
        devices: dict[str, dict[str, object]] = {}
        for item in raw_devices:
            if not isinstance(item, dict):
                raise BleError("Saved BLE device entry has an invalid structure")
            address = item.get("address")
            name = item.get("name")
            original_name = item.get("original_name")
            device_type = item.get("type")
            service_uuids = item.get("service_uuids")
            if (
                not isinstance(address, str)
                or not address
                or not isinstance(name, str)
                or not isinstance(original_name, str)
                or not isinstance(device_type, str)
                or not isinstance(service_uuids, list)
                or any(not isinstance(value, str) for value in service_uuids)
            ):
                raise BleError("Saved BLE device entry has an invalid structure")
            devices[address] = item
        preferred: dict[str, str] = {}
        for device_type, address in raw_preferred.items():
            if not isinstance(device_type, str) or not isinstance(address, str):
                raise BleError("Saved preferred BLE device has an invalid structure")
            preferred[device_type] = address
        return devices, preferred

    def _write(self, devices: dict[str, dict[str, object]], preferred: dict[str, str]) -> None:
        payload = {
            "devices": sorted(devices.values(), key=lambda item: str(item["address"]).casefold()),
            "preferred_devices": preferred,
        }
        descriptor: int | None = None
        temporary_path: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temp_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
            )
            temporary_path = Path(temp_name)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                descriptor = None
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary_path.replace(self.path)
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise BleError(f"Could not save BLE devices ({type(exc).__name__})") from exc


def normalize_ble_uuid(value: str) -> str:
    compact = value.strip().lower().replace("-", "")
    if len(compact) == 4:
        return compact
    base_suffix = "00001000800000805f9b34fb"
    if len(compact) == 32 and compact.startswith("0000") and compact.endswith(base_suffix):
        return compact[4:8]
    return compact


def classify_ble_device(service_uuids: tuple[str, ...]) -> str:
    normalized = {normalize_ble_uuid(value) for value in service_uuids}
    for service_uuid, device_type in _SERVICE_TYPES.items():
        if service_uuid in normalized:
            return device_type
    return "other"


def decode_heart_rate_measurement(payload: bytes | bytearray) -> HeartRateMeasurement:
    data = bytes(payload)
    if len(data) < 2:
        raise BleError("Heart Rate Measurement payload is too short")
    flags = data[0]
    offset = 1
    if flags & 0x01:
        if len(data) < offset + 2:
            raise BleError("Heart Rate Measurement is missing its 16-bit value")
        heart_rate_bpm = int.from_bytes(data[offset : offset + 2], "little")
        offset += 2
    else:
        heart_rate_bpm = data[offset]
        offset += 1

    sensor_contact = bool(flags & 0x02) if flags & 0x04 else None
    energy_expended_kj = None
    if flags & 0x08:
        if len(data) < offset + 2:
            raise BleError("Heart Rate Measurement is missing its energy field")
        energy_expended_kj = int.from_bytes(data[offset : offset + 2], "little")
        offset += 2

    rr_intervals_ms: tuple[float, ...] = ()
    if flags & 0x10:
        remaining = data[offset:]
        if len(remaining) % 2:
            raise BleError("Heart Rate Measurement has a truncated RR interval")
        rr_intervals_ms = tuple(
            round(int.from_bytes(remaining[index : index + 2], "little") * 1000 / 1024, 3)
            for index in range(0, len(remaining), 2)
        )
    return HeartRateMeasurement(heart_rate_bpm, sensor_contact, energy_expended_kj, rr_intervals_ms)


async def scan_ble_devices(
    timeout: float = 8.0,
    *,
    scanner_type: object | None = None,
) -> tuple[BleScanResult, ...]:
    _validate_timeout(timeout)
    if scanner_type is None:
        _, scanner_type = _load_bleak()
    try:
        scanned = await scanner_type.discover(timeout=timeout, return_adv=True)
    except Exception as exc:
        raise BleError(f"BLE scan failed ({type(exc).__name__})") from exc

    results: list[BleScanResult] = []
    for device, advertisement in scanned.values():
        service_uuids = tuple(
            sorted({normalize_ble_uuid(str(value)) for value in (advertisement.service_uuids or [])})
        )
        name = device.name or advertisement.local_name or "Unknown"
        results.append(
            BleScanResult(
                address=str(device.address),
                name=str(name),
                rssi=int(advertisement.rssi) if advertisement.rssi is not None else None,
                service_uuids=service_uuids,
                device_type=classify_ble_device(service_uuids),
            )
        )
    return tuple(sorted(results, key=lambda item: ((item.name or "").casefold(), item.address.casefold())))


async def read_battery_level(
    address: str,
    timeout: float = 15.0,
    *,
    scanner_type: object | None = None,
    client_type: object | None = None,
) -> int:
    _validate_timeout(timeout)
    if scanner_type is None or client_type is None:
        loaded_client, loaded_scanner = _load_bleak()
        scanner_type = scanner_type or loaded_scanner
        client_type = client_type or loaded_client
    try:
        device = await scanner_type.find_device_by_address(address, timeout=timeout)
        if device is None:
            raise BleError(f"BLE device was not found: {address}")
        async with client_type(device, timeout=timeout) as client:
            characteristic = _find_characteristic(client, _BATTERY_SERVICE_UUID, _BATTERY_LEVEL_UUID)
            if characteristic is None:
                raise BleError(f"Device does not expose Battery Service 2A19: {address}")
            value = await client.read_gatt_char(characteristic)
        if not value:
            raise BleError(f"Battery Service returned no value: {address}")
        battery_level = int(value[0])
        if battery_level > 100:
            raise BleError(f"Battery Service returned an invalid percentage: {address}")
        return battery_level
    except BleError:
        raise
    except Exception as exc:
        raise BleError(f"Could not read BLE battery level ({type(exc).__name__})") from exc


async def stream_heart_rate(
    address: str,
    duration_seconds: float,
    timeout: float = 15.0,
    *,
    scanner_type: object | None = None,
    client_type: object | None = None,
) -> AsyncIterator[HeartRateSample]:
    _validate_timeout(timeout)
    if not math.isfinite(duration_seconds) or not 1 <= duration_seconds <= 86400:
        raise BleError("Heart rate duration must be between 1 second and 24 hours")
    if scanner_type is None or client_type is None:
        loaded_client, loaded_scanner = _load_bleak()
        scanner_type = scanner_type or loaded_scanner
        client_type = client_type or loaded_client

    try:
        device = await scanner_type.find_device_by_address(address, timeout=timeout)
        if device is None:
            raise BleError(f"BLE device was not found: {address}")
        async with client_type(device, timeout=timeout) as client:
            characteristic = _find_characteristic(
                client,
                _HEART_RATE_SERVICE_UUID,
                _HEART_RATE_MEASUREMENT_UUID,
            )
            if characteristic is None:
                raise BleError(f"Device does not expose Heart Rate Service 2A37: {address}")
            queue: asyncio.Queue[HeartRateSample | Exception] = asyncio.Queue()

            def on_measurement(_characteristic: object, payload: bytearray) -> None:
                try:
                    measurement = decode_heart_rate_measurement(payload)
                    queue.put_nowait(
                        HeartRateSample(
                            datetime.now(timezone.utc).isoformat(),
                            measurement.heart_rate_bpm,
                            measurement.sensor_contact,
                            measurement.energy_expended_kj,
                            measurement.rr_intervals_ms,
                        )
                    )
                except Exception as exc:
                    queue.put_nowait(exc)

            await client.start_notify(characteristic, on_measurement)
            deadline = asyncio.get_running_loop().time() + duration_seconds
            try:
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        return
                    try:
                        result = await asyncio.wait_for(queue.get(), timeout=remaining)
                    except TimeoutError:
                        return
                    if isinstance(result, Exception):
                        raise BleError(f"Invalid Heart Rate Measurement ({type(result).__name__})") from result
                    yield result
            finally:
                await client.stop_notify(characteristic)
    except BleError:
        raise
    except Exception as exc:
        raise BleError(f"Could not stream BLE heart rate ({type(exc).__name__})") from exc


async def stream_sensor_data(
    address: str,
    duration_seconds: float,
    timeout: float = 15.0,
    *,
    wheel_circumference_m: float | None = None,
    scanner_type: object | None = None,
    client_type: object | None = None,
) -> AsyncIterator[BleSensorSample]:
    validate_sensor_recording_options(duration_seconds, timeout, wheel_circumference_m)
    decoder = SensorMeasurementDecoder(wheel_circumference_m)
    if scanner_type is None or client_type is None:
        loaded_client, loaded_scanner = _load_bleak()
        scanner_type = scanner_type or loaded_scanner
        client_type = client_type or loaded_client

    try:
        device = await scanner_type.find_device_by_address(address, timeout=timeout)
        if device is None:
            raise BleError(f"BLE device was not found: {address}")
        async with client_type(device, timeout=timeout) as client:
            available = [
                (sensor_type, characteristic)
                for sensor_type, (service_uuid, characteristic_uuid) in SENSOR_CHARACTERISTICS.items()
                if (
                    characteristic := _find_characteristic(client, service_uuid, characteristic_uuid)
                ) is not None
            ]
            if not available:
                raise BleError(f"Device does not expose a supported BLE measurement characteristic: {address}")

            queue: asyncio.Queue[BleSensorSample | tuple[str, Exception]] = asyncio.Queue()

            def make_callback(sensor_type: str):
                def on_measurement(_characteristic: object, payload: bytearray) -> None:
                    try:
                        if sensor_type == "heart_rate":
                            measurement = decode_heart_rate_measurement(payload)
                            fields: dict[str, object] = {
                                "heart_rate_bpm": measurement.heart_rate_bpm,
                                "sensor_contact": measurement.sensor_contact,
                                "energy_expended_kj": measurement.energy_expended_kj,
                                "rr_intervals_ms": list(measurement.rr_intervals_ms),
                            }
                        else:
                            fields = decoder.decode(sensor_type, payload)
                        queue.put_nowait(
                            BleSensorSample(
                                datetime.now(timezone.utc).isoformat(),
                                sensor_type,
                                fields,
                            )
                        )
                    except Exception as exc:
                        queue.put_nowait((sensor_type, exc))

                return on_measurement

            started: list[object] = []
            try:
                for _sensor_type, characteristic in available:
                    await client.start_notify(characteristic, make_callback(_sensor_type))
                    started.append(characteristic)

                deadline = asyncio.get_running_loop().time() + duration_seconds
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        return
                    try:
                        result = await asyncio.wait_for(queue.get(), timeout=remaining)
                    except TimeoutError:
                        return
                    if isinstance(result, tuple):
                        sensor_type, error = result
                        raise BleError(
                            f"Invalid {sensor_type} measurement ({type(error).__name__})"
                        ) from error
                    yield result
            finally:
                for characteristic in reversed(started):
                    await client.stop_notify(characteristic)
    except BleError:
        raise
    except Exception as exc:
        raise BleError(f"Could not stream BLE sensor data ({type(exc).__name__})") from exc


def validate_sensor_recording_options(
    duration_seconds: float,
    timeout: float,
    wheel_circumference_m: float | None,
) -> None:
    _validate_timeout(timeout)
    if not math.isfinite(duration_seconds) or not 1 <= duration_seconds <= 86400:
        raise BleError("Sensor recording duration must be between 1 second and 24 hours")
    try:
        SensorMeasurementDecoder(wheel_circumference_m)
    except BleMeasurementError as exc:
        raise BleError(str(exc)) from exc


def _find_characteristic(client: object, service_uuid: str, characteristic_uuid: str):
    for service in client.services:
        if normalize_ble_uuid(str(service.uuid)) != service_uuid:
            continue
        for characteristic in service.characteristics:
            if normalize_ble_uuid(str(characteristic.uuid)) == characteristic_uuid:
                return characteristic
    return None


def _validate_timeout(timeout: float) -> None:
    if not math.isfinite(timeout) or not 1 <= timeout <= 300:
        raise BleError("BLE timeout must be between 1 and 300 seconds")


def _load_bleak():
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError as exc:
        raise BleError("BLE support requires bleak; install the project requirements") from exc
    return BleakClient, BleakScanner
