from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Iterable


SUPPORTED_FORMATS = {"fit", "gpx", "tcx"}

GPX_NS = "http://www.topografix.com/GPX/1/1"
GPX_TPX_NS = "http://www.garmin.com/xmlschemas/TrackPointExtension/v1"
TCX_NS = "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"
TCX_EXT_NS = "http://www.garmin.com/xmlschemas/ActivityExtension/v2"
BRIDGE_NS = "https://sport-sync-bridge.example/xmlschemas/extensions/v1"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

_SPORT_TO_TCX = {
    "cycling": "Biking",
    "ride": "Biking",
    "virtual_ride": "Biking",
    "e_biking": "Biking",
    "indoor_cycling": "Biking",
    "mountain_biking": "Biking",
    "running": "Running",
    "run": "Running",
    "trail_run": "Running",
    "generic": "Other",
}
_TCX_TO_SPORT = {"biking": "cycling", "running": "running", "other": "generic"}
_TCX_COLLAPSED_SPORTS = {"e_biking", "virtual_ride", "indoor_cycling", "mountain_biking", "trail_run"}
_FIT_COLLAPSED_SPORTS = {"virtual_ride", "indoor_cycling", "mountain_biking", "trail_run"}
_FIT_RECORD_FIELDS = {
    "timestamp",
    "position_lat",
    "position_long",
    "altitude",
    "enhanced_altitude",
    "distance",
    "speed",
    "enhanced_speed",
    "heart_rate",
    "cadence",
    "power",
}
_FIT_LAP_FIELDS = {
    "start_time",
    "timestamp",
    "total_elapsed_time",
    "total_timer_time",
    "total_distance",
    "total_calories",
    "avg_heart_rate",
    "max_heart_rate",
    "sport",
    "message_index",
}
_FIT_SESSION_FIELDS = {
    "start_time",
    "timestamp",
    "total_elapsed_time",
    "total_timer_time",
    "total_distance",
    "sport",
    "num_laps",
}
_KNOWN_FIT_MESSAGES = {
    "file_id",
    "device_info",
    "file_creator",
    "developer_data_id",
    "field_description",
    "record",
    "lap",
    "session",
    "activity",
    "event",
}
_KNOWN_EXTENSION_FIELDS = {
    "hr",
    "heartrate",
    "cad",
    "cadence",
    "speed",
    "distance",
    "power",
    "watts",
}


@dataclass(slots=True)
class TrackPoint:
    timestamp: datetime | None = None
    latitude: float | None = None
    longitude: float | None = None
    elevation_m: float | None = None
    distance_m: float | None = None
    speed_mps: float | None = None
    heart_rate_bpm: float | None = None
    cadence_rpm: float | None = None
    power_w: float | None = None


@dataclass(slots=True)
class ActivityLap:
    start_time: datetime | None = None
    end_time: datetime | None = None
    elapsed_time_s: float | None = None
    timer_time_s: float | None = None
    distance_m: float | None = None
    calories: int | None = None
    average_heart_rate: float | None = None
    maximum_heart_rate: float | None = None
    track_points: list[TrackPoint] = field(default_factory=list)


@dataclass(slots=True)
class ActivityFile:
    name: str | None = None
    sport_type: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    elapsed_time_s: float | None = None
    timer_time_s: float | None = None
    distance_m: float | None = None
    laps: list[ActivityLap] = field(default_factory=list)
    creator: str | None = None
    losses: list[str] = field(default_factory=list)

    @property
    def track_points(self) -> list[TrackPoint]:
        return [point for lap in self.laps for point in lap.track_points]


@dataclass(frozen=True, slots=True)
class ConversionResult:
    output_path: Path
    source_format: str
    target_format: str
    losses: tuple[str, ...] = ()


def convert_activity_file(
    input_path: Path,
    output_path: Path,
    target_format: str,
    *,
    activity_name: str | None = None,
    sport_type: str | None = None,
) -> ConversionResult:
    input_path = input_path.resolve()
    output_path = output_path.resolve()
    target_format = target_format.lower()
    source_format = _format_for_path(input_path)
    if target_format not in SUPPORTED_FORMATS:
        raise ValueError(f"Unsupported target format: {target_format}")
    if output_path.suffix.lower() != f".{target_format}":
        raise ValueError(f"Output file extension must be .{target_format}")
    if input_path == output_path:
        raise ValueError("Input and output paths must be different")
    if not input_path.is_file():
        raise ValueError(f"Input file does not exist: {input_path}")

    if source_format == "fit":
        activity = _read_fit(input_path)
    elif source_format == "gpx":
        activity = _read_gpx(input_path)
    else:
        activity = _read_tcx(input_path)

    if activity_name:
        activity.name = activity_name
    if sport_type:
        activity.sport_type = sport_type
    _validate_coordinates(activity, input_path)
    if not any(_has_position(point) for point in activity.track_points):
        raise ValueError(f"No GPS track points found in {input_path}")

    if source_format == target_format:
        payload = input_path.read_bytes()
        has_coordinate_marker = _copy_coordinate_marker(input_path, output_path, payload)
        _atomic_write_bytes(output_path, payload)
        if not has_coordinate_marker:
            Path(f"{output_path}.coord.json").unlink(missing_ok=True)
        return ConversionResult(output_path, source_format, target_format)

    if target_format == "gpx":
        payload = _write_gpx(activity)
    elif target_format == "tcx":
        payload = _write_tcx(activity)
    else:
        payload = _write_fit(activity)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_bytes(output_path, payload, validate_fit=target_format == "fit")
    return ConversionResult(
        output_path=output_path,
        source_format=source_format,
        target_format=target_format,
        losses=tuple(dict.fromkeys([*activity.losses, *_target_format_losses(activity, target_format)])),
    )


def read_activity_file(input_path: Path) -> ActivityFile:
    input_path = input_path.resolve()
    source_format = _format_for_path(input_path)
    if not input_path.is_file():
        raise ValueError(f"Input file does not exist: {input_path}")
    if source_format == "fit":
        return _read_fit(input_path)
    if source_format == "gpx":
        return _read_gpx(input_path)
    return _read_tcx(input_path)


