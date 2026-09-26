from __future__ import annotations

import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from .config import AppConfig
from .formats import convert_activity_file
from .models import Activity, UploadResult
from .sources import SourceAdapter
from .targets import TargetAdapter
from .utils import fit_signature_ok, parse_datetime, safe_filename


class CyclingAnalyticsClient:
    api_root = "https://www.cyclinganalytics.com/api"

    def __init__(self, config: AppConfig, session: requests.Session | None = None):
        self.config = config
        self.session = session or requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self.user_id: str | None = None
        self._authenticated = False

    def is_configured(self) -> bool:
        token = getattr(self.config, "cycling_analytics_access_token", None)
        return isinstance(token, str) and bool(token.strip())

    def authenticate(self) -> None:
        if self._authenticated:
            return
        payload = self.get_json("/me", timeout=30)
        user_id = payload.get("id")
        if user_id in (None, ""):
            raise RuntimeError("Cycling Analytics profile response is missing its user ID")
        self.user_id = str(user_id)
        self._authenticated = True

    def request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        token = getattr(self.config, "cycling_analytics_access_token", None)
        if not isinstance(token, str) or not token.strip():
            raise RuntimeError(
                "Cycling Analytics API token is not configured; set CYCLING_ANALYTICS_ACCESS_TOKEN"
            )
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {token.strip()}"
        headers.setdefault("Accept", "application/json")
        return self.session.request(
            method,
            f"{self.api_root}{path}",
            headers=headers,
            **kwargs,
        )

    def get_json(
        self,
        path: str,
        *,
        params: dict[str, object] | None = None,
        timeout: int = 30,
    ) -> dict[str, Any]:
        response = self.request("GET", path, params=params, timeout=timeout)
        return self.parse_json_response(response, path)

    def parse_json_response(self, response: requests.Response, operation: str) -> dict[str, Any]:
        self._check_response(response, operation)
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Cycling Analytics {operation} returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"Cycling Analytics {operation} response must be a JSON object")
        return payload

    def get_bytes(self, path: str, *, timeout: int = 120) -> bytes:
        response = self.request(
            "GET",
            path,
            headers={"Accept": "application/octet-stream"},
            timeout=timeout,
        )
        self._check_response(response, path)
        payload = response.content
        if not isinstance(payload, bytes) or not payload:
            raise RuntimeError(f"Cycling Analytics {path} returned an empty file")
        return payload

    def _check_response(self, response: requests.Response, operation: str) -> None:
        status = int(response.status_code)
        if 200 <= status < 300:
            return
        detail = ""
        try:
            payload = response.json()
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error") or payload.get("detail")
            if isinstance(error, str):
                detail = error
            elif error is not None:
                detail = str(error)
        if not detail:
            detail = str(getattr(response, "text", "") or "").strip()
        suffix = f": {detail[:300]}" if detail else ""
        raise RuntimeError(f"Cycling Analytics {operation} failed with HTTP {status}{suffix}")


