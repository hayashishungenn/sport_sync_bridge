from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import requests

from .models import Activity, UploadResult
from .ridewithgps_api import RideWithGPSClient, _raise_for_response
from .targets import TargetAdapter
from .utils import fit_signature_ok


class RideWithGPSTarget(TargetAdapter):
    name = "ridewithgps"
    upload_poll_attempts = 40
    upload_poll_interval_seconds = 1.5
    content_types = {
        ".fit": "application/vnd.ant.fit",
        ".gpx": "application/gpx+xml",
        ".tcx": "application/tcx+xml",
    }

    def __init__(self, client: RideWithGPSClient):
        self.client = client

    def is_configured(self) -> bool:
        return self.client.is_configured()

    def authenticate(self) -> None:
        self.client.authenticate()

    def upload_file(self, file_path: Path, activity: Activity, external_id: str) -> UploadResult:
        content_type = self.content_types.get(file_path.suffix.lower())
        if content_type is None:
            return UploadResult(
                status="failed",
                message="Ride with GPS accepts FIT, GPX, and TCX activity files",
            )
        if not file_path.is_file() or file_path.stat().st_size == 0:
            return UploadResult(status="failed", message="Activity file is missing or empty")
        if file_path.suffix.lower() == ".fit" and not fit_signature_ok(file_path):
            return UploadResult(status="failed", message="Activity file is not a valid FIT file")

        try:
            with file_path.open("rb") as handle:
                response = self.client.api_request(
                    "POST",
                    "/trips.json",
                    data={"name": activity.name},
                    files={"file": (file_path.name, handle, content_type)},
                    timeout=120,
                )
            if response.status_code != 202:
                _raise_for_response(response, "upload activity")
                return UploadResult(
                    status="failed",
                    message=f"Ride with GPS returned unexpected upload status {response.status_code}",
                )
            payload = _response_object(response, "upload activity")
            task = payload.get("task")
            if not isinstance(task, dict):
                return UploadResult(status="failed", message="Ride with GPS upload response has no task")
            task_id = _positive_task_id(task.get("id"))
            if task_id is None:
                return UploadResult(status="failed", message="Ride with GPS upload task has no valid ID")
            return self._wait_for_task(task_id)
        except (OSError, requests.RequestException, RuntimeError, ValueError) as exc:
            return UploadResult(status="failed", message=str(exc))

    def _wait_for_task(self, task_id: str) -> UploadResult:
        for attempt in range(self.upload_poll_attempts):
            response = self.client.api_request("GET", f"/tasks/{task_id}.json", timeout=30)
            _raise_for_response(response, f"check upload task {task_id}")
            payload = _response_object(response, f"check upload task {task_id}")
            task = payload.get("task")
            if not isinstance(task, dict):
                return UploadResult(status="failed", message="Ride with GPS task response has no task")
            status = str(task.get("status") or "").strip().lower()
            if status == "completed":
                return _completed_task_result(task)
            if status not in {"pending", "in_progress", "processing"}:
                return UploadResult(
                    status="failed",
                    message=f"Ride with GPS returned unknown upload task status: {status or 'missing'}",
                )
            if attempt + 1 < self.upload_poll_attempts:
                time.sleep(self.upload_poll_interval_seconds)
        return UploadResult(
            status="failed",
            message=f"Ride with GPS upload task {task_id} is still processing",
        )


def _response_object(response: requests.Response, operation: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Ride with GPS {operation} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Ride with GPS {operation} response must be a JSON object")
    return payload


def _positive_task_id(value: object) -> str | None:
    normalized = str(value).strip() if value not in (None, "") else ""
    return normalized if normalized.isdecimal() and int(normalized) > 0 else None


def _completed_task_result(task: dict[str, Any]) -> UploadResult:
    errors = task.get("errors") or []
    if not isinstance(errors, list):
        return UploadResult(status="failed", message="Ride with GPS task errors must be a list")
    for error in errors:
        if isinstance(error, dict):
            code = str(error.get("code") or "").lower()
            if code == "duplicate":
                remote_id = _existing_trip_id(error)
                return UploadResult(
                    status="duplicate",
                    remote_id=remote_id,
                    message="Ride with GPS reported that this activity already exists",
                )
    if errors:
        descriptions = [
            str(error.get("message") or error.get("code") or error)
            if isinstance(error, dict)
            else str(error)
            for error in errors
        ]
        return UploadResult(status="failed", message="; ".join(descriptions))

    items = task.get("items") or []
    if not isinstance(items, list):
        return UploadResult(status="failed", message="Ride with GPS task items must be a list")
    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("item_type") or "").lower()
        if item_type in {"trip", "trips"} and item.get("item_id") not in (None, ""):
            return UploadResult(status="success", remote_id=str(item["item_id"]))
    return UploadResult(status="failed", message="Ride with GPS completed the upload without creating a trip")


def _existing_trip_id(error: dict[str, Any]) -> str | None:
    for key in ("trip_id", "existing_trip_id", "item_id"):
        value = error.get(key)
        if value not in (None, ""):
            return str(value)
    return None
