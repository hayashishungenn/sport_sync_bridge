from __future__ import annotations

import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests

from .config import AppConfig
from .models import Activity
from .sources import SourceAdapter
from .state import StateDB
from .utils import fit_signature_ok, parse_datetime, safe_filename


class Concept2Source(SourceAdapter):
    name = "concept2"
    production_api_root = "https://log.concept2.com"
    development_api_root = "https://log-dev.concept2.com"
    page_size = 250
    read_scope = "user:read,results:read"
    write_scope = "user:read,results:write"

    def __init__(self, config: AppConfig, state_db: StateDB):
        super().__init__(config)
        self.state_db = state_db
        self.api_root = config.concept2_api_root.rstrip("/")
        if self.api_root not in {self.production_api_root, self.development_api_root}:
            raise ValueError("Concept2 API root must be the official production or development host")
        self.authorization_url = f"{self.api_root}/oauth/authorize"
        self.token_url = f"{self.api_root}/oauth/access_token"
        self._authenticated = False
        self.session.headers.update({"Accept": "application/vnd.c2logbook.v1+json"})

    def is_configured(self) -> bool:
        access_token = self.state_db.get_value("concept2_access_token") or self.config.concept2_access_token
        refresh_token = self.state_db.get_value("concept2_refresh_token") or self.config.concept2_refresh_token
        return bool(
            access_token
            or (
                refresh_token
                and self.config.concept2_client_id
                and self.config.concept2_client_secret
            )
            or (self.config.concept2_client_id and self.config.concept2_client_secret)
        )

    def build_authorize_url(self, *, write: bool = False) -> str:
        self._require_client_credentials()
        query = urllib.parse.urlencode(
            {
                "client_id": self.config.concept2_client_id,
                "scope": self.write_scope if write else self.read_scope,
                "response_type": "code",
                "redirect_uri": self.config.concept2_redirect_uri,
            }
        )
        return f"{self.authorization_url}?{query}"

    def exchange_code(self, code: str, *, write: bool = False) -> dict:
        self._require_client_credentials()
        if not code.strip():
            raise ValueError("Concept2 OAuth code cannot be empty")
        scope = self.write_scope if write else self.read_scope
        payload = self._request_token(
            {
                "client_id": self.config.concept2_client_id,
                "client_secret": self.config.concept2_client_secret,
                "grant_type": "authorization_code",
                "code": code.strip(),
                "redirect_uri": self.config.concept2_redirect_uri,
                "scope": scope,
            }
        )
        self._persist_token_payload(payload, requested_scope=scope)
        return payload

    def authenticate(self) -> None:
        if self._authenticated:
            return
        response = self._api_request("get", "/api/users/me", timeout=30)
        response.raise_for_status()
        _response_object(response, "Concept2 profile")
        self._authenticated = True

    def list_activities(
        self,
        since: datetime | None,
        until: datetime | None,
        limit: int | None,
    ) -> list[Activity]:
        if limit is not None and limit <= 0:
            return []

        self.authenticate()
        params: dict[str, str | int] = {"number": self.page_size}
        if since is not None:
            params["from"] = _format_filter_date(since)
        if until is not None:
            params["to"] = _format_filter_date(until)

        activities: list[Activity] = []
        page = 1
        while True:
            response = self._api_request(
                "get",
                "/api/users/me/results",
                params={**params, "page": page},
                timeout=30,
            )
            response.raise_for_status()
            payload = _response_object(response, "Concept2 results")
            rows = payload.get("data")
            if not isinstance(rows, list):
                raise RuntimeError("Concept2 results response must contain a data list")
            if not rows:
                break

            for index, row in enumerate(rows):
                if not isinstance(row, dict):
                    raise RuntimeError(f"Concept2 result {index} must be a JSON object")
                activity = _activity_from_result(row, index)
                if _within_range(activity.start_time, since, until):
                    activities.append(activity)
                    if limit is not None and len(activities) >= limit:
                        break

            if limit is not None and len(activities) >= limit:
                break
            if not _has_next_page(payload.get("meta"), page, len(rows), self.page_size):
                break
            page += 1

        return sorted(activities, key=_activity_sort_key)

    def download_fit(self, activity: Activity, output_dir: Path) -> Path:
        activity_dir = output_dir / self.name
        activity_dir.mkdir(parents=True, exist_ok=True)
        path = activity_dir / f"{safe_filename(activity.source_id)}.fit"
        if path.is_file() and path.stat().st_size >= 100 and fit_signature_ok(path):
            return path

        result_id = urllib.parse.quote(activity.source_id, safe="")
        response = self._api_request(
            "get",
            f"/api/users/me/results/{result_id}/export/fit",
            headers={"Accept": "application/octet-stream"},
            timeout=120,
        )
        if response.status_code == 404:
            raise RuntimeError(
                f"Concept2 FIT export is unavailable for result {activity.source_id}; "
                "the Logbook reports this when stroke data is missing or the result was not found"
            )
        response.raise_for_status()
        content = response.content
        if len(content) < 100 or content[8:12] != b".FIT":
            raise RuntimeError(f"Concept2 export is not a valid FIT for result {activity.source_id}")
        path.write_bytes(content)
        return path

    def delete_result(self, result_id: str) -> None:
        if not result_id.strip():
            raise ValueError("Concept2 result ID cannot be empty")
        if (
            self.api_root == self.production_api_root
            and not self.config.concept2_allow_production_writes
        ):
            raise RuntimeError(
                "Concept2 requires write testing on log-dev.concept2.com and approval before "
                "production writes; CONCEPT2_ALLOW_PRODUCTION_WRITES is disabled"
            )
        scope = self.state_db.get_value("concept2_scope") or self.config.concept2_scope
        if not scope:
            raise RuntimeError(
                "Concept2 token scope is unknown; authorize with `concept2-auth-url --write` "
                "or configure CONCEPT2_SCOPE"
            )
        if "results:write" not in _scopes(scope):
            raise RuntimeError(
                "Concept2 token is read-only; authorize again with `concept2-auth-url --write`"
            )
        escaped_id = urllib.parse.quote(result_id.strip(), safe="")
        response = self._api_request(
            "delete", f"/api/users/me/results/{escaped_id}", timeout=30
        )
        response.raise_for_status()

    def _api_request(self, method: str, path: str, **kwargs) -> requests.Response:
        access_token = self._ensure_access_token()
        request = getattr(self.session, method)
        base_headers = dict(kwargs.pop("headers", {}))

        def send(token: str) -> requests.Response:
            headers = {**base_headers, "Authorization": f"Bearer {token}"}
            return request(f"{self.api_root}{path}", headers=headers, **kwargs)

        response = send(access_token)
        if response.status_code == 401 and self._can_refresh():
            response = send(self._refresh_access_token())
        return response

    def _ensure_access_token(self) -> str:
        access_token = (
            self.state_db.get_value("concept2_access_token")
            or self.config.concept2_access_token
        )
        refresh_token = (
            self.state_db.get_value("concept2_refresh_token")
            or self.config.concept2_refresh_token
        )
        expires_at = (
            self.state_db.get_value("concept2_expires_at")
            or self.config.concept2_expires_at
        )

        if access_token:
            expiry = _expiration_timestamp(expires_at)
            if expiry is None or expiry - time.time() > 60:
                return str(access_token)
            if not refresh_token:
                raise RuntimeError(
                    "Concept2 access token has expired and no refresh token is configured"
                )

        if not refresh_token:
            raise RuntimeError(
                "Concept2 access token is missing. Run `python sync.py concept2-auth-url`, "
                "authorize the app, then run `python sync.py concept2-exchange --code ...`."
            )
        return self._refresh_access_token()

    def _can_refresh(self) -> bool:
        refresh_token = (
            self.state_db.get_value("concept2_refresh_token")
            or self.config.concept2_refresh_token
        )
        return bool(
            refresh_token
            and self.config.concept2_client_id
            and self.config.concept2_client_secret
        )

    def _refresh_access_token(self) -> str:
        refresh_token = (
            self.state_db.get_value("concept2_refresh_token")
            or self.config.concept2_refresh_token
        )
        if not refresh_token:
            raise RuntimeError("Concept2 refresh token is not configured")
        self._require_client_credentials()
        scope = (
            self.state_db.get_value("concept2_scope")
            or self.config.concept2_scope
            or self.read_scope
        )
        payload = self._request_token(
            {
                "client_id": self.config.concept2_client_id,
                "client_secret": self.config.concept2_client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": scope,
            }
        )
        self._persist_token_payload(payload, requested_scope=scope)
        access_token = payload.get("access_token")
        if not access_token:
            raise RuntimeError("Concept2 did not return an access token")
        return str(access_token)

    def _request_token(self, data: dict[str, str | None]) -> dict:
        response = self.session.post(
            self.token_url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        response.raise_for_status()
        return _response_object(response, "Concept2 OAuth")

    def _persist_token_payload(self, payload: dict, *, requested_scope: str) -> None:
        access_token = payload.get("access_token")
        if not access_token:
            raise RuntimeError("Concept2 did not return an access token")

        returned_scope = payload.get("scope")
        scope_text = _scope_text(returned_scope) if returned_scope else requested_scope
        granted_scopes = _scopes(scope_text)
        required_scope = (
            "results:write"
            if "results:write" in _scopes(requested_scope)
            else "results:read"
        )
        if required_scope not in granted_scopes:
            raise RuntimeError(f"Concept2 authorization is missing the {required_scope} scope")

        self.state_db.set_value("concept2_access_token", str(access_token))
        refresh_token = payload.get("refresh_token")
        if refresh_token:
            self.state_db.set_value("concept2_refresh_token", str(refresh_token))

        expires_at = payload.get("expires_at")
        if expires_at is None and payload.get("expires_in") is not None:
            try:
                expires_at = str(int(time.time() + float(payload["expires_in"])))
            except (TypeError, ValueError):
                expires_at = None
        if expires_at is not None:
            self.state_db.set_value("concept2_expires_at", str(expires_at))
        self.state_db.set_value("concept2_scope", scope_text)

    def _require_client_credentials(self) -> None:
        if not self.config.concept2_client_id or not self.config.concept2_client_secret:
            raise RuntimeError("Concept2 client ID and client secret are not configured")


def _activity_from_result(item: dict, index: int) -> Activity:
    raw_id = item.get("id")
    if raw_id in (None, ""):
        raise RuntimeError(f"Concept2 result {index} is missing its ID")
    result_type = item.get("type")
    distance = item.get("distance")
    label = str(result_type or "workout").strip()
    name = item.get("comments")
    if not isinstance(name, str) or not name.strip():
        name = (
            f"Concept2 {label} {distance}m"
            if distance is not None
            else f"Concept2 {label} {raw_id}"
        )
    return Activity(
        source=Concept2Source.name,
        source_id=str(raw_id),
        name=name,
        sport_type=_concept2_sport_type(result_type),
        start_time=parse_datetime(item.get("date_utc") or item.get("date")),
        raw=item,
    )


def _concept2_sport_type(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    mapping = {
        "rower": "rowing",
        "dynamic": "rowing",
        "slides": "rowing",
        "water": "rowing",
        "skierg": "skiing",
        "snow": "skiing",
        "rollerski": "skiing",
        "bike": "cycling",
        "paddle": "paddling",
        "multierg": "training",
    }
    return mapping.get(value.strip().lower())


def _response_object(response: requests.Response, description: str) -> dict:
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"{description} response is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{description} response must be a JSON object")
    return payload


def _has_next_page(meta: object, page: int, row_count: int, page_size: int) -> bool:
    if isinstance(meta, dict):
        pagination = meta.get("pagination")
        if isinstance(pagination, dict):
            try:
                total_pages = int(pagination.get("total_pages"))
            except (TypeError, ValueError):
                total_pages = 0
            if total_pages > 0:
                return page < total_pages
            links = pagination.get("links")
            if isinstance(links, dict) and links.get("next"):
                return True
    return row_count >= page_size


def _within_range(
    start_time: datetime | None,
    since: datetime | None,
    until: datetime | None,
) -> bool:
    if since is not None and (start_time is None or start_time < _as_utc(since)):
        return False
    if until is not None and (start_time is None or start_time > _as_utc(until)):
        return False
    return True


def _format_filter_date(value: datetime) -> str:
    return _as_utc(value).strftime("%Y-%m-%d %H:%M:%S")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _activity_sort_key(activity: Activity) -> tuple[datetime, str]:
    return (
        activity.start_time or datetime(1970, 1, 1, tzinfo=timezone.utc),
        activity.source_id,
    )


def _scope_text(value: object) -> str:
    if isinstance(value, list):
        return ",".join(str(scope) for scope in value)
    return str(value)


def _scopes(value: object) -> set[str]:
    scopes = set(_scope_text(value).replace(",", " ").split())
    if "results:write" in scopes:
        scopes.add("results:read")
    if "user:write" in scopes:
        scopes.add("user:read")
    return scopes


def _expiration_timestamp(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        parsed = parse_datetime(value)
        return parsed.timestamp() if parsed is not None else None