class CyclingAnalyticsSource(SourceAdapter):
    name = "cycling_analytics"

    def __init__(self, config: AppConfig, client: CyclingAnalyticsClient):
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
        lower_bound = parse_datetime(since)
        upper_bound = parse_datetime(until)
        if lower_bound is not None and upper_bound is not None and upper_bound < lower_bound:
            raise ValueError("Cycling Analytics end time must be on or after the start time")

        self.authenticate()
        payload = self.client.get_json("/me/rides", timeout=60)
        rides = payload.get("rides")
        if not isinstance(rides, list):
            raise RuntimeError("Cycling Analytics ride response must include a rides list")

        activities: list[Activity] = []
        for index, ride in enumerate(rides):
            if not isinstance(ride, dict):
                raise RuntimeError(f"Cycling Analytics ride {index} must be a JSON object")
            activity = _activity_from_ride(ride, index)
            if lower_bound is not None and activity.start_time and activity.start_time < lower_bound:
                continue
            if upper_bound is not None and activity.start_time and activity.start_time > upper_bound:
                continue
            activities.append(activity)

        activities.sort(
            key=lambda item: item.start_time or datetime.min.replace(tzinfo=timezone.utc)
        )
        return activities[:limit] if limit is not None else activities

    def download_fit(self, activity: Activity, output_dir: Path) -> Path:
        if activity.source != self.name:
            raise ValueError(f"Cannot download a non-Cycling Analytics activity: {activity.source}")
        activity_dir = output_dir / self.name
        activity_dir.mkdir(parents=True, exist_ok=True)
        output_path = activity_dir / f"{safe_filename(activity.source_id)}.fit"
        if output_path.is_file() and output_path.stat().st_size >= 100 and fit_signature_ok(output_path):
            return output_path
        output_path.unlink(missing_ok=True)

        escaped_id = quote(activity.source_id, safe="")
        try:
            payload = self.client.get_bytes(f"/ride/{escaped_id}/raw")
            file_format = _source_format(payload, activity.raw.get("format"))
            if file_format == "fit":
                output_path.write_bytes(payload)
            else:
                with tempfile.TemporaryDirectory(prefix="cycling-analytics-", dir=activity_dir) as temp_dir:
                    source_path = Path(temp_dir) / f"activity.{file_format}"
                    source_path.write_bytes(payload)
                    convert_activity_file(
                        source_path,
                        output_path,
                        "fit",
                        activity_name=activity.name,
                        sport_type=activity.sport_type,
                    )
            if output_path.stat().st_size < 100 or not fit_signature_ok(output_path):
                raise RuntimeError("raw activity did not produce a valid FIT file")
        except Exception as exc:
            output_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"Could not download Cycling Analytics activity {activity.source_id} as FIT: {exc}"
            ) from exc
        return output_path


class CyclingAnalyticsTarget(TargetAdapter):
    name = "cycling_analytics"
    upload_poll_attempts = 30
    upload_poll_interval_seconds = 2.0
    upload_extensions = {".fit", ".gpx", ".tcx"}

    def __init__(self, client: CyclingAnalyticsClient):
        self.client = client

    def is_configured(self) -> bool:
        return self.client.is_configured()

    def authenticate(self) -> None:
        self.client.authenticate()

    def upload_file(self, file_path: Path, activity: Activity, external_id: str) -> UploadResult:
        del external_id
        extension = file_path.suffix.lower()
        if extension not in self.upload_extensions:
            return UploadResult(
                status="failed",
                message="Cycling Analytics accepts FIT, GPX, and TCX activity files in this project",
            )
        if not file_path.is_file() or file_path.stat().st_size == 0:
            return UploadResult(status="failed", message="Activity file is missing or empty")
        if extension == ".fit" and not fit_signature_ok(file_path):
            return UploadResult(status="failed", message="Activity file is not a valid FIT file")

        file_format = extension[1:]
        try:
            form_data = {
                "filename": file_path.name,
                "format": file_format,
                "title": activity.name,
            }
            with file_path.open("rb") as handle:
                response = self.client.request(
                    "POST",
                    "/me/upload",
                    data=form_data,
                    files={"data": (file_path.name, handle, "application/octet-stream")},
                    timeout=120,
                )
            payload = self.client.parse_json_response(response, "upload activity")
            immediate_result = _upload_status_result(payload)
            if immediate_result is not None:
                return immediate_result

            upload_id = _valid_identifier(payload.get("upload_id"))
            if upload_id is None:
                return UploadResult(
                    status="failed",
                    message="Cycling Analytics upload response has no valid upload_id",
                )
            return self._poll_upload(upload_id)
        except (OSError, requests.RequestException, RuntimeError, ValueError) as exc:
            message = str(exc)
            if "duplicate" in message.lower():
                return UploadResult(status="duplicate", message=message)
            return UploadResult(status="failed", message=message)

    def _poll_upload(self, upload_id: str) -> UploadResult:
        escaped_id = quote(upload_id, safe="")
        for attempt in range(self.upload_poll_attempts):
            payload = self.client.get_json(f"/me/upload/{escaped_id}", timeout=30)
            result = _upload_status_result(payload)
            if result is not None:
                return result
            status = str(payload.get("status") or "").strip().lower()
            if status not in {"processing", "pending", "queued"}:
                return UploadResult(
                    status="failed",
                    message=f"Cycling Analytics returned unknown upload status: {status or 'missing'}",
                )
            if attempt + 1 < self.upload_poll_attempts:
                time.sleep(self.upload_poll_interval_seconds)
        return UploadResult(
            status="failed",
            message=f"Cycling Analytics upload {upload_id} is still processing",
        )

    def delete_ride(self, ride_id: str) -> None:
        normalized_id = ride_id.strip()
        if not normalized_id:
            raise ValueError("Cycling Analytics ride ID must not be empty")
        escaped_id = quote(normalized_id, safe="")
        response = self.client.request("DELETE", f"/ride/{escaped_id}", timeout=30)
        self.client._check_response(response, f"delete ride {normalized_id}")


