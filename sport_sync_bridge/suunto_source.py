from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from .config import AppConfig
from .models import Activity
from .sources import SourceAdapter
from .suunto_api import SuuntoClient, raise_for_response, unwrap_payload
from .utils import fit_signature_ok, safe_filename


_SPORT_NAMES = {
    "cycling": "cycling",
    "bike": "cycling",
    "running": "running",
    "run": "running",
    "trail running": "running",
    "swimming": "swimming",
    "swim": "swimming",
    "walking": "walking",
    "walk": "walking",
    "hiking": "hiking",
    "trekking": "hiking",
    "rowing": "rowing",
    "indoor rowing": "rowing",
    "mountain biking": "cycling",
}
_PAGE_SIZE = 50


class SuuntoSource(SourceAdapter):
    name = "suunto"

    def __init__(self, config: AppConfig, client: SuuntoClient):
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

        activities: list[Activity] = []
        offset = 0
        while limit is None or len(activities) < limit:
            page_size = _PAGE_SIZE if limit is None else min(_PAGE_SIZE, limit - len(activities))
            params: dict[str, object] = {"limit": page_size, "offset": offset}
            if since is not None:
                params["since"] = _utc_isoformat(since)
            if until is not None:
                params["until"] = _utc_isoformat(until)

            rows, has_more = _workout_rows(
                self.client.get_json("/v3/workouts", params=params)
            )
            for index, item in enumerate(rows):
                if not isinstance(item, Mapping):
                    raise RuntimeError(f"Suunto workout {offset + index} must be a JSON object")
                activity = _activity_from_workout(item, offset + index)
                if since and activity.start_time and activity.start_time < _as_utc(since):
                    continue
                if until and activity.start_time and activity.start_time > _as_utc(until):
                    continue
                activities.append(activity)
                if limit is not None and len(activities) >= limit:
                    break

            offset += len(rows)
            if not rows or not has_more:
                break

        activities.sort(
            key=lambda item: (
                item.start_time or datetime.min.replace(tzinfo=timezone.utc),
                item.source_id,
            )
        )
        return activities[:limit] if limit is not None else activities

    def download_fit(self, activity: Activity, output_dir: Path) -> Path:
        activity_dir = output_dir / self.name
        activity_dir.mkdir(parents=True, exist_ok=True)
        output_path = activity_dir / f"{safe_filename(activity.source_id)}.fit"
        if output_path.is_file() and fit_signature_ok(output_path):
            return output_path

        escaped_id = quote(activity.source_id, safe="")
        paths = (
            f"/v3/workouts/{escaped_id}/fit",
            f"/v2/workout/exportFit/{escaped_id}",
        )
        last_error: Exception | None = None
        payload: bytes | None = None
        for path in paths:
            try:
                response = self.client.api_request("GET", path, timeout=90)
                if response.status_code == 404:
                    continue
                raise_for_response(response, "FIT download")
                payload = response.content
                break
            except (OSError, requests.RequestException, RuntimeError) as exc:
                last_error = exc
                break
        if payload is None:
            if last_error is not None:
                raise RuntimeError(f"Suunto FIT download failed for {activity.source_id}") from last_error
            raise RuntimeError(f"Suunto workout {activity.source_id} was not found")

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=".suunto-",
                suffix=".fit",
                dir=activity_dir,
                delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
                stream.write(payload)
            if not fit_signature_ok(temporary_path):
                raise RuntimeError("Suunto returned a file with an invalid FIT signature")
            os.replace(temporary_path, output_path)
            temporary_path = None
            return output_path
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def _workout_rows(payload: Any) -> tuple[list[Any], bool]:
    value = unwrap_payload(payload)
    if isinstance(value, list):
        return value, len(value) >= _PAGE_SIZE
    if not isinstance(value, Mapping):
        raise RuntimeError("Suunto workout list response must contain a JSON array")
    for key in ("workouts", "items", "activities", "results", "data"):
        candidate = value.get(key)
        if isinstance(candidate, list):
            has_more = value.get("hasMore")
            if isinstance(has_more, bool):
                return candidate, has_more
            total = value.get("total")
            offset = value.get("offset")
            if isinstance(total, int) and isinstance(offset, int):
                return candidate, offset + len(candidate) < total
            return candidate, len(candidate) >= _PAGE_SIZE
        if isinstance(candidate, Mapping):
            return _workout_rows(candidate)
    raise RuntimeError("Suunto workout list response did not contain workouts")


def _activity_from_workout(item: Mapping[str, object], index: int) -> Activity:
    raw_id = item.get("workoutKey") or item.get("id") or item.get("workoutId")
    if raw_id in (None, ""):
        raise RuntimeError(f"Suunto workout {index} is missing workoutKey")
    source_id = str(raw_id).strip()
    if not source_id:
        raise RuntimeError(f"Suunto workout {index} has an empty workoutKey")

    raw_name = item.get("workoutName") or item.get("name") or item.get("activityName")
    name = raw_name.strip() if isinstance(raw_name, str) else ""
    if not name:
        name = f"Suunto-{source_id}"
    raw_sport = item.get("activityType") or item.get("sport") or item.get("activityName")
    sport_type = _sport_type(raw_sport)
    start_time = _start_time(item.get("startTime"))
    return Activity(
        source=SuuntoSource.name,
        source_id=source_id,
        name=name,
        sport_type=sport_type,
        start_time=start_time,
        raw=dict(item),
    )


def _sport_type(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.casefold().replace("_", " ").split())
    return _SPORT_NAMES.get(normalized, normalized or None)


def _start_time(value: object) -> datetime | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise RuntimeError("Suunto workout has an invalid startTime") from exc
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RuntimeError("Suunto workout has an invalid startTime") from exc
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    raise RuntimeError("Suunto workout has an unsupported startTime value")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_isoformat(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")
