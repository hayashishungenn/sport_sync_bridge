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


class SuuntoApiError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class SuuntoClient:
    access_token_key = "suunto_access_token"
    refresh_token_key = "suunto_refresh_token"
    expires_at_key = "suunto_expires_at"
    scope_key = "suunto_scope"
    oauth_state_key = "suunto_oauth_state"

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
            getattr(self.config, "suunto_client_id", None)
            and getattr(self.config, "suunto_client_secret", None)
            and getattr(self.config, "suunto_redirect_uri", None)
            and getattr(self.config, "suunto_subscription_key", None)
            and self._valid_https_root(getattr(self.config, "suunto_api_root", None))
            and self._valid_https_root(getattr(self.config, "suunto_oauth_root", None))
        )

    def _credentials(self) -> tuple[str, str, str, str, str, str]:
        client_id = getattr(self.config, "suunto_client_id", None)
        client_secret = getattr(self.config, "suunto_client_secret", None)
        redirect_uri = getattr(self.config, "suunto_redirect_uri", None)
        subscription_key = getattr(self.config, "suunto_subscription_key", None)
        api_root = getattr(self.config, "suunto_api_root", None)
        oauth_root = getattr(self.config, "suunto_oauth_root", None)
        if not all((client_id, client_secret, redirect_uri, subscription_key)):
            raise RuntimeError(
                "Suunto API is not configured; set SUUNTO_CLIENT_ID, "
                "SUUNTO_CLIENT_SECRET, SUUNTO_REDIRECT_URI, and SUUNTO_SUBSCRIPTION_KEY"
            )
        if not self._valid_https_root(api_root) or not self._valid_https_root(oauth_root):
            raise RuntimeError("Suunto API and OAuth roots must be HTTPS URLs")
        return (
            str(client_id),
            str(client_secret),
            str(redirect_uri),
            str(subscription_key),
            str(api_root).rstrip("/"),
            str(oauth_root).rstrip("/"),
        )

    @staticmethod
    def _valid_https_root(value: object) -> bool:
        if not isinstance(value, str):
            return False
        parsed = urllib.parse.urlparse(value.strip())
        return parsed.scheme.lower() == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password

    def build_authorize_url(self) -> str:
        client_id, _, redirect_uri, _, _, oauth_root = self._credentials()
        state = secrets.token_urlsafe(32)
        self.state_db.set_value(self.oauth_state_key, state)
        query = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "scope": "workouts",
                "state": state,
            }
        )
        return f"{oauth_root}/oauth/authorize?{query}"

    def exchange_code(self, code: str, state: str) -> dict[str, Any]:
        client_id, client_secret, redirect_uri, _, _, _ = self._credentials()
        normalized_code = code.strip()
        normalized_state = state.strip()
        expected_state = self.state_db.get_value(self.oauth_state_key)
        if not normalized_code:
            raise ValueError("Suunto OAuth code cannot be empty")
        if not isinstance(expected_state, str) or not expected_state:
            raise ValueError("Suunto OAuth state is missing; run suunto-auth-url again")
        if not hmac.compare_digest(expected_state, normalized_state):
            raise ValueError("Suunto OAuth state did not match the authorization request")

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

    def authenticate(self) -> None:
        self.get_json("/v3/workouts", params={"limit": 1, "offset": 0})

    def api_request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        client_id, _, _, subscription_key, api_root, _ = self._credentials()
        del client_id
        access_token = self._get_access_token()
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {access_token}"
        headers["Ocp-Apim-Subscription-Key"] = subscription_key
        try:
            response = self.session.request(
                method.upper(),
                f"{api_root}{path}",
                headers=headers,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise RuntimeError("Suunto API request failed") from exc

        if response.status_code == 401 and self.refresh_token:
            self._refresh_access_token()
            retry_headers = dict(headers)
            retry_headers["Authorization"] = f"Bearer {self._get_access_token()}"
            try:
                response = self.session.request(
                    method.upper(),
                    f"{api_root}{path}",
                    headers=retry_headers,
                    **kwargs,
                )
            except requests.RequestException as exc:
                raise RuntimeError("Suunto API retry failed") from exc
        if response.status_code == 401:
            raise RuntimeError(
                "Suunto rejected the access token; authorize again with "
                "suunto-auth-url and suunto-exchange"
            )
        return response

    def get_json(self, path: str, *, params: dict[str, object] | None = None) -> Any:
        response = self.api_request("GET", path, params=params, timeout=30)
        raise_for_response(response, "API request")
        try:
            return response.json()
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Suunto API returned invalid JSON") from exc

    def _get_access_token(self) -> str:
        token = self.access_token
        if token is None:
            raise RuntimeError(
                "Suunto is not authorized; run suunto-auth-url and "
                "suunto-exchange --code ... --state ..."
            )
        expires_at = self.state_db.get_value(self.expires_at_key)
        if expires_at:
            try:
                is_expiring = float(expires_at) <= time.time() + 60
            except ValueError as exc:
                raise RuntimeError("Suunto token expiry in local state is invalid") from exc
            if is_expiring:
                self._refresh_access_token()
                token = self.access_token
                if token is None:
                    raise RuntimeError("Suunto token refresh did not save an access token")
        return token

    def _refresh_access_token(self) -> None:
        client_id, client_secret, _, _, _, _ = self._credentials()
        refresh_token = self.refresh_token
        if refresh_token is None:
            raise RuntimeError(
                "Suunto access token expired and no refresh token is available; "
                "authorize again with suunto-auth-url"
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
        _, _, _, _, _, oauth_root = self._credentials()
        try:
            response = self.session.post(
                f"{oauth_root}/oauth/token",
                data=data,
                auth=(client_id, client_secret),
                timeout=30,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Suunto OAuth {operation} request failed") from exc
        raise_for_response(response, f"OAuth {operation}")
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Suunto OAuth {operation} returned invalid JSON") from exc
        if not isinstance(payload, Mapping):
            raise RuntimeError(f"Suunto OAuth {operation} response must be a JSON object")
        result = dict(payload)
        access_token = result.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            raise RuntimeError(f"Suunto OAuth {operation} response did not contain an access token")
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


def raise_for_response(response: requests.Response, operation: str) -> None:
    status = getattr(response, "status_code", 200)
    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        raise SuuntoApiError(f"Suunto {operation} failed with HTTP {status}", status) from exc


def unwrap_payload(value: Any) -> Any:
    if isinstance(value, Mapping) and "payload" in value:
        return value["payload"]
    return value
