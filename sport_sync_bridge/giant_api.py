from __future__ import annotations

import base64
import re
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests

from .config import AppConfig
from .formats import _atomic_write_bytes
from .state import StateDB
from .utils import fit_signature_ok


class GiantClient:
    api_root = "https://rideapp.giant.com.cn/apis"
    api_host = "rideapp.giant.com.cn"
    web_login_url = "https://ridelife.giant.com.cn/index.php/api/login"
    upload_url = "https://ridelife.giant.com.cn/index.php/api/upload_fit"

    user_id_key = "giant_user_id"
    access_token_key = "giant_access_token"
    web_token_key = "giant_web_user_token"
    device_id_key = "giant_device_id"

    api_user_agent = "okhttp/4.9.1"
    web_user_agent = (
        "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:78.0) "
        "Gecko/20100101 Firefox/78.0"
    )

    def __init__(self, config: AppConfig, state_db: StateDB):
        self.config = config
        self.state_db = state_db
        self.session = requests.Session()

    @property
    def credentials(self) -> tuple[str, str] | None:
        username = getattr(self.config, "giant_username", None)
        password = getattr(self.config, "giant_password", None)
        if isinstance(username, str) and username.strip() and isinstance(password, str) and password:
            return username.strip(), password
        return None

    @property
    def user_id(self) -> str | None:
        value = self.state_db.get_value(self.user_id_key)
        return value.strip() if isinstance(value, str) and value.strip() else None

    @property
    def access_token(self) -> str | None:
        value = self.state_db.get_value(self.access_token_key)
        return value.strip() if isinstance(value, str) and value.strip() else None

    def is_configured(self) -> bool:
        return self.credentials is not None or bool(self.user_id and self.access_token)

    def authenticate(self, *, force: bool = False) -> str:
        if not force and self.user_id and self.access_token:
            return self.user_id
        credentials = self.credentials
        if credentials is None:
            raise RuntimeError(
                "Giant is not authenticated; set GIANT_USERNAME and GIANT_PASSWORD"
            )

        username, password = credentials
        response = self._post(
            f"{self.api_root}/v3/user/profile/login",
            data={
                "account": username,
                "password": password,
                "app_version": _config_string(self.config, "giant_app_version", "4.1.3"),
                "device_os": "android",
                "device_os_version": _config_string(
                    self.config, "giant_device_os_version", "25"
                ),
                "model": _config_string(
                    self.config, "giant_device_model", "sport-sync-bridge"
                ),
                "device_id": self._device_id(),
            },
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip, deflate, br, zstd",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": self.api_user_agent,
            },
            timeout=30,
            operation="login",
        )
        payload = _response_object(response, "Giant login")
        if response.status_code != 200:
            raise RuntimeError(f"Giant login failed with HTTP {response.status_code}")
        _ensure_success(payload, "Giant login")
        result = payload.get("retval")
        if not isinstance(result, Mapping):
            raise RuntimeError("Giant login response has no account data")

        user_id = _required_string(
            result.get("_id") or result.get("userId") or result.get("id"),
            "Giant user ID",
        )
        access_token = _required_string(
            result.get("access_token") or result.get("accessToken"),
            "Giant access token",
        )
        self.state_db.set_value(self.user_id_key, user_id)
        self.state_db.set_value(self.access_token_key, access_token)
        return user_id

    def logout(self) -> None:
        for key in (self.user_id_key, self.access_token_key, self.web_token_key):
            self.state_db.set_value(key, "")

    def get_json(
        self,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or "?" in path
            or "#" in path
            or path.startswith("//")
        ):
            raise ValueError("Invalid Giant API path")

        for attempt in range(2):
            self.authenticate(force=attempt > 0)
            headers = self._auth_headers()
            try:
                response = self.session.get(
                    f"{self.api_root}{path}",
                    params=params,
                    headers=headers,
                    timeout=30,
                )
            except requests.RequestException as exc:
                raise RuntimeError("Giant API request failed due to a network error") from exc

            payload = _optional_response_object(response)
            if _is_auth_failure(response.status_code, payload):
                if attempt == 0 and self.credentials is not None:
                    continue
                raise RuntimeError(
                    "Giant rejected the saved session; set valid credentials and authenticate again"
                )
            if response.status_code < 200 or response.status_code >= 300:
                raise RuntimeError(f"Giant API request failed with HTTP {response.status_code}")
            if payload is None:
                raise RuntimeError("Giant API returned invalid JSON")
            _ensure_success(payload, "Giant API request")
            return payload

        raise RuntimeError("Giant API request failed after re-authentication")

    def download_fit_file(self, fit_url: str, output_path: Path) -> Path:
        if not isinstance(fit_url, str) or not fit_url.strip():
            raise ValueError("Giant activity detail has no FIT URL")
        url = urljoin(f"{self.api_root}/", fit_url.strip())
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("Giant returned an invalid FIT URL")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("Giant returned an invalid FIT URL") from exc

        headers = (
            self._auth_headers()
            if parsed.hostname.lower() == self.api_host and port in (None, 443)
            else {"Accept-Encoding": "gzip, deflate, br, zstd", "User-Agent": self.api_user_agent}
        )
        try:
            response = self.session.get(url, headers=headers, timeout=120)
        except requests.RequestException as exc:
            raise RuntimeError("Giant FIT download failed due to a network error") from exc
        if response.status_code != 200:
            raise RuntimeError(f"Giant FIT download failed with HTTP {response.status_code}")
        content = response.content
        if not isinstance(content, bytes) or len(content) < 100:
            raise RuntimeError("Giant returned an empty or truncated FIT file")
        _atomic_write_bytes(output_path, content, validate_fit=True)
        if not fit_signature_ok(output_path):
            output_path.unlink(missing_ok=True)
            raise RuntimeError("Giant returned an invalid FIT file")
        return output_path

    def authenticate_web(self, *, force: bool = False) -> str:
        saved = self.state_db.get_value(self.web_token_key)
        if not force and isinstance(saved, str) and saved.strip():
            return saved.strip()
        credentials = self.credentials
        if credentials is None:
            raise RuntimeError(
                "Giant web upload is not configured; set GIANT_USERNAME and GIANT_PASSWORD"
            )
        username, password = credentials
        response = self._post(
            self.web_login_url,
            data={"username": username, "password": password},
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": self.web_user_agent,
            },
            timeout=30,
            operation="web login",
        )
        if response.status_code != 200:
            raise RuntimeError(f"Giant web login failed with HTTP {response.status_code}")
        payload = _response_object(response, "Giant web login")
        status = payload.get("status")
        if status not in (1, "1", True):
            message = _message(payload)
            raise RuntimeError(f"Giant web login failed: {message or 'invalid response'}")
        token = _required_string(payload.get("user_token"), "Giant web user token")
        self.state_db.set_value(self.web_token_key, token)
        return token

    def upload_fit_file(self, file_path: Path) -> requests.Response:
        if not file_path.is_file() or file_path.stat().st_size < 100:
            raise ValueError("Giant upload file is missing or empty")
        if not fit_signature_ok(file_path):
            raise ValueError("Giant accepts valid FIT activity files")

        token = self.authenticate_web()
        for attempt in range(2):
            try:
                with file_path.open("rb") as fit_file:
                    response = self.session.post(
                        self.upload_url,
                        data={
                            "token": token,
                            "device": "bike_computer",
                            "brand": "garmin",
                        },
                        files={"files[]": (file_path.name, fit_file, "application/octet-stream")},
                        headers={"Accept": "application/json", "User-Agent": self.web_user_agent},
                        timeout=120,
                    )
            except requests.RequestException as exc:
                raise RuntimeError("Giant FIT upload failed due to a network error") from exc

            if response.status_code in {401, 403} and attempt == 0:
                token = self.authenticate_web(force=True)
                continue
            payload = _optional_response_object(response)
            status = payload.get("status") if payload is not None else None
            if (
                response.status_code == 200
                and status is not None
                and status not in (1, "1", True, 3, "3")
                and attempt == 0
            ):
                token = self.authenticate_web(force=True)
                continue
            return response
        raise RuntimeError("Giant FIT upload failed after web re-authentication")

    def _auth_headers(self) -> dict[str, str]:
        user_id = self.user_id
        access_token = self.access_token
        if not user_id or not access_token:
            raise RuntimeError("Giant access credentials are missing")
        encoded = base64.b64encode(f"{user_id}:{access_token}".encode("utf-8")).decode("ascii")
        return {
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Authorization": f"Basic {encoded}",
            "User-Agent": self.api_user_agent,
        }

    def _device_id(self) -> str:
        stored = self.state_db.get_value(self.device_id_key)
        if isinstance(stored, str) and re.fullmatch(r"[0-9a-f]{16}", stored):
            return stored
        value = uuid.uuid4().hex[:16]
        self.state_db.set_value(self.device_id_key, value)
        return value

    def _post(self, url: str, *, operation: str, **kwargs: Any) -> requests.Response:
        try:
            return self.session.post(url, **kwargs)
        except requests.RequestException as exc:
            raise RuntimeError(f"Giant {operation} failed due to a network error") from exc


