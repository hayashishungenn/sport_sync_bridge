from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .ai_workout import _parse_duration, normalize_ai_workout
from .formats import ActivityFile, ActivityLap, TrackPoint, _atomic_write_bytes, _write_fit


_MAX_COURSE_BYTES = 2_000_000
_MAX_COURSE_SEGMENTS = 10_000
_MAX_COURSE_DURATION_S = 24 * 60 * 60
_POWER_ZONE_FTP_PERCENT = (0, 100, 130, 160, 190, 220, 260, 320)
_POWER_TARGET_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(%\s*ftp|w|watts?)$", re.IGNORECASE)
_POWER_ZONE_RE = re.compile(r"^(?:power\s*)?zone\s*(\d+)(?:\s*power)?$", re.IGNORECASE)


class RideCourseError(ValueError):
    pass


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise RideCourseError(f"{name} must be a finite number")
    return float(value)


def _dart_round(value: float) -> int:
    return math.floor(value + 0.5)


def _validate_ftp(ftp_watts: float | None) -> float | None:
    if ftp_watts is None:
        return None
    ftp = _finite_number(ftp_watts, "FTP")
    if not 0 < ftp <= 32767:
        raise RideCourseError("FTP must be greater than 0 and no more than 32767 watts")
    return ftp


def intensity_multiplier_from_percent(percent: float) -> float:
    value = _finite_number(percent, "Intensity percentage")
    if value < 10:
        raise RideCourseError("Intensity percentage must be at least 10")
    return value / 100.0


@dataclass(frozen=True, slots=True)
class PowerSegment:
    start_time_s: float
    end_time_s: float
    start_power_w: float
    end_power_w: float
    label: str = ""

    def __post_init__(self) -> None:
        start = _finite_number(self.start_time_s, "Segment start_time_s")
        end = _finite_number(self.end_time_s, "Segment end_time_s")
        start_power = _finite_number(self.start_power_w, "Segment start_power_w")
        end_power = _finite_number(self.end_power_w, "Segment end_power_w")
        if start < 0 or end <= start:
            raise RideCourseError("Each course segment must have a positive duration and non-negative start time")
        if not 0 <= start_power <= 32767 or not 0 <= end_power <= 32767:
            raise RideCourseError("Course target power must be from 0 to 32767 watts")
        if not isinstance(self.label, str):
            raise RideCourseError("Segment label must be text")
        if len(self.label) > 160:
            raise RideCourseError("Segment label cannot exceed 160 characters")

    @property
    def duration_s(self) -> float:
        return self.end_time_s - self.start_time_s

    def target_power_at(self, segment_elapsed_s: float, intensity: float = 1.0) -> int:
        elapsed = _finite_number(segment_elapsed_s, "Segment elapsed time")
        multiplier = _finite_number(intensity, "Intensity multiplier")
        if multiplier < 0.1:
            raise RideCourseError("Intensity multiplier must be at least 0.1")
        progress = min(1.0, max(0.0, elapsed / self.duration_s))
        power = self.start_power_w + (self.end_power_w - self.start_power_w) * progress
        scaled_power = power * multiplier
        if not math.isfinite(scaled_power):
            raise RideCourseError("Scaled trainer target must be finite")
        target = _dart_round(scaled_power)
        if target > 32767:
            raise RideCourseError("Scaled trainer target exceeds 32767 watts")
        return target


