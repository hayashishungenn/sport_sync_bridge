from __future__ import annotations

import logging
import math
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import AppConfig
from .formats import ActivityFile, ActivityLap, TrackPoint, write_tcx_activity
from .mapmyfitness_api import MapMyFitnessClient
from .models import Activity
from .sources import SourceAdapter
from .utils import parse_datetime, safe_filename


LOGGER = logging.getLogger(__name__)


class MapMyFitnessSource(SourceAdapter):
    name = "mapmyfitness"
    page_size = 50

    def __init__(self, config: AppConfig, client: MapMyFitnessClient):
        super().__init__(config)
        self.client = client
        self._activity_type_cache: dict[str, str | None] = {}

    def is_configured(self) -> bool:
        return self.client.is_configured()

    def authenticate(self) -> None:
        self.client.authenticate()

    def list_activities(
        self,
        since: datetime | None,
        until: datetime | None,
        limit: int | None,
    ) -> list[Activity]:
        if limit is not None and limit <= 0:
            return []

        self.authenticate()
        user_id = self.client.user_id
        if user_id is None:
            raise RuntimeError("MapMyFitness current-user response did not contain a user ID")

        lower_bound = _as_utc(since)
        upper_bound = _as_utc(until)
        activities: list[Activity] = []
        offset = 0
        while True:
            page_limit = min(self.page_size, limit - len(activities)) if limit is not None else self.page_size
            params: dict[str, object] = {
                "user": user_id,
                "limit": page_limit,
                "offset": offset,
                "order_by": "-start_datetime",
            }
            if lower_bound is not None:
                params["started_after"] = _format_query_time(lower_bound)
            if upper_bound is not None:
                params["started_before"] = _format_query_time(upper_bound)

            payload = self.client.get_json("/workout/", params=params)
            embedded = payload.get("_embedded")
            rows = embedded.get("workouts") if isinstance(embedded, Mapping) else None
            if not isinstance(rows, list):
                raise RuntimeError("MapMyFitness workouts response must contain _embedded.workouts")

            for index, item in enumerate(rows):
                if not isinstance(item, Mapping):
                    raise RuntimeError(f"MapMyFitness workout at offset {offset}, index {index} must be an object")
                activity = self._activity_from_workout(item, offset + index)
                if lower_bound is not None and activity.start_time < lower_bound:
                    continue
                if upper_bound is not None and activity.start_time > upper_bound:
                    continue
                activities.append(activity)
                if limit is not None and len(activities) >= limit:
                    return sorted(activities, key=_activity_sort_key)

            if not rows:
                break
            total_count = payload.get("total_count")
            if total_count is not None:
                try:
                    if offset + len(rows) >= int(total_count):
                        break
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("MapMyFitness total_count must be an integer") from exc
            elif len(rows) < page_limit:
                break
            offset += len(rows)

        return sorted(activities, key=_activity_sort_key)

    def download_fit(self, activity: Activity, output_dir: Path) -> Path:
        workout_id = _positive_workout_id(activity.source_id)
        payload = self.client.get_json(
            f"/workout/{workout_id}/",
            params={"field_set": "time_series"},
        )
        workout = payload.get("workout")
        if isinstance(workout, Mapping):
            payload = dict(workout)
        if not isinstance(payload.get("time_series"), Mapping):
            raise ValueError(f"MapMyFitness workout {workout_id} does not contain time_series data")

        sport_type = activity.sport_type or self._sport_type(payload)
        activity_file = _activity_file_from_workout(
            payload,
            name=activity.name,
            sport_type=sport_type,
            fallback_start=activity.start_time,
        )
        for loss in _tcx_conversion_losses(payload, sport_type):
            LOGGER.warning(
                "Conversion loss %s/%s to TCX: %s",
                activity.source,
                activity.source_id,
                loss,
            )
        output_path = output_dir / self.name / f"{safe_filename(workout_id)}.tcx"
        write_tcx_activity(activity_file, output_path)
        return output_path

    def _activity_from_workout(self, item: Mapping[str, Any], index: int) -> Activity:
        workout_id = _workout_id(item, index)
        start_time = _parse_workout_time(item.get("start_datetime"), workout_id)
        if start_time is None:
            raise RuntimeError(f"MapMyFitness workout {workout_id} is missing a valid start_datetime")
        name_value = item.get("name")
        name = str(name_value).strip() if name_value not in (None, "") else f"MapMyFitness workout {workout_id}"
        return Activity(
            source=self.name,
            source_id=workout_id,
            name=name,
            sport_type=self._sport_type(item),
            start_time=start_time,
            raw=dict(item),
        )

    def _sport_type(self, item: Mapping[str, Any]) -> str | None:
        activity_type = item.get("activity_type")
        direct_name = _activity_type_name(activity_type)
        if direct_name:
            return _sport_type_from_name(direct_name)

        type_id = _activity_type_id(item)
        if type_id is None:
            return None
        if type_id not in self._activity_type_cache:
            payload = self.client.get_json(f"/activity_type/{type_id}/")
            label = payload.get("short_name") or payload.get("name")
            self._activity_type_cache[type_id] = (
                _sport_type_from_name(label) if isinstance(label, str) else None
            )
        return self._activity_type_cache[type_id]


