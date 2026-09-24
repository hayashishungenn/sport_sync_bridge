from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path

from .formats import (
    ActivityFile,
    ActivityLap,
    TrackPoint,
    _atomic_write_bytes,
    _write_fit,
    read_activity_file,
)


MAX_MERGED_RECORDS = 50_000
MAX_MERGE_INPUT_BYTES = 64 * 1024 * 1024
_EXTREME_FIELDS = (
    "elevation_m",
    "heart_rate_bpm",
    "cadence_rpm",
    "power_w",
    "speed_mps",
    "distance_m",
)


@dataclass(frozen=True, slots=True)
class ActivityMergeResult:
    output_path: Path
    input_count: int
    records_before: int
    records_after: int
    losses: tuple[str, ...]

    @property
    def decimated(self) -> bool:
        return self.records_after < self.records_before


def merge_fit_files(
    input_paths: list[Path],
    output_path: Path,
    *,
    name: str | None = None,
    max_records: int = MAX_MERGED_RECORDS,
) -> ActivityMergeResult:
    if len(input_paths) < 2:
        raise ValueError("Activity merge requires at least two FIT files")
    if max_records < 2:
        raise ValueError("The merged record limit must be at least 2")

    inputs = [path.expanduser().resolve() for path in input_paths]
    output = output_path.expanduser().resolve()
    if output.suffix.lower() != ".fit":
        raise ValueError("Merged FIT output path must end with .fit")
    if output in inputs:
        raise ValueError("Output path must be different from every input FIT file")
    if len(set(inputs)) != len(inputs):
        raise ValueError("The same FIT input file was specified more than once")

    source_activities: list[tuple[Path, ActivityFile]] = []
    for path in inputs:
        if path.suffix.lower() != ".fit":
            raise ValueError(f"Activity merge only accepts FIT files: {path}")
        if not path.is_file():
            raise ValueError(f"Input FIT file does not exist: {path}")
        if path.stat().st_size > MAX_MERGE_INPUT_BYTES:
            raise ValueError(f"Input FIT file is too large: {path}")
        activity = read_activity_file(path)
        if not activity.track_points:
            raise ValueError(f"FIT file contains no record messages: {path}")
        if any(point.timestamp is None for point in activity.track_points):
            raise ValueError(f"Every FIT record needs a timestamp to merge: {path}")
        source_activities.append((path, activity))

    sport_types = {
        activity.sport_type.strip().lower()
        for _, activity in source_activities
        if activity.sport_type and activity.sport_type.strip()
    }
    if len(sport_types) > 1:
        raise ValueError("FIT files with different sport types cannot be merged together")

    merged = ActivityFile(
        name=name or f"Merged {len(source_activities)} activities",
        sport_type=next(iter(sport_types), None),
        creator="sport-sync-bridge",
    )
    merged_records: list[TrackPoint] = []
    losses: list[str] = []
    previous_end: datetime | None = None
    merged_start: datetime | None = None
    merged_end: datetime | None = None
    distance_offset = 0.0
    timer_total = 0.0

    for path, source in source_activities:
        losses.extend(source.losses)
        source_points = sorted(
            source.track_points,
            key=lambda point: _as_utc(point.timestamp),
        )
        point_start = _as_utc(source_points[0].timestamp)
        point_end = _as_utc(source_points[-1].timestamp)
        source_start = min(_as_utc(source.start_time), point_start) if source.start_time else point_start
        source_end = max(_as_utc(source.end_time), point_end) if source.end_time else point_end
        if source_end < source_start:
            raise ValueError(f"FIT activity has an end time before its start time: {path}")

        forced = previous_end is not None and source_start <= previous_end
        shift = previous_end + timedelta(seconds=1) - source_start if forced and previous_end else timedelta(0)
        output_start = source_start + shift
        output_end = source_end + shift
        if merged_start is None:
            merged_start = output_start
        if previous_end is not None and not forced and output_start > previous_end:
            rest_seconds = (output_start - previous_end).total_seconds()
            if rest_seconds > 2:
                merged.laps.append(
                    ActivityLap(
                        start_time=previous_end + timedelta(seconds=1),
                        end_time=output_start - timedelta(seconds=1),
                        elapsed_time_s=rest_seconds,
                        timer_time_s=0.0,
                        distance_m=0.0,
                    )
                )

        source_record_distances = [
            point.distance_m for point in source_points if point.distance_m is not None
        ]
        source_lap_distances = [
            lap.distance_m for lap in source.laps if lap.distance_m is not None
        ]
        source_distance = source.distance_m
        if source_distance is None:
            source_distance = (
                sum(source_lap_distances)
                if source_lap_distances
                else max(source_record_distances, default=0.0)
            )

        point_replacements: dict[int, TrackPoint] = {}
        for point in source_points:
            timestamp = _as_utc(point.timestamp) + shift
            distance = point.distance_m
            if distance is not None:
                distance += distance_offset
            replacement = replace(point, timestamp=timestamp, distance_m=distance)
            point_replacements[id(point)] = replacement
            merged_records.append(replacement)

        source_laps = source.laps or [
            ActivityLap(
                start_time=source_start,
                end_time=source_end,
                elapsed_time_s=source.elapsed_time_s,
                timer_time_s=source.timer_time_s,
                distance_m=source.distance_m,
                track_points=source_points,
            )
        ]
        for lap in source_laps:
            lap_points = [point_replacements[id(point)] for point in lap.track_points]
            merged.laps.append(
                replace(
                    lap,
                    start_time=_shift_optional_time(lap.start_time, shift),
                    end_time=_shift_optional_time(lap.end_time, shift),
                    track_points=lap_points,
                )
            )

        segment_elapsed = source.elapsed_time_s
        if segment_elapsed is None:
            segment_elapsed = max(0.0, (source_end - source_start).total_seconds())
        segment_timer = source.timer_time_s
        if segment_timer is None:
            lap_timers = [lap.timer_time_s for lap in source.laps if lap.timer_time_s is not None]
            segment_timer = sum(lap_timers) if lap_timers else segment_elapsed
        timer_total += segment_timer
        distance_offset += max(0.0, source_distance)
        previous_end = output_end
        merged_end = output_end

    selected_indexes = _select_record_indexes(merged_records, max_records)
    if len(selected_indexes) != len(merged_records):
        selected_records = {id(merged_records[index]) for index in selected_indexes}
        for lap in merged.laps:
            lap.track_points = [point for point in lap.track_points if id(point) in selected_records]

    merged.laps.sort(key=_lap_sort_key)
    merged.start_time = merged_start
    merged.end_time = merged_end
    merged.elapsed_time_s = (
        max(0.0, (merged_end - merged_start).total_seconds())
        if merged_start is not None and merged_end is not None
        else None
    )
    merged.timer_time_s = timer_total
    merged.distance_m = distance_offset
    merged.losses = list(dict.fromkeys(losses))
    merged.losses.append(
        "Source FIT device identity, developer fields, and non-record messages are not copied"
    )
    if name:
        merged.losses.append("Merged activity name is not stored in the FIT activity messages")

    payload = _write_fit(merged, allow_trackless_records=True)
    _atomic_write_bytes(output, payload, validate_fit=True)
    return ActivityMergeResult(
        output_path=output,
        input_count=len(inputs),
        records_before=len(merged_records),
        records_after=len(selected_indexes),
        losses=tuple(dict.fromkeys(merged.losses)),
    )


