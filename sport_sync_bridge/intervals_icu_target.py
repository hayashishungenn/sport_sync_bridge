from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import requests

from .config import AppConfig
from .models import Activity, UploadResult
from .targets import TargetAdapter


class IntervalsIcuTarget(TargetAdapter):
    name = "intervals_icu"
    api_root = "https://intervals.icu/api/v1"
    supported_formats = frozenset({"fit", "gpx", "tcx"})

    def __init__(self, config: AppConfig):
        self.config = config
        self.session = requests.Session()
        self._authenticated = False

    def is_configured(self) -> bool:
        return bool(self.config.intervals_icu_athlete_id and self.config.intervals_icu_api_key)

    def _auth(self) -> tuple[str, str]:
        api_key = self.config.intervals_icu_api_key
        if not api_key:
            raise RuntimeError("Intervals.icu API key is not configured")
        return "API_KEY", api_key

    def _athlete_url(self, path: str) -> str:
        athlete_id = self.config.intervals_icu_athlete_id
        if not athlete_id:
            raise RuntimeError("Intervals.icu athlete ID is not configured")
        return f"{self.api_root}/athlete/{quote(athlete_id, safe='')}{path}"

    def authenticate(self) -> None:
        if self._authenticated:
            return
        response = self.session.get(
            self._athlete_url("/profile"),
            auth=self._auth(),
            timeout=30,
        )
        response.raise_for_status()
        try:
            profile = response.json()
        except ValueError as exc:
            raise RuntimeError("Intervals.icu profile response is not valid JSON") from exc
        if not isinstance(profile, dict):
            raise RuntimeError("Intervals.icu profile response must be a JSON object")
        self._authenticated = True

    def upload_file(self, file_path: Path, activity: Activity, external_id: str) -> UploadResult:
        activity_format = file_path.suffix.lower().removeprefix(".")
        if activity_format not in self.supported_formats:
            supported = ", ".join(sorted(self.supported_formats)).upper()
            return UploadResult(
                status="failed",
                message=f"Intervals.icu accepts {supported} activity files",
            )

        try:
            self.authenticate()
            with file_path.open("rb") as handle:
                response = self.session.post(
                    self._athlete_url("/activities"),
                    auth=self._auth(),
                    params={"name": activity.name, "external_id": external_id},
                    files={"file": (file_path.name, handle, "application/octet-stream")},
                    timeout=120,
                )
        except (OSError, requests.RequestException, RuntimeError, ValueError) as exc:
            return UploadResult(status="failed", message=str(exc))

        if response.status_code in {200, 409}:
            return UploadResult(status="duplicate", message="Intervals.icu already has this activity")
        if response.status_code != 201:
            return UploadResult(
                status="failed",
                message=_upload_error_message(response),
            )

        try:
            payload = response.json()
        except ValueError as exc:
            return UploadResult(
                status="failed",
                message=f"Intervals.icu upload response is not valid JSON: {exc}",
            )
        if not isinstance(payload, (dict, list)):
            return UploadResult(
                status="failed",
                message="Intervals.icu upload response must be a JSON object or list",
            )

        remote_id = _uploaded_activity_id(payload)
        return UploadResult(status="success", remote_id=remote_id)


def _uploaded_activity_id(payload: object) -> str | None:
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict) and item.get("id") not in (None, ""):
                return str(item["id"])
        return None
    if not isinstance(payload, dict):
        return None
    activities = payload.get("activities")
    if isinstance(activities, list):
        for item in activities:
            if isinstance(item, dict) and item.get("id") not in (None, ""):
                return str(item["id"])
    raw_id = payload.get("id")
    return str(raw_id) if raw_id not in (None, "") else None


def _upload_error_message(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        message = payload.get("error") or payload.get("message")
        if isinstance(message, str) and message.strip():
            return f"Intervals.icu upload failed: {message.strip()}"
    return f"Intervals.icu upload failed with HTTP {response.status_code}"
