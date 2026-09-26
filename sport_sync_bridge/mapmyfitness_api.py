from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import requests

from .config import AppConfig
from .state import StateDB
from .utils import utcnow


class MapMyFitnessClient:
    api_root = "https://api.mapmyfitness.com/v7.1"
    authorization_url = "https://www.mapmyfitness.com/oauth2/authorize/"
    token_url = "https://api.mapmyfitness.com/v7.1/oauth2/access_token/"
    access_token_key = "mapmyfitness_access_token"
    refresh_token_key = "mapmyfitness_refresh_token"
    expires_at_key = "mapmyfitness_expires_at"
    scope_key = "mapmyfitness_scope"
    user_id_key = "mapmyfitness_user_id"

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

    def is_configured(self) -> bool:
        return bool(self.config.mapmyfitness_client_id and self.config.mapmyfitness_client_secret)

    @property
    def access_token(self) -> str | None:
        token = self.state_db.get_value(self.access_token_key)
        return token.strip() if isinstance(token, str) and token.strip() else None

    @property
    def refresh_token(self) -> str | None:
        token = self.state_db.get_value(self.refresh_token_key)
        return token.strip() if isinstance(token, str) and token.strip() else None

    @property
    def user_id(self) -> str | None:
        value = self.state_db.get_value(self.user_id_key)
        return value.strip() if isinstance(value, str) and value.strip() else None

    def _require_client_credentials(self) -> tuple[str, str]:
        client_id = self.config.mapmyfitness_client_id
        client_secret = self.config.mapmyfitness_client_secret
        if not client_id or not client_secret:
            raise RuntimeError(
                "MapMyFitness API client is not configured; set "
                "MAPMYFITNESS_CLIENT_ID and MAPMYFITNESS_CLIENT_SECRET"
            )
        return client_id, client_secret

    def build_authorize_url(self) -> str:
        client_id, _ = self._require_client_credentials()
        redirect_uri = self.config.mapmyfitness_redirect_uri.strip()
        if not redirect_uri:
            raise RuntimeError("MAPMYFITNESS_REDIRECT_URI must not be empty")
        query = urlencode(
            {
                "client_id": client_id,
                "response_type": "code",
                "redirect_uri": redirect_uri,
            }
        )
        return f"{self.authorization_url}?{query}"

    def exchange_code(self, code: str) -> dict[str, Any]:
        normalized_code = code.strip()
        if not normalized_code:
            raise ValueError("MapMyFitness OAuth code cannot be empty")
        payload = self._token_request(
            {
                "grant_type": "authorization_code",
                "code": normalized_code,
            }
        )
        token = _required_string(payload.get("access_token"), "access token")
        profile = self._request_json("GET", "/user/self/", token=token)
        profile_user = profile.get("user")
        if isinstance(profile_user, Mapping):
            profile = dict(profile_user)
        user_id = _positive_id(profile.get("id"), "user ID")

        self._save_tokens(payload, replace_refresh=True)
        self.state_db.set_value(self.user_id_key, user_id)
        return {
            "user_id": user_id,
            "scope": payload.get("scope"),
            "expires_at": self.state_db.get_value(self.expires_at_key),
        }

    def authenticate(self) -> dict[str, Any]:
        profile = self.get_json("/user/self/")
        profile_user = profile.get("user")
        if isinstance(profile_user, Mapping):
            profile = dict(profile_user)
        user_id = _positive_id(profile.get("id"), "user ID")
        self.state_db.set_value(self.user_id_key, user_id)
        return profile

    def get_json(
        self,
        path: str,
        *,
        params: dict[str, object] | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        response = self.api_request("GET", path, params=params, token=token, timeout=30)
        _raise_for_response(response, "API request")
        return _response_object(response, "API request")

    def api_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, object] | None = None,
        token: str | None = None,
        refresh_on_unauthorized: bool = True,
        **kwargs: Any,
    ) -> requests.Response:
        client_id, _ = self._require_client_credentials()
        explicit_token = token is not None
        access_token = token or self._get_access_token()
        response = self._send_api_request(method, path, client_id, access_token, params, kwargs)
        if (
            response.status_code == 401
            and refresh_on_unauthorized
            and not explicit_token
            and self.refresh_token
        ):
            access_token = self.refresh_access_token()
            response = self._send_api_request(method, path, client_id, access_token, params, kwargs)
        if response.status_code == 401:
            raise RuntimeError(
                "MapMyFitness rejected the access token; authorize again with "
                "mapmyfitness-auth-url and mapmyfitness-exchange"
            )
        if response.status_code == 403:
            raise RuntimeError("MapMyFitness denied access to this resource")
        return response

    def refresh_access_token(self) -> str:
        refresh_token = self.refresh_token
        if refresh_token is None:
            raise RuntimeError(
                "MapMyFitness refresh token is missing; authorize again with mapmyfitness-auth-url"
            )
        payload = self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        )
        return self._save_tokens(payload, replace_refresh=False)

    def _get_access_token(self) -> str:
        token = self.access_token
        if token is None:
            raise RuntimeError(
                "MapMyFitness is not authorized; run mapmyfitness-auth-url and "
                "mapmyfitness-exchange --code ..."
            )
        expires_at = self.state_db.get_value(self.expires_at_key)
        parsed_expiration = _parse_expiration(expires_at)
        if parsed_expiration is not None and parsed_expiration <= utcnow() + timedelta(seconds=30):
            if self.refresh_token is None:
                raise RuntimeError(
                    "MapMyFitness access token has expired; authorize again with mapmyfitness-auth-url"
                )
            return self.refresh_access_token()
        return token

    def _token_request(self, fields: dict[str, str]) -> dict[str, Any]:
        client_id, client_secret = self._require_client_credentials()
        response_fields = {
            **fields,
            "client_id": client_id,
            "client_secret": client_secret,
        }
        try:
            response = self.session.post(
                self.token_url,
                data=response_fields,
                headers={"Api-Key": client_id, "Accept": "application/json"},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise RuntimeError("MapMyFitness OAuth request failed due to a network error") from exc
        _raise_for_response(response, "OAuth token request")
        return _response_object(response, "OAuth token request")

    def _save_tokens(self, payload: Mapping[str, Any], *, replace_refresh: bool) -> str:
        access_token = _required_string(payload.get("access_token"), "access token")
        self.state_db.set_value(self.access_token_key, access_token)

        refresh_token = payload.get("refresh_token")
        if isinstance(refresh_token, str) and refresh_token.strip():
            self.state_db.set_value(self.refresh_token_key, refresh_token.strip())
        elif replace_refresh:
            self.state_db.set_value(self.refresh_token_key, "")

        expires_in = payload.get("expires_in")
        if (
            isinstance(expires_in, (int, float))
            and not isinstance(expires_in, bool)
            and math.isfinite(float(expires_in))
            and expires_in > 0
        ):
            expires_at = utcnow() + timedelta(seconds=float(expires_in))
            self.state_db.set_value(self.expires_at_key, expires_at.isoformat())
        else:
            self.state_db.set_value(self.expires_at_key, "")

        scope = payload.get("scope")
        self.state_db.set_value(self.scope_key, scope.strip() if isinstance(scope, str) else "")
        return access_token

    def _request_json(self, method: str, path: str, *, token: str) -> dict[str, Any]:
        response = self.api_request(
            method,
            path,
            token=token,
            refresh_on_unauthorized=False,
            timeout=30,
        )
        _raise_for_response(response, "current-user validation")
        return _response_object(response, "current-user validation")

    def _send_api_request(
        self,
        method: str,
        path: str,
        client_id: str,
        access_token: str,
        params: dict[str, object] | None,
        kwargs: dict[str, Any],
    ) -> requests.Response:
        request_kwargs = dict(kwargs)
        headers = dict(request_kwargs.pop("headers", {}))
        headers["Api-Key"] = client_id
        headers["Authorization"] = f"Bearer {access_token}"
        try:
            return self.session.request(
                method.upper(),
                f"{self.api_root}{path}",
                params=params,
                headers=headers,
                **request_kwargs,
            )
        except requests.RequestException as exc:
            raise RuntimeError("MapMyFitness API request failed due to a network error") from exc


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"MapMyFitness response did not contain a valid {label}")
    return value.strip()


def _positive_id(value: object, label: str) -> str:
    normalized = str(value).strip() if value is not None else ""
    if not normalized.isdecimal() or int(normalized) <= 0:
        raise RuntimeError(f"MapMyFitness response did not contain a valid {label}")
    return normalized


def _raise_for_response(response: requests.Response, operation: str) -> None:
    status = getattr(response, "status_code", 200)
    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"MapMyFitness {operation} failed with HTTP {status}") from exc


def _response_object(response: requests.Response, operation: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"MapMyFitness {operation} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"MapMyFitness {operation} response must be a JSON object")
    return payload


def _parse_expiration(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        expiration = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if expiration.tzinfo is None:
        return expiration.replace(tzinfo=timezone.utc)
    return expiration.astimezone(timezone.utc)