def _activity_from_ride(value: dict[str, Any], index: int) -> Activity:
    raw_id = value.get("id")
    if raw_id in (None, "") or isinstance(raw_id, bool):
        raise RuntimeError(f"Cycling Analytics ride {index} is missing its ID")
    source_id = str(raw_id).strip()
    if not source_id:
        raise RuntimeError(f"Cycling Analytics ride {index} has an empty ID")

    start_time = parse_datetime(value.get("utc_datetime") or value.get("local_datetime"))
    if start_time is None:
        raise RuntimeError(f"Cycling Analytics ride {source_id} is missing a valid start time")

    remote_type = str(value.get("type") or "other").strip().lower()
    sport_type = {
        "cycling": "cycling",
        "running": "running",
        "swimming": "swimming",
        "walking": "walking",
        "hiking": "hiking",
        "rowing": "rowing",
        "kayaking": "kayaking",
        "gym": "generic",
        "sup": "generic",
        "skiing": "skiing",
        "snowboarding": "snowboarding",
        "other": "generic",
    }.get(remote_type, "generic")
    if remote_type == "cycling" and (
        str(value.get("subtype") or "").strip().lower() == "virtual"
        or value.get("trainer") is True
    ):
        sport_type = "virtual_ride"

    name = value.get("title")
    return Activity(
        source=CyclingAnalyticsSource.name,
        source_id=source_id,
        name=str(name).strip() if name else f"{sport_type} {source_id}",
        sport_type=sport_type,
        start_time=start_time,
        raw=value,
    )


def _source_format(payload: bytes, format_hint: object) -> str:
    if len(payload) >= 12 and payload[8:12] == b".FIT":
        return "fit"
    hint = str(format_hint or "").strip().lower().lstrip(".")
    if hint in {"fit", "gpx", "tcx"}:
        return hint
    prefix = payload[:4096].decode("utf-8-sig", errors="ignore").lower()
    if re.search(r"<(?:[a-z0-9_.-]+:)?gpx(?:\s|>)", prefix):
        return "gpx"
    if "trainingcenterdatabase" in prefix:
        return "tcx"
    raise ValueError("raw activity format is not FIT, GPX, or TCX")


def _valid_identifier(value: object) -> str | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    normalized = str(value).strip()
    if not normalized or "/" in normalized or "\\" in normalized:
        return None
    return normalized


def _upload_status_result(payload: dict[str, Any]) -> UploadResult | None:
    status = str(payload.get("status") or "").strip().lower()
    error = payload.get("error")
    error_text = str(error).strip() if error is not None else ""
    error_code = str(payload.get("error_code") or "").strip().lower()
    if status in {"error", "failed"} or error_text:
        message = error_text or f"Cycling Analytics upload failed ({error_code or status})"
        if error_code == "duplicate_ride" or "duplicate" in message.lower():
            return UploadResult(status="duplicate", message=message)
        return UploadResult(status="failed", message=message)
    if status in {"done", "complete", "completed", "success"}:
        ride_id = _valid_identifier(payload.get("ride_id"))
        return UploadResult(status="success", remote_id=ride_id, message=status)
    return None
