from __future__ import annotations

from datetime import date, timedelta
from typing import Protocol


class TrainingReadinessClient(Protocol):
    def get_training_readiness(self, cdate: str) -> object: ...


class GarminUserSummaryClient(Protocol):
    def get_user_summary(self, cdate: str) -> object: ...


class GarminHealthDetailClient(Protocol):
    def connectapi(self, path: str, **kwargs: object) -> object: ...


class IntervalsWellnessClient(Protocol):
    def list_wellness(self, start_date: str, end_date: str) -> object: ...


GARMIN_HEALTH_DETAIL_ENDPOINTS = {
    "sleep": "/sleep-service/sleep/dailySleepData",
    "hrv": "/hrv-service/hrv/daily",
    "stress": "/wellness-service/wellness/dailyStress",
    "body-battery": "/wellness-service/wellness/bodyBattery/events",
    "respiration": "/wellness-service/wellness/daily/respiration",
    "hydration": "/usersummary-service/usersummary/hydration/allData",
    "blood-pressure": "/bloodpressure-service/bloodpressure/dayview",
    "heart-rate": "/wellness-service/wellness/dailyHeartRate",
    "fitness-age": "/fitnessage-service/fitnessage",
    "spo2-acclimation": "/wellness-service/wellness/daily/spo2acclimation",
    "floors-chart": "/wellness-service/wellness/floorsChartData/daily",
    "steps": "/wellness-service/wellness/wellness-goals/consolidated/steps",
}


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


def validate_garmin_user_summary_date_range(start_date: str, end_date: str) -> tuple[date, date]:
    start = _parse_garmin_summary_date(start_date, "start-date")
    end = _parse_garmin_summary_date(end_date, "end-date")
    if end < start:
        raise ValueError("Garmin user summary end date must be on or after the start date")
    return start, end


def fetch_garmin_user_summaries(
    client: GarminUserSummaryClient,
    start_date: str,
    end_date: str,
) -> list[dict[str, object]]:
    start, end = validate_garmin_user_summary_date_range(start_date, end_date)
    fetch_daily = getattr(client, "get_user_summary", None)
    if not callable(fetch_daily):
        raise RuntimeError("Garmin client does not support daily user summaries")

    records: list[dict[str, object]] = []
    current = start
    while current <= end:
        current_date = current.isoformat()
        response = fetch_daily(current_date)
        if not isinstance(response, dict):
            raise ValueError(
                f"Garmin user summary response for {current_date} must be an object"
            )
        record = dict(response)
        response_date = record.get("calendarDate")
        if response_date not in (None, ""):
            parsed_response_date = _parse_garmin_summary_date(
                str(response_date), f"response date for {current_date}"
            )
            if parsed_response_date != current:
                raise ValueError(
                    f"Garmin user summary date {parsed_response_date.isoformat()} "
                    f"does not match requested date {current_date}"
                )
        record["calendarDate"] = current_date
        records.append(record)
        current += timedelta(days=1)
    return records


def validate_garmin_health_detail_date_range(
    start_date: str, end_date: str
) -> tuple[date, date]:
    start = _parse_garmin_detail_date(start_date, "start-date")
    end = _parse_garmin_detail_date(end_date, "end-date")
    if end < start:
        raise ValueError("Garmin health detail end date must be on or after the start date")
    if (end - start).days > 365:
        raise ValueError("Garmin health detail date range must not exceed 366 days")
    return start, end