def _target_format_losses(activity: ActivityFile, target_format: str) -> list[str]:
    losses: list[str] = []
    missing_positions = sum(1 for point in activity.track_points if not _has_position(point))
    if missing_positions:
        losses.append(f"Track samples without GPS coordinates omitted: {missing_positions}")
    if target_format in {"gpx", "tcx"}:
        empty_laps = sum(
            1 for lap in activity.laps if not any(_has_position(point) for point in lap.track_points)
        )
        if empty_laps:
            losses.append(f"Laps without GPS track points omitted: {empty_laps}")
        point_times = [point.timestamp for point in activity.track_points if point.timestamp is not None]
        if target_format == "gpx" and activity.start_time is not None and point_times:
            if activity.start_time != min(point_times):
                losses.append("GPX does not preserve an activity start time before the first track point")
        if activity.end_time is not None and point_times and activity.end_time != max(point_times):
            losses.append("Activity end time after the last track point is not represented in this format")

    if target_format == "gpx":
        if activity.elapsed_time_s is not None or activity.timer_time_s is not None:
            losses.append("GPX does not preserve activity-level elapsed or timer time")
        if activity.distance_m is not None:
            losses.append("GPX does not preserve activity-level distance summaries")
        summary_fields = {
            "elapsed time": lambda lap: lap.elapsed_time_s,
            "timer time": lambda lap: lap.timer_time_s,
            "lap distance": lambda lap: lap.distance_m,
            "calories": lambda lap: lap.calories,
            "average heart rate": lambda lap: lap.average_heart_rate,
            "maximum heart rate": lambda lap: lap.maximum_heart_rate,
        }
        omitted = [
            label
            for label, get_value in summary_fields.items()
            if any(get_value(lap) is not None for lap in activity.laps)
        ]
        if omitted:
            losses.append("GPX does not preserve lap summary fields: " + ", ".join(omitted))
    elif target_format == "tcx":
        sport = (activity.sport_type or "").lower()
        if sport and sport not in _SPORT_TO_TCX:
            losses.append(f"Sport type {activity.sport_type!r} is represented as TCX Other")
        elif sport in _TCX_COLLAPSED_SPORTS:
            losses.append(f"TCX does not preserve the {activity.sport_type!r} sport subtype")
        if any(
            lap.timer_time_s is not None
            and lap.elapsed_time_s is not None
            and not math.isclose(lap.timer_time_s, lap.elapsed_time_s, abs_tol=0.01)
            for lap in activity.laps
        ):
            losses.append("FIT timer time differs from elapsed time and is not represented in TCX")
        lap_elapsed = [lap.elapsed_time_s for lap in activity.laps if lap.elapsed_time_s is not None]
        if activity.elapsed_time_s is not None and len(lap_elapsed) == len(activity.laps) and lap_elapsed and not math.isclose(
            activity.elapsed_time_s,
            sum(lap_elapsed),
            abs_tol=0.01,
        ):
            losses.append("Activity-level elapsed time differs from TCX lap totals")
        lap_distances = [lap.distance_m for lap in activity.laps if lap.distance_m is not None]
        if activity.distance_m is not None and len(lap_distances) == len(activity.laps) and lap_distances and not math.isclose(
            activity.distance_m,
            sum(lap_distances),
            abs_tol=1.0,
        ):
            losses.append("Activity-level distance differs from TCX lap totals")
        if activity.timer_time_s is not None and (
            activity.elapsed_time_s is None
            or not math.isclose(activity.timer_time_s, activity.elapsed_time_s, abs_tol=0.01)
        ):
            losses.append("Activity-level timer time is not represented in TCX")
    elif target_format == "fit":
        if activity.name:
            losses.append("Activity name is not represented in FIT files")
        if activity.creator:
            losses.append("Source creator metadata is not preserved in FIT output")
        sport = (activity.sport_type or "").lower()
        if sport and _sport_fit_value(sport, default=None) is None:
            losses.append(f"Sport type {activity.sport_type!r} is represented as FIT generic")
        elif sport in _FIT_COLLAPSED_SPORTS:
            losses.append(f"FIT sport field does not preserve the {activity.sport_type!r} subtype")
        empty_laps = sum(1 for lap in activity.laps if not lap.track_points)
        if empty_laps:
            losses.append(f"Laps without track points omitted: {empty_laps}")
    return losses


def _format_for_path(path: Path) -> str:
    value = path.suffix.lower().lstrip(".")
    if value not in SUPPORTED_FORMATS:
        supported = ", ".join(sorted(SUPPORTED_FORMATS))
        raise ValueError(f"Unsupported input file type: .{value}; supported formats: {supported}")
    return value


