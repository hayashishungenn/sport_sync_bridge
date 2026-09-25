from __future__ import annotations

import hmac
import json
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .config import AppConfig
from .formats import convert_activity_file
from .models import Activity
from .sources import SourceAdapter
from .state import StateDB
from .utils import fit_signature_ok, parse_datetime, safe_filename


GOOGLE_HEALTH_SCOPES = (
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.location.readonly",
)


class GoogleHealthClient:
    authorization_uri = "https://accounts.google.com/o/oauth2/v2/auth"
    token_uri = "https://oauth2.googleapis.com/token"
    api_root = "https://health.googleapis.com/v4"
    credentials_key = "google_health_credentials"
    oauth_state_key = "google_health_oauth_state"
    oauth_code_verifier_key = "google_health_oauth_code_verifier"

    def __init__(self, config: AppConfig, state_db: StateDB):
        self.config = config
        self.state_db = state_db

    def is_oauth_configured(self) -> bool:
        return bool(self.config.google_health_client_id and self.config.google_health_client_secret)

    def is_configured(self) -> bool:
        if not self.is_oauth_configured():
            return False
        raw_credentials = self.state_db.get_value(self.credentials_key)
        if not raw_credentials:
            return False
        try:
            payload = json.loads(raw_credentials)
        except (TypeError, ValueError):
            return False
        return isinstance(payload, dict) and bool(payload.get("refresh_token"))

    def build_authorize_url(self) -> str:
        flow = self._new_flow()
        authorization_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        self.state_db.set_value(self.oauth_state_key, state)
        self.state_db.set_value(self.oauth_code_verifier_key, flow.code_verifier or "")
        return authorization_url

    def exchange_code(self, code: str, state: str) -> dict[str, object]:
        if not code.strip():
            raise ValueError("Google Health OAuth code must not be empty")
        expected_state = self.state_db.get_value(self.oauth_state_key)
        if not expected_state or not hmac.compare_digest(expected_state, state.strip()):
            raise RuntimeError("Google Health OAuth state does not match the authorization request")

        code_verifier = self.state_db.get_value(self.oauth_code_verifier_key) or None
        flow = self._new_flow(state=expected_state, code_verifier=code_verifier)
        try:
            flow.fetch_token(code=code.strip())
        except Exception as exc:
            raise RuntimeError("Google Health OAuth code exchange failed") from exc

        credentials = flow.credentials
        if not credentials.refresh_token:
            raise RuntimeError(
                "Google did not return an offline refresh token. Run google-health-auth-url "
                "again and approve the requested access."
            )
        granted_scopes = set(credentials.granted_scopes or credentials.scopes or ())
        missing_scopes = set(GOOGLE_HEALTH_SCOPES) - granted_scopes
        if missing_scopes:
            raise RuntimeError(
                "Google Health authorization must include both activity and location read-only permissions"
            )
        self._save_credentials(credentials)
        self.state_db.set_value(self.oauth_state_key, "")
        self.state_db.set_value(self.oauth_code_verifier_key, "")

        expiry = credentials.expiry
        return {
            "expires_at": expiry.isoformat() if expiry else None,
            "scopes": list(credentials.granted_scopes or credentials.scopes or ()),
        }

    def authenticate(self) -> None:
        credentials = self._load_credentials()
        if not credentials.valid:
            from google.auth.transport.requests import Request

            try:
                credentials.refresh(Request())
            except Exception as exc:
                if _is_invalid_grant(exc):
                    raise RuntimeError(
                        "Google Health refresh token expired or was revoked; run "
                        "`python sync.py google-health-auth-url` and authorize again"
                    ) from exc
                raise RuntimeError("Google Health token refresh failed") from exc
            self._save_credentials(credentials)

    def get_json(self, path: str, *, params: dict[str, str | int] | None = None) -> dict[str, Any]:
        response = self._get(path, params=params, timeout=30)
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(f"Google Health response for {path} is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"Google Health response for {path} must be a JSON object")
        return payload

    def get_bytes(self, path: str, *, params: dict[str, str] | None = None) -> bytes:
        response = self._get(path, params=params, timeout=120)
        content = response.content
        if not isinstance(content, bytes) or not content:
            raise RuntimeError(f"Google Health response for {path} is empty")
        return content

    def _get(
        self,
        path: str,
        *,
        params: dict[str, str | int] | None,
        timeout: int,
    ) -> Any:
        credentials = self._load_credentials()
        try:
            from google.auth.transport.requests import AuthorizedSession

            session = AuthorizedSession(credentials)
        except ImportError as exc:
            raise RuntimeError("google-auth is required for Google Health API access") from exc

        try:
            response = session.get(f"{self.api_root}{path}", params=params, timeout=timeout)
            if response.status_code >= 400:
                message = _error_message(response)
                detail = f": {message}" if message else ""
                raise RuntimeError(f"Google Health API request failed ({response.status_code}){detail}")
            return response
        except Exception as exc:
            if _is_invalid_grant(exc):
                raise RuntimeError(
                    "Google Health refresh token expired or was revoked; run "
                    "`python sync.py google-health-auth-url` and authorize again"
                ) from exc
            raise
        finally:
            self._save_credentials(credentials)
            session.close()

    def _new_flow(
        self,
        *,
        state: str | None = None,
        code_verifier: str | None = None,
    ) -> Any:
        if not self.is_oauth_configured():
            raise RuntimeError(
                "Set GOOGLE_HEALTH_CLIENT_ID and GOOGLE_HEALTH_CLIENT_SECRET before OAuth authorization"
            )
        try:
            from google_auth_oauthlib.flow import Flow
        except ImportError as exc:
            raise RuntimeError("google-auth-oauthlib is required for Google Health OAuth") from exc

        redirect_uri = self.config.google_health_redirect_uri
        client_config = {
            "web": {
                "client_id": self.config.google_health_client_id,
                "client_secret": self.config.google_health_client_secret,
                "auth_uri": self.authorization_uri,
                "token_uri": self.token_uri,
                "redirect_uris": [redirect_uri],
            }
        }
        flow_options: dict[str, str] = {}
        if state is not None:
            flow_options["state"] = state
        if code_verifier is not None:
            flow_options["code_verifier"] = code_verifier
        flow = Flow.from_client_config(client_config, scopes=list(GOOGLE_HEALTH_SCOPES), **flow_options)
        flow.redirect_uri = redirect_uri
        return flow

    def _load_credentials(self) -> Any:
        raw_credentials = self.state_db.get_value(self.credentials_key)
        if not raw_credentials:
            raise RuntimeError("Fitbit is not authorized; run `python sync.py google-health-auth-url` first")
        try:
            stored = json.loads(raw_credentials)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Stored Google Health credentials are invalid; authorize again") from exc
        if not isinstance(stored, dict) or not stored.get("refresh_token"):
            raise RuntimeError("Stored Google Health refresh token is missing; authorize again")

        try:
            from google.oauth2.credentials import Credentials

            info = {
                **stored,
                "client_id": self.config.google_health_client_id,
                "client_secret": self.config.google_health_client_secret,
                "token_uri": self.token_uri,
            }
            return Credentials.from_authorized_user_info(info, scopes=list(GOOGLE_HEALTH_SCOPES))
        except ImportError as exc:
            raise RuntimeError("google-auth is required for Google Health API access") from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Stored Google Health credentials are invalid; authorize again") from exc

    def _save_credentials(self, credentials: Any) -> None:
        expiry = credentials.expiry
        payload = {
            "token": credentials.token,
            "refresh_token": credentials.refresh_token,
            "expiry": expiry.isoformat() if expiry else None,
            "scopes": list(credentials.granted_scopes or credentials.scopes or GOOGLE_HEALTH_SCOPES),
        }
        self.state_db.set_value(
            self.credentials_key,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        )


class GoogleHealthSource(SourceAdapter):
    name = "fitbit"
    page_size = 25
    exercise_collection = "/users/me/dataTypes/exercise/dataPoints"

    def __init__(self, config: AppConfig, client: GoogleHealthClient):
        super().__init__(config)
        self.client = client

    def is_configured(self) -> bool:
        return self.client.is_configured()

    def authenticate(self) -> None:
        self.client.authenticate()

    def list_activities(
        self,
        since: datetime | None,
        until: datetime | None,
        limit: int | None,
    ) -> list[Activity]:
        if limit is not None and limit <= 0:
            return []
        lower_bound = parse_datetime(since)
        upper_bound = parse_datetime(until)
        if lower_bound is not None and upper_bound is not None and upper_bound < lower_bound:
            raise ValueError("Google Health activity end time must be on or after the start time")

        activities: list[Activity] = []
        page_token: str | None = None
        while True:
            params: dict[str, str | int] = {"pageSize": self.page_size}
            if page_token:
                params["pageToken"] = page_token
            payload = self.client.get_json(self.exercise_collection, params=params)
            rows = payload.get("dataPoints")
            if not isinstance(rows, list):
                raise RuntimeError("Google Health exercise response must include a dataPoints list")

            reached_lower_bound = False
            for index, row in enumerate(rows):
                activity = _activity_from_data_point(row, index)
                if lower_bound is not None and activity.start_time < lower_bound:
                    reached_lower_bound = True
                    break
                if upper_bound is not None and activity.start_time > upper_bound:
                    continue
                activities.append(activity)
                if limit is not None and len(activities) >= limit:
                    return sorted(activities, key=lambda item: item.start_time)

            if reached_lower_bound:
                break
            next_page_token = payload.get("nextPageToken")
            if next_page_token in (None, ""):
                break
            if not isinstance(next_page_token, str):
                raise RuntimeError("Google Health nextPageToken must be a string")
            page_token = next_page_token

        return sorted(activities, key=lambda item: item.start_time)

    def download_fit(self, activity: Activity, output_dir: Path) -> Path:
        if activity.source != self.name:
            raise ValueError(f"Cannot download a non-Fitbit activity with the Fitbit source: {activity.source}")
        activity_dir = output_dir / self.name
        activity_dir.mkdir(parents=True, exist_ok=True)
        path = activity_dir / f"{safe_filename(activity.source_id)}.fit"
        if path.is_file() and path.stat().st_size >= 100 and fit_signature_ok(path):
            return path
        path.unlink(missing_ok=True)

        encoded_id = quote(activity.source_id, safe="-_")
        export_path = f"{self.exercise_collection}/{encoded_id}:exportExerciseTcx"
        try:
            tcx_bytes = self.client.get_bytes(export_path, params={"alt": "media"})
            with tempfile.TemporaryDirectory(prefix="fitbit-", dir=activity_dir) as temporary_dir:
                tcx_path = Path(temporary_dir) / "activity.tcx"
                tcx_path.write_bytes(tcx_bytes)
                convert_activity_file(
                    tcx_path,
                    path,
                    "fit",
                    activity_name=activity.name,
                    sport_type=activity.sport_type,
                )
        except Exception as exc:
            path.unlink(missing_ok=True)
            raise RuntimeError(
                f"Could not export Fitbit activity {activity.source_id} as a GPS FIT file: {exc}"
            ) from exc
        if path.stat().st_size < 100 or not fit_signature_ok(path):
            path.unlink(missing_ok=True)
            raise RuntimeError(f"Google Health export did not produce a valid FIT for activity {activity.source_id}")
        return path


def _activity_from_data_point(value: object, index: int) -> Activity:
    if not isinstance(value, dict):
        raise RuntimeError(f"Google Health exercise {index} must be a JSON object")
    name = value.get("name")
    if not isinstance(name, str) or not name:
        raise RuntimeError(f"Google Health exercise {index} is missing its resource name")
    source_id = name.rsplit("/", 1)[-1]
    if not source_id or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in source_id):
        raise RuntimeError(f"Google Health exercise {index} has an invalid resource name")

    exercise = value.get("exercise")
    if not isinstance(exercise, dict):
        raise RuntimeError(f"Google Health exercise {source_id} is missing its exercise data")
    interval = exercise.get("interval")
    if not isinstance(interval, dict):
        raise RuntimeError(f"Google Health exercise {source_id} is missing its time interval")
    start_time = parse_datetime(interval.get("startTime"))
    if start_time is None:
        raise RuntimeError(f"Google Health exercise {source_id} is missing a valid start time")

    exercise_type = exercise.get("exerciseType")
    sport_type = _sport_type(exercise_type)
    display_name = exercise.get("displayName")
    activity_name = str(display_name).strip() if display_name else f"{sport_type or 'activity'} {source_id}"
    return Activity(
        source=GoogleHealthSource.name,
        source_id=source_id,
        name=activity_name,
        sport_type=sport_type,
        start_time=start_time,
        raw=value,
    )


def _sport_type(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value.upper()
    known = {
        "RUNNING": "running",
        "INCLINE_RUN": "running",
        "BIKING": "cycling",
        "OUTDOOR_BIKE": "cycling",
        "ELECTRIC_BIKE": "cycling",
        "MOUNTAIN_BIKE": "mountain_biking",
        "HAND_CYCLING": "hand_cycling",
        "WALKING": "walking",
        "INCLINE_WALK": "walking",
        "HIKING": "hiking",
        "BACKPACKING": "hiking",
        "SWIMMING": "swimming",
        "ROWING": "rowing",
        "ROWING_MACHINE": "indoor_rowing",
        "OTHER": "other",
        "EXERCISE_TYPE_UNSPECIFIED": None,
    }
    return known.get(normalized, normalized.lower())


def _error_message(response: Any) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        return str(message) if message else None
    return str(error) if error else None


def _is_invalid_grant(error: Exception) -> bool:
    return "invalid_grant" in str(error).casefold()