@dataclass(frozen=True, slots=True)
class RideCourse:
    segments: tuple[PowerSegment, ...]
    name: str = "Ride course"
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.segments:
            raise RideCourseError("Ride course must contain at least one segment")
        if len(self.segments) > _MAX_COURSE_SEGMENTS:
            raise RideCourseError(f"Ride course exceeds {_MAX_COURSE_SEGMENTS} segments")
        if not isinstance(self.name, str) or not self.name.strip() or len(self.name) > 160:
            raise RideCourseError("Ride course name must contain 1 to 160 characters")
        previous_end = 0.0
        for index, segment in enumerate(self.segments):
            if not isinstance(segment, PowerSegment):
                raise RideCourseError(f"segments[{index}] is not a power segment")
            if not math.isclose(segment.start_time_s, previous_end, rel_tol=0, abs_tol=1e-6):
                raise RideCourseError("Ride course segments must start at 0 and be contiguous")
            previous_end = segment.end_time_s
        if previous_end > _MAX_COURSE_DURATION_S:
            raise RideCourseError(f"Ride course exceeds {_MAX_COURSE_DURATION_S} seconds")

    @property
    def duration_s(self) -> float:
        return self.segments[-1].end_time_s

    def segment_index_at(self, elapsed_s: float) -> int | None:
        elapsed = _finite_number(elapsed_s, "Course elapsed time")
        if elapsed < 0:
            raise RideCourseError("Course elapsed time cannot be negative")
        for index, segment in enumerate(self.segments):
            if elapsed < segment.end_time_s:
                return index
        return None

    def target_power_at(self, elapsed_s: float, intensity: float = 1.0) -> int:
        elapsed = _finite_number(elapsed_s, "Course elapsed time")
        index = self.segment_index_at(elapsed)
        if index is None:
            return 0
        segment = self.segments[index]
        return segment.target_power_at(elapsed - segment.start_time_s, intensity)


@dataclass(slots=True)
class RideSession:
    course: RideCourse
    intensity: float = 1.0
    erg_mode: bool = True
    segment_index: int = 0
    segment_elapsed_s: float = 0.0
    wall_elapsed_s: float = 0.0
    last_sent_power_w: int = -1

    def __post_init__(self) -> None:
        self.intensity = _finite_number(self.intensity, "Intensity multiplier")
        if self.intensity < 0.1:
            raise RideCourseError("Intensity multiplier must be at least 0.1")

    @property
    def current_segment(self) -> PowerSegment | None:
        if self.segment_index >= len(self.course.segments):
            return None
        return self.course.segments[self.segment_index]

    @property
    def course_elapsed_s(self) -> float:
        segment = self.current_segment
        if segment is None:
            return self.course.duration_s
        return segment.start_time_s + self.segment_elapsed_s

    @property
    def segment_remaining_s(self) -> float | None:
        segment = self.current_segment
        if segment is None:
            return None
        return max(0.0, segment.duration_s - self.segment_elapsed_s)

    @property
    def current_target_power_w(self) -> int:
        segment = self.current_segment
        if segment is None:
            return 0
        return segment.target_power_at(self.segment_elapsed_s, self.intensity)

    @property
    def power_bias_percentage(self) -> int:
        return _dart_round(self.intensity * 100)

    def increase_intensity(self) -> int:
        self.intensity = round(self.intensity + 0.05, 10)
        return self.current_target_power_w

    def decrease_intensity(self) -> int:
        self.intensity = max(0.1, round(self.intensity - 0.05, 10))
        return self.current_target_power_w

    def skip_interval(self) -> bool:
        remaining = self.segment_remaining_s
        if remaining is None or self.segment_index + 1 >= len(self.course.segments):
            return False
        self.segment_index += 1
        self.segment_elapsed_s = 0.0
        return True

    def toggle_erg_mode(self) -> int | None:
        self.erg_mode = not self.erg_mode
        return self.current_target_power_w if self.erg_mode else None

    def advance(self, seconds: float = 1.0) -> None:
        remaining_time = _finite_number(seconds, "Advance duration")
        if remaining_time <= 0:
            raise RideCourseError("Advance duration must be positive")
        self.wall_elapsed_s += remaining_time
        while remaining_time > 0.0:
            segment = self.current_segment
            if segment is None:
                return
            segment_remaining = segment.duration_s - self.segment_elapsed_s
            step = min(remaining_time, segment_remaining)
            self.segment_elapsed_s += step
            remaining_time = max(0.0, remaining_time - step)
            if self.segment_elapsed_s >= segment.duration_s - 1e-9:
                self.segment_index += 1
                self.segment_elapsed_s = 0.0


def demo_ride_course() -> RideCourse:
    return RideCourse(
        (
            PowerSegment(0, 300, 110, 140, "Warm up"),
            PowerSegment(300, 420, 165, 165, "Interval"),
            PowerSegment(420, 600, 140, 110, "Cool down"),
        ),
        "GarSync demo ride",
    )