def _read_fit(path: Path) -> ActivityFile:
    try:
        from fit_tool.fit_file import FitFile
    except ImportError as exc:
        raise RuntimeError("fit-tool is required for FIT conversion") from exc

    try:
        fit_file = FitFile.from_file(str(path))
    except Exception as exc:
        raise ValueError(f"Could not decode FIT file {path}: {exc}") from exc

    activity = ActivityFile()
    records: list[TrackPoint] = []
    laps: list[ActivityLap] = []
    unsupported_record_fields: set[str] = set()
    unsupported_message_types: set[str] = set()
    unsupported_summary_fields: set[str] = set()
    unsupported_metadata_messages: set[str] = set()
    unsupported_sport_values: set[int] = set()
    session_start: datetime | None = None
    session_end: datetime | None = None
    session_count = 0

    for record in fit_file.records:
        message = getattr(record, "message", None)
        name = getattr(message, "name", None)
        if name is None:
            continue
        if name == "record":
            values = _valid_message_values(message)
            records.append(
                TrackPoint(
                    timestamp=_fit_datetime(values.get("timestamp")),
                    latitude=_optional_float(values.get("position_lat")),
                    longitude=_optional_float(values.get("position_long")),
                    elevation_m=_first_float(values, "enhanced_altitude", "altitude"),
                    distance_m=_optional_float(values.get("distance")),
                    speed_mps=_first_float(values, "enhanced_speed", "speed"),
                    heart_rate_bpm=_optional_float(values.get("heart_rate")),
                    cadence_rpm=_optional_float(values.get("cadence")),
                    power_w=_optional_float(values.get("power")),
                )
            )
            unsupported_record_fields.update(
                field.name
                for field in message.fields
                if field.is_valid() and field.name not in _FIT_RECORD_FIELDS
            )
        elif name == "lap":
            values = _valid_message_values(message)
            lap_sport = _optional_int(values.get("sport"))
            if activity.sport_type is None and lap_sport is not None:
                activity.sport_type = _fit_sport_name(lap_sport)
                if activity.sport_type is None:
                    unsupported_sport_values.add(lap_sport)
            laps.append(
                ActivityLap(
                    start_time=_fit_datetime(values.get("start_time")),
                    end_time=_fit_datetime(values.get("timestamp")),
                    elapsed_time_s=_optional_float(values.get("total_elapsed_time")),
                    timer_time_s=_optional_float(values.get("total_timer_time")),
                    distance_m=_optional_float(values.get("total_distance")),
                    calories=_optional_int(values.get("total_calories")),
                    average_heart_rate=_optional_float(values.get("avg_heart_rate")),
                    maximum_heart_rate=_optional_float(values.get("max_heart_rate")),
                )
            )
            unsupported_summary_fields.update(
                field.name
                for field in message.fields
                if field.is_valid() and field.name not in _FIT_LAP_FIELDS
            )
        elif name == "session":
            session_count += 1
            values = _valid_message_values(message)
            session_start = _fit_datetime(values.get("start_time")) or session_start
            session_end = _fit_datetime(values.get("timestamp")) or session_end
            elapsed = _optional_float(values.get("total_elapsed_time"))
            timer_time = _optional_float(values.get("total_timer_time"))
            if elapsed is not None:
                activity.elapsed_time_s = elapsed
            if timer_time is not None:
                activity.timer_time_s = timer_time
            distance = _optional_float(values.get("total_distance"))
            if distance is not None:
                activity.distance_m = distance
            sport_value = _optional_int(values.get("sport"))
            if sport_value is not None:
                activity.sport_type = _fit_sport_name(sport_value)
                if activity.sport_type is None:
                    unsupported_sport_values.add(sport_value)
            unsupported_summary_fields.update(
                field.name
                for field in message.fields
                if field.is_valid() and field.name not in _FIT_SESSION_FIELDS
            )
        elif name == "activity":
            values = _valid_message_values(message)
            activity.end_time = _fit_datetime(values.get("timestamp")) or activity.end_time
            if activity.timer_time_s is None:
                activity.timer_time_s = _optional_float(values.get("total_timer_time"))
            declared_sessions = _optional_int(values.get("num_sessions"))
            if declared_sessions is not None:
                session_count = max(session_count, declared_sessions)
            unsupported_summary_fields.update(
                field.name
                for field in message.fields
                if field.is_valid()
                and field.name not in {"timestamp", "total_timer_time", "num_sessions", "event", "event_type"}
            )
        elif name in {"file_id", "device_info", "file_creator", "developer_data_id", "field_description"}:
            if any(field.is_valid() for field in message.fields):
                unsupported_metadata_messages.add(name)
        elif name not in _KNOWN_FIT_MESSAGES:
            unsupported_message_types.add(name)

    activity.start_time = session_start or activity.start_time
    activity.end_time = session_end or activity.end_time
    if activity.start_time is None and records:
        activity.start_time = next((point.timestamp for point in records if point.timestamp), None)
    if activity.end_time is None and records:
        activity.end_time = next((point.timestamp for point in reversed(records) if point.timestamp), None)

    if laps:
        for point in records:
            lap = _lap_for_point(laps, point)
            lap.track_points.append(point)
        activity.laps = laps
    elif records:
        activity.laps = [_make_lap(records)]
    if activity.elapsed_time_s is None:
        activity.elapsed_time_s = _sum_lap_times(activity.laps, "elapsed_time_s")
    if activity.timer_time_s is None:
        activity.timer_time_s = _sum_lap_times(activity.laps, "timer_time_s")
    if activity.distance_m is None:
        activity.distance_m = _sum_lap_distances(activity.laps)

    if unsupported_record_fields:
        activity.losses.append(
            "FIT record fields omitted: " + ", ".join(sorted(unsupported_record_fields))
        )
    if unsupported_summary_fields:
        activity.losses.append(
            "FIT lap/session summary fields omitted: " + ", ".join(sorted(unsupported_summary_fields))
        )
    if unsupported_message_types:
        activity.losses.append(
            "FIT message types omitted: " + ", ".join(sorted(unsupported_message_types))
        )
    if unsupported_metadata_messages:
        activity.losses.append(
            "FIT file/device metadata omitted: " + ", ".join(sorted(unsupported_metadata_messages))
        )
    if unsupported_sport_values:
        values = ", ".join(str(value) for value in sorted(unsupported_sport_values))
        activity.losses.append(f"Unsupported FIT sport values omitted: {values}")
    if session_count > 1:
        activity.losses.append(f"Multiple FIT sessions combined into one activity: {session_count}")
    return activity


