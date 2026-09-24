from __future__ import annotations

import asyncio

from .ble_sensors import BleError, _find_characteristic, _load_bleak, _validate_timeout


FTMS_SERVICE_UUID = "1826"
FTMS_CONTROL_POINT_UUID = "2ad9"
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


def _require_writable_indication_characteristic(characteristic: object) -> None:
    properties = {
        str(value).casefold().replace("_", "-")
        for value in getattr(characteristic, "properties", ())
    }
    if "write" not in properties:
        raise BleError("FTMS Control Point does not support acknowledged writes")
    if not properties.intersection({"indicate", "notify"}):
        raise BleError("FTMS Control Point does not support response indications")


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
