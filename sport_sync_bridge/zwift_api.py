from __future__ import annotations

import math
import re
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from .config import AppConfig
from .state import StateDB
from .utils import fit_signature_ok


class ZwiftClient:
    client_id = "Zwift_Mobile_Link"
    token_url = "https://secure.zwift.com/auth/realms/zwift/tokens/access/codes"
    logout_url = "https://secure.zwift.com/auth/realms/zwift/tokens/logout"
    api_root = "https://us-or-rly101.zwift.com"
    api_host = "us-or-rly101.zwift.com"
    login_host = "secure.zwift.com"

    access_token_key = "zwift_access_token"
    refresh_token_key = "zwift_refresh_token"
    expires_at_key = "zwift_expires_at"
    player_id_key = "zwift_player_id"

    def __init__(self, config: AppConfig, state_db: StateDB):
        self.config = config
        self.state_db = state_db
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "CNL/3.4.1 (Darwin Kernel 20.3.0) zwift/1.0.61590 curl/7.64.1",
                "Accept-Encoding": "gzip, deflate",
                "Connection": "keep-alive",
                "Accept-Language": "en-US;q=1",
            }
        )

    @property
    def player_id(self) -> str | None:
        value = self.state_db.get_value(self.player_id_key)
        return value.strip() if value and value.strip() else None

    @property
    def configured_credentials(self) -> tuple[str, str] | None:
        username = getattr(self.config, "zwift_username", None)
        password = getattr(self.config, "zwift_password", None)
        if isinstance(username, str) and username.strip() and isinstance(password, str) and password:
            return username.strip(), password
        return None

    def is_configured(self) -> bool:
        if self.configured_credentials:
            return True
        if self.state_db.get_value(self.refresh_token_key):
            return True
        access_token = self.state_db.get_value(self.access_token_key)
        if not access_token:
            return False
        expires_at_value = self.state_db.get_value(self.expires_at_key)
        if not expires_at_value:
            return True
        expires_at = _optional_float(expires_at_value)
        return expires_at is not None and expires_at > time.time() + 60

    def authenticate(self) -> str:
        access_token = self._get_access_token()
        player_id = self.player_id
        if player_id is None:
            player_id = _player_id(self._fetch_profile(access_token))
            self.state_db.set_value(self.player_id_key, player_id)
        return player_id

    def get_json(self, path: str, *, params: Mapping[str, object] | None = None) -> Any:
        if not path.startswith("/") or "?" in path or "#" in path:
            raise ValueError("Invalid Zwift API path")
        token = self._get_access_token()
        response = self._request_api("GET", path, token, params=params)
        if response.status_code == 401:
            token = self._refresh_after_unauthorized()
            response = self._request_api("GET", path, token, params=params)
        if response.status_code < 200 or response.status_code >= 300:
            raise RuntimeError(f"Zwift API request failed: HTTP {response.status_code}")
        return _json_payload(response, "Zwift API response")

    def download_fit_file(self, bucket: str, object_key: str, output_path: Path) -> Path:
        if not isinstance(bucket, str) or len(bucket) < 3 or len(bucket) > 63 or not _S3_BUCKET_RE.fullmatch(bucket):
            raise ValueError("Zwift activity detail contains an invalid FIT bucket")
        if not isinstance(object_key, str) or not object_key.strip():
            raise ValueError("Zwift activity detail contains an empty FIT object key")

        url = f"https://{bucket}.s3.amazonaws.com/{quote(object_key, safe='/')}"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=output_path.parent, prefix=f".{output_path.name}.", suffix=".tmp", delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
                try:
                    response = self.session.get(url, timeout=120)
                    status = getattr(response, "status_code", None)
                    if status != 200:
                        raise RuntimeError(f"Zwift FIT download failed: HTTP {status}")
                    content = response.content
                except requests.RequestException as exc:
                    raise RuntimeError("Zwift FIT download failed: network error") from exc
                if not isinstance(content, bytes) or len(content) < 100:
                    raise RuntimeError("Zwift returned an empty or truncated FIT file")
                temporary.write(content)
            if not fit_signature_ok(temporary_path):
                raise RuntimeError("Zwift returned an invalid FIT file")
            temporary_path.replace(output_path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return output_path

    def logout(self) -> None:
        refresh_token = self.state_db.get_value(self.refresh_token_key)
        if refresh_token:
            self._refresh_access_token(refresh_token)
            refresh_token = self.state_db.get_value(self.refresh_token_key)
            if not refresh_token:
                raise RuntimeError("Zwift logout failed: refresh response did not contain a refresh token")
            try:
                response = self.session.post(
                    self.logout_url,
                    data={"client_id": self.client_id, "refresh_token": refresh_token},
                    headers={"Host": self.login_host, "Accept": "application/json"},
                    timeout=30,
                )
            except requests.RequestException as exc:
                raise RuntimeError("Zwift logout failed: network error") from exc
            if response.status_code < 200 or response.status_code >= 300:
                raise RuntimeError(f"Zwift logout failed: HTTP {response.status_code}")
        self._clear_tokens()

    def _get_access_token(self) -> str:
        access_token = self.state_db.get_value(self.access_token_key)
        expires_at_value = self.state_db.get_value(self.expires_at_key)
        if access_token:
            if not expires_at_value:
                return access_token
            expires_at = _optional_float(expires_at_value)
            if expires_at is not None and expires_at > time.time() + 60:
                return access_token

        refresh_token = self.state_db.get_value(self.refresh_token_key)
        if refresh_token:
            return self._refresh_access_token(refresh_token)
        credentials = self.configured_credentials
        if credentials is None:
            raise RuntimeError("Zwift is not authenticated; set ZWIFT_USERNAME and ZWIFT_PASSWORD")
        return self._password_login(*credentials)

    def _password_login(self, username: str, password: str) -> str:
        payload = self._post_token(
            {
                "client_id": self.client_id,
                "grant_type": "password",
                "username": username,
                "password": password,
            }
        )
        access_token = _access_token(payload)
        player_id = _player_id(self._fetch_profile(access_token))
        self._save_tokens(payload, player_id)
        return access_token

    def _refresh_access_token(self, refresh_token: str) -> str:
        payload = self._post_token(
            {
                "client_id": self.client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        )
        access_token = _access_token(payload)
        player_id = self.player_id
        if player_id is None:
            player_id = _player_id(self._fetch_profile(access_token))
        self._save_tokens(payload, player_id, fallback_refresh_token=refresh_token)
        return access_token

    def _refresh_after_unauthorized(self) -> str:
        refresh_token = self.state_db.get_value(self.refresh_token_key)
        if refresh_token:
            return self._refresh_access_token(refresh_token)
        credentials = self.configured_credentials
        if credentials is not None:
            return self._password_login(*credentials)
        raise RuntimeError("Zwift API rejected the saved access token; authenticate again")

    def _post_token(self, form: dict[str, str]) -> dict[str, Any]:
        try:
            response = self.session.post(
                self.token_url,
                data=form,
                headers={"Host": self.login_host, "Accept": "application/json"},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise RuntimeError("Zwift authentication failed: network error") from exc
        if response.status_code != 200:
            raise RuntimeError(f"Zwift authentication failed: HTTP {response.status_code}")
        payload = _json_payload(response, "Zwift token response")
        if not isinstance(payload, Mapping):
            raise RuntimeError("Zwift token response must be a JSON object")
        return dict(payload)

    def _fetch_profile(self, access_token: str) -> dict[str, Any]:
        response = self._request_api("GET", "/api/profiles/me", access_token)
        if response.status_code < 200 or response.status_code >= 300:
            raise RuntimeError(f"Zwift profile validation failed: HTTP {response.status_code}")
        payload = _json_payload(response, "Zwift profile response")
        if not isinstance(payload, Mapping):
            raise RuntimeError("Zwift profile response must be a JSON object")
        return dict(payload)

    def _request_api(
        self,
        method: str,
        path: str,
        access_token: str,
        *,
        params: Mapping[str, object] | None = None,
    ) -> Any:
        try:
            return self.session.request(
                method,
                f"{self.api_root}{path}",
                params=params,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Host": self.api_host,
                    "Accept": "application/json",
                },
                timeout=30,
            )
        except requests.RequestException as exc:
            raise RuntimeError("Zwift API request failed: network error") from exc

    def _save_tokens(
        self,
        payload: Mapping[str, Any],
        player_id: str,
        *,
        fallback_refresh_token: str | None = None,
    ) -> None:
        access_token = _access_token(payload)
        refresh_token = payload.get("refresh_token") or fallback_refresh_token
        raw_expires_in = payload.get("expires_in")
        expires_in = None if raw_expires_in in (None, "") else _optional_float(raw_expires_in)
        if raw_expires_in not in (None, "") and (expires_in is None or expires_in <= 0):
            raise RuntimeError("Zwift token response contains an invalid expires_in value")
        expires_at = "" if expires_in is None else str(time.time() + expires_in)
        self.state_db.set_value(self.access_token_key, access_token)
        if isinstance(refresh_token, str) and refresh_token:
            self.state_db.set_value(self.refresh_token_key, refresh_token)
        self.state_db.set_value(self.expires_at_key, expires_at)
        self.state_db.set_value(self.player_id_key, player_id)

    def _clear_tokens(self) -> None:
        for key in (
            self.access_token_key,
            self.refresh_token_key,
            self.expires_at_key,
            self.player_id_key,
        ):
            self.state_db.set_value(key, "")


_S3_BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]\Z")


def _json_payload(response: Any, label: str) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(f"{label} did not contain valid JSON") from exc


def _access_token(payload: Mapping[str, Any]) -> str:
    token = payload.get("access_token")
    if not isinstance(token, str) or not token.strip():
        raise RuntimeError("Zwift token response did not contain an access token")
    return token


def _player_id(profile: Mapping[str, Any]) -> str:
    value = profile.get("id")
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise RuntimeError("Zwift profile response did not contain a player ID")
    return str(value).strip()


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None