def _read_gpx(path: Path) -> ActivityFile:
    root = _parse_xml(path)
    activity = ActivityFile(creator=root.attrib.get("creator"))
    unsupported_gpx_fields: set[str] = set()
    tracks = [element for element in root.iter() if _local_name(element.tag) == "trk"]
    if tracks:
        if len(tracks) > 1:
            activity.losses.append("Multiple GPX tracks combined into one activity")
        for track in tracks:
            activity.name = activity.name or _child_text(track, "name")
            activity.sport_type = activity.sport_type or _child_text(track, "type")
            unsupported_gpx_fields.update(
                _nonempty_child_names(track, {"name", "type", "trkseg", "extensions"})
            )
            segments = [child for child in track if _local_name(child.tag) == "trkseg"]
            for segment in segments:
                unsupported_gpx_fields.update(_nonempty_child_names(segment, {"trkpt", "extensions"}))
                points = [
                    _parse_gpx_point(element)
                    for element in segment
                    if _local_name(element.tag) == "trkpt"
                ]
                if points:
                    activity.laps.append(_make_lap(points))
    else:
        routes = [element for element in root.iter() if _local_name(element.tag) == "rte"]
        for route in routes:
            activity.name = activity.name or _child_text(route, "name")
            unsupported_gpx_fields.update(
                _nonempty_child_names(route, {"name", "rtept", "extensions"})
            )
            points = [
                _parse_gpx_point(element)
                for element in route
                if _local_name(element.tag) == "rtept"
            ]
            if points:
                activity.laps.append(_make_lap(points))
        if routes:
            activity.losses.append("GPX routes converted to activity track segments")

    if not activity.laps:
        raise ValueError(f"No GPX tracks or routes found in {path}")
    activity.start_time = next(
        (point.timestamp for point in activity.track_points if point.timestamp), None
    )
    activity.end_time = next(
        (point.timestamp for point in reversed(activity.track_points) if point.timestamp), None
    )
    activity.elapsed_time_s = _sum_lap_times(activity.laps, "elapsed_time_s")
    activity.distance_m = _sum_lap_distances(activity.laps)
    activity.sport_type = _normalize_sport(activity.sport_type)
    for point_element in root.iter():
        if _local_name(point_element.tag) in {"trkpt", "rtept"}:
            unsupported_gpx_fields.update(
                _nonempty_child_names(point_element, {"ele", "time", "extensions"})
            )
    if unsupported_gpx_fields:
        activity.losses.append(
            "GPX fields omitted: " + ", ".join(sorted(unsupported_gpx_fields))
        )
    activity.losses.extend(_unknown_xml_extension_losses(root))
    return activity


def _nonempty_child_names(parent: ET.Element, handled: set[str]) -> set[str]:
    return {
        _local_name(child.tag)
        for child in parent
        if _local_name(child.tag).lower() not in handled
        and (child.attrib or (child.text is not None and child.text.strip()) or len(child))
    }


def _parse_gpx_point(element: ET.Element) -> TrackPoint:
    latitude = _optional_float(element.attrib.get("lat"))
    longitude = _optional_float(element.attrib.get("lon"))
    extensions = next((child for child in element if _local_name(child.tag) == "extensions"), None)
    values = _extension_values(extensions)
    return TrackPoint(
        timestamp=_parse_datetime(_child_text(element, "time")),
        latitude=latitude,
        longitude=longitude,
        elevation_m=_optional_float(_child_text(element, "ele")),
        distance_m=values.get("distance"),
        speed_mps=values.get("speed"),
        heart_rate_bpm=values.get("hr"),
        cadence_rpm=values.get("cad"),
        power_w=values.get("power"),
    )


def _read_tcx(path: Path) -> ActivityFile:
    root = _parse_xml(path)
    activities = [element for element in root.iter() if _local_name(element.tag) == "Activity"]
    if not activities:
        raise ValueError(f"No TCX activities found in {path}")
    activity_element = activities[0]
    raw_sport = activity_element.attrib.get("Sport", "")
    activity = ActivityFile(sport_type=_TCX_TO_SPORT.get(raw_sport.lower(), _normalize_sport(raw_sport)))
    if len(activities) > 1:
        activity.losses.append("Only the first activity in the TCX file was converted")
    if _child(activity_element, "Creator") is not None:
        activity.losses.append("TCX activity creator metadata omitted")
    if _child(activity_element, "Training") is not None:
        activity.losses.append("TCX training metadata omitted")
    unsupported_activity_fields = _nonempty_child_names(
        activity_element,
        {"id", "notes", "lap", "creator", "training", "extensions"},
    )
    unsupported_lap_fields: set[str] = set()
    unsupported_track_fields: set[str] = set()
    unsupported_trackpoint_fields: set[str] = set()

    for lap_element in (child for child in activity_element if _local_name(child.tag) == "Lap"):
        unsupported_lap_fields.update(
            _nonempty_child_names(
                lap_element,
                {
                    "totaltimeseconds",
                    "distancemeters",
                    "calories",
                    "averageheartratebpm",
                    "maximumheartratebpm",
                    "track",
                    "extensions",
                },
            )
        )
        for field_name in ("MaximumSpeed", "AverageCadence", "MaximumCadence", "Intensity", "TriggerMethod"):
            if _child_text(lap_element, field_name) is not None:
                unsupported_lap_fields.add(field_name)
        lap = ActivityLap(
            start_time=_parse_datetime(lap_element.attrib.get("StartTime")),
            elapsed_time_s=_optional_float(_child_text(lap_element, "TotalTimeSeconds")),
            distance_m=_optional_float(_child_text(lap_element, "DistanceMeters")),
            calories=_optional_int(_child_text(lap_element, "Calories")),
            average_heart_rate=_optional_float(
                _child_text_by_path(lap_element, "AverageHeartRateBpm", "Value")
            ),
            maximum_heart_rate=_optional_float(
                _child_text_by_path(lap_element, "MaximumHeartRateBpm", "Value")
            ),
        )
        for track in (child for child in lap_element if _local_name(child.tag) == "Track"):
            unsupported_track_fields.update(
                _nonempty_child_names(track, {"trackpoint", "extensions"})
            )
            for point in (child for child in track if _local_name(child.tag) == "Trackpoint"):
                unsupported_trackpoint_fields.update(
                    _nonempty_child_names(
                        point,
                        {
                            "time",
                            "position",
                            "altitudemeters",
                            "distancemeters",
                            "heartratebpm",
                            "cadence",
                            "extensions",
                        },
                    )
                )
                position = _child(point, "Position")
                unsupported_trackpoint_fields.update(
                    _nonempty_child_names(position, {"latitudedegrees", "longitudedegrees"})
                )
                lap.track_points.append(_parse_tcx_point(point))
        lap.end_time = next(
            (point.timestamp for point in reversed(lap.track_points) if point.timestamp), None
        )
        if lap.track_points:
            activity.laps.append(lap)

    if not activity.laps:
        raise ValueError(f"No TCX track points found in {path}")
    activity.name = _child_text(activity_element, "Notes")
    activity.start_time = _parse_datetime(_child_text(activity_element, "Id")) or activity.laps[0].start_time or next(
        (point.timestamp for point in activity.track_points if point.timestamp), None
    )
    activity.end_time = next(
        (point.timestamp for point in reversed(activity.track_points) if point.timestamp), None
    )
    activity.elapsed_time_s = _sum_lap_times(activity.laps, "elapsed_time_s")
    activity.distance_m = _sum_lap_distances(activity.laps)
    if unsupported_activity_fields:
        activity.losses.append(
            "TCX activity fields omitted: " + ", ".join(sorted(unsupported_activity_fields))
        )
    if unsupported_lap_fields:
        activity.losses.append(
            "TCX lap fields omitted: " + ", ".join(sorted(unsupported_lap_fields))
        )
    if unsupported_track_fields:
        activity.losses.append(
            "TCX track fields omitted: " + ", ".join(sorted(unsupported_track_fields))
        )
    if unsupported_trackpoint_fields:
        activity.losses.append(
            "TCX track point fields omitted: " + ", ".join(sorted(unsupported_trackpoint_fields))
        )
    activity.losses.extend(_unknown_xml_extension_losses(root))
    return activity


