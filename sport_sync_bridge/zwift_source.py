from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .config import AppConfig
from .models import Activity
from .sources import SourceAdapter
from .utils import fit_signature_ok, parse_datetime, safe_filename
from .zwift_api import ZwiftClient


class ZwiftSource(SourceAdapter):
    name = "zwift"
    page_size = 50

    def __init__(self, config: AppConfig, client: ZwiftClient):
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
        player_id = self.client.player_id
        if player_id is None:
            raise RuntimeError("Zwift profile did not contain a player ID")

        lower_bound = _as_utc(since)
        upper_bound = _as_utc(until)
        result: list[Activity] = []
        seen_ids: set[str] = set()
        seen_pages: set[tuple[str, ...]] = set()
        offset = 0
        while True:
            payload = self.client.get_json(
                f"/api/profiles/{quote(player_id, safe='')}/activities/",
                params={"start": offset, "limit": self.page_size},
            )
            rows = _activity_rows(payload, offset)
            page_ids = tuple(_activity_id(row, offset + index) for index, row in enumerate(rows))
            if page_ids and page_ids in seen_pages:
                raise RuntimeError("Zwift repeated an activity page while paginating")
            if page_ids:
                seen_pages.add(page_ids)

            for index, row in enumerate(rows):
                activity = _activity_from_record(row, offset + index)
                if activity.source_id in seen_ids:
                    raise RuntimeError(f"Zwift returned duplicate activity ID {activity.source_id}")
                seen_ids.add(activity.source_id)
                if lower_bound is not None and (
                    activity.start_time is None or activity.start_time < lower_bound
                ):
                    continue
                if upper_bound is not None and (
                    activity.start_time is None or activity.start_time > upper_bound
                ):
                    continue
                result.append(activity)
                if limit is not None and len(result) >= limit:
                    return sorted(result, key=_activity_sort_key)

            if not rows or len(rows) < self.page_size:
                break
            offset += len(rows)
        return sorted(result, key=_activity_sort_key)

    def download_fit(self, activity: Activity, output_dir: Path) -> Path:
        activity_dir = output_dir / self.name
        path = activity_dir / f"{safe_filename(activity.source_id)}.fit"
        if path.is_file() and path.stat().st_size >= 100 and fit_signature_ok(path):
            return path
        player_id = self.client.player_id
        if player_id is None:
            player_id = self.client.authenticate()
        detail = self.client.get_json(
            f"/api/profiles/{quote(player_id, safe='')}/activities/{quote(activity.source_id, safe='')}"
        )
        if not isinstance(detail, Mapping):
            raise RuntimeError(f"Zwift activity {activity.source_id} detail must be a JSON object")
        return self.client.download_fit_file(detail.get("fitFileBucket"), detail.get("fitFileKey"), path)


def _activity_rows(payload: Any, offset: int) -> list[Mapping[str, Any]]:
    rows = payload
    if isinstance(payload, Mapping):
        rows = next(
            (payload[key] for key in ("activities", "results", "data") if isinstance(payload.get(key), list)),
            None,
        )
    if not isinstance(rows, list):
        raise RuntimeError(f"Zwift activity response at offset {offset} must be an array")
    result: list[Mapping[str, Any]] = []
    for index, item in enumerate(rows):
        if not isinstance(item, Mapping):
            raise RuntimeError(f"Zwift activity at offset {offset}, index {index} must be an object")
        result.append(item)
    return result


def _activity_id(row: Mapping[str, Any], index: int) -> str:
    value = row.get("id_str") or row.get("id")
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise RuntimeError(f"Zwift activity at index {index} is missing a valid ID")
    return str(value).strip()


def _activity_from_record(row: Mapping[str, Any], index: int) -> Activity:
    activity_id = _activity_id(row, index)
    raw_start = row.get("startDate")
    start_time = parse_datetime(raw_start) if raw_start not in (None, "") else None
    if raw_start not in (None, "") and start_time is None:
        raise RuntimeError(f"Zwift activity {activity_id} has an invalid startDate")
    raw_name = row.get("name")
    name = str(raw_name).strip() if raw_name not in (None, "") else f"Zwift activity {activity_id}"
    return Activity(
        source="zwift",
        source_id=activity_id,
        name=name,
        sport_type=_sport_type(row.get("sport")),
        start_time=start_time,
        raw=dict(row),
    )


def _sport_type(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    if not normalized:
        return None
    tokens = set(normalized.split("_"))
    if tokens & {"ride", "cycling", "cycle", "bike", "biking"}:
        return "cycling"
    if tokens & {"run", "running"}:
        return "running"
    if tokens & {"row", "rowing"}:
        return "rowing"
    if tokens & {"swim", "swimming"}:
        return "swimming"
    return normalized


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _activity_sort_key(activity: Activity) -> tuple[bool, datetime, str]:
    start_time = _as_utc(activity.start_time)
    return (start_time is None, start_time or datetime.max.replace(tzinfo=timezone.utc), activity.source_id)
