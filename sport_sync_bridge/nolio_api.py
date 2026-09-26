from __future__ import annotations

import hmac
import secrets
import time
import urllib.parse
from collections.abc import Mapping
from typing import Any

import requests

from .config import AppConfig
from .state import StateDB


class NolioClient:
    api_root = "https://www.nolio.io/api"
    authorization_url = f"{api_root}/authorize/"
    token_url = f"{api_root}/token/"

    access_token_key = "nolio_access_token"
    refresh_token_key = "nolio_refresh_token"
    expires_at_key = "nolio_expires_at"
    scope_key = "nolio_scope"
    oauth_state_key = "nolio_oauth_state"

    def __init__(self, config: AppConfig, state_db: StateDB):
        self.config = config
        self.state_db = state_db
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "sport-sync-bridge",
            }
        )

    @property
    def access_token(self) -> str | None:
        value = self.state_db.get_value(self.access_token_key)
        return value.strip() if isinstance(value, str) and value.strip() else None

    @property
    def refresh_token(self) -> str | None:
        value = self.state_db.get_value(self.refresh_token_key)
        return value.strip() if isinstance(value, str) and value.strip() else None

    def is_configured(self) -> bool:
        return bool(
            getattr(self.config, "nolio_client_id", None)
            and getattr(self.config, "nolio_client_secret", None)
            and getattr(self.config, "nolio_redirect_uri", None)
        )

    def _require_client_credentials(self) -> tuple[str, str, str]:
        client_id = getattr(self.config, "nolio_client_id", None)
        client_secret = getattr(self.config, "nolio_client_secret", None)
        redirect_uri = getattr(self.config, "nolio_redirect_uri", None)
        if not client_id or not client_secret or not redirect_uri:
            raise RuntimeError(
                "Nolio OAuth client is not configured; set NOLIO_CLIENT_ID, "
                "NOLIO_CLIENT_SECRET, and NOLIO_REDIRECT_URI"
            )
        return client_id, client_secret, redirect_uri

    def build_authorize_url(self) -> str:
        client_id, _, redirect_uri = self._require_client_credentials()
        state = secrets.token_urlsafe(32)
        self.state_db.set_value(self.oauth_state_key, state)
        query = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "state": state,
            }
        )
        return f"{self.authorization_url}?{query}"

    def exchange_code(self, code: str, state: str) -> dict[str, Any]:
        client_id, client_secret, redirect_uri = self._require_client_credentials()
        normalized_code = code.strip()
        normalized_state = state.strip()
        expected_state = self.state_db.get_value(self.oauth_state_key)
        if not normalized_code:
            raise ValueError("Nolio OAuth code cannot be empty")
        if not isinstance(expected_state, str) or not expected_state:
            raise ValueError("Nolio OAuth state is missing; run nolio-auth-url again")
        if not hmac.compare_digest(expected_state, normalized_state):
            raise ValueError("Nolio OAuth state did not match the authorization request")

        payload = self._request_token(
            {
                "grant_type": "authorization_code",
                "code": normalized_code,
                "redirect_uri": redirect_uri,
            },
            client_id=client_id,
            client_secret=client_secret,
            operation="code exchange",
        )
        self._store_tokens(payload)
        self.state_db.set_value(self.oauth_state_key, "")
        return {"expires_in": payload.get("expires_in"), "scope": payload.get("scope")}

    def authenticate(self) -> dict[str, Any]:
        payload = self.get_json("/get/user/")
        if not isinstance(payload, Mapping):
            raise RuntimeError("Nolio profile response must be a JSON object")
        return dict(payload)

    def get_json(
        self,
        path: str,
        *,
        params: dict[str, object] | None = None,
    ) -> Any:
        response = self.api_request("GET", path, params=params, timeout=30)
        _raise_for_response(response, "API request")
        return _response_json(response, "API request")

    def api_request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        access_token = self._get_access_token()
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {access_token}"
        response = self.session.request(
            method.upper(),
            f"{self.api_root}{path}",
            headers=headers,
            **kwargs,
        )
        if response.status_code == 401 and self.refresh_token:
            self._refresh_access_token()
            retry_headers = dict(headers)
            retry_headers["Authorization"] = f"Bearer {self._get_access_token()}"
            response = self.session.request(
                method.upper(),
                f"{self.api_root}{path}",
                headers=retry_headers,
                **kwargs,
            )
        if response.status_code == 401:
            raise RuntimeError(
                "Nolio rejected the access token; authorize again with "
                "`python sync.py nolio-auth-url` and `nolio-exchange`"
            )
        return response

    def _get_access_token(self) -> str:
        token = self.access_token
        if token is None:
            raise RuntimeError(
                "Nolio is not authorized; run `python sync.py nolio-auth-url` and "
                "`python sync.py nolio-exchange --code ... --state ...`"
            )

        expires_at = self.state_db.get_value(self.expires_at_key)
        if expires_at:
            try:
                is_expiring = float(expires_at) <= time.time() + 60
            except ValueError as exc:
                raise RuntimeError("Nolio token expiry in local state is invalid") from exc
            if is_expiring:
                self._refresh_access_token()
                token = self.access_token
                if token is None:
                    raise RuntimeError("Nolio token refresh did not save an access token")
        return token

    def _refresh_access_token(self) -> None:
        client_id, client_secret, _ = self._require_client_credentials()
        refresh_token = self.refresh_token
        if refresh_token is None:
            raise RuntimeError(
                "Nolio access token expired and no refresh token is available; "
                "authorize again with `python sync.py nolio-auth-url`"
            )
        payload = self._request_token(
            {"grant_type": "refresh_token", "refresh_token": refresh_token},
            client_id=client_id,
            client_secret=client_secret,
            operation="token refresh",
        )
        self._store_tokens(payload)

    def _request_token(
        self,
        data: dict[str, str],
        *,
        client_id: str,
        client_secret: str,
        operation: str,
    ) -> dict[str, Any]:
        try:
            response = self.session.post(
                self.token_url,
                data=data,
                auth=(client_id, client_secret),
                timeout=30,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Nolio OAuth {operation} request failed") from exc
        _raise_for_response(response, f"OAuth {operation}")
        payload = _response_json(response, f"OAuth {operation}")
        if not isinstance(payload, Mapping):
            raise RuntimeError(f"Nolio OAuth {operation} response must be a JSON object")
        result = dict(payload)
        access_token = result.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            raise RuntimeError(f"Nolio OAuth {operation} response did not contain an access token")
        return result

    def _store_tokens(self, payload: Mapping[str, Any]) -> None:
        access_token = payload["access_token"]
        self.state_db.set_value(self.access_token_key, str(access_token).strip())

        refresh_token = payload.get("refresh_token")
        if isinstance(refresh_token, str) and refresh_token.strip():
            self.state_db.set_value(self.refresh_token_key, refresh_token.strip())

        expires_in = payload.get("expires_in")
        if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool) and expires_in > 0:
            self.state_db.set_value(self.expires_at_key, str(time.time() + float(expires_in)))
        else:
            self.state_db.set_value(self.expires_at_key, "")

        scope = payload.get("scope")
        if isinstance(scope, str):
            self.state_db.set_value(self.scope_key, scope)


def _raise_for_response(response: requests.Response, operation: str) -> None:
    status = getattr(response, "status_code", 200)
    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Nolio {operation} failed with HTTP {status}") from exc


def _response_json(response: requests.Response, operation: str) -> Any:
    try:
        return response.json()
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Nolio {operation} returned invalid JSON") from exc


def normalize_athlete_id(value: object) -> int | None:
    if value is None or not str(value).strip():
        return None
    try:
        athlete_id = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("NOLIO_ATHLETE_ID must be a positive integer") from exc
    if athlete_id <= 0:
        raise ValueError("NOLIO_ATHLETE_ID must be a positive integer")
    return athlete_id
