from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

from .ble_sensors import (
    BleError,
    _find_characteristic,
    _load_bleak,
    _validate_timeout,
    decode_indoor_bike_data,
)
from .virtual_ride import RideCourse, RideSession


FTMS_SERVICE_UUID = "1826"
FTMS_CONTROL_POINT_UUID = "2ad9"
FTMS_INDOOR_BIKE_DATA_UUID = "2ad2"
FTMS_RESPONSE_CODE = 0x80
FTMS_REQUEST_CONTROL = 0x00
FTMS_SET_TARGET_POWER = 0x05

_FTMS_RESPONSE_NAMES = {
    0x01: "success",
    0x02: "operation not supported",
    0x03: "invalid parameter",
    0x04: "operation failed",
    0x05: "control not permitted",
}


@dataclass(frozen=True, slots=True)
class RideRunResult:
    elapsed_time_s: float
    timer_time_s: float


def encode_target_power_command(watts: int) -> bytes:
    if isinstance(watts, bool) or not isinstance(watts, int) or not 0 <= watts <= 32767:
        raise BleError("FTMS target power must be an integer from 0 to 32767 watts")
    return bytes((FTMS_SET_TARGET_POWER,)) + watts.to_bytes(2, "little", signed=True)


async def set_trainer_target_power(
    address: str,
    watts: int,
    timeout: float = 15.0,
    *,
    scanner_type: object | None = None,
    client_type: object | None = None,
) -> None:
    _validate_timeout(timeout)
    command = encode_target_power_command(watts)
    if not isinstance(address, str) or not address.strip():
        raise BleError("BLE device address is required")
    address = address.strip()

    if scanner_type is None or client_type is None:
        loaded_client, loaded_scanner = _load_bleak()
        scanner_type = scanner_type or loaded_scanner
        client_type = client_type or loaded_client

    try:
        device = await scanner_type.find_device_by_address(address, timeout=timeout)
        if device is None:
            raise BleError(f"BLE device was not found: {address}")

        async with client_type(device, timeout=timeout) as client:
            control_point = _find_characteristic(
                client,
                FTMS_SERVICE_UUID,
                FTMS_CONTROL_POINT_UUID,
            )
            if control_point is None:
                raise BleError(f"Device does not expose the FTMS Control Point: {address}")
            _require_writable_indication_characteristic(control_point)

            responses: asyncio.Queue[bytes] = asyncio.Queue()

            def on_response(_characteristic: object, payload: bytearray) -> None:
                responses.put_nowait(bytes(payload))

            await client.start_notify(control_point, on_response)
            try:
                await _send_and_check_response(
                    client,
                    control_point,
                    bytes((FTMS_REQUEST_CONTROL,)),
                    responses,
                    timeout,
                )
                await _send_and_check_response(
                    client,
                    control_point,
                    command,
                    responses,
                    timeout,
                )
            finally:
                await client.stop_notify(control_point)
    except BleError:
        raise
    except Exception as exc:
        raise BleError(f"Could not set FTMS trainer target power ({type(exc).__name__})") from exc


