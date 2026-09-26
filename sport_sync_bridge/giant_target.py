from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import requests

from .giant_api import GiantClient
from .models import Activity, UploadResult
from .targets import TargetAdapter


class GiantTarget(TargetAdapter):
    name = "giant"
    supported_extensions = {".fit"}

    def __init__(self, client: GiantClient):
        self.client = client

    def is_configured(self) -> bool:
        saved_web_token = self.client.state_db.get_value(self.client.web_token_key)
        return self.client.credentials is not None or bool(
            isinstance(saved_web_token, str) and saved_web_token.strip()
        )

    def authenticate(self) -> None:
        self.client.authenticate_web()

    def upload_file(self, file_path: Path, activity: Activity, external_id: str) -> UploadResult:
        if file_path.suffix.lower() not in self.supported_extensions:
            return UploadResult(status="failed", message="Giant accepts FIT activity files")
        try:
            response = self.client.upload_fit_file(file_path)
        except (OSError, requests.RequestException, RuntimeError, ValueError) as exc:
            return UploadResult(status="failed", message=str(exc))

        payload = _optional_json(response)
        if response.status_code in {409}:
            return UploadResult(status="duplicate", message="Giant reports a duplicate activity")
        if response.status_code < 200 or response.status_code >= 300:
            message = _message(payload)
            if _looks_duplicate(message):
                return UploadResult(status="duplicate", message=message)
            return UploadResult(
                status="failed",
                message=f"Giant upload failed with HTTP {response.status_code}",
            )

        if payload is not None:
            message = _message(payload)
            if _looks_duplicate(message):
                return UploadResult(status="duplicate", message=message)
            if payload.get("success") in (False, 0, "0", "false", "False"):
                return UploadResult(status="failed", message=message or "Giant rejected the upload")
            status = payload.get("status")
            if status not in (None, 1, "1", True, 200, "200", "success", "ok"):
                return UploadResult(status="failed", message=message or "Giant rejected the upload")
            return UploadResult(
                status="success",
                remote_id=_remote_id(payload),
                message=message or None,
            )
        return UploadResult(status="success")


def _optional_json(response: requests.Response) -> Mapping[str, object] | None:
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _message(payload: Mapping[str, object] | None) -> str:
    if payload is None:
        return ""
    for key in ("message", "msg", "error"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _looks_duplicate(message: str) -> bool:
    lowered = message.lower()
    return "duplicate" in lowered or "already exists" in lowered or "already imported" in lowered


def _remote_id(payload: Mapping[str, object]) -> str | None:
    candidate: object = payload
    if isinstance(payload.get("retval"), Mapping):
        candidate = payload["retval"]
    if not isinstance(candidate, Mapping):
        return None
    for key in ("id", "activity_id", "activityId", "upload_id"):
        value = candidate.get(key)
        if value not in (None, ""):
            return str(value)
    return None