def load_ride_course(path: Path, *, ftp_watts: float | None = None) -> RideCourse:
    ftp = _validate_ftp(ftp_watts)
    source_path = path.expanduser()
    if source_path.suffix.lower() == ".fit":
        source_path = Path(f"{source_path}.meta")
    try:
        if source_path.stat().st_size > _MAX_COURSE_BYTES:
            raise RideCourseError(f"Ride course file exceeds {_MAX_COURSE_BYTES} bytes")
        payload_text = source_path.read_text(encoding="utf-8-sig")
    except RideCourseError:
        raise
    except OSError as exc:
        raise RideCourseError(f"Cannot read ride course {source_path}: {exc}") from exc

    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise RideCourseError(f"Ride course is not valid JSON: {source_path}") from exc
    if not isinstance(payload, dict):
        raise RideCourseError("Ride course JSON must contain an object")
    if isinstance(payload.get("segments"), list):
        return _course_from_segments(payload)

    try:
        workout_document = payload
        if payload.get("type") == "workout":
            workout_document = {"workouts": {"workout": payload}}
        workout = normalize_ai_workout(
            json.dumps(workout_document, ensure_ascii=False),
            sport="cycling",
        )
    except ValueError as exc:
        raise RideCourseError(f"Unsupported ride workout: {exc}") from exc
    return _course_from_workout(workout, ftp)


def write_ride_activity_fit(
    name: str,
    start_time: datetime,
    elapsed_time_s: float,
    samples: Iterable[TrackPoint],
    output_path: Path,
) -> Path:
    if not isinstance(name, str) or not name.strip() or len(name) > 160:
        raise RideCourseError("Ride activity name must contain 1 to 160 characters")
    if not isinstance(start_time, datetime) or start_time.tzinfo is None or start_time.utcoffset() is None:
        raise RideCourseError("Ride activity start time must include a timezone")
    duration = _finite_number(elapsed_time_s, "Ride activity duration")
    if duration <= 0 or duration > _MAX_COURSE_DURATION_S:
        raise RideCourseError(f"Ride activity duration must be from 0 to {_MAX_COURSE_DURATION_S} seconds")
    destination = output_path.expanduser().resolve()
    if destination.suffix.lower() != ".fit":
        raise RideCourseError("Ride activity output path must end in .fit")
    if destination.exists():
        raise RideCourseError(f"Ride activity output already exists: {destination}")

    start = start_time.astimezone(timezone.utc)
    end = start + timedelta(seconds=duration)
    points: list[TrackPoint] = []
    previous_timestamp: datetime | None = None
    for index, sample in enumerate(samples):
        if (
            not isinstance(sample, TrackPoint)
            or sample.timestamp is None
            or sample.timestamp.tzinfo is None
            or sample.timestamp.utcoffset() is None
        ):
            raise RideCourseError(f"Ride activity sample {index} needs a timezone-aware timestamp")
        timestamp = sample.timestamp.astimezone(timezone.utc)
        if timestamp < start or timestamp > end:
            raise RideCourseError(f"Ride activity sample {index} falls outside the activity time range")
        if previous_timestamp is not None and timestamp < previous_timestamp:
            raise RideCourseError("Ride activity samples must be in timestamp order")
        points.append(
            TrackPoint(
                timestamp=timestamp,
                latitude=sample.latitude,
                longitude=sample.longitude,
                elevation_m=sample.elevation_m,
                distance_m=sample.distance_m,
                speed_mps=sample.speed_mps,
                heart_rate_bpm=sample.heart_rate_bpm,
                cadence_rpm=sample.cadence_rpm,
                power_w=sample.power_w,
            )
        )
        previous_timestamp = timestamp

    if not points or points[0].timestamp > start:
        points.insert(0, TrackPoint(timestamp=start))
    if points[-1].timestamp < end:
        points.append(TrackPoint(timestamp=end))
    lap = ActivityLap(
        start_time=start,
        end_time=end,
        elapsed_time_s=duration,
        timer_time_s=duration,
        track_points=points,
    )
    activity = ActivityFile(
        name=name.strip(),
        sport_type="virtual_ride",
        start_time=start,
        end_time=end,
        elapsed_time_s=duration,
        timer_time_s=duration,
        laps=[lap],
    )
    try:
        payload = _write_fit(activity, allow_trackless_records=True)
        _atomic_write_bytes(destination, payload, validate_fit=True)
    except Exception as exc:
        raise RideCourseError(f"Could not write ride activity FIT: {exc}") from exc
    return destination