def _parse_tcx_point(element: ET.Element) -> TrackPoint:
    position = next((child for child in element if _local_name(child.tag) == "Position"), None)
    heart_rate = _child_text_by_path(element, "HeartRateBpm", "Value")
    extensions = next((child for child in element if _local_name(child.tag) == "Extensions"), None)
    values = _extension_values(extensions)
    return TrackPoint(
        timestamp=_parse_datetime(_child_text(element, "Time")),
        latitude=_optional_float(_child_text(position, "LatitudeDegrees")) if position is not None else None,
        longitude=_optional_float(_child_text(position, "LongitudeDegrees")) if position is not None else None,
        elevation_m=_optional_float(_child_text(element, "AltitudeMeters")),
        distance_m=_optional_float(_child_text(element, "DistanceMeters")),
        speed_mps=values.get("speed"),
        heart_rate_bpm=_optional_float(heart_rate),
        cadence_rpm=_optional_float(_child_text(element, "Cadence")),
        power_w=values.get("power"),
    )


def _parse_xml(path: Path) -> ET.Element:
    try:
        return ET.fromstring(path.read_bytes())
    except (OSError, ET.ParseError) as exc:
        raise ValueError(f"Could not parse XML activity file {path}: {exc}") from exc


def _write_gpx(activity: ActivityFile) -> bytes:
    ET.register_namespace("", GPX_NS)
    ET.register_namespace("gpxtpx", GPX_TPX_NS)
    ET.register_namespace("ssb", BRIDGE_NS)
    root = ET.Element(
        _qname(GPX_NS, "gpx"),
        {"version": "1.1", "creator": activity.creator or "sport_sync_bridge"},
    )
    track = ET.SubElement(root, _qname(GPX_NS, "trk"))
    if activity.name:
        _subtext(track, GPX_NS, "name", activity.name)
    if activity.sport_type:
        _subtext(track, GPX_NS, "type", activity.sport_type)

    for lap in activity.laps:
        if not any(_has_position(point) for point in lap.track_points):
            continue
        segment = ET.SubElement(track, _qname(GPX_NS, "trkseg"))
        for point in lap.track_points:
            if not _has_position(point):
                continue
            element = ET.SubElement(
                segment,
                _qname(GPX_NS, "trkpt"),
                {"lat": _format_number(point.latitude), "lon": _format_number(point.longitude)},
            )
            if point.elevation_m is not None:
                _subtext(element, GPX_NS, "ele", _format_number(point.elevation_m))
            if point.timestamp is not None:
                _subtext(element, GPX_NS, "time", _format_datetime(point.timestamp))
            has_tpx_values = point.heart_rate_bpm is not None or point.cadence_rpm is not None
            bridge_values = (
                ("speed", point.speed_mps),
                ("distance", point.distance_m),
                ("power", point.power_w),
            )
            if has_tpx_values or any(value is not None for _, value in bridge_values):
                extension = ET.SubElement(element, _qname(GPX_NS, "extensions"))
                if has_tpx_values:
                    tpx = ET.SubElement(extension, _qname(GPX_TPX_NS, "TrackPointExtension"))
                    if point.heart_rate_bpm is not None:
                        _subtext(tpx, GPX_TPX_NS, "hr", _format_number(point.heart_rate_bpm))
                    if point.cadence_rpm is not None:
                        _subtext(tpx, GPX_TPX_NS, "cad", _format_number(point.cadence_rpm))
                for field_name, value in bridge_values:
                    if value is not None:
                        _subtext(extension, BRIDGE_NS, field_name, _format_number(value))

    return _xml_bytes(root)


