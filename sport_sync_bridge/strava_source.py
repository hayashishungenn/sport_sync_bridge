from __future__ import annotations

import math
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from .config import AppConfig
from .formats import ActivityFile, ActivityLap, TrackPoint, _write_fit
from .models import Activity
from .sources import SourceAdapter
from .utils import fit_signature_ok, parse_datetime, safe_filename


class StravaTokenProvider(Protocol):
    def is_configured(self) -> bool: ...

    def get_access_token(self, *, force_refresh: bool = False) -> str: ...


class StravaSource(SourceAdapter):
    name = "strava"
    api_root = "https://www.strava.com/api/v3"
    stream_keys = (
        "time",
        "distance",
        "latlng",
        "altitude",
        "velocity_smooth",
        "heartrate",
        "cadence",
        "watts",
    )

    def __init__(self, config: AppConfig, token_provider: StravaTokenProvider):
        super().__init__(config)
        self.token_provider = token_provider

    def is_configured(self) -> bool:
        return self.token_provider.is_configured()

    def authenticate(self) -> None:
        self.token_provider.get_access_token()

    def list_activities(
        self,
        since: datetime | None,
        until: datetime | None,
        limit: int | None,
    ) -> list[Activity]:
        if limit is not None and limit <= 0:
            return []

        lower_bound = parse_datetime(since)
        upper_bound = parse_datetime(until)
        page_size = min(limit, 100) if limit is not None else 100
        params: dict[str, int] = {"per_page": page_size}
        if lower_bound is not None:
            params["after"] = int(lower_bound.timestamp())
        if upper_bound is not None:
            params["before"] = int(upper_bound.timestamp()) + 1

        activities: list[Activity] = []
        page = 1
        while True:
            page_params = {**params, "page": page}
            payload = self._get_json("/athlete/activities", params=page_params)
            if not isinstance(payload, list):
                raise RuntimeError("Strava activity response must be a JSON list")
            if not payload:
                break

            for index, item in enumerate(payload):
                if not isinstance(item, dict):
                    raise RuntimeError(f"Strava activity {index} must be a JSON object")
                raw_id = item.get("id")
                if raw_id in (None, ""):
                    raise RuntimeError(f"Strava activity {index} is missing its ID")
                source_id = str(raw_id)
                start_time = parse_datetime(item.get("start_date") or item.get("start_date_local"))
                if start_time is None:
                    raise RuntimeError(f"Strava activity {source_id} is missing a valid start time")
                if lower_bound is not None and start_time < lower_bound:
                    continue
                if upper_bound is not None and start_time > upper_bound:
                    continue

                sport_type = _strava_sport_type(item.get("sport_type") or item.get("type"))
                name = item.get("name")
                activities.append(
                    Activity(
                        source=self.name,
                        source_id=source_id,
                        name=str(name) if name else f"{sport_type or 'activity'} {source_id}",
                        sport_type=sport_type,
                        start_time=start_time,
                        raw=item,
                    )
                )
                if limit is not None and len(activities) >= limit:
                    break

            if limit is not None and len(activities) >= limit:
                break
            if len(payload) < page_size:
                break
            page += 1

        return sorted(activities, key=_activity_sort_key)

    def download_fit(self, activity: Activity, output_dir: Path) -> Path:
        activity_dir = output_dir / self.name
        activity_dir.mkdir(parents=True, exist_ok=True)
        path = activity_dir / f"{safe_filename(activity.source_id)}.fit"
        if path.is_file() and path.stat().st_size >= 100 and fit_signature_ok(path):
            return path

        encoded_id = quote(activity.source_id, safe="")
        detail = self._get_json(f"/activities/{encoded_id}")
        if not isinstance(detail, dict):
            raise RuntimeError(f"Strava activity {activity.source_id} response must be a JSON object")
        stream_payload = self._get_json(
            f"/activities/{encoded_id}/streams",
            params={"keys": ",".join(self.stream_keys), "key_by_type": "true"},
        )
        streams = _stream_data(stream_payload)
        fit_bytes = _activity_fit_bytes({**activity.raw, **detail}, streams, activity.source_id)

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=activity_dir, prefix=f".{path.name}.", suffix=".tmp", delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(fit_bytes)
            if temporary_path is None:
                raise RuntimeError("Could not create a temporary FIT file")
            temporary_path.replace(path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

        if path.stat().st_size < 100 or not fit_signature_ok(path):
            path.unlink(missing_ok=True)
            raise RuntimeError(f"Generated file is not a valid FIT for Strava activity {activity.source_id}")
        return path

    def _get_json(self, path: str, *, params: dict[str, int | str] | None = None) -> object:
        token = self.token_provider.get_access_token()
        response = self.session.get(
            f"{self.api_root}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=30,
        )
        if response.status_code == 401:
            token = self.token_provider.get_access_token(force_refresh=True)
            response = self.session.get(
                f"{self.api_root}{path}",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
                timeout=30,
            )
        response.raise_for_status()
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(f"Strava response for {path} is not valid JSON") from exc


def _activity_fit_bytes(
    activity_data: dict[str, object], streams: dict[str, list[object]], source_id: str
) -> bytes:
    start_time = parse_datetime(activity_data.get("start_date") or activity_data.get("start_date_local"))
    if start_time is None:
        raise RuntimeError(f"Strava activity {source_id} is missing a valid start time")

    times = streams.get("time")
    if not times:
        raise RuntimeError(f"Strava activity {source_id} has no time stream data")

    points: list[TrackPoint] = []
    previous_elapsed = -1.0
    for index, raw_elapsed in enumerate(times):
        elapsed = _required_number(raw_elapsed, f"time stream value {index}")
        if elapsed < 0 or elapsed < previous_elapsed:
            raise RuntimeError(f"Strava activity {source_id} has invalid time stream ordering at point {index}")
        previous_elapsed = elapsed
        latitude, longitude = _coordinates(_stream_value(streams, "latlng", index), source_id, index)
        points.append(
            TrackPoint(
                timestamp=start_time + timedelta(seconds=elapsed),
                latitude=latitude,
                longitude=longitude,
                elevation_m=_stream_number(streams, "altitude", index),
                distance_m=_stream_number(streams, "distance", index),
                speed_mps=_stream_number(streams, "velocity_smooth", index),
                heart_rate_bpm=_stream_number(streams, "heartrate", index),
                cadence_rpm=_stream_number(streams, "cadence", index),
                power_w=_stream_number(streams, "watts", index),
            )
        )

    if not points:
        raise RuntimeError(f"Strava activity {source_id} has no track records")

    elapsed_time = _summary_number(activity_data, "elapsed_time", source_id)
    moving_time = _summary_number(activity_data, "moving_time", source_id)
    distance = _summary_number(activity_data, "distance", source_id)
    if distance is None:
        distance = _last_stream_number(streams.get("distance"), source_id)
    last_point_time = points[-1].timestamp or start_time
    end_time = max(last_point_time, start_time + timedelta(seconds=elapsed_time or 0.0))
    track_elapsed = (end_time - start_time).total_seconds()
    total_elapsed = max(elapsed_time or 0.0, track_elapsed)
    timer_time = moving_time if moving_time is not None else total_elapsed
    average_hr = _summary_number(activity_data, "average_heartrate", source_id)
    maximum_hr = _summary_number(activity_data, "max_heartrate", source_id)
    average_power = _summary_number(activity_data, "average_watts", source_id)
    maximum_power = _summary_number(activity_data, "max_watts", source_id)
    normalized_power = _summary_number(activity_data, "weighted_average_watts", source_id)
    calories_value = _summary_number(activity_data, "calories", source_id)
    calories = int(round(calories_value)) if calories_value is not None else None
    sport_type = _strava_sport_type(activity_data.get("sport_type") or activity_data.get("type"))

    lap = ActivityLap(
        start_time=start_time,
        end_time=end_time,
        elapsed_time_s=total_elapsed,
        timer_time_s=timer_time,
        distance_m=distance,
        calories=calories,
        average_heart_rate=average_hr,
        maximum_heart_rate=maximum_hr,
        track_points=points,
    )
    activity_file = ActivityFile(
        name=str(activity_data.get("name") or f"Strava activity {source_id}"),
        sport_type=sport_type,
        start_time=start_time,
        end_time=end_time,
        elapsed_time_s=total_elapsed,
        timer_time_s=timer_time,
        distance_m=distance,
        laps=[lap],
        average_heart_rate_bpm=average_hr,
        maximum_heart_rate_bpm=maximum_hr,
        average_power_w=average_power,
        maximum_power_w=maximum_power,
        normalized_power_w=normalized_power,
    )
    try:
        return _write_fit(activity_file, allow_trackless_records=True)
    except (ValueError, RuntimeError) as exc:
        raise RuntimeError(f"Could not generate FIT for Strava activity {source_id}: {exc}") from exc


def _stream_data(payload: object) -> dict[str, list[object]]:
    streams: dict[str, list[object]] = {}
    if isinstance(payload, dict):
        for key, value in payload.items():
            data = value.get("data") if isinstance(value, dict) else value
            if data is not None:
                if not isinstance(data, list):
                    raise RuntimeError(f"Strava {key} stream data must be a JSON list")
                streams[str(key)] = data
        return streams
    if isinstance(payload, list):
        for index, value in enumerate(payload):
            if not isinstance(value, dict):
                raise RuntimeError(f"Strava stream {index} must be a JSON object")
            key = value.get("type")
            data = value.get("data")
            if not isinstance(key, str) or not key:
                raise RuntimeError(f"Strava stream {index} is missing its type")
            if data is not None:
                if not isinstance(data, list):
                    raise RuntimeError(f"Strava {key} stream data must be a JSON list")
                streams[key] = data
        return streams
    raise RuntimeError("Strava activity streams response must be a JSON object or list")


def _stream_value(streams: dict[str, list[object]], key: str, index: int) -> object | None:
    values = streams.get(key)
    return values[index] if values is not None and index < len(values) else None


def _stream_number(streams: dict[str, list[object]], key: str, index: int) -> float | None:
    value = _stream_value(streams, key, index)
    return _optional_number(value, f"{key} stream value {index}")


def _coordinates(value: object | None, source_id: str, index: int) -> tuple[float | None, float | None]:
    if value is None:
        return None, None
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise RuntimeError(f"Strava activity {source_id} has an invalid latlng value at point {index}")
    latitude = _optional_number(value[0], f"latlng latitude {index}")
    longitude = _optional_number(value[1], f"latlng longitude {index}")
    if latitude is None or longitude is None:
        if latitude is not None or longitude is not None:
            raise RuntimeError(f"Strava activity {source_id} has an incomplete latlng value at point {index}")
        return None, None
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise RuntimeError(f"Strava activity {source_id} has out-of-range coordinates at point {index}")
    return latitude, longitude


def _summary_number(data: dict[str, object], key: str, source_id: str) -> float | None:
    if data.get(key) is None:
        return None
    value = _optional_number(data.get(key), f"{key} summary")
    if value is None or value < 0:
        raise RuntimeError(f"Strava activity {source_id} has an invalid {key} summary")
    return value


def _last_stream_number(values: list[object] | None, source_id: str) -> float | None:
    if values is None:
        return None
    for index in range(len(values) - 1, -1, -1):
        value = _optional_number(values[index], f"distance stream value {index}")
        if value is not None:
            return value
    return None


def _required_number(value: object, label: str) -> float:
    number = _optional_number(value, label)
    if number is None:
        raise RuntimeError(f"Strava {label} is missing")
    return number


def _optional_number(value: object | None, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise RuntimeError(f"Strava {label} is not numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Strava {label} is not numeric") from exc
    if not math.isfinite(number):
        raise RuntimeError(f"Strava {label} is not finite")
    return number


def _strava_sport_type(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower().replace(" ", "").replace("_", "")
    if normalized in {"ride", "virtualride", "ebikeride", "mountainbikeride", "gravelride", "handcycle"}:
        return "cycling"
    if normalized in {"run", "trailrun", "virtualrun", "treadmillrun"}:
        return "running"
    if normalized in {"swim", "openwaterswim"}:
        return "swimming"
    if normalized in {"walk", "hike"}:
        return "walking" if normalized == "walk" else "hiking"
    if normalized in {"weighttraining", "crossfit", "workout"}:
        return "strength_training"
    return value.strip().lower().replace(" ", "_") or None


def _activity_sort_key(activity: Activity) -> tuple[datetime, str]:
    return (activity.start_time or datetime(1970, 1, 1, tzinfo=timezone.utc), activity.source_id)
