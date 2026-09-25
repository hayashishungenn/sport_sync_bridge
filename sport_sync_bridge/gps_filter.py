from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence


_EARTH_RADIUS_M = 6_371_008.8


@dataclass(frozen=True, slots=True)
class GpsTrackPoint:
    latitude: float
    longitude: float
    timestamp_ms: int
    accuracy_m: float
    speed_mps: float | None = None
    heading_deg: float | None = None


def smooth_gps_track(
    points: Sequence[GpsTrackPoint],
    *,
    q_metres_per_second: float = 2.5,
    adaptive_q: bool = True,
) -> list[tuple[float, float]]:
    if not math.isfinite(q_metres_per_second) or q_metres_per_second < 0:
        raise ValueError("Kalman Q must be a finite, non-negative number")
    if not points:
        return []

    reference_latitude = math.radians(points[0].latitude)
    reference_longitude = math.radians(points[0].longitude)
    state_east = 0.0
    state_north = 0.0
    variance: float | None = None
    previous_timestamp_seconds: int | None = None
    previous_heading: float | None = None
    smoothed: list[tuple[float, float]] = []

    for index, point in enumerate(points):
        if not math.isfinite(point.latitude) or not -90 <= point.latitude <= 90:
            raise ValueError(f"GPS point {index} has an invalid latitude")
        if not math.isfinite(point.longitude) or not -180 <= point.longitude <= 180:
            raise ValueError(f"GPS point {index} has an invalid longitude")
        if not math.isfinite(point.accuracy_m) or point.accuracy_m < 0:
            raise ValueError(f"GPS point {index} has invalid horizontal accuracy")

        east, north = _project_to_local_metres(
            point.latitude,
            point.longitude,
            reference_latitude,
            reference_longitude,
        )
        timestamp_seconds = point.timestamp_ms // 1000
        heading = _normalize_heading(point.heading_deg)

        if variance is None:
            state_east = east
            state_north = north
            variance = point.accuracy_m**2
            previous_timestamp_seconds = timestamp_seconds
            smoothed.append((point.latitude, point.longitude))
            continue

        if previous_timestamp_seconds is None:
            previous_timestamp_seconds = timestamp_seconds
        elapsed_seconds = timestamp_seconds - previous_timestamp_seconds
        if elapsed_seconds > 0:
            q = q_metres_per_second
            if adaptive_q:
                if point.speed_mps is not None and math.isfinite(point.speed_mps):
                    if point.speed_mps > 5:
                        q = 1.5
                    elif point.speed_mps > 2:
                        q = 0.8
                if heading is not None and previous_heading is not None:
                    heading_delta = abs((heading - previous_heading + 180) % 360 - 180)
                    if heading_delta > 15:
                        q *= 3
            variance += elapsed_seconds * q * q
            previous_timestamp_seconds = timestamp_seconds
            if adaptive_q and heading is not None:
                previous_heading = heading

        measurement_variance = point.accuracy_m**2
        denominator = variance + measurement_variance
        gain = variance / denominator if denominator > 0 else 1.0
        state_east += gain * (east - state_east)
        state_north += gain * (north - state_north)
        variance = (1 - gain) * variance

        latitude, longitude = _unproject_from_local_metres(
            state_east,
            state_north,
            reference_latitude,
            reference_longitude,
        )
        smoothed.append((latitude, longitude))

    return smoothed


def _project_to_local_metres(
    latitude: float,
    longitude: float,
    reference_latitude: float,
    reference_longitude: float,
) -> tuple[float, float]:
    lat = math.radians(latitude)
    lon = math.radians(longitude)
    delta_lon = (lon - reference_longitude + math.pi) % (2 * math.pi) - math.pi
    cosine_distance = (
        math.sin(reference_latitude) * math.sin(lat)
        + math.cos(reference_latitude) * math.cos(lat) * math.cos(delta_lon)
    )
    angular_distance = math.acos(max(-1.0, min(1.0, cosine_distance)))
    if angular_distance < 1e-12:
        return 0.0, 0.0
    sine_distance = math.sin(angular_distance)
    if abs(sine_distance) < 1e-12:
        raise ValueError("GPS track spans an unsupported near-antipodal distance")
    scale = angular_distance / sine_distance
    east = (
        _EARTH_RADIUS_M
        * scale
        * math.cos(lat)
        * math.sin(delta_lon)
    )
    north = _EARTH_RADIUS_M * scale * (
        math.cos(reference_latitude) * math.sin(lat)
        - math.sin(reference_latitude) * math.cos(lat) * math.cos(delta_lon)
    )
    return east, north


def _unproject_from_local_metres(
    east: float,
    north: float,
    reference_latitude: float,
    reference_longitude: float,
) -> tuple[float, float]:
    distance = math.hypot(east, north)
    if distance < 1e-12:
        return math.degrees(reference_latitude), math.degrees(reference_longitude)
    angular_distance = distance / _EARTH_RADIUS_M
    sine_distance = math.sin(angular_distance)
    cosine_distance = math.cos(angular_distance)
    latitude_sine = (
        cosine_distance * math.sin(reference_latitude)
        + north * sine_distance * math.cos(reference_latitude) / distance
    )
    latitude = math.asin(max(-1.0, min(1.0, latitude_sine)))
    longitude = reference_longitude + math.atan2(
        east * sine_distance,
        distance * math.cos(reference_latitude) * cosine_distance
        - north * math.sin(reference_latitude) * sine_distance,
    )
    return math.degrees(latitude), (math.degrees(longitude) + 540) % 360 - 180


def _normalize_heading(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return value % 360
