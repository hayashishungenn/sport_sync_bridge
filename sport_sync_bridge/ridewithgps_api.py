from __future__ import annotations

import urllib.parse
from collections.abc import Mapping
from typing import Any

import requests

from .config import AppConfig
from .state import StateDB


class RideWithGPSClient:
    api_root = "https://ridewithgps.com/api/v1"
    authorization_url = "https://ridewithgps.com/oauth/authorize"
    token_url = "https://ridewithgps.com/oauth/token.json"
    access_token_key = "ridewithgps_access_token"
    user_id_key = "ridewithgps_user_id"

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
        stored = self.state_db.get_value(self.access_token_key)
        configured = self.config.ridewithgps_access_token
        token = stored or configured
        return token.strip() if isinstance(token, str) and token.strip() else None

    def is_configured(self) -> bool:
        return self.access_token is not None or bool(
            self.config.ridewithgps_client_id and self.config.ridewithgps_client_secret
        )

    def _require_client_credentials(self) -> tuple[str, str, str]:
        client_id = self.config.ridewithgps_client_id
        client_secret = self.config.ridewithgps_client_secret
        redirect_uri = self.config.ridewithgps_redirect_uri
        if not client_id or not client_secret or not redirect_uri:
            raise RuntimeError(
                "Ride with GPS API client credentials are not configured; set "
                "RIDEWITHGPS_CLIENT_ID, RIDEWITHGPS_CLIENT_SECRET, and "
                "RIDEWITHGPS_REDIRECT_URI"
            )
        return client_id, client_secret, redirect_uri

    def build_authorize_url(self) -> str:
        client_id, _, redirect_uri = self._require_client_credentials()
        query = urllib.parse.urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
            }
        )
        return f"{self.authorization_url}?{query}"

    def exchange_code(self, code: str) -> dict[str, Any]:
        client_id, client_secret, redirect_uri = self._require_client_credentials()
        normalized_code = code.strip()
        if not normalized_code:
            raise ValueError("Ride with GPS OAuth code cannot be empty")

        response = self.session.post(
            self.token_url,
            data={
                "grant_type": "authorization_code",
                "code": normalized_code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
            },
            timeout=30,
        )
        _raise_for_response(response, "OAuth token exchange")
        payload = _response_object(response, "OAuth token exchange")
        token = payload.get("access_token")
        if not isinstance(token, str) or not token.strip():
            raise RuntimeError("Ride with GPS OAuth response did not contain an access token")

        profile = self._request_json("GET", "/users/current.json", token=token.strip())
        user = profile.get("user")
        if not isinstance(user, Mapping):
            raise RuntimeError("Ride with GPS current-user response did not contain a user")

        self.state_db.set_value(self.access_token_key, token.strip())
        user_id = user.get("id")
        if user_id not in (None, ""):
            self.state_db.set_value(self.user_id_key, str(user_id))
        return {"user_id": user_id, "scope": payload.get("scope")}

    def authenticate(self) -> dict[str, Any]:
        return self.get_json("/users/current.json")

    def get_json(self, path: str, *, params: dict[str, object] | None = None) -> dict[str, Any]:
        response = self.api_request("GET", path, params=params, timeout=30)
        _raise_for_response(response, "API request")
        return _response_object(response, "API request")

    def api_request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        access_token = token or self.access_token
        if access_token is None:
            raise RuntimeError(
                "Ride with GPS is not authorized; run `python sync.py ridewithgps-auth-url` "
                "and `python sync.py ridewithgps-exchange --code ...`"
            )
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {access_token}"
        response = self.session.request(
            method.upper(),
            f"{self.api_root}{path}",
            headers=headers,
            **kwargs,
        )
        if response.status_code == 401:
            raise RuntimeError(
                "Ride with GPS rejected the access token; authorize again with "
                "`python sync.py ridewithgps-auth-url` and `ridewithgps-exchange`"
            )
        if response.status_code == 403:
            raise RuntimeError("Ride with GPS denied access to this resource")
        return response

    def delete_trip(self, trip_id: str | int) -> None:
        normalized_id = _positive_id(trip_id, "trip ID")
        response = self.api_request("DELETE", f"/trips/{normalized_id}.json", timeout=30)
        _raise_for_response(response, f"delete trip {normalized_id}")

    def _request_json(self, method: str, path: str, *, token: str) -> dict[str, Any]:
        response = self.api_request(method, path, token=token, timeout=30)
        _raise_for_response(response, "current-user validation")
        return _response_object(response, "current-user validation")


def _raise_for_response(response: requests.Response, operation: str) -> None:
    status = getattr(response, "status_code", 200)
    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Ride with GPS {operation} failed with HTTP {status}") from exc


def _response_object(response: requests.Response, operation: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Ride with GPS {operation} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Ride with GPS {operation} response must be a JSON object")
    return payload


def _positive_id(value: str | int, label: str) -> str:
    normalized = str(value).strip()
    if not normalized.isdecimal() or int(normalized) <= 0:
        raise ValueError(f"Ride with GPS {label} must be a positive integer")
    return normalized
