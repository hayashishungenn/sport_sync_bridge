from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests

from .config import AppConfig
from .models import Activity
from .sources import SourceAdapter
from .utils import fit_signature_ok, parse_datetime, safe_filename
from .wahoo_target import WahooTarget


class WahooSource(SourceAdapter):
    name = "wahoo"
    page_size = 30
    required_scope = "workouts_read"

    def __init__(self, config: AppConfig, target: WahooTarget):
        super().__init__(config)
        self.target = target

    def is_configured(self) -> bool:
        return self.target.is_configured()

    def authenticate(self) -> None:
        self.target.authenticate_for_scope(self.required_scope)

    def list_activities(
        self,
        since: datetime | None,
        until: datetime | None,
        limit: int | None,
    ) -> list[Activity]:
        if limit is not None and limit <= 0:
            return []
        self.authenticate()

        activities: list[Activity] = []
        page = 1
        while True:
            response = self.target.api_request(
                "get",
                "/v1/workouts",
                params={"page": page, "per_page": self.page_size},
                timeout=30,
            )
            response.raise_for_status()
            payload = _response_object(response)
            rows = payload.get("workouts")
            if not isinstance(rows, list):
                raise RuntimeError("Wahoo workouts response must contain a workouts list")
            if not rows:
                break

            for index, row in enumerate(rows):
                if not isinstance(row, dict):
                    raise RuntimeError(f"Wahoo workout {index} must be a JSON object")
                summary = row.get("workout_summary")
                if not isinstance(summary, dict) or not summary:
                    continue
                activity = _activity_from_workout(row)
                if not _within_range(activity.start_time, since, until):
                    continue
                activities.append(activity)
                if limit is not None and len(activities) >= limit:
                    break

            if limit is not None and len(activities) >= limit:
                break
            total = _integer_or_none(payload.get("total"))
            if (total is not None and page * self.page_size >= total) or len(rows) < self.page_size:
                break
            page += 1

        return sorted(activities, key=_activity_sort_key)

    def download_fit(self, activity: Activity, output_dir: Path) -> Path:
        activity_dir = output_dir / self.name
        activity_dir.mkdir(parents=True, exist_ok=True)
        path = activity_dir / f"{safe_filename(activity.source_id)}.fit"
        if path.is_file() and fit_signature_ok(path):
            return path

        download_url = _summary_file_url(activity.raw.get("workout_summary"))
        if download_url is None:
            workout_id = quote(activity.source_id, safe="")
            response = self.target.api_request(
                "get",
                f"/v1/workouts/{workout_id}/workout_summary",
                timeout=30,
            )
            response.raise_for_status()
            summary = _response_object(response)
            wrapped = summary.get("workout_summary")
            if isinstance(wrapped, dict):
                summary = wrapped
            download_url = _summary_file_url(summary)
        if download_url is None:
            raise RuntimeError(f"Wahoo did not provide a FIT file URL for workout {activity.source_id}")

        parsed_url = urlsplit(download_url)
        if parsed_url.scheme != "https" or not parsed_url.netloc or parsed_url.username or parsed_url.password:
            raise RuntimeError(f"Wahoo returned an invalid FIT file URL for workout {activity.source_id}")

        response = requests.get(download_url, timeout=120)
        response.raise_for_status()
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=activity_dir, prefix=f".{path.name}.", suffix=".tmp", delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(response.content)
            if not fit_signature_ok(temporary_path):
                raise RuntimeError(f"Wahoo returned an invalid FIT file for workout {activity.source_id}")
            temporary_path.replace(path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return path


def _response_object(response: requests.Response) -> dict:
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("Wahoo returned a non-JSON response") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Wahoo returned an invalid response object")
    return payload


def _activity_from_workout(row: dict) -> Activity:
    raw_id = row.get("id")
    if raw_id in (None, ""):
        raise RuntimeError("Wahoo workout is missing its ID")
    source_id = str(raw_id)
    summary = row.get("workout_summary")
    if not isinstance(summary, dict) or not summary:
        raise RuntimeError(f"Wahoo workout {source_id} is missing its workout summary")
    name = summary.get("name") or row.get("name") or f"Wahoo workout {source_id}"
    return Activity(
        source="wahoo",
        source_id=source_id,
        name=str(name),
        sport_type=_sport_type(row.get("workout_type_id")),
        start_time=parse_datetime(row.get("starts")),
        raw=row,
    )


def _summary_file_url(summary: object) -> str | None:
    if not isinstance(summary, dict):
        return None
    file_info = summary.get("file")
    if not isinstance(file_info, dict):
        return None
    url = file_info.get("url")
    if not isinstance(url, str) or not url.strip():
        return None
    return url.strip()


def _sport_type(value: object) -> str | None:
    try:
        workout_type = int(value)
    except (TypeError, ValueError):
        return None
    return {
        0: "cycling",
        1: "running",
        3: "running",
        4: "trail_run",
        5: "running",
        6: "walking",
        7: "walking",
        8: "walking",
        9: "hiking",
        10: "hiking",
        11: "cycling",
        12: "indoor_cycling",
        13: "mountain_biking",
        14: "cycling",
        15: "cycling",
        16: "cycling",
        21: "indoor_cycling",
        22: "rowing",
        25: "swimming",
        26: "swimming",
        39: "rowing",
        49: "indoor_cycling",
        56: "walking",
        61: "virtual_ride",
        64: "e_biking",
        67: "running",
        68: "virtual_ride",
        70: "cycling",
        71: "running",
    }.get(workout_type)


def _integer_or_none(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _within_range(
    value: datetime | None,
    since: datetime | None,
    until: datetime | None,
) -> bool:
    if value is None:
        return since is None and until is None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    if since is not None:
        bound = since.replace(tzinfo=timezone.utc) if since.tzinfo is None else since
        if value < bound.astimezone(timezone.utc):
            return False
    if until is not None:
        bound = until.replace(tzinfo=timezone.utc) if until.tzinfo is None else until
        if value > bound.astimezone(timezone.utc):
            return False
    return True


def _activity_sort_key(activity: Activity) -> tuple[int, float, str]:
    if activity.start_time is None:
        return (1, 0.0, activity.source_id)
    return (0, -activity.start_time.timestamp(), activity.source_id)