def _select_record_indexes(points: list[TrackPoint], limit: int) -> list[int]:
    count = len(points)
    if count <= limit:
        return list(range(count))

    retained = {0, count - 1}
    for field_name in _EXTREME_FIELDS:
        candidates = [
            (index, float(value))
            for index, point in enumerate(points)
            if (value := getattr(point, field_name)) is not None
            and math.isfinite(float(value))
        ]
        if not candidates:
            continue
        for index, _ in (min(candidates, key=lambda item: item[1]), max(candidates, key=lambda item: item[1])):
            if len(retained) < limit:
                retained.add(index)

    available = [index for index in range(count) if index not in retained]
    slots = limit - len(retained)
    if slots >= len(available):
        retained.update(available)
    elif slots == 1:
        retained.add(available[len(available) // 2])
    elif slots > 1:
        last = len(available) - 1
        retained.update(available[round(slot * last / (slots - 1))] for slot in range(slots))
    return sorted(retained)


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        raise ValueError("FIT record timestamp is missing")
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _shift_optional_time(value: datetime | None, shift: timedelta) -> datetime | None:
    return _as_utc(value) + shift if value is not None else None


def _lap_sort_key(lap: ActivityLap) -> datetime:
    if lap.start_time is not None:
        return _as_utc(lap.start_time)
    point_times = [point.timestamp for point in lap.track_points if point.timestamp is not None]
    return min((_as_utc(value) for value in point_times), default=datetime.max.replace(tzinfo=timezone.utc))