def _write_tcx(activity: ActivityFile) -> bytes:
    if any(point.timestamp is None for point in activity.track_points if _has_position(point)):
        raise ValueError("TCX output requires a timestamp for every GPS track point")
    start = activity.start_time or next(
        (point.timestamp for point in activity.track_points if point.timestamp), None
    )
    if start is None:
        raise ValueError("TCX output requires an activity start time")

    ET.register_namespace("", TCX_NS)
    ET.register_namespace("xsi", XSI_NS)
    ET.register_namespace("activity", TCX_EXT_NS)
    root = ET.Element(
        _qname(TCX_NS, "TrainingCenterDatabase"),
        {_qname(XSI_NS, "schemaLocation"): f"{TCX_NS} http://www.garmin.com/xmlschemas/TrainingCenterDatabasev2.xsd"},
    )
    activities = ET.SubElement(root, _qname(TCX_NS, "Activities"))
    activity_element = ET.SubElement(
        activities,
        _qname(TCX_NS, "Activity"),
        {"Sport": _SPORT_TO_TCX.get((activity.sport_type or "").lower(), "Other")},
    )
    _subtext(activity_element, TCX_NS, "Id", _format_datetime(start))
    if activity.name:
        _subtext(activity_element, TCX_NS, "Notes", activity.name)

    for lap in activity.laps:
        points = [point for point in lap.track_points if _has_position(point)]
        if not points:
            continue
        lap_start = lap.start_time or next((point.timestamp for point in points if point.timestamp), None)
        if lap_start is None:
            raise ValueError("TCX output requires a start time for each lap")
        lap_element = ET.SubElement(
            activity_element,
            _qname(TCX_NS, "Lap"),
            {"StartTime": _format_datetime(lap_start)},
        )
        elapsed = lap.elapsed_time_s
        if elapsed is None:
            lap_end = lap.end_time or next(
                (point.timestamp for point in reversed(points) if point.timestamp), None
            )
            elapsed = max(0.0, (lap_end - lap_start).total_seconds()) if lap_end else 0.0
        distance = lap.distance_m if lap.distance_m is not None else _lap_distance(points)
        _subtext(lap_element, TCX_NS, "TotalTimeSeconds", _format_number(elapsed))
        _subtext(lap_element, TCX_NS, "DistanceMeters", _format_number(distance))
        if lap.calories is not None:
            _subtext(lap_element, TCX_NS, "Calories", str(lap.calories))
        average_hr = lap.average_heart_rate if lap.average_heart_rate is not None else _average_hr(points)
        maximum_hr = lap.maximum_heart_rate if lap.maximum_heart_rate is not None else _maximum_hr(points)
        _write_tcx_heart_rate(lap_element, "AverageHeartRateBpm", average_hr)
        _write_tcx_heart_rate(lap_element, "MaximumHeartRateBpm", maximum_hr)
        _subtext(lap_element, TCX_NS, "Intensity", "Active")
        _subtext(lap_element, TCX_NS, "TriggerMethod", "Manual")
        track = ET.SubElement(lap_element, _qname(TCX_NS, "Track"))
        for point in points:
            _write_tcx_point(track, point)

    return _xml_bytes(root)


def _write_tcx_heart_rate(parent: ET.Element, name: str, value: float | None) -> None:
    if value is None:
        return
    element = ET.SubElement(parent, _qname(TCX_NS, name))
    _subtext(element, TCX_NS, "Value", str(round(value)))


def _write_tcx_point(track: ET.Element, point: TrackPoint) -> None:
    if point.timestamp is None:
        raise ValueError("TCX output requires a timestamp for every GPS track point")
    element = ET.SubElement(track, _qname(TCX_NS, "Trackpoint"))
    _subtext(element, TCX_NS, "Time", _format_datetime(point.timestamp))
    position = ET.SubElement(element, _qname(TCX_NS, "Position"))
    _subtext(position, TCX_NS, "LatitudeDegrees", _format_number(point.latitude))
    _subtext(position, TCX_NS, "LongitudeDegrees", _format_number(point.longitude))
    if point.elevation_m is not None:
        _subtext(element, TCX_NS, "AltitudeMeters", _format_number(point.elevation_m))
    if point.distance_m is not None:
        _subtext(element, TCX_NS, "DistanceMeters", _format_number(point.distance_m))
    if point.heart_rate_bpm is not None:
        _write_tcx_heart_rate(element, "HeartRateBpm", point.heart_rate_bpm)
    if point.cadence_rpm is not None:
        _subtext(element, TCX_NS, "Cadence", str(round(point.cadence_rpm)))
    if point.speed_mps is not None or point.power_w is not None:
        extensions = ET.SubElement(element, _qname(TCX_NS, "Extensions"))
        tpx = ET.SubElement(extensions, _qname(TCX_EXT_NS, "TPX"))
        if point.speed_mps is not None:
            _subtext(tpx, TCX_EXT_NS, "Speed", _format_number(point.speed_mps))
        if point.power_w is not None:
            _subtext(tpx, TCX_EXT_NS, "Watts", _format_number(point.power_w))


