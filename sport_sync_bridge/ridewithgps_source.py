from __future__ import annotations

import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from typing import Any

from .config import AppConfig
from .models import Activity
from .ridewithgps_api import RideWithGPSClient, _positive_id, _raise_for_response
from .sources import SourceAdapter
from .utils import fit_signature_ok, parse_datetime, safe_filename


class RideWithGPSSource(SourceAdapter):
    name = "ridewithgps"
    page_size = 200

    def __init__(self, config: AppConfig, client: RideWithGPSClient):
        super().__init__(config)
        self.client = client

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

        lower_bound = _as_utc(since)
        upper_bound = _as_utc(until)
        activities: list[Activity] = []
        page = 1
        while True:
            payload = self.client.get_json(
                "/trips.json",
                params={"page": page, "page_size": self.page_size},
            )
            rows = payload.get("trips")
            if not isinstance(rows, list):
                raise RuntimeError("Ride with GPS trips response must contain a trips list")
            for index, item in enumerate(rows):
                if not isinstance(item, Mapping):
                    raise RuntimeError(f"Ride with GPS trip {index} must be a JSON object")
                activity = _activity_from_trip(item, index)
                if lower_bound is not None and activity.start_time < lower_bound:
                    continue
                if upper_bound is not None and activity.start_time > upper_bound:
                    continue
                activities.append(activity)
                if limit is not None and len(activities) >= limit:
                    return sorted(activities, key=_activity_sort_key)

            if not rows:
                break
            pagination = _pagination(payload)
            page_count = pagination.get("page_count")
            if page_count is not None:
                try:
                    if page >= int(page_count):
                        break
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("Ride with GPS pagination page_count must be an integer") from exc
            elif len(rows) < self.page_size:
                break
            page += 1

        return sorted(activities, key=_activity_sort_key)

    def download_fit(self, activity: Activity, output_dir: Path) -> Path:
        trip_id = _positive_id(activity.source_id, "trip ID")
        activity_dir = output_dir / self.name
        activity_dir.mkdir(parents=True, exist_ok=True)
        path = activity_dir / f"{safe_filename(trip_id)}.fit"
        if path.is_file() and path.stat().st_size >= 100 and fit_signature_ok(path):
            return path

        response = self.client.api_request(
            "GET",
            f"/trips/{quote(trip_id, safe='')}.fit",
            headers={"Accept": "application/vnd.ant.fit"},
            timeout=120,
        )
        _raise_for_response(response, f"download trip {trip_id}")
        content = response.content
        if not isinstance(content, bytes) or len(content) < 100:
            raise RuntimeError(f"Ride with GPS returned an empty FIT file for trip {trip_id}")

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=activity_dir, prefix=f".{path.name}.", suffix=".tmp", delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(content)
            if not fit_signature_ok(temporary_path):
                raise RuntimeError(f"Ride with GPS returned an invalid FIT file for trip {trip_id}")
            temporary_path.replace(path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return path


def _activity_from_trip(item: Mapping[str, Any], index: int) -> Activity:
    raw_id = item.get("id")
    if raw_id in (None, ""):
        raise RuntimeError(f"Ride with GPS trip {index} is missing its ID")
    source_id = _positive_id(raw_id, "trip ID")
    start_time = parse_datetime(item.get("departed_at"))
    if start_time is None:
        raise RuntimeError(f"Ride with GPS trip {source_id} is missing a valid departed_at")
    name_value = item.get("name")
    name = str(name_value).strip() if name_value not in (None, "") else f"Ride with GPS trip {source_id}"
    return Activity(
        source="ridewithgps",
        source_id=source_id,
        name=name,
        sport_type=_sport_type(item.get("activity_type")),
        start_time=start_time,
        raw=dict(item),
    )


def _sport_type(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    category = value.split(":", maxsplit=1)[0].strip().lower()
    return {
        "cycling": "cycling",
        "running": "running",
        "walking": "walking",
        "swimming": "swimming",
        "motorcycling": "motorcycling",
        "driving": "driving",
        "snow": "winter_sports",
    }.get(category)


def _pagination(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    meta = payload.get("meta")
    if not isinstance(meta, Mapping):
        return {}
    pagination = meta.get("pagination")
    return pagination if isinstance(pagination, Mapping) else {}


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _activity_sort_key(activity: Activity) -> tuple[bool, datetime]:
    timestamp = activity.start_time or datetime.min.replace(tzinfo=timezone.utc)
    return activity.start_time is None, timestamp
