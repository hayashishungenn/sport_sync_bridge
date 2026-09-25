from __future__ import annotations

import math
from datetime import timezone

from .activity_analysis import summarize_activity
from .formats import ActivityFile, TrackPoint


def build_activity_publish_data(
    activity: ActivityFile,
    *,
    activity_id: str,
    display_name: str,
    title: str | None = None,
    avatar_url: str | None = None,
    location_name: str | None = None,
) -> dict[str, object]:
    activity_id = _required_text(activity_id, "activity ID")
    display_name = _required_text(display_name, "display name")
    title = _required_text(title or activity.name, "title")
    sport = _required_text(activity.sport_type, "sport")
    avatar_url = _optional_text(avatar_url, "avatar URL")
    location_name = _optional_text(location_name, "location name")

    summary = summarize_activity(activity)
    start_time = activity.start_time or next(
        (point.timestamp for point in activity.track_points if point.timestamp is not None),
        None,
    )
    if start_time is None:
        raise ValueError("Activity has no start time")
    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=timezone.utc)
    start_time = start_time.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    coordinates = _activity_coordinates(activity.track_points)
    first_location = (
        {"lat": coordinates[0][0], "lng": coordinates[0][1]} if coordinates else None
    )
    calories = [lap.calories for lap in activity.laps if lap.calories is not None]
    moving_time = (
        activity.timer_time_s
        if activity.timer_time_s is not None
        else activity.elapsed_time_s
    )

    return {
        "activityId": activity_id,
        "displayName": display_name,
        "avatarUrl": avatar_url,
        "title": title,
        "sport": sport,
        "subSport": None,
        "deviceName": activity.creator,
        "distanceMeters": _finite_or_none(summary.get("distance_m"), "distance"),
        "movingTimeSeconds": _finite_or_none(moving_time, "moving time"),
        "elevationGainMeters": _finite_or_none(summary.get("total_ascent_m"), "elevation gain"),
        "avgSpeedMs": _finite_or_none(summary.get("average_speed_mps"), "average speed"),
        "summaryPolyline": _encode_polyline(coordinates) if coordinates else None,
        "location": first_location,
        "locationName": location_name,
        "avgHeartRate": _finite_or_none(summary.get("average_heart_rate_bpm"), "average heart rate"),
        "avgPower": _finite_or_none(summary.get("average_power_w"), "average power"),
        "normPower": _finite_or_none(activity.normalized_power_w, "normalized power"),
        "avgCadence": _finite_or_none(summary.get("average_cadence_rpm"), "average cadence"),
        "calories": sum(calories) if calories else None,
        "startTime": start_time,
    }


def _activity_coordinates(points: list[TrackPoint]) -> list[tuple[float, float]]:
    coordinates: list[tuple[float, float]] = []
    for point in points:
        if point.latitude is None and point.longitude is None:
            continue
        if point.latitude is None or point.longitude is None:
            raise ValueError("Activity contains incomplete GPS coordinates")
        latitude = _finite_number(point.latitude, "latitude")
        longitude = _finite_number(point.longitude, "longitude")
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise ValueError("Activity contains invalid GPS coordinates")
        coordinates.append((latitude, longitude))
    return coordinates


def _encode_polyline(coordinates: list[tuple[float, float]]) -> str:
    previous_latitude = 0
    previous_longitude = 0
    encoded: list[str] = []
    for latitude, longitude in coordinates:
        latitude_value = round(latitude * 100_000)
        longitude_value = round(longitude * 100_000)
        encoded.append(_encode_delta(latitude_value - previous_latitude))
        encoded.append(_encode_delta(longitude_value - previous_longitude))
        previous_latitude = latitude_value
        previous_longitude = longitude_value
    return "".join(encoded)


def _encode_delta(delta: int) -> str:
    value = ~(delta << 1) if delta < 0 else delta << 1
    encoded: list[str] = []
    while value >= 0x20:
        encoded.append(chr((0x20 | (value & 0x1F)) + 63))
        value >>= 5
    encoded.append(chr(value + 63))
    return "".join(encoded)


def _finite_or_none(value: object, label: str) -> float | None:
    if value is None:
        return None
    return _finite_number(value, label)


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Activity {label} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"Activity {label} must be finite")
    return number


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Activity {label} must be non-empty text")
    return value.strip()


def _optional_text(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Activity {label} must be text")
    return value.strip() or None
