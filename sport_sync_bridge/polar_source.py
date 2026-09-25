from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from .config import AppConfig
from .models import Activity
from .polar_api import PolarClient
from .sources import SourceAdapter
from .utils import fit_signature_ok, parse_datetime, safe_filename


class PolarSource(SourceAdapter):
    name = "polar"

    def __init__(self, config: AppConfig, client: PolarClient):
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
        self.authenticate()
        response = self.client.api_request("get", "exercises", timeout=30)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise RuntimeError("Polar exercise response must be a JSON list")

        activities: list[Activity] = []
        for row in payload:
            if not isinstance(row, dict):
                raise RuntimeError("Polar exercise entries must be JSON objects")
            activity = _activity_from_summary(row)
            if not _within_range(activity.start_time, since, until):
                continue
            activities.append(activity)
        activities.sort(key=_sort_key)
        return activities if limit is None else activities[:limit]

    def download_fit(self, activity: Activity, output_dir: Path) -> Path:
        activity_dir = output_dir / self.name
        activity_dir.mkdir(parents=True, exist_ok=True)
        path = activity_dir / f"{safe_filename(activity.source_id)}.fit"
        if fit_signature_ok(path):
            return path

        response = self.client.api_request(
            "get",
            f"exercises/{quote(activity.source_id, safe='')}/fit",
            headers={"Accept": "*/*"},
            timeout=120,
        )
        response.raise_for_status()
        temporary_path = path.with_name(f"{path.name}.tmp")
        try:
            temporary_path.write_bytes(response.content)
            if not fit_signature_ok(temporary_path):
                raise RuntimeError(f"Polar returned an invalid FIT file for exercise {activity.source_id}")
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)
        return path


def _activity_from_summary(row: dict) -> Activity:
    raw_id = row.get("id")
    if raw_id in (None, ""):
        raise RuntimeError("Polar exercise is missing its hashed ID")
    source_id = str(raw_id)
    sport = row.get("detailed_sport_info") or row.get("sport")
    return Activity(
        source="polar",
        source_id=source_id,
        name=f"Polar {sport}" if sport else f"Polar {source_id}",
        sport_type=_sport_type(sport),
        start_time=_parse_start_time(row),
        raw=row,
    )


def _parse_start_time(row: dict) -> datetime | None:
    raw_value = row.get("start_time")
    if isinstance(raw_value, str):
        try:
            value = datetime.fromisoformat(raw_value.strip().replace("Z", "+00:00"))
        except ValueError:
            value = parse_datetime(raw_value)
    else:
        value = parse_datetime(raw_value)
    if value is None or value.tzinfo is not None:
        return value
    offset = row.get("start_time_utc_offset")
    try:
        offset_minutes = int(offset)
    except (TypeError, ValueError):
        return value.replace(tzinfo=timezone.utc)
    return value.replace(tzinfo=timezone(timedelta(minutes=offset_minutes)))


def _sport_type(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().upper()
    if any(token in normalized for token in ("RUN", "JOG", "TRAIL")):
        return "running"
    if "SWIM" in normalized:
        return "swimming"
    if any(token in normalized for token in ("CYCL", "BIK", "MTB")):
        return "cycling"
    if "WALK" in normalized or "HIK" in normalized:
        return "walking"
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


def _sort_key(activity: Activity) -> tuple[int, float, str]:
    if activity.start_time is None:
        return (1, 0.0, activity.source_id)
    return (0, -activity.start_time.timestamp(), activity.source_id)
