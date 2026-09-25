from __future__ import annotations

import base64
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests

from .config import AppConfig
from .models import Activity, UploadResult
from .state import StateDB
from .targets import TargetAdapter
from .utils import parse_datetime


class WahooTarget(TargetAdapter):
    name = "wahoo"
    api_root = "https://api.wahooligan.com"
    authorization_url = f"{api_root}/oauth/authorize"
    token_url = f"{api_root}/oauth/token"
    required_scope = "workouts_write"
    default_scopes = "user_read workouts_read workouts_write"
    upload_poll_attempts = 6
    upload_poll_interval_seconds = 1

    def __init__(self, config: AppConfig, state_db: StateDB):
        self.config = config
        self.state_db = state_db
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})

    def is_configured(self) -> bool:
        return bool(
            self.config.wahoo_access_token
            or (self.config.wahoo_client_id and self.config.wahoo_client_secret)
        )

    def authenticate(self) -> None:
        self.authenticate_for_scope(self.required_scope)

    def authenticate_for_scope(self, required_scope: str) -> None:
        self._ensure_access_token()
        self._require_scope(required_scope)

    def build_authorize_url(self) -> str:
        self._require_client_credentials()
        query = urllib.parse.urlencode(
            {
                "client_id": self.config.wahoo_client_id,
                "redirect_uri": self.config.wahoo_redirect_uri,
                "scope": self.config.wahoo_scope or self.default_scopes,
                "response_type": "code",
            }
        )
        return f"{self.authorization_url}?{query}"

    def exchange_code(self, code: str) -> dict:
        self._require_client_credentials()
        if not code.strip():
            raise ValueError("Wahoo OAuth code cannot be empty")
        payload = self._request_token(
            {
                "client_id": self.config.wahoo_client_id,
                "client_secret": self.config.wahoo_client_secret,
                "code": code.strip(),
                "redirect_uri": self.config.wahoo_redirect_uri,
                "grant_type": "authorization_code",
            }
        )
        self._persist_token_payload(
            payload,
            requested_scope=self.config.wahoo_scope or self.default_scopes,
        )
        return payload

    def upload_file(self, file_path: Path, activity: Activity, external_id: str) -> UploadResult:
        if file_path.suffix.lower() != ".fit":
            return UploadResult(status="failed", message="Wahoo accepts FIT uploads only")

        try:
            self._require_scope(self.required_scope)
            file_data = base64.b64encode(file_path.read_bytes()).decode("ascii")
            form = {
                "workout_file_upload[file]": f"data:application/vnd.fit;base64,{file_data}",
                "workout_file_upload[filename]": file_path.name,
                "workout_file_upload[workout_name]": activity.name,
            }
            response = self._api_request(
                "post",
                "/v1/workout_file_uploads",
                data=form,
                timeout=120,
            )
            response.raise_for_status()
            payload = _unwrap_upload_payload(_response_object(response))
            initial_result = _finished_upload_result(payload)
            if initial_result is not None:
                return initial_result

            upload_token = payload.get("token")
            if not upload_token:
                return UploadResult(status="failed", message="Wahoo did not return an upload token")

            escaped_token = urllib.parse.quote(str(upload_token), safe="")
            for attempt in range(self.upload_poll_attempts):
                status_response = self._api_request(
                    "get",
                    f"/v1/workout_file_uploads/{escaped_token}",
                    timeout=30,
                )
                status_response.raise_for_status()
                status_payload = _unwrap_upload_payload(_response_object(status_response))
                finished = _finished_upload_result(status_payload)
                if finished is not None:
                    return finished
                status = str(status_payload.get("status") or "").lower()
                if status not in {"pending", "in_progress"}:
                    return UploadResult(
                        status="failed",
                        message=f"Wahoo returned unknown upload status: {status or 'missing'}",
                    )
                if attempt + 1 < self.upload_poll_attempts:
                    time.sleep(self.upload_poll_interval_seconds)

            return UploadResult(status="failed", message="Wahoo upload is still processing")
        except (OSError, requests.RequestException, ValueError, RuntimeError) as exc:
            return UploadResult(status="failed", message=str(exc))

    def _api_request(self, method: str, path: str, **kwargs) -> requests.Response:
        access_token = self._ensure_access_token()
        request = getattr(self.session, method)
        base_headers = dict(kwargs.pop("headers", {}))

        def send(token: str) -> requests.Response:
            headers = {**base_headers, "Authorization": f"Bearer {token}"}
            return request(f"{self.api_root}{path}", headers=headers, **kwargs)

        response = send(access_token)

        if response.status_code == 401 and self._can_refresh():
            access_token = self._refresh_access_token()
            response = send(access_token)
        return response

    def api_request(self, method: str, path: str, **kwargs) -> requests.Response:
        return self._api_request(method, path, **kwargs)

    def _ensure_access_token(self) -> str:
        access_token = self.state_db.get_value("wahoo_access_token") or self.config.wahoo_access_token
        expires_at = self.state_db.get_value("wahoo_expires_at") or self.config.wahoo_expires_at
        refresh_token = self.state_db.get_value("wahoo_refresh_token") or self.config.wahoo_refresh_token

        if access_token:
            expiry = _expiration_timestamp(expires_at)
            if expiry is None or expiry - time.time() > 60:
                return str(access_token)
            if not refresh_token:
                raise RuntimeError("Wahoo access token has expired and no refresh token is configured")

        if not refresh_token:
            raise RuntimeError(
                "Wahoo access token is missing. Run `python sync.py wahoo-auth-url` and "
                "`python sync.py wahoo-exchange --code ...`."
            )
        return self._refresh_access_token()

    def _can_refresh(self) -> bool:
        refresh_token = self.state_db.get_value("wahoo_refresh_token") or self.config.wahoo_refresh_token
        return bool(refresh_token and self.config.wahoo_client_id and self.config.wahoo_client_secret)

    def _refresh_access_token(self) -> str:
        refresh_token = self.state_db.get_value("wahoo_refresh_token") or self.config.wahoo_refresh_token
        if not refresh_token:
            raise RuntimeError("Wahoo refresh token is not configured")
        self._require_client_credentials()
        payload = self._request_token(
            {
                "client_id": self.config.wahoo_client_id,
                "client_secret": self.config.wahoo_client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        )
        self._persist_token_payload(payload)
        access_token = payload.get("access_token")
        if not access_token:
            raise RuntimeError("Wahoo did not return an access token")
        return str(access_token)

    def _request_token(self, data: dict[str, str | None]) -> dict:
        response = self.session.post(self.token_url, data=data, timeout=30)
        response.raise_for_status()
        return _response_object(response)

    def _persist_token_payload(self, payload: dict, *, requested_scope: str | None = None) -> None:
        access_token = payload.get("access_token")
        if not access_token:
            raise RuntimeError("Wahoo did not return an access token")

        scope_text = None
        scope = payload.get("scope")
        if scope is None:
            scope = requested_scope
        if scope is not None:
            if isinstance(scope, list):
                scope = " ".join(str(value) for value in scope)
            scope_text = str(scope)

        self.state_db.set_value("wahoo_access_token", str(access_token))

        refresh_token = payload.get("refresh_token")
        if refresh_token:
            self.state_db.set_value("wahoo_refresh_token", str(refresh_token))

        expires_at = payload.get("expires_at")
        if expires_at is None and payload.get("expires_in") is not None:
            try:
                expires_at = str(int(time.time() + float(payload["expires_in"])))
            except (TypeError, ValueError):
                expires_at = None
        if expires_at is not None:
            self.state_db.set_value("wahoo_expires_at", str(expires_at))

        if scope_text is not None:
            self.state_db.set_value("wahoo_scope", scope_text)

    def _require_client_credentials(self) -> None:
        if not self.config.wahoo_client_id or not self.config.wahoo_client_secret:
            raise RuntimeError("Wahoo client ID and client secret are not configured")

    def _require_scope(self, required_scope: str) -> None:
        scope = (
            self.state_db.get_value("wahoo_scope")
            or self.config.wahoo_scope
            or self.default_scopes
        )
        scopes = set(str(scope).replace(",", " ").split())
        if required_scope not in scopes:
            raise RuntimeError(
                f"Wahoo token is missing {required_scope}. Set WAHOO_SCOPE to include it, "
                "then run `python sync.py wahoo-auth-url` and `python sync.py wahoo-exchange --code ...`."
            )


def _response_object(response: requests.Response) -> dict:
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("Wahoo returned a non-JSON response") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Wahoo returned an invalid response object")
    return payload


def _unwrap_upload_payload(payload: dict) -> dict:
    wrapped = payload.get("workout_file_upload")
    return wrapped if isinstance(wrapped, dict) else payload


def _finished_upload_result(payload: dict) -> UploadResult | None:
    status = str(payload.get("status") or "").lower()
    workout_id = payload.get("workout_id")
    if status == "complete":
        if workout_id is None:
            return UploadResult(status="failed", message="Wahoo completed the upload without a workout ID")
        return UploadResult(status="success", remote_id=str(workout_id))
    if status == "duplicate":
        return UploadResult(
            status="duplicate",
            remote_id=str(workout_id) if workout_id is not None else None,
            message="Wahoo reports this FIT activity as a duplicate",
        )
    if status == "error":
        return UploadResult(status="failed", message=str(payload.get("error") or "Wahoo could not process the FIT file"))
    return None


def _expiration_timestamp(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        parsed: datetime | None = parse_datetime(value)
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
