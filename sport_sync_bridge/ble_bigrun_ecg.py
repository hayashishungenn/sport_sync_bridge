from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator

from .ble_sensors import BleError, _find_characteristic, _load_bleak, _validate_timeout


BIGRUN_ECG_SERVICE_UUID = "f000efe004514000000000000000b000"
BIGRUN_ECG_CONTROL_UUID = "f000efe104514000000000000000b000"
BIGRUN_ECG_DATA_UUID = "f000efe304514000000000000000b000"
BIGRUN_ECG_ENABLE_COMMAND = bytes((0x62,))
BIGRUN_ECG_MODES = ("standard", "hrv", "ecg")
BIGRUN_ECG_SAMPLE_RATE_HZ = 125
_BIGRUN_ECG_SAMPLE_MARKER = 0x41
_BIGRUN_ECG_SAMPLE_OFFSET = 2
_BIGRUN_ECG_SAMPLE_CENTER = 10000

_BIGRUN_ECG_MODE_BYTES = {
    "standard": (0, 0, 0),
    "hrv": (0, 2, 2),
    "ecg": (2, 0, 2),
}
_BIGRUN_ECG_MODE_COMMANDS = (0xC0, 0xC4, 0xC2)
_BIGRUN_ECG_MODE_PACKET_DELAY_SECONDS = 0.05


@dataclass(frozen=True, slots=True)
class BigRunEcgFrame:
    timestamp: str
    payload_length: int
    payload_hex: str


def decode_bigrun_ecg_payload(payload: bytes) -> tuple[int, ...] | None:
    if not payload or payload[0] != _BIGRUN_ECG_SAMPLE_MARKER:
        return None

    return tuple(
        ((payload[index + 1] << 8) | payload[index]) - _BIGRUN_ECG_SAMPLE_CENTER
        for index in range(_BIGRUN_ECG_SAMPLE_OFFSET, len(payload) - 1, 2)
    )


async def stream_bigrun_ecg(
    address: str,
    duration_seconds: float,
    timeout: float = 15.0,
    *,
    scanner_type: object | None = None,
    client_type: object | None = None,
) -> AsyncIterator[BigRunEcgFrame]:
    validate_bigrun_ecg_options(duration_seconds, timeout)
    if scanner_type is None or client_type is None:
        loaded_client, loaded_scanner = _load_bleak()
        scanner_type = scanner_type or loaded_scanner
        client_type = client_type or loaded_client

    try:
        device = await scanner_type.find_device_by_address(address, timeout=timeout)
        if device is None:
            raise BleError(f"BLE device was not found: {address}")

        async with client_type(device, timeout=timeout) as client:
            control = _find_characteristic(
                client,
                BIGRUN_ECG_SERVICE_UUID,
                BIGRUN_ECG_CONTROL_UUID,
            )
            data = _find_characteristic(
                client,
                BIGRUN_ECG_SERVICE_UUID,
                BIGRUN_ECG_DATA_UUID,
            )
            if control is None or data is None:
                raise BleError(f"Device does not expose the BigRun ECG service: {address}")

            queue: asyncio.Queue[BigRunEcgFrame] = asyncio.Queue()

            def on_measurement(_characteristic: object, payload: bytearray) -> None:
                raw = bytes(payload)
                queue.put_nowait(
                    BigRunEcgFrame(
                        datetime.now(timezone.utc).isoformat(),
                        len(raw),
                        raw.hex(),
                    )
                )

            started = False
            try:
                await _write_enable_command(client, control)
                await client.start_notify(data, on_measurement)
                started = True

                deadline = asyncio.get_running_loop().time() + duration_seconds
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        return
                    try:
                        frame = await asyncio.wait_for(queue.get(), timeout=remaining)
                    except TimeoutError:
                        return
                    yield frame
            finally:
                if started:
                    await client.stop_notify(data)
    except BleError:
        raise
    except Exception as exc:
        raise BleError(f"Could not record BigRun ECG data ({type(exc).__name__})") from exc


async def set_bigrun_ecg_work_mode(
    address: str,
    mode: str,
    timeout: float = 15.0,
    *,
    scanner_type: object | None = None,
    client_type: object | None = None,
) -> None:
    _validate_timeout(timeout)
    packets = build_bigrun_ecg_mode_packets(mode)
    if scanner_type is None or client_type is None:
        loaded_client, loaded_scanner = _load_bleak()
        scanner_type = scanner_type or loaded_scanner
        client_type = client_type or loaded_client

    try:
        device = await scanner_type.find_device_by_address(address, timeout=timeout)
        if device is None:
            raise BleError(f"BLE device was not found: {address}")

        async with client_type(device, timeout=timeout) as client:
            control = _find_characteristic(
                client,
                BIGRUN_ECG_SERVICE_UUID,
                BIGRUN_ECG_CONTROL_UUID,
            )
            if control is None:
                raise BleError(f"Device does not expose the BigRun ECG control characteristic: {address}")

            response = _write_response(control)
            for index, packet in enumerate(packets):
                await client.write_gatt_char(control, packet, response=response)
                if index + 1 < len(packets):
                    await asyncio.sleep(_BIGRUN_ECG_MODE_PACKET_DELAY_SECONDS)
    except BleError:
        raise
    except Exception as exc:
        raise BleError(f"Could not set BigRun ECG work mode ({type(exc).__name__})") from exc


def build_bigrun_ecg_mode_packets(mode: str) -> tuple[bytes, bytes, bytes]:
    if not isinstance(mode, str):
        raise BleError(f"Unknown BigRun ECG mode: {mode!r}")
    normalized_mode = mode.strip().casefold()
    mode_bytes = _BIGRUN_ECG_MODE_BYTES.get(normalized_mode)
    if mode_bytes is None:
        choices = ", ".join(BIGRUN_ECG_MODES)
        raise BleError(f"Unknown BigRun ECG mode {mode!r}; choose one of: {choices}")

    packets = []
    for command, value in zip(_BIGRUN_ECG_MODE_COMMANDS, mode_bytes, strict=True):
        # The AOT stores this header value as 0x130; the Android BLE bridge writes byte[].
        packet = bytes((0x30, 0x1E, command, 0, 0, 0, 2, value))
        packets.append(packet + bytes(20 - len(packet)))
    return tuple(packets)


def validate_bigrun_ecg_options(duration_seconds: float, timeout: float) -> None:
    _validate_timeout(timeout)
    if not math.isfinite(duration_seconds) or not 1 <= duration_seconds <= 86400:
        raise BleError("BigRun ECG duration must be between 1 second and 24 hours")


async def _write_enable_command(client: object, characteristic: object) -> None:
    response = _write_response(characteristic)
    await client.write_gatt_char(characteristic, BIGRUN_ECG_ENABLE_COMMAND, response=response)


def _write_response(characteristic: object) -> bool:
    properties = {
        str(value).casefold().replace("_", "-")
        for value in getattr(characteristic, "properties", ())
    }
    if "write" in properties:
        response = True
    elif "write-without-response" in properties:
        response = False
    else:
        raise BleError("BigRun ECG control characteristic is not writable")
    return response