def _write_fit(activity: ActivityFile) -> bytes:
    try:
        from fit_tool.fit_file_builder import FitFileBuilder
        from fit_tool.profile.messages.activity_message import ActivityMessage
        from fit_tool.profile.messages.event_message import EventMessage
        from fit_tool.profile.messages.file_id_message import FileIdMessage
        from fit_tool.profile.messages.lap_message import LapMessage
        from fit_tool.profile.messages.record_message import RecordMessage
        from fit_tool.profile.messages.session_message import SessionMessage
        from fit_tool.profile.profile_type import Event, EventType, FileType, Manufacturer
    except ImportError as exc:
        raise RuntimeError("fit-tool is required for FIT conversion") from exc

    points = [point for point in activity.track_points if _has_position(point)]
    if not points:
        raise ValueError("FIT output requires GPS track points")
    if any(point.timestamp is None for point in points):
        raise ValueError("FIT output requires a timestamp for every GPS track point")

    first_point_time = min(point.timestamp for point in points if point.timestamp)
    last_point_time = max(point.timestamp for point in points if point.timestamp)
    start = activity.start_time or first_point_time
    end = activity.end_time or last_point_time
    if start > first_point_time or end < last_point_time or end < start:
        raise ValueError("Activity times must enclose all GPS track point timestamps")
    builder = FitFileBuilder(auto_define=True)

    file_id = FileIdMessage()
    _set_field(file_id, "type", FileType.ACTIVITY.value)
    _set_field(file_id, "manufacturer", Manufacturer.DEVELOPMENT.value)
    _set_field(file_id, "product", 0)
    _set_field(file_id, "time_created", _fit_timestamp(start))
    builder.add(file_id)

    start_event = EventMessage()
    _set_field(start_event, "event", Event.TIMER.value)
    _set_field(start_event, "event_type", EventType.START.value)
    _set_field(start_event, "timestamp", _fit_timestamp(start))
    builder.add(start_event)

    laps = activity.laps or [_make_lap(points)]
    for lap_index, lap in enumerate(laps):
        lap_points = [point for point in lap.track_points if _has_position(point)]
        if not lap_points:
            continue
        for point in lap_points:
            message = RecordMessage()
            _set_field(message, "timestamp", _fit_timestamp(point.timestamp))
            _set_field(message, "position_lat", point.latitude)
            _set_field(message, "position_long", point.longitude)
            _set_field(message, "altitude", point.elevation_m)
            _set_field(message, "distance", point.distance_m)
            _set_field(message, "speed", point.speed_mps)
            _set_field(message, "heart_rate", _rounded(point.heart_rate_bpm))
            _set_field(message, "cadence", _rounded(point.cadence_rpm))
            _set_field(message, "power", _rounded(point.power_w))
            builder.add(message)

        lap_message = LapMessage()
        lap_start = lap.start_time or lap_points[0].timestamp
        lap_end = lap.end_time or lap_points[-1].timestamp
        _set_field(lap_message, "start_time", _fit_timestamp(lap_start))
        _set_field(lap_message, "timestamp", _fit_timestamp(lap_end))
        elapsed = lap.elapsed_time_s
        if elapsed is None:
            elapsed = max(0.0, (lap_end - lap_start).total_seconds())
        _set_field(lap_message, "total_elapsed_time", elapsed)
        timer_time = lap.timer_time_s if lap.timer_time_s is not None else elapsed
        _set_field(lap_message, "total_timer_time", timer_time)
        lap_distance = lap.distance_m if lap.distance_m is not None else _lap_distance(lap_points)
        _set_field(lap_message, "total_distance", lap_distance)
        _set_field(lap_message, "total_calories", lap.calories)
        average_hr = lap.average_heart_rate if lap.average_heart_rate is not None else _average_hr(lap_points)
        maximum_hr = lap.maximum_heart_rate if lap.maximum_heart_rate is not None else _maximum_hr(lap_points)
        _set_field(lap_message, "avg_heart_rate", _rounded(average_hr))
        _set_field(lap_message, "max_heart_rate", _rounded(maximum_hr))
        _set_field(lap_message, "sport", _sport_fit_value(activity.sport_type))
        _set_field(lap_message, "message_index", lap_index)
        builder.add(lap_message)

    session = SessionMessage()
    _set_field(session, "start_time", _fit_timestamp(start))
    _set_field(session, "timestamp", _fit_timestamp(end))
    elapsed = activity.elapsed_time_s
    if elapsed is None:
        elapsed = max(0.0, (end - start).total_seconds())
    timer_time = activity.timer_time_s
    if timer_time is None:
        timer_time = _sum_lap_times(laps, "timer_time_s")
        if timer_time is None:
            timer_time = elapsed
    _set_field(session, "total_elapsed_time", elapsed)
    _set_field(session, "total_timer_time", timer_time)
    total_distance = activity.distance_m
    if total_distance is None:
        total_distance = sum(
            lap.distance_m if lap.distance_m is not None else _lap_distance(lap.track_points)
            for lap in laps
            if lap.track_points
        )
    _set_field(session, "total_distance", total_distance)
    _set_field(session, "sport", _sport_fit_value(activity.sport_type))
    _set_field(session, "num_laps", len([lap for lap in laps if lap.track_points]))
    builder.add(session)

    stop_event = EventMessage()
    _set_field(stop_event, "event", Event.TIMER.value)
    _set_field(stop_event, "event_type", EventType.STOP.value)
    _set_field(stop_event, "timestamp", _fit_timestamp(end))
    builder.add(stop_event)

    activity_message = ActivityMessage()
    _set_field(activity_message, "timestamp", _fit_timestamp(end))
    _set_field(activity_message, "total_timer_time", timer_time)
    _set_field(activity_message, "num_sessions", 1)
    builder.add(activity_message)
    return builder.build().to_bytes()


def _set_field(message: object, name: str, value: object | None) -> None:
    if value is None:
        return
    field = message.get_field_by_name(name)
    if field is not None:
        field.set_value(0, value)


def _valid_message_values(message: object) -> dict[str, object]:
    return {
        item.name: item.get_value()
        for item in getattr(message, "fields", [])
        if item.is_valid()
    }


def _first_float(values: dict[str, object], *names: str) -> float | None:
    for name in names:
        result = _optional_float(values.get(name))
        if result is not None:
            return result
    return None


def _optional_float(value: object | None) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    if isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


def _optional_int(value: object | None) -> int | None:
    number = _optional_float(value)
    return round(number) if number is not None else None


def _rounded(value: float | None) -> int | None:
    return round(value) if value is not None else None


