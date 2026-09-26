from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import requests

from .formats import read_activity_file
from .models import Activity, UploadResult
from .nolio_api import NolioClient, normalize_athlete_id
from .targets import TargetAdapter
from .utils import fit_signature_ok


class NolioTarget(TargetAdapter):
    name = "nolio"
    supported_extensions = {".fit", ".tcx"}

    def __init__(self, client: NolioClient):
        self.client = client

    def is_configured(self) -> bool:
        return self.client.is_configured()

    def authenticate(self) -> None:
        self.client.authenticate()

    def upload_file(self, file_path: Path, activity: Activity, external_id: str) -> UploadResult:
        suffix = file_path.suffix.lower()
        if suffix not in self.supported_extensions:
            return UploadResult(status="failed", message="Nolio accepts FIT and TCX activity files")
        if not file_path.is_file() or file_path.stat().st_size == 0:
            return UploadResult(status="failed", message="Activity file is missing or empty")

        try:
            if suffix == ".fit" and not fit_signature_ok(file_path):
                return UploadResult(status="failed", message="Activity file is not a valid FIT file")
            if suffix == ".tcx":
                read_activity_file(file_path)

            payload_bytes = file_path.read_bytes()
            request_payload: dict[str, Any] = {
                "id_partner": _partner_id(external_id),
                "format": suffix[1:],
                "data": base64.b64encode(payload_bytes).decode("ascii"),
                "title": activity.name,
            }
            description = activity.raw.get("description")
            if isinstance(description, str) and description.strip():
                request_payload["comment"] = description.strip()
            athlete_id = normalize_athlete_id(getattr(self.client.config, "nolio_athlete_id", None))
            if athlete_id is not None:
                request_payload["athlete_id"] = athlete_id

            response = self.client.api_request(
                "POST",
                "/upload/file/",
                json=request_payload,
                timeout=120,
            )
        except (OSError, requests.RequestException, RuntimeError, ValueError) as exc:
            return UploadResult(status="failed", message=str(exc))

        if response.status_code == 202:
            return UploadResult(
                status="success",
                remote_id=_remote_id(response),
                message="Nolio accepted the file for processing",
            )
        if response.status_code == 400 and _is_duplicate(response):
            return UploadResult(status="duplicate", message="Nolio reports that this workout was already imported")

        try:
            response.raise_for_status()
        except requests.RequestException:
            return UploadResult(status="failed", message=f"Nolio upload failed with HTTP {response.status_code}")
        return UploadResult(
            status="failed",
            message=f"Nolio returned unexpected upload status {response.status_code}",
        )


def _partner_id(external_id: str) -> str:
    digest = hashlib.sha256(external_id.encode("utf-8")).hexdigest()
    return f"sport_sync_bridge:{digest}"


def _remote_id(response: requests.Response) -> str | None:
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    for key in ("nolio_id", "training_id", "id"):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _is_duplicate(response: requests.Response) -> bool:
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return False
    text = str(payload).lower() if isinstance(payload, (Mapping, list, str)) else ""
    return "already imported" in text or "already exists" in text or "duplicate" in text
