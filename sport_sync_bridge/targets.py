from __future__ import annotations

import time
import urllib.parse
from abc import ABC, abstractmethod
from pathlib import Path

import requests

from .config import AppConfig
from .models import Activity, UploadResult
from .state import StateDB
from .utils import parse_datetime, restore_directory_from_base64_zip, strava_sport_type


class TargetAdapter(ABC):
    name: str

    @abstractmethod
    def is_configured(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def authenticate(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def upload_file(self, file_path: Path, activity: Activity, external_id: str) -> UploadResult:
        raise NotImplementedError


class GarminTarget(TargetAdapter):
    name = "garmin"

    def __init__(self, config: AppConfig):
        self.config = config
        self.client = None
        self.session_dir = config.data_dir / ".garmin_session"

    def is_configured(self) -> bool:
        return bool(self.config.garmin_email and self.config.garmin_password)

    def authenticate(self) -> None:
        if self.client is not None:
            return

        if not self.is_configured():
            raise RuntimeError("Garmin credentials are not configured")

        try:
            import garminconnect
        except ImportError as exc:
            raise RuntimeError("garminconnect is required for Garmin uploads") from exc

        self.session_dir.mkdir(parents=True, exist_ok=True)
        if self.config.garmin_session_b64 and not any(self.session_dir.glob("*.json")):
            restore_directory_from_base64_zip(self.config.garmin_session_b64, self.session_dir)

        client = garminconnect.Garmin(
            email=self.config.garmin_email,
            password=self.config.garmin_password,
            is_cn=False,
            prompt_mfa=self._prompt_mfa,
        )

        try:
            client.login(str(self.session_dir))
        except Exception:
            client.login()
            client.garth.dump(str(self.session_dir))
        self.client = client

    def upload_file(self, file_path: Path, activity: Activity, external_id: str) -> UploadResult:
        self.authenticate()
        try:
            result = self.client.upload_activity(str(file_path))
        except Exception as exc:
            message = str(exc)
            lower = message.lower()
            if "duplicate" in lower or "already" in lower or "409" in lower:
                return UploadResult(status="duplicate", message=message)
            return UploadResult(status="failed", message=message)

        if isinstance(result, dict):
            failures = result.get("failures") or []
            if failures:
                failure_text = str(failures)
                if "duplicate" in failure_text.lower() or "already" in failure_text.lower():
                    return UploadResult(status="duplicate", message=failure_text)
                return UploadResult(status="failed", message=failure_text)

            remote_id = (
                result.get("activity", {}).get("activityId")
                or result.get("uploadId")
                or result.get("detailedImportResult", {}).get("uploadId")
            )
            return UploadResult(status="success", remote_id=str(remote_id) if remote_id else None)

        return UploadResult(status="success")

    @staticmethod
    def _prompt_mfa() -> str:
        return input("Enter Garmin MFA code: ").strip()


class StravaTarget(TargetAdapter):
    name = "strava"

    def __init__(self, config: AppConfig, state_db: StateDB):
        self.config = config
        self.state_db = state_db
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
                )
            }
        )

    def is_configured(self) -> bool:
        return bool(self.config.strava_client_id and self.config.strava_client_secret)

    def authenticate(self) -> None:
        self._ensure_access_token()

    def build_authorize_url(self, force_prompt: bool = False) -> str:
        if not self.is_configured():
            raise RuntimeError("Strava client_id/client_secret are not configured")
        query = urllib.parse.urlencode(
            {
                "client_id": self.config.strava_client_id,
                "redirect_uri": self.config.strava_redirect_uri,
                "response_type": "code",
                "approval_prompt": "force" if force_prompt else "auto",
                "scope": "activity:read_all,activity:write",
            }
        )
        return f"https://www.strava.com/oauth/authorize?{query}"

    def exchange_code(self, code: str) -> dict:
        if not self.is_configured():
            raise RuntimeError("Strava client_id/client_secret are not configured")
        response = self.session.post(
            "https://www.strava.com/oauth/token",
            data={
                "client_id": self.config.strava_client_id,
                "client_secret": self.config.strava_client_secret,
                "code": code,
                "grant_type": "authorization_code",
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        self._persist_token_payload(payload)
        return payload

    def upload_file(self, file_path: Path, activity: Activity, external_id: str) -> UploadResult:
        token = self._ensure_access_token()
        data_type = _strava_data_type(file_path)
        data = {
            "data_type": data_type,
            "external_id": external_id,
            "name": activity.name,
        }
        sport_type = strava_sport_type(activity.sport_type)
        if sport_type:
            data["sport_type"] = sport_type

        with file_path.open("rb") as handle:
            response = self.session.post(
                "https://www.strava.com/api/v3/uploads",
                headers={"Authorization": f"Bearer {token}"},
                data=data,
                files={"file": (file_path.name, handle, "application/octet-stream")},
                timeout=120,
            )

        if response.status_code == 401:
            token = self._refresh_access_token()
            with file_path.open("rb") as handle:
                response = self.session.post(
                    "https://www.strava.com/api/v3/uploads",
                    headers={"Authorization": f"Bearer {token}"},
                    data=data,
                    files={"file": (file_path.name, handle, "application/octet-stream")},
                    timeout=120,
                )

        if response.status_code != 201:
            message = _extract_strava_error(response)
            if "duplicate" in message.lower():
                return UploadResult(status="duplicate", message=message)
            return UploadResult(status="failed", message=message)

        payload = response.json()
        upload_id = payload.get("id")
        if not upload_id:
            return UploadResult(status="success", message=payload.get("status"))

        for _ in range(15):
            time.sleep(1)
            status = self.session.get(
                f"https://www.strava.com/api/v3/uploads/{upload_id}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            if status.status_code == 401:
                token = self._refresh_access_token()
                continue
            status.raise_for_status()
            body = status.json()
            error = body.get("error")
            activity_id = body.get("activity_id")
            if error:
                if "duplicate" in error.lower():
                    return UploadResult(status="duplicate", message=error)
                return UploadResult(status="failed", message=error)
            if activity_id:
                return UploadResult(status="success", remote_id=str(activity_id), message=body.get("status"))
            if body.get("status") == "Your activity is ready.":
                return UploadResult(status="success", remote_id=None, message=body.get("status"))

        return UploadResult(status="success", remote_id=None, message="Upload accepted and still processing")

    def _ensure_access_token(self) -> str:
        access_token = self.state_db.get_value("strava_access_token") or self.config.strava_access_token
        expires_at = self.state_db.get_value("strava_expires_at") or self.config.strava_expires_at
        refresh_token = self.state_db.get_value("strava_refresh_token") or self.config.strava_refresh_token
        scope = self.state_db.get_value("strava_scope") or self.config.strava_scope

        if refresh_token and not self.state_db.get_value("strava_refresh_token"):
            self.state_db.set_value("strava_refresh_token", refresh_token)
        if access_token and not self.state_db.get_value("strava_access_token"):
            self.state_db.set_value("strava_access_token", access_token)
        if expires_at and not self.state_db.get_value("strava_expires_at"):
            self.state_db.set_value("strava_expires_at", expires_at)
        if scope and not self.state_db.get_value("strava_scope"):
            self.state_db.set_value("strava_scope", scope)

        expiry = parse_datetime(expires_at)
        if access_token and expiry and expiry.timestamp() - time.time() > 3600:
            return access_token
        if access_token and not refresh_token:
            return access_token

        return self._refresh_access_token()

    def _refresh_access_token(self) -> str:
        if not self.is_configured():
            raise RuntimeError("Strava client_id/client_secret are not configured")

        refresh_token = self.state_db.get_value("strava_refresh_token") or self.config.strava_refresh_token
        if not refresh_token:
            raise RuntimeError(
                "Strava refresh token is missing. Run `python sync.py strava-auth-url` then `strava-exchange`."
            )

        response = self.session.post(
            "https://www.strava.com/oauth/token",
            data={
                "client_id": self.config.strava_client_id,
                "client_secret": self.config.strava_client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        self._persist_token_payload(payload)
        access_token = payload.get("access_token")
        if not access_token:
            raise RuntimeError("Strava did not return access_token")
        return access_token

    def _persist_token_payload(self, payload: dict) -> None:
        if "access_token" in payload:
            self.state_db.set_value("strava_access_token", str(payload["access_token"]))
        if "refresh_token" in payload:
            self.state_db.set_value("strava_refresh_token", str(payload["refresh_token"]))
        if "expires_at" in payload:
            self.state_db.set_value("strava_expires_at", str(payload["expires_at"]))
        if "scope" in payload:
            scope_value = payload["scope"]
            if isinstance(scope_value, list):
                scope_value = ",".join(scope_value)
            self.state_db.set_value("strava_scope", str(scope_value))


def _extract_strava_error(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"HTTP {response.status_code}: {response.text[:200]}"

    if isinstance(payload, dict):
        if payload.get("error"):
            return str(payload["error"])
        if payload.get("message"):
            return str(payload["message"])
        errors = payload.get("errors")
        if errors:
            return str(errors)
    return f"HTTP {response.status_code}: {payload}"


def _strava_data_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".fit":
        return "fit"
    if suffix == ".gpx":
        return "gpx"
    if suffix == ".tcx":
        return "tcx"
    raise RuntimeError(f"Unsupported file type for Strava upload: {suffix}")
