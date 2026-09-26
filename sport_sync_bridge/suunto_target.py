from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import requests

from .models import Activity, UploadResult
from .suunto_api import SuuntoClient, raise_for_response, unwrap_payload
from .suunto_route import validate_route_gpx
from .targets import TargetAdapter
from .utils import fit_signature_ok


_MAX_STATUS_POLLS = 60
_STATUS_POLL_INTERVAL_SECONDS = 1.0


class SuuntoTarget(TargetAdapter):
    name = "suunto"
    supported_extensions = {".fit"}

    def __init__(self, client: SuuntoClient):
        self.client = client
        self.upload_session = requests.Session()

    def is_configured(self) -> bool:
        return self.client.is_configured()

    def authenticate(self) -> None:
        self.client.authenticate()

    def upload_file(self, file_path: Path, activity: Activity, external_id: str) -> UploadResult:
        del external_id
        if file_path.suffix.lower() not in self.supported_extensions:
            return UploadResult(status="failed", message="Suunto workout upload accepts FIT files only")
        if not file_path.is_file() or file_path.stat().st_size == 0:
            return UploadResult(status="failed", message="Activity file is missing or empty")
        if not fit_signature_ok(file_path):
            return UploadResult(status="failed", message="Activity file is not a valid FIT file")

        metadata: dict[str, object] = {
            "description": activity.name,
            "notifyUser": False,
        }
        comment = activity.raw.get("description")
        if isinstance(comment, str) and comment.strip():
            metadata["comment"] = comment.strip()
        try:
            initialization = self.client.api_request(
                "POST",
                "/v2/upload",
                json=metadata,
                timeout=30,
            )
            raise_for_response(initialization, "upload initialization")
            upload_data = _as_mapping(unwrap_payload(initialization.json()), "upload initialization")
            upload_id = _required_string(upload_data, ("id", "uploadId"), "upload ID")
            upload_url = _required_string(upload_data, ("url", "uploadUrl"), "upload URL")
            parsed_url = urlparse(upload_url)
            if (
                parsed_url.scheme.lower() != "https"
                or not parsed_url.hostname
                or parsed_url.username
                or parsed_url.password
            ):
                raise RuntimeError("Suunto upload URL must be a credential-free HTTPS URL")
            method = upload_data.get("method", "PUT")
            if not isinstance(method, str) or method.upper() != "PUT":
                raise RuntimeError("Suunto upload initialization returned an unsupported file upload method")
            headers = _upload_headers(upload_data.get("headers"))

            try:
                blob_response = self.upload_session.request(
                    "PUT",
                    upload_url,
                    headers=headers,
                    data=file_path.read_bytes(),
                    timeout=120,
                )
            except requests.RequestException as exc:
                raise RuntimeError("Suunto FIT transfer to storage failed") from exc
            raise_for_response(blob_response, "FIT transfer")
            if blob_response.status_code not in {200, 201, 202}:
                raise RuntimeError(
                    f"Suunto storage returned unexpected HTTP {blob_response.status_code}"
                )
            return self._poll_upload(upload_id)
        except (OSError, requests.RequestException, RuntimeError, TypeError, ValueError) as exc:
            return UploadResult(status="failed", message=str(exc))

    def import_route(self, file_path: Path, activities: str = "1") -> Any:
        if file_path.suffix.casefold() != ".gpx":
            raise ValueError("Suunto route import accepts GPX files only")
        if not file_path.is_file() or file_path.stat().st_size == 0:
            raise ValueError("Suunto route file is missing or empty")
        if not _valid_activity_ids(activities):
            raise ValueError("Suunto route activities must be comma-separated positive integers")
        payload = file_path.read_bytes()
        validate_route_gpx(payload)
        response = self.client.api_request(
            "POST",
            "/v2/route/import",
            params={"activities": activities},
            headers={"Content-Type": "application/gpx+xml"},
            data=payload,
            timeout=120,
        )
        raise_for_response(response, "route import")
        try:
            return response.json()
        except (TypeError, ValueError):
            return {"status_code": response.status_code}

    def _poll_upload(self, upload_id: str) -> UploadResult:
        escaped_id = quote(upload_id, safe="")
        for attempt in range(_MAX_STATUS_POLLS):
            if attempt:
                time.sleep(_STATUS_POLL_INTERVAL_SECONDS)
            payload = unwrap_payload(
                self.client.get_json(f"/v2/upload/{escaped_id}")
            )
            status_data = _as_mapping(payload, "upload status")
            status = status_data.get("status")
            workout_key = status_data.get("workoutKey")
            message = status_data.get("message")
            if isinstance(status, str) and status.upper() == "ERROR":
                detail = message.strip() if isinstance(message, str) and message.strip() else "Suunto processing failed"
                return UploadResult(status="failed", remote_id=upload_id, message=detail)
            if (
                isinstance(status, str)
                and status.upper() in {"PROCESSED", "DONE", "SUCCESS", "COMPLETED"}
                and workout_key not in (None, "")
            ):
                return UploadResult(
                    status="success",
                    remote_id=str(workout_key),
                    message="Suunto processed the uploaded workout",
                )
        return UploadResult(
            status="failed",
            remote_id=upload_id,
            message="Suunto accepted the FIT file but processing did not finish before the status polling limit",
        )


def _as_mapping(value: Any, description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeError(f"Suunto {description} response must be a JSON object")
    return value


def _required_string(
    value: Mapping[str, Any],
    keys: tuple[str, ...],
    description: str,
) -> str:
    for key in keys:
        result = value.get(key)
        if isinstance(result, str) and result.strip():
            return result.strip()
    raise RuntimeError(f"Suunto {description} was missing from the response")


def _upload_headers(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise RuntimeError("Suunto upload headers must be a JSON object")
    headers: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise RuntimeError("Suunto upload headers must contain string names and values")
        if key.casefold() in {"authorization", "ocp-apim-subscription-key"}:
            raise RuntimeError("Suunto storage upload headers must not contain API credentials")
        headers[key] = item
    return headers


def _valid_activity_ids(value: str) -> bool:
    parts = [part.strip() for part in value.split(",")]
    return bool(parts) and all(part.isdigit() and int(part) > 0 for part in parts)
