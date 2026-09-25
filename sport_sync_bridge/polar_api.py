from __future__ import annotations

import hmac
import secrets
import urllib.parse

import requests

from .config import AppConfig
from .state import StateDB


class PolarClient:
    authorization_url = "https://flow.polar.com/oauth2/authorization"
    token_url = "https://polarremote.com/v2/oauth2/token"
    api_root = "https://www.polaraccesslink.com/v3"
    oauth_state_key = "polar_oauth_state"

    def __init__(self, config: AppConfig, state_db: StateDB):
        self.config = config
        self.state_db = state_db
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})

    def is_configured(self) -> bool:
        return bool(
            self.state_db.get_value("polar_access_token")
            or self.config.polar_access_token
        )

    def authenticate(self) -> None:
        self.register_user()

    def build_authorize_url(self) -> str:
        self._require_client_credentials()
        state = secrets.token_urlsafe(32)
        self.state_db.set_value(self.oauth_state_key, state)
        query = {
            "response_type": "code",
            "client_id": self.config.polar_client_id,
            "scope": "accesslink.read_all",
            "state": state,
        }
        if self.config.polar_redirect_uri:
            query["redirect_uri"] = self.config.polar_redirect_uri
        return f"{self.authorization_url}?{urllib.parse.urlencode(query)}"

    def exchange_code(self, code: str, state: str) -> dict:
        self._require_client_credentials()
        if not code.strip():
            raise ValueError("Polar OAuth code cannot be empty")
        expected_state = self.state_db.get_value(self.oauth_state_key)
        if not expected_state or not hmac.compare_digest(expected_state, state.strip()):
            raise ValueError("Polar OAuth state is missing or does not match")

        data = {"grant_type": "authorization_code", "code": code.strip()}
        if self.config.polar_redirect_uri:
            data["redirect_uri"] = self.config.polar_redirect_uri
        response = self.session.post(
            self.token_url,
            data=data,
            auth=(self.config.polar_client_id, self.config.polar_client_secret),
            headers={
                "Accept": "application/json;charset=UTF-8",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise RuntimeError("Polar token response must contain an access_token")

        self.state_db.set_value("polar_access_token", str(payload["access_token"]))
        user_id = payload.get("x_user_id")
        self.state_db.set_value("polar_x_user_id", "" if user_id is None else str(user_id))
        self.state_db.set_value(self.oauth_state_key, "")
        self.register_user()
        return payload

    def register_user(self) -> None:
        access_token = self._access_token()
        user_id = self.state_db.get_value("polar_x_user_id") or ""
        member_id = self._member_id(user_id)
        registered_user_id = self.state_db.get_value("polar_registered_user_id") or ""
        registered_member_id = self.state_db.get_value("polar_registered_member_id") or ""
        if registered_member_id == member_id and registered_user_id == user_id:
            return

        response = self.session.post(
            f"{self.api_root}/users",
            json={"member-id": member_id},
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            timeout=30,
        )
        if response.status_code == 409:
            if user_id and not self.config.polar_member_id:
                self.state_db.set_value("polar_registered_user_id", user_id)
                self.state_db.set_value("polar_registered_member_id", member_id)
                return
            raise RuntimeError(
                "Polar reports this user or member ID is already registered, but local registration "
                "state does not match. Set a unique POLAR_MEMBER_ID or restore the matching local SQLite state."
            )
        response.raise_for_status()

        payload: object = {}
        if response.content:
            payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Polar user registration response must be a JSON object")
        response_user_id = payload.get("polar-user-id")
        if response_user_id is not None:
            self.state_db.set_value("polar_api_user_id", str(response_user_id))
        self.state_db.set_value("polar_registered_user_id", user_id)
        self.state_db.set_value("polar_registered_member_id", member_id)

    def api_request(self, method: str, path: str, **kwargs) -> requests.Response:
        request = getattr(self.session, method)
        headers = {"Authorization": f"Bearer {self._access_token()}"}
        headers.update(kwargs.pop("headers", {}))
        return request(
            f"{self.api_root}/{path.lstrip('/')}",
            headers=headers,
            **kwargs,
        )

    def _access_token(self) -> str:
        token = self.state_db.get_value("polar_access_token") or self.config.polar_access_token
        if not token:
            raise RuntimeError(
                "Polar AccessLink token is missing. Run `python sync.py polar-auth-url`, "
                "authorize the app, then run `python sync.py polar-exchange --code ... --state ...`."
            )
        return str(token)

    def _member_id(self, user_id: str) -> str:
        configured = self.config.polar_member_id
        if configured:
            member_id = configured.strip()
            if member_id:
                return member_id
        if user_id:
            return f"sport_sync_bridge_{user_id}"
        return "sport_sync_bridge"

    def _require_client_credentials(self) -> None:
        if not self.config.polar_client_id or not self.config.polar_client_secret:
            raise RuntimeError("Polar AccessLink client ID and client secret are not configured")
