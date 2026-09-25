from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from .formats import ActivityFile, ActivityLap, TrackPoint
from .utils import parse_datetime


_MOTION_DETAIL_NAME = "motion path detail data"
_PART_TIME_MAP = re.compile(r'"partTimeMap"\s*:\s*\{[^}]*\}')
_HUAWEI_SPORT_TYPES = {4: "running", 5: "walking"}
_SPORT_NAMES = {
    "bike": "cycling",
    "biking": "cycling",
    "cycle": "cycling",
    "cycling": "cycling",
    "hike": "hiking",
    "hiking": "hiking",
    "run": "running",
    "running": "running",
    "swim": "swimming",
    "swimming": "swimming",
    "walk": "walking",
    "walking": "walking",
}


def is_huawei_motion_detail_file(filename: str) -> bool:
    normalized = filename.replace("\\", "/").casefold()
    return _MOTION_DETAIL_NAME in normalized


def parse_huawei_activity_json(
    payload: bytes,
    default_name: str,
    *,
    required: bool = False,
) -> list[ActivityFile] | None:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        if required:
            raise ValueError("Huawei motion-detail JSON is not valid UTF-8") from exc
        return None

    repaired = _PART_TIME_MAP.sub('"partTimeMap": {}', text)
    try:
        decoded = json.loads(repaired)
    except json.JSONDecodeError as exc:
        if required:
            raise ValueError("Huawei motion-detail JSON is invalid") from exc
        return None

    records = list(_iter_activity_records(decoded))
    if not records:
        if required:
            raise ValueError("Huawei motion-detail JSON contains no supported activities")
        return None

    activities: list[ActivityFile] = []
    for index, record in enumerate(records, start=1):
        activities.append(_activity_from_record(record, f"{default_name}-{index}"))
    return activities


def serialize_activity_json(activity: ActivityFile) -> bytes:
    def iso(value: datetime | None) -> str | None:
        return value.astimezone(timezone.utc).isoformat() if value else None

    value = {
        "name": activity.name,
        "sport_type": activity.sport_type,
        "start_time": iso(activity.start_time),
        "end_time": iso(activity.end_time),
        "elapsed_time_s": activity.elapsed_time_s,
        "timer_time_s": activity.timer_time_s,
        "distance_m": activity.distance_m,
        "average_heart_rate_bpm": activity.average_heart_rate_bpm,
        "maximum_heart_rate_bpm": activity.maximum_heart_rate_bpm,
        "laps": [
            {
                "start_time": iso(lap.start_time),
                "end_time": iso(lap.end_time),
                "elapsed_time_s": lap.elapsed_time_s,
                "timer_time_s": lap.timer_time_s,
                "distance_m": lap.distance_m,
                "calories": lap.calories,
                "average_heart_rate": lap.average_heart_rate,
                "maximum_heart_rate": lap.maximum_heart_rate,
                "track_points": [
                    {
                        "timestamp": iso(point.timestamp),
                        "latitude": point.latitude,
                        "longitude": point.longitude,
                        "elevation_m": point.elevation_m,
                        "distance_m": point.distance_m,
                        "speed_mps": point.speed_mps,
                        "heart_rate_bpm": point.heart_rate_bpm,
                        "cadence_rpm": point.cadence_rpm,
                        "power_w": point.power_w,
                    }
                    for point in lap.track_points
                ],
            }
            for lap in activity.laps
        ],
    }
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _iter_activity_records(decoded: object):
    rows = decoded if isinstance(decoded, list) else [decoded]
    for row in rows:
        if not isinstance(row, dict):
            continue
        nested = row.get("motionPathData")
        if isinstance(nested, list):
            parent = {key: value for key, value in row.items() if key != "motionPathData"}
            for detail in nested:
                if isinstance(detail, dict):
                    record = parent | detail
                    if _is_activity_record(record):
                        yield record
        elif _is_activity_record(row):
            yield row


def _is_activity_record(value: dict[str, object]) -> bool:
    return (
        _first(value, "startTime", "start_time") is not None
        and _first(value, "sportType", "sport_type", "sport") is not None
        and (
            _first(value, "attribute", "trackPoints", "track_points", "points") is not None
        )
    )