def _activity_file_from_workout(
    workout: Mapping[str, Any],
    *,
    name: str,
    sport_type: str | None,
    fallback_start: datetime | None,
) -> ActivityFile:
    start_time = _parse_workout_time(workout.get("start_datetime"), "detail")
    if start_time is None:
        start_time = _as_utc(fallback_start)
    if start_time is None:
        raise ValueError("MapMyFitness workout is missing a valid start_datetime")

    series = workout.get("time_series")
    if not isinstance(series, Mapping):
        raise ValueError("MapMyFitness workout does not contain a time_series object")

    points_by_offset: dict[float, TrackPoint] = {}

    def point_for_offset(offset_value: object, field_name: str, index: int) -> tuple[float, TrackPoint]:
        offset = _finite_number(offset_value, f"time_series.{field_name}[{index}] offset")
        if offset < 0:
            raise ValueError(f"MapMyFitness time_series.{field_name}[{index}] offset cannot be negative")
        point = points_by_offset.get(offset)
        if point is None:
            point = TrackPoint(timestamp=start_time + timedelta(seconds=offset))
            points_by_offset[offset] = point
        return offset, point

    position_rows = _series_rows(series, "position")
    for index, sample in enumerate(position_rows):
        offset_value, value = _series_sample(sample, "position", index)
        if not isinstance(value, Mapping):
            raise ValueError(f"MapMyFitness time_series.position[{index}] value must be an object")
        _, point = point_for_offset(offset_value, "position", index)
        point.latitude = _finite_number(value.get("lat"), f"time_series.position[{index}].lat")
        point.longitude = _finite_number(value.get("lng"), f"time_series.position[{index}].lng")
        elevation = value.get("elevation")
        if elevation is not None:
            point.elevation_m = _finite_number(elevation, f"time_series.position[{index}].elevation")

    metric_fields = {
        "distance": "distance_m",
        "heartrate": "heart_rate_bpm",
        "speed": "speed_mps",
        "cadence": "cadence_rpm",
        "power": "power_w",
    }
    for series_name, point_field in metric_fields.items():
        for index, sample in enumerate(_series_rows(series, series_name)):
            offset_value, value = _series_sample(sample, series_name, index)
            if value is None:
                continue
            _, point = point_for_offset(offset_value, series_name, index)
            setattr(
                point,
                point_field,
                _finite_number(value, f"time_series.{series_name}[{index}] value"),
            )

    points = [points_by_offset[offset] for offset in sorted(points_by_offset)]
    if not any(point.latitude is not None and point.longitude is not None for point in points):
        raise ValueError("MapMyFitness workout has no GPS position track")
    end_time = max(point.timestamp for point in points if point.timestamp is not None)

    aggregates = workout.get("aggregates")
    if aggregates is None:
        aggregates = {}
    if not isinstance(aggregates, Mapping):
        raise ValueError("MapMyFitness workout aggregates must be an object")

    elapsed_time = _optional_number(aggregates.get("elapsed_time_total"), "elapsed_time_total")
    if elapsed_time is None:
        elapsed_time = max(0.0, (end_time - start_time).total_seconds())
    timer_time = _optional_number(aggregates.get("active_time_total"), "active_time_total")
    distance = _optional_number(aggregates.get("distance_total"), "distance_total")
    if distance is None:
        distances = [point.distance_m for point in points if point.distance_m is not None]
        distance = max(distances) if distances else None
    energy_joules = _optional_number(
        aggregates.get("metabolic_energy_total"), "metabolic_energy_total"
    )
    calories = round(energy_joules / 4184) if energy_joules is not None else None
    average_heart_rate = _optional_number(aggregates.get("heartrate_avg"), "heartrate_avg")
    maximum_heart_rate = _optional_number(aggregates.get("heartrate_max"), "heartrate_max")
    average_cadence = _optional_number(aggregates.get("cadence_avg"), "cadence_avg")
    maximum_cadence = _optional_number(aggregates.get("cadence_max"), "cadence_max")
    lap = ActivityLap(
        start_time=start_time,
        end_time=end_time,
        elapsed_time_s=elapsed_time,
        timer_time_s=timer_time,
        distance_m=distance,
        calories=calories,
        average_heart_rate=average_heart_rate,
        maximum_heart_rate=maximum_heart_rate,
        track_points=points,
        average_cadence=average_cadence,
        maximum_cadence=maximum_cadence,
    )
    return ActivityFile(
        name=name,
        sport_type=sport_type,
        start_time=start_time,
        end_time=end_time,
        elapsed_time_s=elapsed_time,
        timer_time_s=timer_time,
        distance_m=distance,
        laps=[lap],
        average_heart_rate_bpm=average_heart_rate,
        maximum_heart_rate_bpm=maximum_heart_rate,
        average_cadence=average_cadence,
    )


