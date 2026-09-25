from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from .config import AppConfig
from .hammerhead_api import HammerheadClient
from .models import Activity
from .sources import SourceAdapter
from .utils import fit_signature_ok, parse_datetime, safe_filename


class HammerheadSource(SourceAdapter):
    name = "hammerhead"
    page_size = 100

    def __init__(self, config: AppConfig, client: HammerheadClient):
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
        params: dict[str, str | int] = {"perPage": self.page_size}
        if since is not None:
            params["startDate"] = _utc_date(since)

        activities: list[Activity] = []
        for row in self._list_pages("activities", params):
            activity = _activity_from_summary(row)
            if not _within_range(activity.start_time, since, until):
                continue
            activities.append(activity)
            if limit is not None and len(activities) >= limit:
                break
        return sorted(activities, key=_activity_sort_key)

    def list_routes(self, limit: int | None = None) -> list[dict]:
        if limit is not None and limit <= 0:
            return []
        routes: list[dict] = []
        for row in self._list_pages("routes", {"perPage": self.page_size}):
            routes.append(row)
            if limit is not None and len(routes) >= limit:
                break
        return routes

    def download_fit(self, activity: Activity, output_dir: Path) -> Path:
        activity_dir = output_dir / self.name
        activity_dir.mkdir(parents=True, exist_ok=True)
        path = activity_dir / f"{safe_filename(activity.source_id)}.fit"
        if fit_signature_ok(path):
            return path

        response = self.client.api_request(
            "get",
            f"activities/{quote(activity.source_id, safe='')}/file",
            headers={"Accept": "application/vnd.ant.fit"},
            timeout=120,
        )
        response.raise_for_status()
        temporary_path = path.with_name(f"{path.name}.tmp")
        try:
            temporary_path.write_bytes(response.content)
            if not fit_signature_ok(temporary_path):
                raise RuntimeError(
                    f"Hammerhead returned an invalid FIT file for activity {activity.source_id}"
                )
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)
        return path

    def _list_pages(self, endpoint: str, params: dict[str, str | int]) -> Iterator[dict]:
        page = 1
        while True:
            response = self.client.api_request(
                "get",
                endpoint,
                params={**params, "page": page},
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise RuntimeError(f"Hammerhead {endpoint} response must contain a data list")
            for row in payload["data"]:
                if not isinstance(row, dict):
                    raise RuntimeError(f"Hammerhead {endpoint} entries must be JSON objects")
                yield row

            total_pages = payload.get("totalPages")
            try:
                total_pages = int(total_pages)
            except (TypeError, ValueError):
                raise RuntimeError(f"Hammerhead {endpoint} response is missing totalPages") from None
            if total_pages < 1:
                raise RuntimeError(f"Hammerhead {endpoint} response has invalid totalPages")
            if page >= total_pages:
                break
            page += 1


def _activity_from_summary(row: dict) -> Activity:
    raw_id = row.get("id")
    if raw_id in (None, ""):
        raise RuntimeError("Hammerhead activity is missing its ID")
    source_id = str(raw_id)
    return Activity(
        source="hammerhead",
        source_id=source_id,
        name=str(row.get("name") or source_id),
        sport_type=_sport_type(row.get("activityType")),
        start_time=parse_datetime(row.get("createdAt")),
        raw=row,
    )


def _sport_type(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().upper()
    return {
        "RIDE": "cycling",
        "EBIKE": "e_biking",
        "MOUNTAIN_BIKE": "mountain_biking",
        "GRAVEL": "cycling",
        "EMOUNTAIN_BIKE": "e_biking",
        "VELOMOBILE": "cycling",
    }.get(normalized)


def _utc_date(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).date().isoformat()


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