def _activity_from_record(record: dict[str, object], default_name: str) -> ActivityFile:
    start_time = _timestamp(_first(record, "startTime", "start_time"), None)
    if start_time is None:
        raise ValueError("Huawei activity has no valid startTime")

    attribute = _first(record, "attribute")
    detail_text = _attribute_section(attribute, "HW_EXT_TRACK_DETAIL")
    simplified = _parse_simplified_data(attribute)
    metric_sources = [record, simplified]
    for source in (record.get("wearSportData"), simplified.get("wearSportData")):
        if isinstance(source, dict):
            metric_sources.append(source)

    points = _parse_track_points(detail_text, start_time)
    direct_points = _first(record, "trackPoints", "track_points", "points")
    if not points and isinstance(direct_points, list):
        points = [point for value in direct_points if isinstance(value, dict) if (point := _point_from_json(value, start_time))]

    sport_type = _sport_type(_first(record, "sportType", "sport_type", "sport"))
    elapsed_time_s = _duration_seconds(metric_sources)
    distance_m = _metric(metric_sources, "totalDistance", "distance_m", "distanceMeters", "distance")
    calories = _metric_int(metric_sources, "totalCalories", "calories")
    average_hr = _metric(metric_sources, "avgHeartRate", "averageHeartRate", "average_heart_rate_bpm")
    maximum_hr = _metric(metric_sources, "maxHeartRate", "maximumHeartRate", "maximum_heart_rate_bpm")
    end_time = _timestamp(_first(record, "endTime", "end_time"), start_time)
    if end_time is None and elapsed_time_s is not None:
        end_time = start_time + timedelta(seconds=elapsed_time_s)

    name = _text(_first(record, "name", "activityName", "title"))
    if not name:
        label = sport_type.replace("_", " ") if sport_type else "activity"
        name = f"Huawei {label} {start_time:%Y-%m-%d %H:%M UTC}"

    lap = ActivityLap(
        start_time=start_time,
        end_time=end_time,
        elapsed_time_s=elapsed_time_s,
        timer_time_s=elapsed_time_s,
        distance_m=distance_m,
        calories=calories,
        average_heart_rate=average_hr,
        maximum_heart_rate=maximum_hr,
        track_points=points,
    )
    return ActivityFile(
        name=name,
        sport_type=sport_type,
        start_time=start_time,
        end_time=end_time,
        elapsed_time_s=elapsed_time_s,
        timer_time_s=elapsed_time_s,
        distance_m=distance_m,
        laps=[lap],
        creator="Huawei Health archive",
        average_heart_rate_bpm=average_hr,
        maximum_heart_rate_bpm=maximum_hr,
    )


def _attribute_section(attribute: object, section: str) -> str:
    if isinstance(attribute, dict):
        for key in (section, section.casefold(), section.removesuffix("_DETAIL").casefold()):
            value = attribute.get(key)
            if isinstance(value, str):
                return value
        return ""
    if not isinstance(attribute, str):
        return ""

    marker = f"{section}@is"
    start = attribute.find(marker)
    if start < 0:
        return ""
    start += len(marker)
    end = attribute.find("&&HW_EXT_TRACK_", start)
    return attribute[start:end if end >= 0 else None].strip()


def _parse_simplified_data(attribute: object) -> dict[str, object]:
    text = _attribute_section(attribute, "HW_EXT_TRACK_SIMPLIFY")
    if not text:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _parse_track_points(detail: str, start_time: datetime) -> list[TrackPoint]:
    points_by_time: dict[int, TrackPoint] = {}
    untimed_points: list[TrackPoint] = []
    metric_events: list[tuple[datetime, str, float]] = []

    for line in detail.splitlines():
        fields: dict[str, str] = {}
        for token in line.strip().split(";"):
            if "=" in token:
                key, value = token.split("=", 1)
                fields[key.strip().casefold()] = value.strip()
        if not fields:
            continue

        record_type = fields.get("tp", "").casefold()
        if record_type in {"h-r", "alti", "s-r", "rs"} and "lat" not in fields:
            raw_value = _number(fields.get("v"))
            raw_time = fields.get("k")
            event_time = _event_timestamp(raw_time, start_time, record_type)
            if raw_value is not None and event_time is not None:
                if record_type == "h-r":
                    metric_events.append((event_time, "heart_rate_bpm", raw_value))
                elif record_type == "alti":
                    metric_events.append((event_time, "elevation_m", raw_value))
                elif record_type == "s-r":
                    metric_events.append((event_time, "cadence_rpm", raw_value))
                else:
                    metric_events.append((event_time, "speed_mps", raw_value / 10))
            continue

        latitude = _number(fields.get("lat", fields.get("latitude")))
        longitude = _number(fields.get("lon", fields.get("lng", fields.get("longitude"))))
        if latitude is None or longitude is None:
            continue
        if latitude == 90 and longitude == -80:
            continue

        timestamp = _timestamp(fields.get("t", fields.get("timestamp")), start_time)
        point = TrackPoint(
            timestamp=timestamp,
            latitude=latitude,
            longitude=longitude,
            elevation_m=_number(fields.get("alt", fields.get("elevation"))),
            speed_mps=_number(fields.get("speed")),
            heart_rate_bpm=_number(fields.get("hr", fields.get("heartrate"))),
            cadence_rpm=_number(fields.get("cad", fields.get("cadence", fields.get("s-r")))),
            power_w=_number(fields.get("power", fields.get("watts"))),
        )
        if point.speed_mps is None and fields.get("rs") is not None:
            raw_speed = _number(fields.get("rs"))
            point.speed_mps = raw_speed / 10 if raw_speed is not None else None

        if timestamp is None:
            untimed_points.append(point)
            continue
        key = round(timestamp.timestamp() * 1000)
        existing = points_by_time.get(key)
        if existing is None:
            points_by_time[key] = point
        else:
            _merge_point(existing, point)

    points = list(points_by_time.values())
    for event_time, field_name, value in metric_events:
        nearest = min(points, key=lambda point: abs((point.timestamp - event_time).total_seconds())) if points else None
        if nearest is not None and nearest.timestamp is not None:
            if abs((nearest.timestamp - event_time).total_seconds()) <= 10 and getattr(nearest, field_name) is None:
                setattr(nearest, field_name, value)

    points.sort(key=lambda point: (point.timestamp is None, point.timestamp or datetime.max.replace(tzinfo=timezone.utc)))
    return points + untimed_points