def _series_rows(series: Mapping[str, Any], key: str) -> Sequence[Any]:
    value = series.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"MapMyFitness time_series.{key} must be a list")
    return value


def _series_sample(sample: object, key: str, index: int) -> tuple[object, object]:
    if not isinstance(sample, (list, tuple)) or len(sample) != 2:
        raise ValueError(f"MapMyFitness time_series.{key}[{index}] must be an [offset, value] pair")
    return sample[0], sample[1]


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"MapMyFitness {label} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"MapMyFitness {label} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"MapMyFitness {label} must be a finite number")
    return number


def _optional_number(value: object, label: str) -> float | None:
    if value is None:
        return None
    return _finite_number(value, f"aggregates.{label}")


def _workout_id(item: Mapping[str, Any], index: int) -> str:
    direct_id = item.get("id")
    if direct_id not in (None, ""):
        return _positive_workout_id(direct_id)
    links = item.get("_links")
    self_links = links.get("self") if isinstance(links, Mapping) else None
    if isinstance(self_links, list) and self_links and isinstance(self_links[0], Mapping):
        link = self_links[0]
        raw_id = link.get("id") or _id_from_href(link.get("href"))
        if raw_id not in (None, ""):
            return _positive_workout_id(raw_id)
    raise RuntimeError(f"MapMyFitness workout at index {index} is missing a valid ID")


def _activity_type_id(item: Mapping[str, Any]) -> str | None:
    links = item.get("_links")
    values = links.get("activity_type") if isinstance(links, Mapping) else None
    if isinstance(values, list) and values and isinstance(values[0], Mapping):
        value = values[0].get("id") or _id_from_href(values[0].get("href"))
        if value not in (None, ""):
            normalized = str(value).strip()
            if not normalized.isdecimal() or int(normalized) <= 0:
                raise RuntimeError("MapMyFitness workout has an invalid activity type ID")
            return normalized
    return None