async def run_trainer_course(
    address: str,
    course: RideCourse,
    intensity: float = 1.0,
    timeout: float = 15.0,
    *,
    scanner_type: object | None = None,
    client_type: object | None = None,
    on_target: Callable[[float, int, int], None] | None = None,
    on_tick: Callable[[float], None] | None = None,
    on_measurement: Callable[[dict[str, object]], None] | None = None,
    on_control: Callable[[], str | None] | None = None,
    on_pause: Callable[[bool], None] | None = None,
) -> RideRunResult:
    _validate_timeout(timeout)
    if not isinstance(address, str) or not address.strip():
        raise BleError("BLE device address is required")
    address = address.strip()
    session = RideSession(course, intensity=intensity)
    for segment in course.segments:
        segment.target_power_at(0, session.intensity)
        segment.target_power_at(segment.duration_s, session.intensity)

    if scanner_type is None or client_type is None:
        loaded_client, loaded_scanner = _load_bleak()
        scanner_type = scanner_type or loaded_scanner
        client_type = client_type or loaded_client

    started_at: float | None = None
    active_started_at: float | None = None
    paused_at: float | None = None
    try:
        device = await scanner_type.find_device_by_address(address, timeout=timeout)
        if device is None:
            raise BleError(f"BLE device was not found: {address}")

        async with client_type(device, timeout=timeout) as client:
            control_point = _find_characteristic(
                client,
                FTMS_SERVICE_UUID,
                FTMS_CONTROL_POINT_UUID,
            )
            if control_point is None:
                raise BleError(f"Device does not expose the FTMS Control Point: {address}")
            _require_writable_indication_characteristic(control_point)
            indoor_bike_data = (
                _find_characteristic(client, FTMS_SERVICE_UUID, FTMS_INDOOR_BIKE_DATA_UUID)
                if on_measurement is not None
                else None
            )
            if indoor_bike_data is not None:
                _require_notifying_characteristic(indoor_bike_data)

            responses: asyncio.Queue[bytes] = asyncio.Queue()
            measurements: asyncio.Queue[dict[str, object] | Exception] = asyncio.Queue()

            def on_response(_characteristic: object, payload: bytearray) -> None:
                responses.put_nowait(bytes(payload))

            def on_indoor_bike_data(_characteristic: object, payload: bytearray) -> None:
                try:
                    measurements.put_nowait(decode_indoor_bike_data(bytes(payload)))
                except ValueError as exc:
                    measurements.put_nowait(exc)

            def drain_measurements(*, emit: bool = True) -> None:
                while True:
                    try:
                        measurement = measurements.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    if isinstance(measurement, Exception):
                        raise BleError("FTMS trainer sent malformed Indoor Bike Data") from measurement
                    if emit and on_measurement is not None:
                        on_measurement(measurement)

            notifications: list[object] = []
            last_target: int | None = None
            try:
                if indoor_bike_data is not None:
                    await client.start_notify(indoor_bike_data, on_indoor_bike_data)
                    notifications.append(indoor_bike_data)
                await client.start_notify(control_point, on_response)
                notifications.append(control_point)
                await _send_and_check_response(
                    client,
                    control_point,
                    bytes((FTMS_REQUEST_CONTROL,)),
                    responses,
                    timeout,
                )
                started_at = time.monotonic()
                active_started_at = started_at
                try:
                    while session.current_segment is not None:
                        drain_measurements(emit=not session.paused)
                        control = on_control() if on_control is not None else None
                        if control == "increase":
                            session.increase_intensity()
                        elif control == "decrease":
                            session.decrease_intensity()
                        elif control == "skip":
                            session.skip_interval()
                        elif control == "pause":
                            if session.toggle_pause():
                                paused_at = time.monotonic()
                                if last_target is not None and last_target > 0:
                                    await _send_and_check_response(
                                        client,
                                        control_point,
                                        encode_target_power_command(0),
                                        responses,
                                        timeout,
                                    )
                                    last_target = 0
                            else:
                                now = time.monotonic()
                                if paused_at is not None and active_started_at is not None:
                                    active_started_at += now - paused_at
                                paused_at = None
                            if on_pause is not None:
                                on_pause(session.paused)
                        if session.paused:
                            await asyncio.sleep(0.1)
                            continue
                        target = session.current_target_power_w
                        if target != last_target:
                            await _send_and_check_response(
                                client,
                                control_point,
                                encode_target_power_command(target),
                                responses,
                                timeout,
                            )
                            last_target = target
                            if on_target is not None:
                                on_target(session.course_elapsed_s, session.segment_index, target)

                        if on_tick is not None:
                            on_tick(session.wall_elapsed_s)
                        remaining = session.segment_remaining_s
                        if remaining is None:
                            break
                        step = min(1.0, remaining)
                        assert active_started_at is not None
                        deadline = active_started_at + session.wall_elapsed_s + step
                        await asyncio.sleep(max(0.0, deadline - time.monotonic()))
                        session.advance(step)
                    drain_measurements()
                finally:
                    if last_target is not None and last_target > 0:
                        await _send_and_check_response(
                            client,
                            control_point,
                            encode_target_power_command(0),
                            responses,
                            timeout,
                        )
            finally:
                await _stop_notifications(client, notifications)
    except BleError:
        raise
    except Exception as exc:
        raise BleError(f"Could not run FTMS trainer course ({type(exc).__name__})") from exc
    if started_at is None or active_started_at is None:
        raise BleError("FTMS trainer course did not start")
    finished_at = time.monotonic()
    return RideRunResult(
        elapsed_time_s=finished_at - started_at,
        timer_time_s=finished_at - active_started_at,
    )


def _require_writable_indication_characteristic(characteristic: object) -> None:
    properties = {
        str(value).casefold().replace("_", "-")
        for value in getattr(characteristic, "properties", ())
    }
    if "write" not in properties:
        raise BleError("FTMS Control Point does not support acknowledged writes")
    if not properties.intersection({"indicate", "notify"}):
        raise BleError("FTMS Control Point does not support response indications")


def _require_notifying_characteristic(characteristic: object) -> None:
    properties = {
        str(value).casefold().replace("_", "-")
        for value in getattr(characteristic, "properties", ())
    }
    if not properties.intersection({"notify", "indicate"}):
        raise BleError("FTMS Indoor Bike Data does not support notifications")


async def _stop_notifications(client: object, characteristics: list[object]) -> None:
    first_error: Exception | None = None
    for characteristic in reversed(characteristics):
        try:
            await client.stop_notify(characteristic)
        except Exception as exc:
            if first_error is None:
                first_error = exc
    if first_error is not None:
        raise first_error


async def _send_and_check_response(
    client: object,
    characteristic: object,
    command: bytes,
    responses: asyncio.Queue[bytes],
    timeout: float,
) -> None:
    await client.write_gatt_char(characteristic, command, response=True)
    try:
        response = await asyncio.wait_for(responses.get(), timeout=timeout)
    except TimeoutError as exc:
        raise BleError(f"FTMS trainer did not respond to opcode 0x{command[0]:02x}") from exc

    if len(response) != 3 or response[0] != FTMS_RESPONSE_CODE:
        raise BleError("FTMS trainer returned a malformed Control Point response")
    if response[1] != command[0]:
        raise BleError(
            f"FTMS trainer responded to opcode 0x{response[1]:02x} while waiting for 0x{command[0]:02x}"
        )

    result = response[2]
    if result != 0x01:
        result_name = _FTMS_RESPONSE_NAMES.get(result, f"unknown result 0x{result:02x}")
        raise BleError(f"FTMS trainer rejected opcode 0x{command[0]:02x}: {result_name}")