def _fit_datetime(value: object | None) -> datetime | None:
    timestamp = _optional_float(value)
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(timestamp / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _fit_timestamp(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return round(value.astimezone(timezone.utc).timestamp() * 1000)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _format_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _normalize_sport(value: str | None) -> str | None:
    return value.strip().lower().replace(" ", "_") if value and value.strip() else None


def _fit_sport_name(value: int) -> str | None:
    return _fit_sport_ids_by_value().get(value)


@lru_cache(maxsize=1)
def _fit_sport_ids_by_value() -> dict[int, str]:
    try:
        from fit_tool.profile.profile_type import Sport
    except ImportError as exc:
        raise RuntimeError("fit-tool is required for FIT sport type conversion") from exc

    result: dict[int, str] = {}
    for name in dir(Sport):
        if name.startswith("_") or name == "ALL":
            continue
        value = getattr(getattr(Sport, name), "value", None)
        if isinstance(value, int):
            result[value] = name.lower()
    return result


def _sport_fit_value(value: str | None, *, default: int | None = 0) -> int | None:
    normalized = (value or "").lower()
    aliases = {
        "ride": "cycling",
        "virtual_ride": "cycling",
        "indoor_cycling": "cycling",
        "mountain_biking": "cycling",
        "run": "running",
        "trail_run": "running",
        "walk": "walking",
        "hike": "hiking",
    }
    fit_name = aliases.get(normalized, normalized)
    fit_names_by_value = _fit_sport_ids_by_value()
    value_by_name = {name: number for number, name in fit_names_by_value.items()}
    return value_by_name.get(fit_name, default)


def _lap_for_point(laps: list[ActivityLap], point: TrackPoint) -> ActivityLap:
    if point.timestamp is not None:
        for lap in laps:
            if lap.start_time and lap.end_time and lap.start_time <= point.timestamp <= lap.end_time:
                return lap
    return laps[-1]


def _make_lap(points: Iterable[TrackPoint]) -> ActivityLap:
    track_points = list(points)
    timestamps = [point.timestamp for point in track_points if point.timestamp is not None]
    elapsed = (timestamps[-1] - timestamps[0]).total_seconds() if len(timestamps) > 1 else None
    return ActivityLap(
        start_time=timestamps[0] if timestamps else None,
        end_time=timestamps[-1] if timestamps else None,
        elapsed_time_s=max(0.0, elapsed) if elapsed is not None else None,
        distance_m=_lap_distance(track_points),
        average_heart_rate=_average_hr(track_points),
        maximum_heart_rate=_maximum_hr(track_points),
        track_points=track_points,
    )


def _lap_distance(points: Iterable[TrackPoint]) -> float:
    track_points = list(points)
    distances = [point.distance_m for point in track_points if point.distance_m is not None]
    if distances:
        return max(distances) - min(distances) if len(distances) > 1 else distances[0]
    total = 0.0
    previous: TrackPoint | None = None
    for point in track_points:
        if previous is not None and _has_position(previous) and _has_position(point):
            total += _haversine_m(previous.latitude, previous.longitude, point.latitude, point.longitude)
        previous = point
    return total


def _haversine_m(lat1: float | None, lon1: float | None, lat2: float | None, lon2: float | None) -> float:
    if None in (lat1, lon1, lat2, lon2):
        return 0.0
    radius = 6_371_008.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    value = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(value)))


def _average_hr(points: Iterable[TrackPoint]) -> float | None:
    values = [point.heart_rate_bpm for point in points if point.heart_rate_bpm is not None]
    return sum(values) / len(values) if values else None


def _maximum_hr(points: Iterable[TrackPoint]) -> float | None:
    values = [point.heart_rate_bpm for point in points if point.heart_rate_bpm is not None]
    return max(values) if values else None


def _sum_lap_times(laps: Iterable[ActivityLap], attribute: str) -> float | None:
    lap_values = [getattr(lap, attribute) for lap in laps]
    if not lap_values or any(value is None for value in lap_values):
        return None
    return sum(value for value in lap_values if value is not None)


def _sum_lap_distances(laps: Iterable[ActivityLap]) -> float | None:
    values = [lap.distance_m for lap in laps]
    if not values or any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def _has_position(point: TrackPoint) -> bool:
    return point.latitude is not None and point.longitude is not None


def _validate_coordinates(activity: ActivityFile, path: Path) -> None:
    for point in activity.track_points:
        if not _has_position(point):
            continue
        if not (-90 <= point.latitude <= 90 and -180 <= point.longitude <= 180):
            raise ValueError(f"Invalid GPS coordinates in {path}: {point.latitude}, {point.longitude}")


def _parse_extension_values(element: ET.Element | None) -> dict[str, float]:
    result: dict[str, float] = {}
    if element is None:
        return result
    for child in element.iter():
        local = _local_name(child.tag).lower()
        if local in _KNOWN_EXTENSION_FIELDS:
            value = _optional_float(child.text)
            if value is not None:
                if local in {"heartrate"}:
                    local = "hr"
                elif local == "cadence":
                    local = "cad"
                elif local == "watts":
                    local = "power"
                result[local] = value
    return result


def _extension_values(element: ET.Element | None) -> dict[str, float]:
    return _parse_extension_values(element)


def _unknown_xml_extension_losses(root: ET.Element) -> list[str]:
    unknown: set[str] = set()
    for element in root.iter():
        if _local_name(element.tag) != "extensions":
            continue
        for child in element.iter():
            if child is element:
                continue
            local = _local_name(child.tag).lower()
            if local not in _KNOWN_EXTENSION_FIELDS and child.text and child.text.strip():
                unknown.add(local)
    return ["XML extension fields omitted: " + ", ".join(sorted(unknown))] if unknown else []


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(parent: ET.Element | None, name: str) -> ET.Element | None:
    if parent is None:
        return None
    return next((element for element in parent if _local_name(element.tag) == name), None)


def _child_text(parent: ET.Element | None, name: str) -> str | None:
    element = _child(parent, name)
    return element.text.strip() if element is not None and element.text and element.text.strip() else None


def _child_text_by_path(parent: ET.Element | None, *names: str) -> str | None:
    current = parent
    for name in names:
        current = _child(current, name)
        if current is None:
            return None
    return current.text.strip() if current.text and current.text.strip() else None


def _qname(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}"


def _subtext(parent: ET.Element, namespace: str, name: str, value: str) -> ET.Element:
    element = ET.SubElement(parent, _qname(namespace, name))
    element.text = value
    return element


def _format_number(value: float | None) -> str:
    if value is None:
        raise ValueError("Cannot format a missing number")
    return format(value, ".12g")


def _xml_bytes(root: ET.Element) -> bytes:
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _atomic_write_bytes(path: Path, payload: bytes, *, validate_fit: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if validate_fit:
            from fit_tool.fit_file import FitFile

            FitFile.from_file(str(temporary))
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _copy_coordinate_marker(input_path: Path, output_path: Path, output_payload: bytes) -> bool:
    input_marker = Path(f"{input_path}.coord.json")
    output_marker = Path(f"{output_path}.coord.json")
    if not input_marker.exists():
        return False
    try:
        marker = json.loads(input_marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read coordinate marker {input_marker}: {exc}") from exc

    input_digest = _sha256(input_path)
    if not isinstance(marker, dict) or marker.get("output_sha256") != input_digest:
        raise RuntimeError(f"Coordinate marker does not match {input_path}")
    marker["output_sha256"] = hashlib.sha256(output_payload).hexdigest()
    _write_json_marker_atomically(output_marker, marker)
    return True


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_marker_atomically(path: Path, marker: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(marker, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