def _activity_type_name(value: object) -> str | None:
    if isinstance(value, Mapping):
        for field in ("short_name", "name"):
            name = value.get(field)
            if isinstance(name, str) and name.strip():
                return name.strip()
    if isinstance(value, str):
        normalized = value.strip().strip("/")
        if normalized and not normalized.rsplit("/", 1)[-1].isdecimal():
            return normalized.rsplit("/", 1)[-1]
    return None


def _id_from_href(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    last_part = value.strip().rstrip("/").rsplit("/", 1)[-1]
    return last_part if last_part.isdecimal() else None


def _positive_workout_id(value: object) -> str:
    normalized = str(value).strip() if value is not None else ""
    if not normalized.isdecimal() or int(normalized) <= 0:
        raise ValueError("MapMyFitness workout ID must be a positive integer")
    return normalized


def _sport_type_from_name(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().lower().replace("_", " ").replace("-", " ")
    if "mountain" in normalized and any(token in normalized for token in ("bike", "bik", "cycl")):
        return "mountain_biking"
    if "indoor" in normalized and any(token in normalized for token in ("bike", "bik", "cycl")):
        return "indoor_cycling"
    if "virtual" in normalized and any(token in normalized for token in ("bike", "bik", "cycl")):
        return "virtual_ride"
    if any(token in normalized for token in ("e bike", "ebike")):
        return "e_biking"
    if any(token in normalized for token in ("bike", "bik", "cycl", "ride", "cycling")):
        return "cycling"
    if "trail" in normalized and any(token in normalized for token in ("run", "jog")):
        return "trail_run"
    if any(token in normalized for token in ("run", "jog")):
        return "running"
    if "swim" in normalized:
        return "swimming"
    if "walk" in normalized:
        return "walking"
    if "hike" in normalized:
        return "hiking"
    if any(token in normalized for token in ("fitness", "workout", "exercise", "generic", "other")):
        return "generic"
    return re.sub(r"[^a-z0-9]+", "_", normalized).strip("_") or None


def _tcx_conversion_losses(
    workout: Mapping[str, Any],
    sport_type: str | None,
) -> tuple[str, ...]:
    losses: list[str] = []
    aggregates = workout.get("aggregates")
    if isinstance(aggregates, Mapping):
        unsupported_aggregates = {
            "heartrate_min": "minimum heart rate",
            "speed_min": "minimum speed",
            "speed_max": "maximum speed",
            "speed_avg": "average speed",
            "cadence_min": "minimum cadence",
            "power_min": "minimum power",
            "power_avg": "average power",
            "power_max": "maximum power",
            "steps_total": "step count",
        }
        omitted = [
            label
            for field, label in unsupported_aggregates.items()
            if aggregates.get(field) is not None
        ]
        if omitted:
            losses.append("TCX does not preserve activity aggregates: " + ", ".join(omitted))
        active_time = aggregates.get("active_time_total")
        elapsed_time = aggregates.get("elapsed_time_total")
        if active_time is not None and elapsed_time is not None and active_time != elapsed_time:
            losses.append("TCX does not preserve moving time separately from elapsed time")
    series = workout.get("time_series")
    if isinstance(series, Mapping):
        unsupported_series = sorted(
            field
            for field, rows in series.items()
            if field not in {"position", "distance", "heartrate", "speed", "cadence", "power"}
            and rows
        )
        if unsupported_series:
            losses.append(
                "TCX does not preserve MapMyFitness time series: " + ", ".join(unsupported_series)
            )
    if sport_type and sport_type not in {"cycling", "running", "generic"}:
        losses.append(f"TCX represents the {sport_type} sport type as Other")
    return tuple(losses)


def _parse_workout_time(value: object, workout_id: object) -> datetime | None:
    parsed = value if isinstance(value, datetime) else parse_datetime(value)
    if parsed is None:
        if value is None:
            return None
        raise RuntimeError(f"MapMyFitness workout {workout_id} has an invalid start_datetime")
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    parsed = value
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_query_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _activity_sort_key(activity: Activity) -> tuple[bool, datetime]:
    timestamp = activity.start_time or datetime.min.replace(tzinfo=timezone.utc)
    return activity.start_time is None, timestamp
