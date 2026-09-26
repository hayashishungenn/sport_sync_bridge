from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from .config import AppConfig
from .formats import ActivityFile, ActivityLap, TrackPoint, _atomic_write_bytes, _write_fit
from .models import Activity
from .sources import SourceAdapter
from .state import StateDB
from .utils import fit_signature_ok, parse_datetime, safe_filename


LOGGER = logging.getLogger(__name__)


class SmashrunSource(SourceAdapter):
    name = "smashrun"
    api_root = "https://api.smashrun.com/v1"
    access_token_key = "smashrun_access_token"
    page_size = 100

    def __init__(self, config: AppConfig, state_db: StateDB):
        super().__init__(config)
        self.state_db = state_db

    @property
    def access_token(self) -> str | None:
        stored = self.state_db.get_value(self.access_token_key)
        configured = getattr(self.config, "smashrun_access_token", None)
        token = stored or configured
        return token.strip() if isinstance(token, str) and token.strip() else None

    def is_configured(self) -> bool:
        return self.access_token is not None

    def authenticate(self) -> None:
        token = self.access_token
        if token is None:
            raise RuntimeError("Smashrun is not authorized; run `python sync.py smashrun-auth`")
        payload = self._request_json("/my/activities/search/briefs", params={"count": 1}, token=token)
        _response_rows(payload, "Smashrun activity search")

    def save_access_token(self, token: str) -> None:
        normalized = token.strip()
        if not normalized:
            raise ValueError("Smashrun access token must not be empty")
        payload = self._request_json(
            "/my/activities/search/briefs",
            params={"count": 1},
            token=normalized,
        )
        _response_rows(payload, "Smashrun activity search")
        self.state_db.set_value(self.access_token_key, normalized)

    def list_activities(
        self,
        since: datetime | None,
        until: datetime | None,
        limit: int | None,
    ) -> list[Activity]:
        if limit is not None and limit <= 0:
            return []

        token = self.access_token
        if token is None:
            raise RuntimeError("Smashrun is not authorized; run `python sync.py smashrun-auth`")

        lower_bound = _as_utc(since)
        upper_bound = _as_utc(until)
        page_size = min(limit, self.page_size) if limit is not None else self.page_size
        params: dict[str, int] = {"count": page_size, "page": 0}
        if lower_bound is not None:
            params["fromDateUTC"] = int(lower_bound.timestamp())

        activities: list[Activity] = []
        page = 0
        while True:
            page_params = {**params, "page": page}
            payload = self._request_json(
                "/my/activities/search/briefs", params=page_params, token=token
            )
            rows = _response_rows(payload, "Smashrun activity search")
            for index, row in enumerate(rows):
                activity = _activity_from_summary(row, index)
                if activity.start_time is None:
                    raise RuntimeError(f"Smashrun activity {activity.source_id} has no start time")
                if lower_bound is not None and activity.start_time < lower_bound:
                    continue
                if upper_bound is not None and activity.start_time > upper_bound:
                    continue
                activities.append(activity)
                if limit is not None and len(activities) >= limit:
                    return sorted(activities, key=_activity_sort_key)

            if len(rows) < page_size:
                break
            page += 1

        return sorted(activities, key=_activity_sort_key)

    def download_fit(self, activity: Activity, output_dir: Path) -> Path:
        activity_dir = output_dir / self.name
        activity_dir.mkdir(parents=True, exist_ok=True)
        path = activity_dir / f"{safe_filename(activity.source_id)}.fit"
        if path.is_file() and fit_signature_ok(path):
            return path

        token = self.access_token
        if token is None:
            raise RuntimeError("Smashrun is not authorized; run `python sync.py smashrun-auth`")
        payload = self._request_json(
            f"/my/activities/{quote(activity.source_id, safe='')}", token=token
        )
        detail = _activity_detail(payload, activity.source_id)
        activity_file = _activity_file_from_detail(detail, activity)
        try:
            fit_payload = _write_fit(activity_file, allow_trackless_records=True)
        except (ValueError, RuntimeError) as exc:
            raise RuntimeError(
                f"Could not generate FIT for Smashrun activity {activity.source_id}: {exc}"
            ) from exc

        for loss in activity_file.losses:
            LOGGER.warning("Smashrun conversion loss for %s: %s", activity.source_id, loss)
        _atomic_write_bytes(path, fit_payload, validate_fit=True)
        return path

    def _request_json(
        self,
        path: str,
        *,
        params: dict[str, int] | None = None,
        token: str | None = None,
    ) -> object:
        access_token = token or self.access_token
        if access_token is None:
            raise RuntimeError("Smashrun is not authorized; run `python sync.py smashrun-auth`")
        try:
            response = self.session.get(
                f"{self.api_root}{path}",
                params=params,
                headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Smashrun request failed: {exc}") from exc
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            if getattr(response, "status_code", None) in {401, 403}:
                raise RuntimeError(
                    "Smashrun rejected the access token or its read_activity permission; "
                    "run `python sync.py smashrun-auth` again"
                ) from exc
            status = getattr(response, "status_code", "unknown")
            raise RuntimeError(f"Smashrun request failed with HTTP {status}") from exc
        try:
            return response.json()
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Smashrun returned invalid JSON") from exc


def _response_rows(payload: object, operation: str) -> list[Mapping[str, Any]]:
    candidate = payload
    if isinstance(candidate, Mapping):
        for key in ("activities", "activitySummaries", "results", "items", "data"):
            value = candidate.get(key)
            if isinstance(value, list):
                candidate = value
                break
            if isinstance(value, Mapping):
                return _response_rows(value, operation)
    if not isinstance(candidate, list):
        raise RuntimeError(f"{operation} response must contain a JSON array")
    rows: list[Mapping[str, Any]] = []
    for index, item in enumerate(candidate):
        if not isinstance(item, Mapping):
            raise RuntimeError(f"{operation} item {index} must be a JSON object")
        rows.append(item)
    return rows


def _activity_from_summary(item: Mapping[str, Any], index: int) -> Activity:
    raw_id = item.get("runId") or item.get("activityId") or item.get("id")
    if raw_id in (None, ""):
        raise RuntimeError(f"Smashrun activity summary {index} is missing its run ID")
    source_id = str(raw_id)
    start_time = _datetime_value(
        item.get("startTime")
        or item.get("startDateTime")
        or item.get("startDateTimeUTC")
        or item.get("startDateTimeLocal")
    )
    if start_time is None:
        raise RuntimeError(f"Smashrun activity {source_id} is missing a valid start time")
    name_value = item.get("name") or item.get("description") or item.get("notes")
    name = str(name_value).strip() if name_value not in (None, "") else f"Smashrun activity {source_id}"
    return Activity(
        source="smashrun",
        source_id=source_id,
        name=name,
        sport_type="running",
        start_time=start_time,
        raw=dict(item),
    )


def _activity_detail(payload: object, source_id: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise RuntimeError(f"Smashrun activity {source_id} response must be a JSON object")
    detail = payload.get("activityDetail", payload)
    if isinstance(detail, Mapping) and isinstance(detail.get("data"), Mapping):
        detail = detail["data"]
    if not isinstance(detail, Mapping):
        raise RuntimeError(f"Smashrun activity {source_id} has no activity detail object")
    return detail


def _activity_file_from_detail(
    detail: Mapping[str, Any],
    activity: Activity,
) -> ActivityFile:
    series = _recording_series(detail, activity.source_id)
    clock_values = series.get("clock") or series.get("time")
    if not clock_values:
        raise RuntimeError(f"Smashrun activity {activity.source_id} has no clock/time recording")
    point_count = len(clock_values)
    for key, values in series.items():
        if len(values) != point_count:
            raise RuntimeError(
                f"Smashrun activity {activity.source_id} recording {key!r} has a different point count"
            )

    start_time = _datetime_value(
        detail.get("startDateTimeLocal")
        or detail.get("startDateTime")
        or detail.get("startTime")
        or activity.start_time
    )
    first_absolute = _absolute_recording_time(clock_values[0])
    if start_time is None:
        start_time = first_absolute
    if start_time is None:
        raise RuntimeError(f"Smashrun activity {activity.source_id} has no valid start time")

    point_times = _recording_times(clock_values, start_time, activity.source_id)
    if point_times[0] < start_time:
        raise RuntimeError(f"Smashrun activity {activity.source_id} has a recording before its start time")
    clock_offsets = [(value - start_time).total_seconds() for value in point_times]
    if any(right < left for left, right in zip(clock_offsets, clock_offsets[1:])):
        raise RuntimeError(f"Smashrun activity {activity.source_id} has non-monotonic timestamps")

    duration_values = series.get("duration")
    points: list[TrackPoint] = []
    distance_m_values: list[float | None] = []
    timer_values: list[float | None] = []
    supported = {
        "clock", "time", "duration", "distance", "latitude", "longitude", "elevation",
        "heartrate", "speed", "cadence", "power",
    }
    for index in range(point_count):
        latitude = _series_number(series, "latitude", index, activity.source_id)
        longitude = _series_number(series, "longitude", index, activity.source_id)
        if (latitude is None) != (longitude is None):
            raise RuntimeError(
                f"Smashrun activity {activity.source_id} has an incomplete GPS coordinate at point {index}"
            )
        if latitude is not None and not -90 <= latitude <= 90:
            raise RuntimeError(f"Smashrun activity {activity.source_id} has an invalid latitude at point {index}")
        if longitude is not None and not -180 <= longitude <= 180:
            raise RuntimeError(f"Smashrun activity {activity.source_id} has an invalid longitude at point {index}")

        distance_km = _series_number(series, "distance", index, activity.source_id)
        distance_m = distance_km * 1000 if distance_km is not None else None
        distance_m_values.append(distance_m)
        timer_values.append(_series_number(series, "duration", index, activity.source_id))
        speed_kph = _series_number(series, "speed", index, activity.source_id)
        points.append(
            TrackPoint(
                timestamp=point_times[index],
                latitude=latitude,
                longitude=longitude,
                elevation_m=_series_number(series, "elevation", index, activity.source_id),
                distance_m=distance_m,
                speed_mps=_smashrun_speed_kph_to_mps(speed_kph),
                heart_rate_bpm=_series_number(series, "heartrate", index, activity.source_id),
                cadence_rpm=_series_number(series, "cadence", index, activity.source_id),
                power_w=_series_number(series, "power", index, activity.source_id),
            )
        )

    elapsed_time_s = max(0.0, clock_offsets[-1])
    timer_time_s = (
        _optional_number(duration_values[-1], f"activity {activity.source_id} duration")
        if duration_values
        else _optional_number(detail.get("duration"), f"activity {activity.source_id} duration")
    )
    if timer_time_s is None:
        timer_time_s = elapsed_time_s
    if timer_time_s < 0:
        raise RuntimeError(f"Smashrun activity {activity.source_id} has a negative duration")
    total_distance_km = _optional_number(detail.get("distance"), f"activity {activity.source_id} distance")
    total_distance_m = (
        total_distance_km * 1000
        if total_distance_km is not None
        else next((value for value in reversed(distance_m_values) if value is not None), None)
    )
    end_time = _datetime_value(detail.get("endDateTime") or detail.get("endTime"))
    if end_time is None or end_time < point_times[-1]:
        end_time = point_times[-1]

    average_heart_rate = _optional_number(
        detail.get("heartRateAverage"), f"activity {activity.source_id} heartRateAverage"
    )
    maximum_heart_rate = _optional_number(
        detail.get("heartRateMax"), f"activity {activity.source_id} heartRateMax"
    )
    average_cadence = _optional_number(
        detail.get("cadenceAverage"), f"activity {activity.source_id} cadenceAverage"
    )
    calories_value = _optional_number(detail.get("calories"), f"activity {activity.source_id} calories")
    calories = round(calories_value) if calories_value is not None else None

    losses: list[str] = ["FIT output does not preserve the Smashrun activity name"]
    omitted_series = sorted(set(series) - supported)
    if omitted_series:
        losses.append("FIT does not preserve Smashrun recordings: " + ", ".join(omitted_series))
    raw_laps = detail.get("laps")
    if isinstance(raw_laps, list) and any(
        isinstance(lap, Mapping) and lap.get("lapType") for lap in raw_laps
    ):
        losses.append("FIT does not preserve Smashrun lap labels")

    laps = _activity_laps(
        detail,
        points,
        clock_offsets,
        timer_values,
        distance_m_values,
        start_time,
        elapsed_time_s,
        timer_time_s,
        total_distance_m,
        calories,
        average_heart_rate,
        maximum_heart_rate,
        average_cadence,
        activity.source_id,
    )
    return ActivityFile(
        name=str(detail.get("notes") or detail.get("name") or activity.name),
        sport_type="running",
        start_time=start_time,
        end_time=end_time,
        elapsed_time_s=max(elapsed_time_s, (end_time - start_time).total_seconds()),
        timer_time_s=timer_time_s,
        distance_m=total_distance_m,
        laps=laps,
        losses=losses,
        average_heart_rate_bpm=average_heart_rate,
        maximum_heart_rate_bpm=maximum_heart_rate,
        average_cadence=average_cadence,
    )


def _recording_series(detail: Mapping[str, Any], source_id: str) -> dict[str, list[object]]:
    keys = detail.get("recordingKeys")
    values = detail.get("recordingValues")
    if isinstance(keys, list) and isinstance(values, list):
        if len(keys) != len(values):
            raise RuntimeError(f"Smashrun activity {source_id} recording key/value counts do not match")
        series: dict[str, list[object]] = {}
        for index, (key, raw_values) in enumerate(zip(keys, values)):
            if not isinstance(key, str) or not key.strip():
                raise RuntimeError(f"Smashrun activity {source_id} recording key {index} is invalid")
            if not isinstance(raw_values, list):
                raise RuntimeError(f"Smashrun activity {source_id} recording {key!r} must be an array")
            normalized = _normalize_series_key(key)
            if normalized in series:
                raise RuntimeError(f"Smashrun activity {source_id} contains duplicate recording {key!r}")
            series[normalized] = raw_values
        return series

    tracks = detail.get("trackPoints")
    if isinstance(tracks, list) and tracks and all(isinstance(point, Mapping) for point in tracks):
        keys = {str(key) for point in tracks for key in point}
        return {
            _normalize_series_key(key): [point.get(key) for point in tracks]
            for key in keys
        }
    raise RuntimeError(f"Smashrun activity {source_id} has no recording arrays")


def _activity_laps(
    detail: Mapping[str, Any],
    points: list[TrackPoint],
    clock_offsets: list[float],
    timer_values: list[float | None],
    distance_values: list[float | None],
    start_time: datetime,
    elapsed_time_s: float,
    timer_time_s: float,
    distance_m: float | None,
    calories: int | None,
    average_heart_rate: float | None,
    maximum_heart_rate: float | None,
    average_cadence: float | None,
    source_id: str,
) -> list[ActivityLap]:
    raw_laps = detail.get("laps")
    boundaries: list[tuple[int, Mapping[str, Any]]] = []
    if isinstance(raw_laps, list):
        next_index = 0
        for raw_lap in raw_laps:
            if not isinstance(raw_lap, Mapping):
                continue
            index: int | None = None
            end_time = _optional_number(raw_lap.get("endTime"), f"activity {source_id} lap endTime")
            end_duration = _optional_number(
                raw_lap.get("endDuration"), f"activity {source_id} lap endDuration"
            )
            end_distance = _optional_number(
                raw_lap.get("endDistance"), f"activity {source_id} lap endDistance"
            )
            if end_time is not None:
                index = next((i for i in range(next_index, len(points)) if clock_offsets[i] >= end_time), None)
            elif end_duration is not None:
                index = next(
                    (i for i in range(next_index, len(points)) if timer_values[i] is not None and timer_values[i] >= end_duration),
                    None,
                )
            elif end_distance is not None:
                index = next(
                    (i for i in range(next_index, len(points)) if distance_values[i] is not None and distance_values[i] >= end_distance),
                    None,
                )
            if index is not None:
                boundaries.append((index, raw_lap))
                next_index = index + 1

    ranges: list[tuple[int, int, Mapping[str, Any] | None]] = []
    start_index = 0
    for index, raw_lap in boundaries:
        if index >= start_index:
            ranges.append((start_index, index, raw_lap))
            start_index = index + 1
    if start_index < len(points):
        ranges.append((start_index, len(points) - 1, None))
    if not ranges:
        ranges = [(0, len(points) - 1, None)]

    laps: list[ActivityLap] = []
    previous_distance = 0.0
    for first, last, raw_lap in ranges:
        lap_points = points[first : last + 1]
        lap_distance_end = distance_values[last]
        lap_distance = (
            max(0.0, lap_distance_end - previous_distance)
            if lap_distance_end is not None
            else None
        )
        if lap_distance_end is not None:
            previous_distance = lap_distance_end
        lap_timer_end = timer_values[last]
        lap_timer_start = timer_values[first - 1] if first > 0 else 0.0
        lap_timer = (
            max(0.0, lap_timer_end - (lap_timer_start or 0.0))
            if lap_timer_end is not None
            else None
        )
        lap_start = (points[first - 1].timestamp or start_time) if first > 0 else start_time
        lap_end = lap_points[-1].timestamp or lap_start
        lap_elapsed = max(
            0.0,
            (lap_end - lap_start).total_seconds(),
        )
        lap_calories = (
            _optional_number(raw_lap.get("calories"), f"activity {source_id} lap calories")
            if raw_lap is not None and raw_lap.get("calories") is not None
            else calories if len(ranges) == 1 else None
        )
        lap_average_hr = (
            _optional_number(raw_lap.get("heartRateAverage"), f"activity {source_id} lap heart rate")
            if raw_lap is not None and raw_lap.get("heartRateAverage") is not None
            else average_heart_rate if len(ranges) == 1 else None
        )
        lap_maximum_hr = (
            _optional_number(raw_lap.get("heartRateMax"), f"activity {source_id} lap max heart rate")
            if raw_lap is not None and raw_lap.get("heartRateMax") is not None
            else maximum_heart_rate if len(ranges) == 1 else None
        )
        laps.append(
            ActivityLap(
                start_time=lap_start,
                end_time=lap_end,
                elapsed_time_s=lap_elapsed if lap_elapsed else elapsed_time_s if len(ranges) == 1 else 0.0,
                timer_time_s=lap_timer if lap_timer is not None else timer_time_s if len(ranges) == 1 else None,
                distance_m=lap_distance if lap_distance is not None else distance_m if len(ranges) == 1 else None,
                calories=round(lap_calories) if lap_calories is not None else None,
                average_heart_rate=lap_average_hr,
                maximum_heart_rate=lap_maximum_hr,
                track_points=lap_points,
                average_cadence=average_cadence if len(ranges) == 1 else None,
            )
        )
    return laps


def _recording_times(values: list[object], start_time: datetime, source_id: str) -> list[datetime]:
    times: list[datetime] = []
    for index, value in enumerate(values):
        absolute = _absolute_recording_time(value)
        if absolute is not None:
            timestamp = absolute
        else:
            seconds = _optional_number(value, f"activity {source_id} clock point {index}")
            if seconds is None or seconds < 0:
                raise RuntimeError(f"Smashrun activity {source_id} has an invalid clock at point {index}")
            timestamp = start_time + timedelta(seconds=seconds)
        times.append(timestamp)
    return times


def _datetime_value(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if not math.isfinite(number) or abs(number) < 100_000_000:
            return None
        if abs(number) >= 100_000_000_000:
            number /= 1000
        try:
            parsed = datetime.fromtimestamp(number, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        parsed = parse_datetime(value)
        if parsed is None:
            try:
                number = float(value)
            except ValueError:
                return None
            if not math.isfinite(number) or abs(number) < 100_000_000:
                return None
            if abs(number) >= 100_000_000_000:
                number /= 1000
            try:
                parsed = datetime.fromtimestamp(number, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _absolute_recording_time(value: object) -> datetime | None:
    parsed = _datetime_value(value)
    if parsed is None:
        return None
    if isinstance(value, (int, float)) or isinstance(value, str) and value.strip().replace(".", "", 1).isdigit():
        number = float(value)
        if abs(number) < 100_000_000:
            return None
    return parsed


def _optional_number(value: object, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise RuntimeError(f"Smashrun {label} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Smashrun {label} must be numeric") from exc
    if not math.isfinite(number):
        raise RuntimeError(f"Smashrun {label} must be finite")
    return number


def _series_number(
    series: Mapping[str, list[object]],
    key: str,
    index: int,
    source_id: str,
) -> float | None:
    values = series.get(key)
    if values is None:
        return None
    return _optional_number(values[index], f"activity {source_id} {key} point {index}")


def _smashrun_speed_kph_to_mps(value: float | None) -> float | None:
    return value / 3.6 if value is not None else None


def _normalize_series_key(value: str) -> str:
    normalized = "".join(character for character in value.casefold() if character.isalnum())
    aliases = {
        "heartratebpm": "heartrate",
        "lat": "latitude",
        "lon": "longitude",
        "timestamp": "clock",
    }
    return aliases.get(normalized, normalized)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _activity_sort_key(activity: Activity) -> tuple[datetime, str]:
    return (activity.start_time or datetime.min.replace(tzinfo=timezone.utc), activity.source_id)
