from __future__ import annotations

from datetime import date, timedelta
from typing import Protocol


class TrainingReadinessClient(Protocol):
    def get_training_readiness(self, cdate: str) -> object: ...


def validate_training_readiness_date_range(start_date: str, end_date: str) -> tuple[date, date]:
    start = _parse_iso_date(start_date, "start-date")
    end = _parse_iso_date(end_date, "end-date")
    if end < start:
        raise ValueError("Training readiness end date must be on or after the start date")
    return start, end


def fetch_garmin_training_readiness(
    client: TrainingReadinessClient,
    start_date: str,
    end_date: str,
) -> list[dict[str, object]]:
    start, end = validate_training_readiness_date_range(start_date, end_date)
    fetch_daily = getattr(client, "get_training_readiness", None)
    if not callable(fetch_daily):
        raise RuntimeError("Garmin client does not support training readiness data")

    records: list[dict[str, object]] = []
    current = start
    while current <= end:
        response = fetch_daily(current.isoformat())
        if not isinstance(response, list):
            raise ValueError(
                f"Garmin training readiness response for {current.isoformat()} must be a list"
            )
        for index, item in enumerate(response, 1):
            if not isinstance(item, dict):
                raise ValueError(
                    "Garmin training readiness record "
                    f"{index} for {current.isoformat()} must be an object"
                )
            record = dict(item)
            if not record.get("calendarDate"):
                record["calendarDate"] = current.isoformat()
            if not record.get("sourceId"):
                record["sourceId"] = "garmin"
            records.append(record)
        current += timedelta(days=1)
    return records


def _parse_iso_date(value: str, field: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Training readiness {field} must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"Training readiness {field} must use YYYY-MM-DD")
    return parsed