def _config_string(config: AppConfig, key: str, default: str) -> str:
    value = getattr(config, key, None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _required_string(value: object, label: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise RuntimeError(f"{label} is missing from the response")
    return str(value).strip()


def _response_object(response: requests.Response, operation: str) -> dict[str, Any]:
    payload = _optional_response_object(response)
    if payload is None:
        raise RuntimeError(f"{operation} returned invalid JSON")
    return payload


def _optional_response_object(response: requests.Response) -> dict[str, Any] | None:
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return None
    return dict(payload) if isinstance(payload, Mapping) else None


def _ensure_success(payload: Mapping[str, Any], operation: str) -> None:
    success = payload.get("success")
    if success in (False, 0, "0", "false", "False"):
        raise RuntimeError(f"{operation} failed: {_message(payload) or 'request rejected'}")
    error_code = payload.get("errCode")
    if error_code not in (None, "", 0, "0"):
        raise RuntimeError(f"{operation} failed with error code {error_code}")


def _is_auth_failure(status_code: int, payload: Mapping[str, Any] | None) -> bool:
    if status_code == 401:
        return True
    if payload is None:
        return False
    if str(payload.get("errCode", "")).strip() == "E0001":
        return True
    message = _message(payload).lower()
    return "unauthorized" in message or "connection closed" in message


def _message(payload: Mapping[str, Any]) -> str:
    value = payload.get("message") or payload.get("msg")
    return value.strip() if isinstance(value, str) else ""
