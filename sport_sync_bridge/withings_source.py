from __future__ import annotations

import hmac
import json
import math
import secrets
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

from .config import AppConfig
from .formats import ActivityFile, ActivityLap, TrackPoint, _atomic_write_bytes, _write_fit
from .models import Activity
from .sources import SourceAdapter
from .state import StateDB
from .utils import safe_filename


class WithingsClient:
    authorization_uri = "https://account.withings.com/oauth2_user/authorize2"
    api_root = "https://wbsapi.withings.net"
    credentials_key = "withings_credentials"
    oauth_state_key = "withings_oauth_state"
    scope = "user.activity"

    def __init__(self, config: AppConfig, state_db: StateDB):
        self.config = config
        self.state_db = state_db
        self.session = requests.Session()

    def is_oauth_configured(self) -> bool:
        return bool(self.config.withings_client_id and self.config.withings_client_secret)

    def is_authorized(self) -> bool:
        credentials = self._load_credentials()
        if not credentials or not credentials.get("refresh_token"):
            return False
        granted = _scope_set(credentials.get("scope"))
        return not granted or self.scope in granted

    def build_authorize_url(self) -> str:
        if not self.is_oauth_configured():
            raise RuntimeError("Withings client ID and client secret are not configured")
        state = secrets.token_urlsafe(32)
        self.state_db.set_value(self.oauth_state_key, state)
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.config.withings_client_id,
                "scope": self.scope,
                "redirect_uri": self.config.withings_redirect_uri,
                "state": state,
            }
        )
        return f"{self.authorization_uri}?{query}"

    def exchange_code(self, code: str, state: str) -> dict[str, object]:
        if not code.strip():
            raise ValueError("Withings OAuth code must not be empty")
        expected_state = self.state_db.get_value(self.oauth_state_key)
        if not expected_state or not hmac.compare_digest(expected_state, state.strip()):
            raise RuntimeError("Withings OAuth state does not match the authorization request")

        body = self._request_token(
            {
                "grant_type": "authorization_code",
                "code": code.strip(),
                "redirect_uri": self.config.withings_redirect_uri,
            },
            "authorization code exchange",
        )
        credentials = self._credentials_from_body(body)
        if not credentials.get("refresh_token"):
            raise RuntimeError("Withings did not return a refresh token")
        granted = _scope_set(credentials.get("scope"))
        if granted and self.scope not in granted:
            raise RuntimeError("Withings authorization did not grant the user.activity scope")
        self._save_credentials(credentials)
        self.state_db.set_value(self.oauth_state_key, "")
        return {
            "expires_at": _expiry_iso(credentials.get("expires_at")),
            "scope": credentials.get("scope"),
        }

    def access_token(self) -> str:
        if not self.is_oauth_configured():
            raise RuntimeError("Withings client ID and client secret are not configured")
        credentials = self._load_credentials()
        if not credentials or not credentials.get("refresh_token"):
            raise RuntimeError("Withings is not authorized; run `python sync.py withings-auth-url`")

        expires_at = _number(credentials.get("expires_at"))
        access_token = credentials.get("access_token")
        if isinstance(access_token, str) and access_token and expires_at > time.time() + 60:
            return access_token

        body = self._request_token(
            {
                "grant_type": "refresh_token",
                "refresh_token": str(credentials["refresh_token"]),
            },
            "token refresh",
        )
        if not body.get("refresh_token"):
            raise RuntimeError("Withings token refresh did not rotate the refresh token")
        refreshed = self._credentials_from_body(body, previous=credentials)
        if not refreshed.get("refresh_token"):
            raise RuntimeError("Withings token refresh did not return a refresh token")
        self._save_credentials(refreshed)
        new_access_token = refreshed.get("access_token")
        if not isinstance(new_access_token, str) or not new_access_token:
            raise RuntimeError("Withings token refresh did not return an access token")
        return new_access_token

    def post_activity(self, fields: Mapping[str, object], operation: str) -> dict[str, Any]:
        response = self.session.post(
            f"{self.api_root}/v2/measure",
            data=dict(fields),
            headers={"Authorization": f"Bearer {self.access_token()}", "Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        return _response_body(response, operation)

    def _request_token(self, grant_fields: Mapping[str, str], operation: str) -> dict[str, Any]:
        if not self.is_oauth_configured():
            raise RuntimeError("Withings client ID and client secret are not configured")
        fields = {
            "action": "requesttoken",
            "client_id": self.config.withings_client_id,
            "client_secret": self.config.withings_client_secret,
            **grant_fields,
        }
        response = self.session.post(
            f"{self.api_root}/v2/oauth2",
            data=fields,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        return _response_body(response, operation)

    def _load_credentials(self) -> dict[str, Any]:
        raw = self.state_db.get_value(self.credentials_key)
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Saved Withings credentials are invalid JSON") from exc
        if not isinstance(value, dict):
            raise RuntimeError("Saved Withings credentials are not an object")
        return value

    def _credentials_from_body(
        self,
        body: Mapping[str, Any],
        *,
        previous: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        access_token = body.get("access_token")
        refresh_token = body.get("refresh_token") or (previous or {}).get("refresh_token")
        if not isinstance(access_token, str) or not access_token:
            raise RuntimeError("Withings did not return an access token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise RuntimeError("Withings did not return a refresh token")
        expires_in = _number(body.get("expires_in"))
        if expires_in <= 0:
            raise RuntimeError("Withings returned an invalid token lifetime")
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": int(time.time() + expires_in),
            "scope": body.get("scope") or (previous or {}).get("scope"),
            "userid": body.get("userid") or (previous or {}).get("userid"),
        }

    def _save_credentials(self, credentials: Mapping[str, Any]) -> None:
        self.state_db.set_value(
            self.credentials_key,
            json.dumps(dict(credentials), ensure_ascii=False, sort_keys=True),
        )


class WithingsSource(SourceAdapter):
    name = "withings"

    def __init__(self, config: AppConfig, client: WithingsClient):
        super().__init__(config)
        self.client = client

    def is_configured(self) -> bool:
        return self.client.is_oauth_configured() and self.client.is_authorized()

    def authenticate(self) -> None:
        self.client.access_token()

    def list_activities(
        self,
        since: datetime | None,
        until: datetime | None,
        limit: int | None,
    ) -> list[Activity]:
        fields: dict[str, object] = {"action": "getworkouts"}
        if since:
            fields["startdateymd"] = _as_utc(since).date().isoformat()
        if until:
            fields["enddateymd"] = _as_utc(until).date().isoformat()

        body = self.client.post_activity(fields, "workout listing")
        workouts = body.get("series")
        if workouts is None:
            workouts = []
        if not isinstance(workouts, list):
            raise RuntimeError("Withings workout response series is not a list")

        activities: list[Activity] = []
        for workout in workouts:
            if not isinstance(workout, dict):
                raise RuntimeError("Withings workout response contains a non-object entry")
            activity = _map_workout(workout)
            if since and activity.start_time and activity.start_time < _as_utc(since):
                continue
            if until and activity.start_time and activity.start_time > _as_utc(until):
                continue
            activities.append(activity)
            if limit and len(activities) >= limit:
                break
        return sorted(activities, key=lambda item: item.start_time or datetime.min.replace(tzinfo=timezone.utc))

    def download_fit(self, activity: Activity, output_dir: Path) -> Path:
        activity_dir = output_dir / self.name
        activity_dir.mkdir(parents=True, exist_ok=True)
        path = activity_dir / f"{safe_filename(activity.source_id)}.fit"
        if path.is_file() and path.stat().st_size > 100:
            with path.open("rb") as cached:
                if cached.read(12)[8:12] == b".FIT":
                    return path

        workout = _workout_for_activity(activity, self)
        start_time = activity.start_time or _datetime(workout.get("startdate"))
        end_time = _datetime(workout.get("enddate")) or start_time
        if start_time is None or end_time is None:
            raise RuntimeError(f"Withings workout {activity.source_id} has no valid start/end time")
        start_time = _as_utc(start_time)
        end_time = _as_utc(max(end_time, start_time))

        gps_body = self.client.post_activity(
            {
                "action": "getactivity",
                "startdateymd": start_time.date().isoformat(),
                "enddateymd": end_time.date().isoformat(),
                "data_fields": "gps",
            },
            "GPS detail fetch",
        )
        intraday_body = self.client.post_activity(
            {
                "action": "getintradayactivity",
                "startdate": int(start_time.timestamp()),
                "enddate": int(end_time.timestamp()),
            },
            "intraday detail fetch",
        )
        fit_activity = _build_fit_activity(workout, start_time, end_time, gps_body, intraday_body)
        payload = _write_fit(fit_activity, allow_trackless_records=True)
        _atomic_write_bytes(path, payload, validate_fit=True)
        return path


def _response_body(response: Any, operation: str) -> dict[str, Any]:
    response.raise_for_status()
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Withings {operation} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Withings {operation} response is not an object")
    status = payload.get("status")
    if status != 0:
        raise RuntimeError(f"Withings {operation} failed (status={status!r})")
    body = payload.get("body")
    if not isinstance(body, dict):
        raise RuntimeError(f"Withings {operation} response body is not an object")
    return body


def _map_workout(workout: dict[str, Any]) -> Activity:
    source_id = workout.get("id")
    start_time = _datetime(workout.get("startdate"))
    if source_id is None or start_time is None:
        raise RuntimeError("Withings workout is missing an ID or valid start date")
    name = workout.get("name")
    return Activity(
        source=WithingsSource.name,
        source_id=str(source_id),
        name=str(name or f"Withings-{source_id}"),
        sport_type=_sport_type(workout.get("category"), workout.get("sport")),
        start_time=start_time,
        raw=workout,
    )


def _workout_for_activity(activity: Activity, source: WithingsSource) -> dict[str, Any]:
    raw = activity.raw
    if isinstance(raw, dict) and str(raw.get("id")) == activity.source_id:
        return raw
    found = source.list_activities(activity.start_time, activity.start_time, None)
    match = next((item.raw for item in found if item.source_id == activity.source_id), None)
    if not isinstance(match, dict):
        raise RuntimeError(f"Withings workout {activity.source_id} was not found")
    return match


def _build_fit_activity(
    workout: Mapping[str, Any],
    start_time: datetime,
    end_time: datetime,
    gps_body: Mapping[str, Any],
    intraday_body: Mapping[str, Any],
) -> ActivityFile:
    summary = workout.get("data")
    if not isinstance(summary, Mapping):
        summary = {}
    gps_points = _extract_gps_points(gps_body, start_time, end_time)
    intraday_points = _extract_intraday_points(intraday_body, start_time, end_time)
    points = _merge_detail_points(gps_points, intraday_points)
    if not points:
        points = [TrackPoint(timestamp=start_time)]

    distance = _optional_number(summary.get("distance"))
    calories = _optional_int(summary.get("calories"))
    average_hr = _optional_number(summary.get("hr_average"))
    maximum_hr = _optional_number(summary.get("hr_max"))
    average_cadence = _optional_number(summary.get("cadence_average"))
    elapsed = max(0.0, (end_time - start_time).total_seconds())
    lap = ActivityLap(
        start_time=start_time,
        end_time=end_time,
        elapsed_time_s=elapsed,
        timer_time_s=elapsed,
        distance_m=distance,
        calories=calories,
        average_heart_rate=average_hr,
        maximum_heart_rate=maximum_hr,
        track_points=points,
        average_cadence=average_cadence,
    )
    return ActivityFile(
        name=str(workout.get("name") or f"Withings-{workout.get('id', '')}"),
        sport_type=_sport_type(workout.get("category"), workout.get("sport")),
        start_time=start_time,
        end_time=end_time,
        elapsed_time_s=elapsed,
        timer_time_s=elapsed,
        distance_m=distance,
        laps=[lap],
        average_heart_rate_bpm=average_hr,
        maximum_heart_rate_bpm=maximum_hr,
        average_cadence=average_cadence,
        creator="sport_sync_bridge Withings import",
    )


def _extract_gps_points(payload: Mapping[str, Any], start: datetime, end: datetime) -> list[TrackPoint]:
    body = payload.get("series", payload)
    gps_values = list(_find_named_values(body, {"gps", "track", "track_points", "trackpoints"}))
    points: list[TrackPoint] = []
    for value in gps_values:
        points.extend(_parse_points(value, start, end, require_position=True))
    unique: dict[datetime, TrackPoint] = {}
    for point in points:
        if point.timestamp is None or point.latitude is None or point.longitude is None:
            continue
        unique[point.timestamp] = point
    return [unique[key] for key in sorted(unique)]


def _extract_intraday_points(
    payload: Mapping[str, Any], start: datetime, end: datetime
) -> list[TrackPoint]:
    series = payload.get("series", payload)
    points = _parse_points(series, start, end, require_position=False)
    unique: dict[datetime, TrackPoint] = {}
    for point in points:
        if point.timestamp is not None:
            unique[point.timestamp] = point
    return [unique[key] for key in sorted(unique)]


def _parse_points(
    value: Any,
    start: datetime,
    end: datetime,
    *,
    require_position: bool,
    timestamp_hint: datetime | None = None,
) -> list[TrackPoint]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if isinstance(value, Mapping):
        direct = _point_from_mapping(value, timestamp_hint)
        if direct is not None and (not require_position or (direct.latitude is not None and direct.longitude is not None)):
            return [direct] if _within_activity(direct.timestamp, start, end) else []

        array_points = _points_from_parallel_arrays(value, start, end, require_position)
        if array_points:
            return array_points

        points: list[TrackPoint] = []
        for key, child in value.items():
            child_time = _datetime(key) or timestamp_hint
            points.extend(
                _parse_points(
                    child,
                    start,
                    end,
                    require_position=require_position,
                    timestamp_hint=child_time,
                )
            )
        return points
    if isinstance(value, (list, tuple)):
        if len(value) >= 3:
            timestamp = _datetime(value[0])
            latitude = _optional_number(value[1])
            longitude = _optional_number(value[2])
            if timestamp and latitude is not None and longitude is not None:
                point = TrackPoint(
                    timestamp=timestamp,
                    latitude=latitude,
                    longitude=longitude,
                    elevation_m=_optional_number(value[3]) if len(value) > 3 else None,
                )
                if _within_activity(point.timestamp, start, end):
                    return [point]
        if require_position and timestamp_hint is not None and len(value) >= 2:
            latitude = _optional_number(value[0])
            longitude = _optional_number(value[1])
            if (
                latitude is not None
                and longitude is not None
                and -90 <= latitude <= 90
                and -180 <= longitude <= 180
            ):
                point = TrackPoint(
                    timestamp=timestamp_hint,
                    latitude=latitude,
                    longitude=longitude,
                    elevation_m=_optional_number(value[2]) if len(value) > 2 else None,
                )
                if _within_activity(point.timestamp, start, end):
                    return [point]
        points = []
        for child in value:
            points.extend(
                _parse_points(child, start, end, require_position=require_position)
            )
        return points
    return []


def _point_from_mapping(value: Mapping[str, Any], timestamp_hint: datetime | None) -> TrackPoint | None:
    keys = {str(key).lower(): item for key, item in value.items()}
    timestamp = next(
        (
            _datetime(keys[key])
            for key in ("timestamp", "time", "date", "startdate", "start_time")
            if key in keys and _datetime(keys[key]) is not None
        ),
        timestamp_hint,
    )
    metric_keys = {"heart_rate", "hr", "speed", "distance", "latitude", "longitude", "lat", "lon"}
    if timestamp is None and not metric_keys.intersection(keys):
        return None
    return TrackPoint(
        timestamp=timestamp,
        latitude=_first_number(keys, "latitude", "lat"),
        longitude=_first_number(keys, "longitude", "lon", "lng"),
        elevation_m=_first_number(keys, "altitude", "elevation", "altitude_m"),
        distance_m=_first_number(keys, "distance", "distance_m"),
        speed_mps=_first_number(keys, "speed", "speed_mps"),
        heart_rate_bpm=_first_number(keys, "heart_rate", "hr"),
        cadence_rpm=_first_number(keys, "cadence", "cadence_rpm"),
        power_w=_first_number(keys, "power", "power_w"),
    )


def _points_from_parallel_arrays(
    value: Mapping[str, Any], start: datetime, end: datetime, require_position: bool
) -> list[TrackPoint]:
    keys = {str(key).lower(): item for key, item in value.items()}
    latitudes = keys.get("latitude", keys.get("latitudes", keys.get("lat")))
    longitudes = keys.get("longitude", keys.get("longitudes", keys.get("lon")))
    timestamps = keys.get("timestamps", keys.get("timestamp", keys.get("times")))
    if not (isinstance(latitudes, list) and isinstance(longitudes, list) and isinstance(timestamps, list)):
        return []
    if len(latitudes) != len(longitudes) or len(latitudes) != len(timestamps):
        raise RuntimeError("Withings GPS arrays have different lengths")
    points = []
    for stamp, latitude, longitude in zip(timestamps, latitudes, longitudes, strict=True):
        point = TrackPoint(
            timestamp=_datetime(stamp),
            latitude=_optional_number(latitude),
            longitude=_optional_number(longitude),
        )
        if point.timestamp and point.latitude is not None and point.longitude is not None:
            if _within_activity(point.timestamp, start, end):
                points.append(point)
    return points


def _merge_detail_points(gps: list[TrackPoint], intraday: list[TrackPoint]) -> list[TrackPoint]:
    if not gps:
        return intraday
    merged = list(gps)
    for sample in intraday:
        if sample.timestamp is None:
            continue
        nearest = min(
            (record for record in gps if record.timestamp is not None),
            key=lambda record: abs((record.timestamp - sample.timestamp).total_seconds()),
            default=None,
        )
        if nearest is None or nearest.timestamp is None:
            merged.append(sample)
            continue
        if abs((nearest.timestamp - sample.timestamp).total_seconds()) > 1:
            merged.append(sample)
            continue
        copied = False
        for field in ("heart_rate_bpm", "cadence_rpm", "speed_mps", "distance_m", "power_w"):
            value = getattr(sample, field)
            if value is not None and getattr(nearest, field) is None:
                setattr(nearest, field, value)
                copied = True
        if not copied and any(
            getattr(sample, field) is not None
            for field in ("heart_rate_bpm", "cadence_rpm", "speed_mps", "distance_m", "power_w")
        ):
            merged.append(sample)
    return sorted(merged, key=lambda record: record.timestamp or datetime.min.replace(tzinfo=timezone.utc))


def _find_named_values(value: Any, names: set[str]):
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).lower() in names:
                yield child
            yield from _find_named_values(child, names)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _find_named_values(child, names)


def _sport_type(category: Any, sport: Any = None) -> str:
    text = str(sport or category or "").strip().lower()
    if text in {"running", "run", "1"}:
        return "running"
    if text in {"walking", "walk", "2"}:
        return "walking"
    if text in {"cycling", "cycle", "biking", "6"}:
        return "cycling"
    return "generic"


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        try:
            stamp = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if abs(stamp) > 100_000_000_000:
            stamp /= 1000
        try:
            return datetime.fromtimestamp(stamp, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return _as_utc(parsed)
    return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _within_activity(value: datetime | None, start: datetime, end: datetime) -> bool:
    return value is not None and start.timestamp() - 5 <= value.timestamp() <= end.timestamp() + 5


def _number(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _optional_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _optional_int(value: Any) -> int | None:
    number = _optional_number(value)
    return int(round(number)) if number is not None else None


def _first_number(values: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key in values:
            number = _optional_number(values[key])
            if number is not None:
                return number
    return None


def _scope_set(value: Any) -> set[str]:
    if isinstance(value, str):
        return {part.strip() for part in value.replace(" ", ",").split(",") if part.strip()}
    if isinstance(value, list):
        return {str(part).strip() for part in value if str(part).strip()}
    return set()


def _expiry_iso(value: Any) -> str | None:
    stamp = _optional_number(value)
    if stamp is None:
        return None
    return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat()