def _point_from_json(value: dict[str, object], start_time: datetime) -> TrackPoint | None:
    latitude = _number(_first(value, "latitude", "lat"))
    longitude = _number(_first(value, "longitude", "lon", "lng"))
    if latitude is None or longitude is None:
        return None
    if latitude == 90 and longitude == -80:
        return None
    return TrackPoint(
        timestamp=_timestamp(_first(value, "timestamp", "time", "date", "t"), start_time),
        latitude=latitude,
        longitude=longitude,
        elevation_m=_number(_first(value, "elevation_m", "elevation", "altitude", "alt")),
        distance_m=_number(_first(value, "distance_m", "distance", "distanceMeters")),
        speed_mps=_number(_first(value, "speed_mps", "speed")),
        heart_rate_bpm=_number(_first(value, "heart_rate_bpm", "heart_rate", "heartRate", "hr")),
        cadence_rpm=_number(_first(value, "cadence_rpm", "cadence", "cad")),
        power_w=_number(_first(value, "power_w", "power", "watts")),
    )


def _event_timestamp(value: object, start_time: datetime, record_type: str) -> datetime | None:
    numeric = _number(value)
    if numeric is not None and numeric < 1_000_000_000 and record_type in {"rs", "s-r"}:
        return start_time + timedelta(seconds=numeric + 5)
    return _timestamp(value, start_time)


def _timestamp(value: object, start_time: datetime | None) -> datetime | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        numeric = _number(text)
        if numeric is not None:
            if numeric == 0:
                return start_time
            if abs(numeric) >= 100_000_000_000:
                seconds = numeric / 1000
            elif abs(numeric) >= 1_000_000_000:
                seconds = numeric
            elif start_time is not None:
                return start_time + timedelta(seconds=numeric)
            else:
                return None
            try:
                return datetime.fromtimestamp(seconds, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        parsed = parse_datetime(text)
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _duration_seconds(sources: list[dict[str, object]]) -> float | None:
    value = _metric(sources, "totalTime")
    if value is not None:
        return value / 1000
    return _metric(sources, "elapsed_time_s", "elapsedTime", "duration")


def _metric(sources: list[dict[str, object]], *keys: str) -> float | None:
    for source in sources:
        value = _number(_first(source, *keys))
        if value is not None:
            return value
    return None


def _metric_int(sources: list[dict[str, object]], *keys: str) -> int | None:
    value = _metric(sources, *keys)
    return int(value) if value is not None else None


def _sport_type(value: object) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return _HUAWEI_SPORT_TYPES.get(int(value))
    normalized = str(value).strip().casefold().replace(" ", "_")
    return _SPORT_NAMES.get(normalized, normalized or None)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first(value: dict[str, object], *keys: str) -> object | None:
    for key in keys:
        if key in value and value[key] is not None and value[key] != "":
            return value[key]
    return None


def _merge_point(target: TrackPoint, source: TrackPoint) -> None:
    for field_name in (
        "latitude",
        "longitude",
        "elevation_m",
        "distance_m",
        "speed_mps",
        "heart_rate_bpm",
        "cadence_rpm",
        "power_w",
    ):
        if getattr(target, field_name) is None:
            setattr(target, field_name, getattr(source, field_name))
