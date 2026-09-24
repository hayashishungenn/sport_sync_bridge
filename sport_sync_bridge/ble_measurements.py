from __future__ import annotations

import math
from dataclasses import dataclass, field


class BleMeasurementError(ValueError):
    pass


SENSOR_CHARACTERISTICS = {
    "heart_rate": ("180d", "2a37"),
    "running_speed_cadence": ("1814", "2a53"),
    "cycling_speed_cadence": ("1816", "2a5b"),
    "cycling_power": ("1818", "2a63"),
    "cycling_power_vector": ("1818", "2a64"),
    "fitness_machine": ("1826", "2ad2"),
}


@dataclass(frozen=True, slots=True)
class BleSensorSample:
    timestamp: str
    sensor_type: str
    measurement: dict[str, object]


@dataclass(slots=True)
class SensorMeasurementDecoder:
    wheel_circumference_m: float | None = None
    _previous_revolutions: dict[tuple[str, str], tuple[int, int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.wheel_circumference_m is not None and (
            not math.isfinite(self.wheel_circumference_m) or self.wheel_circumference_m <= 0
        ):
            raise BleMeasurementError("Wheel circumference must be a finite positive number")

    def decode(self, sensor_type: str, payload: bytes | bytearray) -> dict[str, object]:
        decoders = {
            "running_speed_cadence": decode_rsc_measurement,
            "cycling_speed_cadence": decode_csc_measurement,
            "cycling_power": decode_cycling_power_measurement,
            "cycling_power_vector": decode_cycling_power_vector,
            "fitness_machine": decode_indoor_bike_data,
        }
        decoder = decoders.get(sensor_type)
        if decoder is None:
            raise BleMeasurementError(f"Unsupported BLE measurement type: {sensor_type}")
        values = decoder(payload)
        if sensor_type in {"cycling_speed_cadence", "cycling_power", "cycling_power_vector"}:
            self._add_revolution_metrics(sensor_type, values)
        return values

    def _add_revolution_metrics(self, sensor_type: str, values: dict[str, object]) -> None:
        wheel_revolutions = values.get("cumulative_wheel_revolutions")
        wheel_event_time = values.get("wheel_event_time_ticks")
        if isinstance(wheel_revolutions, int) and isinstance(wheel_event_time, int):
            if self.wheel_circumference_m is not None:
                values["distance_m"] = round(wheel_revolutions * self.wheel_circumference_m, 3)
            self._add_rate(
                sensor_type,
                "wheel",
                wheel_revolutions,
                wheel_event_time,
                counter_bits=32,
                rate_key="speed_mps",
                units_per_minute=1.0,
                scale=self.wheel_circumference_m,
                values=values,
            )

        crank_revolutions = values.get("cumulative_crank_revolutions")
        crank_event_time = values.get("crank_event_time_ticks")
        if isinstance(crank_revolutions, int) and isinstance(crank_event_time, int):
            self._add_rate(
                sensor_type,
                "crank",
                crank_revolutions,
                crank_event_time,
                counter_bits=16,
                rate_key="cadence_rpm",
                units_per_minute=60.0,
                scale=1.0,
                values=values,
            )

    def _add_rate(
        self,
        sensor_type: str,
        measurement: str,
        revolutions: int,
        event_time: int,
        *,
        counter_bits: int,
        rate_key: str,
        units_per_minute: float,
        scale: float | None,
        values: dict[str, object],
    ) -> None:
        key = (sensor_type, measurement)
        previous = self._previous_revolutions.get(key)
        self._previous_revolutions[key] = (revolutions, event_time)
        if previous is None or scale is None:
            return

        previous_revolutions, previous_event_time = previous
        event_delta = (event_time - previous_event_time) & 0xFFFF
        if event_delta == 0:
            return
        counter_mask = (1 << counter_bits) - 1
        revolution_delta = (revolutions - previous_revolutions) & counter_mask
        elapsed_seconds = event_delta / 1024.0
        rate = revolution_delta * scale * units_per_minute / elapsed_seconds
        values[rate_key] = round(rate, 6)


def decode_rsc_measurement(payload: bytes | bytearray) -> dict[str, object]:
    data = bytes(payload)
    if not data:
        raise BleMeasurementError("RSC Measurement is missing its Flags field")
    flags = data[0]
    offset = 1
    instantaneous_speed = _read_unsigned(data, offset, 2, "RSC instantaneous speed")
    offset += 2
    instantaneous_cadence = _read_unsigned(data, offset, 1, "RSC instantaneous cadence")
    offset += 1
    values: dict[str, object] = {
        "flags": flags,
        "speed_mps": round(instantaneous_speed / 256.0, 6),
        "cadence_spm": instantaneous_cadence,
        "running": bool(flags & 0x04),
    }
    if flags & 0x01:
        stride_length_cm = _read_unsigned(data, offset, 2, "RSC stride length")
        offset += 2
        values["stride_length_m"] = round(stride_length_cm / 100.0, 2)
    if flags & 0x02:
        total_distance_tenths_m = _read_unsigned(data, offset, 4, "RSC total distance")
        offset += 4
        values["distance_m"] = round(total_distance_tenths_m / 10.0, 1)
    if offset != len(data):
        raise BleMeasurementError("RSC Measurement has unexpected trailing bytes")
    return values


def decode_csc_measurement(payload: bytes | bytearray) -> dict[str, int]:
    data = bytes(payload)
    if not data:
        raise BleMeasurementError("CSC Measurement is missing its Flags field")
    flags = data[0]
    offset = 1
    values: dict[str, int] = {"flags": flags}
    if flags & 0x01:
        values["cumulative_wheel_revolutions"] = _read_unsigned(data, offset, 4, "CSC wheel revolutions")
        offset += 4
        values["wheel_event_time_ticks"] = _read_unsigned(data, offset, 2, "CSC wheel event time")
        offset += 2
    if flags & 0x02:
        values["cumulative_crank_revolutions"] = _read_unsigned(data, offset, 2, "CSC crank revolutions")
        offset += 2
        values["crank_event_time_ticks"] = _read_unsigned(data, offset, 2, "CSC crank event time")
        offset += 2
    if offset != len(data):
        raise BleMeasurementError("CSC Measurement has unexpected trailing bytes")
    if not flags & 0x03:
        raise BleMeasurementError("CSC Measurement contains no wheel or crank data")
    return values


def decode_cycling_power_measurement(payload: bytes | bytearray) -> dict[str, object]:
    data = bytes(payload)
    flags = _read_unsigned(data, 0, 2, "Cycling Power flags")
    offset = 2
    instantaneous_power_w = _read_signed(data, offset, 2, "Cycling Power instantaneous power")
    offset += 2
    values: dict[str, object] = {"flags": flags, "power_w": instantaneous_power_w}

    if flags & (1 << 0):
        values["pedal_power_balance_percent"] = round(
            _read_unsigned(data, offset, 1, "Cycling Power pedal balance") / 2.0, 1
        )
        offset += 1
    if flags & (1 << 2):
        values["accumulated_torque_raw"] = _read_unsigned(data, offset, 2, "Cycling Power accumulated torque")
        offset += 2
    if flags & (1 << 4):
        values["cumulative_wheel_revolutions"] = _read_unsigned(data, offset, 4, "Cycling Power wheel revolutions")
        offset += 4
        values["wheel_event_time_ticks"] = _read_unsigned(data, offset, 2, "Cycling Power wheel event time")
        offset += 2
    if flags & (1 << 5):
        values["cumulative_crank_revolutions"] = _read_unsigned(data, offset, 2, "Cycling Power crank revolutions")
        offset += 2
        values["crank_event_time_ticks"] = _read_unsigned(data, offset, 2, "Cycling Power crank event time")
        offset += 2
    if flags & (1 << 6):
        values["extreme_force_maximum_raw"] = _read_signed(
            data, offset, 2, "Cycling Power maximum extreme force"
        )
        values["extreme_force_minimum_raw"] = _read_signed(
            data, offset + 2, 2, "Cycling Power minimum extreme force"
        )
        offset += 4
    if flags & (1 << 7):
        values["extreme_torque_maximum_raw"] = _read_signed(
            data, offset, 2, "Cycling Power maximum extreme torque"
        )
        values["extreme_torque_minimum_raw"] = _read_signed(
            data, offset + 2, 2, "Cycling Power minimum extreme torque"
        )
        offset += 4
    if flags & (1 << 8):
        values["extreme_angles_packed_raw"] = _read_unsigned(data, offset, 3, "Cycling Power extreme angles")
        offset += 3
    if flags & (1 << 9):
        values["top_dead_spot_angle_raw"] = _read_unsigned(data, offset, 2, "Cycling Power top dead spot angle")
        offset += 2
    if flags & (1 << 10):
        values["bottom_dead_spot_angle_raw"] = _read_unsigned(data, offset, 2, "Cycling Power bottom dead spot angle")
        offset += 2
    if flags & (1 << 11):
        values["accumulated_energy_kj"] = _read_unsigned(data, offset, 2, "Cycling Power accumulated energy")
        offset += 2
    return values


def decode_indoor_bike_data(payload: bytes | bytearray) -> dict[str, object]:
    data = bytes(payload)
    flags = _read_unsigned(data, 0, 2, "Indoor Bike Data flags")
    offset = 2
    values: dict[str, object] = {"flags": flags, "more_data": bool(flags & 0x01)}

    if not flags & 0x01:
        speed_hundredths_kmh = _read_unsigned(data, offset, 2, "Indoor Bike instantaneous speed")
        offset += 2
        values["speed_mps"] = round(speed_hundredths_kmh / 360.0, 6)
    if flags & (1 << 1):
        average_speed_hundredths_kmh = _read_unsigned(data, offset, 2, "Indoor Bike average speed")
        offset += 2
        values["average_speed_mps"] = round(average_speed_hundredths_kmh / 360.0, 6)
    if flags & (1 << 2):
        cadence_half_rpm = _read_unsigned(data, offset, 2, "Indoor Bike instantaneous cadence")
        offset += 2
        values["cadence_rpm"] = round(cadence_half_rpm / 2.0, 1)
    if flags & (1 << 3):
        average_cadence_half_rpm = _read_unsigned(data, offset, 2, "Indoor Bike average cadence")
        offset += 2
        values["average_cadence_rpm"] = round(average_cadence_half_rpm / 2.0, 1)
    if flags & (1 << 4):
        values["distance_m"] = _read_unsigned(data, offset, 3, "Indoor Bike total distance")
        offset += 3
    if flags & (1 << 5):
        resistance_tenths = _read_signed(data, offset, 2, "Indoor Bike resistance level")
        offset += 2
        values["resistance_level"] = round(resistance_tenths / 10.0, 1)
    if flags & (1 << 6):
        values["power_w"] = _read_signed(data, offset, 2, "Indoor Bike instantaneous power")
        offset += 2
    if flags & (1 << 7):
        values["average_power_w"] = _read_signed(data, offset, 2, "Indoor Bike average power")
        offset += 2
    if flags & (1 << 8):
        values["energy_kcal"] = _read_unsigned(data, offset, 2, "Indoor Bike total energy")
        offset += 2
        values["energy_per_hour_kcal"] = _read_unsigned(data, offset, 2, "Indoor Bike energy per hour")
        offset += 2
        values["energy_per_minute_kcal"] = _read_unsigned(data, offset, 1, "Indoor Bike energy per minute")
        offset += 1
    if flags & (1 << 9):
        values["heart_rate_bpm"] = _read_unsigned(data, offset, 1, "Indoor Bike heart rate")
        offset += 1
    if flags & (1 << 10):
        values["metabolic_equivalent"] = round(
            _read_unsigned(data, offset, 1, "Indoor Bike metabolic equivalent") / 10.0, 1
        )
        offset += 1
    if flags & (1 << 11):
        values["elapsed_time_s"] = _read_unsigned(data, offset, 2, "Indoor Bike elapsed time")
        offset += 2
    if flags & (1 << 12):
        values["remaining_time_s"] = _read_unsigned(data, offset, 2, "Indoor Bike remaining time")
        offset += 2
    if offset != len(data):
        raise BleMeasurementError("Indoor Bike Data has unexpected trailing bytes")
    return values


def decode_cycling_power_vector(payload: bytes | bytearray) -> dict[str, object]:
    data = bytes(payload)
    flags = _read_unsigned(data, 0, 1, "Cycling Power Vector flags")
    offset = 1
    directions = ("unknown", "tangential", "radial", "lateral")
    values: dict[str, object] = {
        "flags": flags,
        "measurement_direction": directions[(flags >> 4) & 0x03],
    }
    if flags & 0x01:
        values["cumulative_crank_revolutions"] = _read_unsigned(
            data, offset, 2, "Cycling Power Vector crank revolutions"
        )
        offset += 2
        values["crank_event_time_ticks"] = _read_unsigned(
            data, offset, 2, "Cycling Power Vector crank event time"
        )
        offset += 2
    if flags & 0x02:
        values["first_crank_measurement_angle_deg"] = _read_unsigned(
            data, offset, 2, "Cycling Power Vector first crank angle"
        )
        offset += 2

    has_force_array = bool(flags & 0x04)
    has_torque_array = bool(flags & 0x08)
    remaining = data[offset:]
    if has_force_array and has_torque_array and remaining:
        raise BleMeasurementError("Cycling Power Vector cannot split simultaneous force and torque arrays")
    if len(remaining) % 2:
        raise BleMeasurementError("Cycling Power Vector has a truncated magnitude value")
    if len(remaining) > 18:
        raise BleMeasurementError("Cycling Power Vector contains more than 9 magnitude values")
    raw_magnitudes = [
        int.from_bytes(remaining[index : index + 2], "little", signed=True)
        for index in range(0, len(remaining), 2)
    ]
    if has_force_array:
        values["force_magnitudes_n"] = raw_magnitudes
    if has_torque_array:
        values["torque_magnitudes_raw"] = raw_magnitudes
        values["torque_magnitudes_nm"] = [round(value / 32.0, 6) for value in raw_magnitudes]
    if not has_force_array and not has_torque_array and remaining:
        raise BleMeasurementError("Cycling Power Vector has values without a magnitude-array flag")
    return values


def _read_unsigned(data: bytes, offset: int, size: int, field_name: str) -> int:
    end = offset + size
    if offset < 0 or end > len(data):
        raise BleMeasurementError(f"{field_name} field is truncated")
    return int.from_bytes(data[offset:end], "little", signed=False)


def _read_signed(data: bytes, offset: int, size: int, field_name: str) -> int:
    end = offset + size
    if offset < 0 or end > len(data):
        raise BleMeasurementError(f"{field_name} field is truncated")
    return int.from_bytes(data[offset:end], "little", signed=True)
