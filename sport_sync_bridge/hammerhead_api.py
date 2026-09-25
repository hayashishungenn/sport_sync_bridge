from __future__ import annotations

import hmac
import secrets
import time
import urllib.parse
from datetime import datetime

import requests

from .config import AppConfig
from .state import StateDB
from .utils import parse_datetime


class HammerheadClient:
    api_root = "https://api.hammerhead.io/v1/api"
    auth_root = "https://api.hammerhead.io/v1/auth"
    authorization_url = f"{auth_root}/oauth/authorize"
    token_url = f"{auth_root}/oauth/token"
    default_scopes = "activity:read route:read route:write"
    oauth_state_key = "hammerhead_oauth_state"

    def __init__(self, config: AppConfig, state_db: StateDB):
        self.config = config
        self.state_db = state_db
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})

    def is_configured(self) -> bool:
        return bool(
            self.config.hammerhead_access_token
            or self.config.hammerhead_refresh_token
            or (self.config.hammerhead_client_id and self.config.hammerhead_client_secret)
        )

    def authenticate(self) -> None:
        self._ensure_access_token()

    def build_authorize_url(self) -> str:
        self._require_client_credentials()
        state = secrets.token_urlsafe(32)
        self.state_db.set_value(self.oauth_state_key, state)
        query = urllib.parse.urlencode(
            {
                "client_id": self.config.hammerhead_client_id,
                "redirect_uri": self.config.hammerhead_redirect_uri,
                "response_type": "code",
                "scope": self.config.hammerhead_scope or self.default_scopes,
                "state": state,
            }
        )
        return f"{self.authorization_url}?{query}"

    def exchange_code(self, code: str, state: str) -> dict:
        self._require_client_credentials()
        if not code.strip():
            raise ValueError("Hammerhead OAuth code cannot be empty")
        expected_state = self.state_db.get_value(self.oauth_state_key)
        if not expected_state or not hmac.compare_digest(expected_state, state.strip()):
            raise ValueError("Hammerhead OAuth state is missing or does not match")

        payload = self._request_token(
            {
                "client_id": self.config.hammerhead_client_id,
                "client_secret": self.config.hammerhead_client_secret,
                "code": code.strip(),
                "redirect_uri": self.config.hammerhead_redirect_uri,
                "grant_type": "authorization_code",
            }
        )
        self._persist_token_payload(payload)
        self.state_db.set_value(self.oauth_state_key, "")
        return payload

    def api_request(self, method: str, path: str, **kwargs) -> requests.Response:
        request = getattr(self.session, method)
        base_headers = dict(kwargs.pop("headers", {}))

        def send(access_token: str) -> requests.Response:
            headers = {**base_headers, "Authorization": f"Bearer {access_token}"}
            return request(
                f"{self.api_root}/{path.lstrip('/')}",
                headers=headers,
                **kwargs,
            )

        response = send(self._ensure_access_token())
        if response.status_code == 401 and self._can_refresh():
            response = send(self._refresh_access_token())
        return response

    def _ensure_access_token(self) -> str:
        access_token = (
            self.state_db.get_value("hammerhead_access_token")
            or self.config.hammerhead_access_token
        )
        refresh_token = (
            self.state_db.get_value("hammerhead_refresh_token")
            or self.config.hammerhead_refresh_token
        )
        expires_at = (
            self.state_db.get_value("hammerhead_expires_at")
            or self.config.hammerhead_expires_at
        )

        expiry = _expiration_timestamp(expires_at)
        if access_token and (expiry is None or expiry - time.time() > 60):
            return str(access_token)
        if refresh_token and self.config.hammerhead_client_id and self.config.hammerhead_client_secret:
            return self._refresh_access_token()
        if access_token:
            raise RuntimeError("Hammerhead access token has expired and cannot be refreshed")
        raise RuntimeError(
            "Hammerhead access token is missing. Run `python sync.py hammerhead-auth-url`, "
            "authorize the app, then run `python sync.py hammerhead-exchange --code ... --state ...`."
        )

    def _can_refresh(self) -> bool:
        refresh_token = (
            self.state_db.get_value("hammerhead_refresh_token")
            or self.config.hammerhead_refresh_token
        )
        return bool(
            refresh_token
            and self.config.hammerhead_client_id
            and self.config.hammerhead_client_secret
        )

    def _refresh_access_token(self) -> str:
        refresh_token = (
            self.state_db.get_value("hammerhead_refresh_token")
            or self.config.hammerhead_refresh_token
        )
        if not refresh_token:
            raise RuntimeError("Hammerhead refresh token is not configured")
        self._require_client_credentials()
        payload = self._request_token(
            {
                "client_id": self.config.hammerhead_client_id,
                "client_secret": self.config.hammerhead_client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        )
        self._persist_token_payload(payload)
        access_token = payload.get("access_token")
        if not access_token:
            raise RuntimeError("Hammerhead did not return an access token")
        return str(access_token)

    def _request_token(self, data: dict[str, str | None]) -> dict:
        response = self.session.post(
            self.token_url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Hammerhead OAuth response must be a JSON object")
        return payload

    def _persist_token_payload(self, payload: dict) -> None:
        access_token = payload.get("access_token")
        if not access_token:
            raise RuntimeError("Hammerhead did not return an access token")
        self.state_db.set_value("hammerhead_access_token", str(access_token))

        refresh_token = payload.get("refresh_token")
        if refresh_token:
            self.state_db.set_value("hammerhead_refresh_token", str(refresh_token))

        expires_at = payload.get("expires_at")
        if expires_at is None and payload.get("expires_in") is not None:
            try:
                expires_at = str(int(time.time() + float(payload["expires_in"])))
            except (TypeError, ValueError):
                expires_at = None
        self.state_db.set_value(
            "hammerhead_expires_at", "" if expires_at is None else str(expires_at)
        )

    def _require_client_credentials(self) -> None:
        if not self.config.hammerhead_client_id or not self.config.hammerhead_client_secret:
            raise RuntimeError("Hammerhead client ID and client secret are not configured")


def _expiration_timestamp(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        parsed = parse_datetime(value)
        return parsed.timestamp() if isinstance(parsed, datetime) else None