def _course_from_segments(payload: dict[str, Any]) -> RideCourse:
    raw_segments = payload["segments"]
    if not raw_segments:
        raise RideCourseError("Ride course must contain at least one segment")
    if len(raw_segments) > _MAX_COURSE_SEGMENTS:
        raise RideCourseError(f"Ride course exceeds {_MAX_COURSE_SEGMENTS} segments")
    segments: list[PowerSegment] = []
    for index, value in enumerate(raw_segments):
        if not isinstance(value, dict):
            raise RideCourseError(f"segments[{index}] must be an object")
        try:
            segments.append(
                PowerSegment(
                    value["start_time_s"],
                    value["end_time_s"],
                    value["start_power_w"],
                    value["end_power_w"],
                    value.get("label", ""),
                )
            )
        except KeyError as exc:
            raise RideCourseError(f"segments[{index}] is missing {exc.args[0]}") from exc
    name = payload.get("name", "Ride course")
    return RideCourse(tuple(segments), name)


def _course_from_workout(workout: dict[str, Any], ftp: float | None) -> RideCourse:
    segments: list[PowerSegment] = []
    warnings: list[str] = []
    elapsed = 0.0
    for index, step in enumerate(_expand_steps(workout["steps"])):
        try:
            duration_kind, amount = _parse_duration(step["duration"])
        except (KeyError, ValueError) as exc:
            raise RideCourseError(f"steps[{index}] has an invalid duration: {exc}") from exc
        if duration_kind == "time" and amount is not None:
            duration = amount
        elif duration_kind == "distance" and amount is not None:
            duration = amount * 0.12
        else:
            duration = 300.0
        target, warning = _workout_target_watts(step.get("target"), ftp, index)
        if warning:
            warnings.append(warning)
        intensity = str(step.get("intensity") or "").strip()
        segments.append(PowerSegment(elapsed, elapsed + duration, target, target, intensity))
        elapsed += duration
    if not segments:
        raise RideCourseError("Ride workout contains no usable steps")
    return RideCourse(tuple(segments), workout["name"], tuple(warnings))


def _expand_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []

    def add(items: list[dict[str, Any]], depth: int) -> None:
        if depth > 5:
            raise RideCourseError("Workout repeat nesting exceeds 5 levels")
        for step in items:
            if "repeat" in step:
                repeat = step["repeat"]
                children = step.get("steps")
                if not isinstance(repeat, int) or isinstance(repeat, bool) or not 1 <= repeat <= 100:
                    raise RideCourseError("Workout repeat count must be from 1 to 100")
                if not isinstance(children, list) or not children:
                    raise RideCourseError("Workout repeat group must contain steps")
                for _ in range(repeat):
                    add(children, depth + 1)
            else:
                expanded.append(step)
                if len(expanded) > 250:
                    raise RideCourseError("Ride workout expands to more than 250 steps")

    add(steps, 0)
    return expanded


def _workout_target_watts(
    target: str | None,
    ftp: float | None,
    step_index: int,
) -> tuple[float, str | None]:
    if target:
        match = _POWER_TARGET_RE.fullmatch(target.strip())
        if match:
            value = float(match.group(1))
            if "%" in match.group(2):
                if ftp is None:
                    raise RideCourseError(f"steps[{step_index}] uses %FTP; supply --ftp")
                return ftp * value / 100.0, None
            return value, None
        zone = _POWER_ZONE_RE.fullmatch(target.strip())
        if zone:
            if ftp is None:
                raise RideCourseError(f"steps[{step_index}] uses a power zone; supply --ftp")
            zone_index = min(7, max(0, int(zone.group(1))))
            return ftp * _POWER_ZONE_FTP_PERCENT[zone_index] / 100.0, None
    if ftp is None:
        raise RideCourseError(f"steps[{step_index}] needs a default 50% FTP target; supply --ftp")
    if target:
        return ftp * 0.5, f"steps[{step_index}] target {target!r} uses GarSync's 50% FTP fallback"
    return ftp * 0.5, None
