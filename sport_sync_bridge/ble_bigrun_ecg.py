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


@dataclass(frozen=True, slots=True)
class BigRunEcgFrame:
    timestamp: str
    payload_length: int
    payload_hex: str


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


def validate_bigrun_ecg_options(duration_seconds: float, timeout: float) -> None:
    _validate_timeout(timeout)
    if not math.isfinite(duration_seconds) or not 1 <= duration_seconds <= 86400:
        raise BleError("BigRun ECG duration must be between 1 second and 24 hours")


async def _write_enable_command(client: object, characteristic: object) -> None:
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
    await client.write_gatt_char(characteristic, BIGRUN_ECG_ENABLE_COMMAND, response=response)