def fetch_garmin_health_details(
    client: GarminHealthDetailClient,
    datasets: list[str],
    start_date: str,
    end_date: str,
) -> list[dict[str, object]]:
    start, end = validate_garmin_health_detail_date_range(start_date, end_date)
    if not datasets:
        raise ValueError("At least one Garmin health detail dataset is required")
    if len(set(datasets)) != len(datasets):
        raise ValueError("Garmin health detail datasets must not be repeated")
    unsupported = [dataset for dataset in datasets if dataset not in GARMIN_HEALTH_DETAIL_ENDPOINTS]
    if unsupported:
        raise ValueError(f"Unsupported Garmin health detail dataset: {unsupported[0]}")

    connect_api = getattr(client, "connectapi", None)
    if not callable(connect_api):
        raise RuntimeError("Garmin client does not support raw Garmin Connect API requests")

    records: list[dict[str, object]] = []
    current = start
    while current <= end:
        current_date = current.isoformat()
        for dataset in datasets:
            path = GARMIN_HEALTH_DETAIL_ENDPOINTS[dataset]
            params: dict[str, object] = {}
            if dataset in {"sleep", "heart-rate"}:
                params = {"date": current_date}
                if dataset == "sleep":
                    params["nonSleepBufferMinutes"] = 60
            elif dataset == "hrv":
                path = f"{path}/{current_date}/{current_date}"
            else:
                path = f"{path}/{current_date}"
            response = connect_api(path, **({"params": params} if params else {}))

            if response is None:
                pass
            elif dataset == "body-battery":
                if not isinstance(response, (dict, list)) or (
                    isinstance(response, list)
                    and any(not isinstance(item, dict) for item in response)
                ):
                    raise ValueError(
                        f"Garmin body-battery response for {current_date} must be an object or list of objects"
                    )
            elif not isinstance(response, dict):
                raise ValueError(
                    f"Garmin {dataset} response for {current_date} must be an object"
                )

            for item in _garmin_health_detail_objects(response):
                response_date = item.get("calendarDate")
                if response_date not in (None, ""):
                    parsed_date = _parse_garmin_detail_date(
                        str(response_date), f"{dataset} response date for {current_date}"
                    )
                    if parsed_date != current:
                        raise ValueError(
                            f"Garmin {dataset} response date {parsed_date.isoformat()} "
                            f"does not match requested date {current_date}"
                        )

            records.append(
                {"dataset": dataset, "calendarDate": current_date, "payload": response}
            )
        current += timedelta(days=1)
    return records


def _garmin_health_detail_objects(value: object) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            found.append(current)
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return found


def _parse_garmin_detail_date(value: str, field: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Garmin health detail {field} must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"Garmin health detail {field} must use YYYY-MM-DD")
    return parsed


def validate_intervals_wellness_date_range(start_date: str, end_date: str) -> tuple[date, date]:
    start = _parse_wellness_date(start_date, "start-date")
    end = _parse_wellness_date(end_date, "end-date")
    if end < start:
        raise ValueError("Intervals.icu wellness end date must be on or after the start date")
    return start, end


def fetch_intervals_icu_wellness(
    client: IntervalsWellnessClient,
    start_date: str,
    end_date: str,
) -> list[dict[str, object]]:
    start, end = validate_intervals_wellness_date_range(start_date, end_date)
    fetch = getattr(client, "list_wellness", None)
    if not callable(fetch):
        raise RuntimeError("Intervals.icu client does not support wellness data")
    response = fetch(start.isoformat(), end.isoformat())
    if not isinstance(response, list):
        raise ValueError("Intervals.icu wellness response must be a list")

    records: list[dict[str, object]] = []
    for index, item in enumerate(response):
        if not isinstance(item, dict):
            raise ValueError(f"Intervals.icu wellness record {index} must be an object")
        record_id = item.get("id")
        if not isinstance(record_id, str) or not record_id:
            raise ValueError(f"Intervals.icu wellness record {index} is missing its date ID")
        record_date = _parse_wellness_date(record_id, f"record {index} date ID")
        if not start <= record_date <= end:
            raise ValueError(
                f"Intervals.icu wellness record {record_id} is outside the requested date range"
            )
        records.append(dict(item))
    return records


def _parse_iso_date(value: str, field: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Training readiness {field} must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"Training readiness {field} must use YYYY-MM-DD")
    return parsed


def _parse_garmin_summary_date(value: str, field: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Garmin user summary {field} must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"Garmin user summary {field} must use YYYY-MM-DD")
    return parsed


def _parse_wellness_date(value: str, field: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Intervals.icu wellness {field} must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"Intervals.icu wellness {field} must use YYYY-MM-DD")
    return parsed
