from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .config import AppConfig
from .giant_api import GiantClient
from .models import Activity
from .sources import SourceAdapter
from .utils import fit_signature_ok, parse_datetime, safe_filename


class GiantSource(SourceAdapter):
    name = "giant"

    def __init__(self, config: AppConfig, client: GiantClient):
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
        lower_bound = _as_utc(since)
        upper_bound = _as_utc(until)

        year_payload = self.client.get_json("/v4/cycling")
        years = _integer_values(year_payload, "year", "Giant cycling years")
        result_by_id: dict[str, Activity] = {}
        for year in years:
            if lower_bound is not None and year < lower_bound.year:
                continue
            if upper_bound is not None and year > upper_bound.year:
                continue
            month_payload = self.client.get_json(f"/v4/cycling/{year}")
            months = _integer_values(month_payload, "month", f"Giant cycling months for {year}")
            for month in months:
                if not 1 <= month <= 12:
                    raise RuntimeError(f"Giant returned an invalid month: {month}")
                if lower_bound is not None and (year, month) < (lower_bound.year, lower_bound.month):
                    continue
                if upper_bound is not None and (year, month) > (upper_bound.year, upper_bound.month):
                    continue
                payload = self.client.get_json(f"/v1.1/cycling/{year}/{month}")
                rows = _activity_rows(payload, year, month)
                for index, row in enumerate(rows):
                    activity = _activity_from_record(row, year, month, index)
                    if lower_bound is not None and activity.start_time < lower_bound:
                        continue
                    if upper_bound is not None and activity.start_time > upper_bound:
                        continue
                    result_by_id.setdefault(activity.source_id, activity)
                    if limit is not None and len(result_by_id) >= limit:
                        return sorted(result_by_id.values(), key=_activity_sort_key)

        return sorted(result_by_id.values(), key=_activity_sort_key)

    def download_fit(self, activity: Activity, output_dir: Path) -> Path:
        activity_dir = output_dir / self.name
        path = activity_dir / f"{safe_filename(activity.source_id)}.fit"
        if path.is_file() and path.stat().st_size >= 100 and fit_signature_ok(path):
            return path

        detail_payload = self.client.get_json(
            f"/v3.2/cycling/{quote(activity.source_id, safe='')}"
        )
        detail = _retval_mapping(detail_payload, f"Giant activity {activity.source_id} detail")
        return self.client.download_fit_file(detail.get("fitUrl"), path)


def _activity_rows(payload: Mapping[str, Any], year: int, month: int) -> list[Mapping[str, Any]]:
    rows = _list_from_response(payload, f"Giant activities for {year}-{month:02d}")
    result: list[Mapping[str, Any]] = []
    for index, item in enumerate(rows):
        if not isinstance(item, Mapping):
            raise RuntimeError(
                f"Giant activity at {year}-{month:02d}, index {index} must be an object"
            )
        result.append(item)
    return result


def _activity_from_record(
    row: Mapping[str, Any],
    year: int,
    month: int,
    index: int,
) -> Activity:
    raw_id = row.get("id")
    if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)) or not str(raw_id).strip():
        raise RuntimeError(f"Giant activity at {year}-{month:02d}, index {index} has no valid ID")
    source_id = str(raw_id).strip()
    started_at = _datetime_value(row.get("started_at"))
    if started_at is None:
        raise RuntimeError(f"Giant activity {source_id} has no valid started_at value")
    raw_title = row.get("title")
    name = str(raw_title).strip() if raw_title not in (None, "") else f"Giant activity {source_id}"
    return Activity(
        source="giant",
        source_id=source_id,
        name=name,
        sport_type="cycling",
        start_time=started_at,
        raw=dict(row),
    )


def _integer_values(payload: Mapping[str, Any], key: str, label: str) -> list[int]:
    values = _list_from_response(payload, label)
    result: list[int] = []
    for index, item in enumerate(values):
        value = item.get(key) if isinstance(item, Mapping) else item
        parsed = _integer_value(value)
        if parsed is None:
            raise RuntimeError(f"{label} item {index} has no valid {key}")
        if parsed not in result:
            result.append(parsed)
    return sorted(result)


def _list_from_response(payload: Mapping[str, Any], label: str) -> list[Any]:
    candidate: Any = payload.get("retval", payload)
    if isinstance(candidate, Mapping):
        candidate = next(
            (
                candidate[name]
                for name in ("years", "months", "activities", "items", "results", "list")
                if isinstance(candidate.get(name), list)
            ),
            None,
        )
    if not isinstance(candidate, list):
        raise RuntimeError(f"{label} response must contain a list")
    return candidate


def _retval_mapping(payload: Mapping[str, Any], label: str) -> Mapping[str, Any]:
    candidate = payload.get("retval", payload)
    if not isinstance(candidate, Mapping):
        raise RuntimeError(f"{label} response must contain an object")
    return candidate


def _integer_value(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _datetime_value(value: object) -> datetime | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            return datetime.fromtimestamp(float(value.strip()), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            parsed = parse_datetime(value)
            return _as_utc(parsed)
    return None


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _activity_sort_key(activity: Activity) -> tuple[datetime, str]:
    start_time = _as_utc(activity.start_time)
    return (start_time or datetime.max.replace(tzinfo=timezone.utc), activity.source_id)
