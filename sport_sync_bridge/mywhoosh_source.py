from __future__ import annotations

import base64
import json
import math
import tempfile
import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse
from typing import Any

import requests

from .config import AppConfig
from .models import Activity
from .sources import SourceAdapter
from .state import StateDB
from .utils import fit_signature_ok, parse_datetime, safe_filename


class MyWhooshSource(SourceAdapter):
    name = "mywhoosh"
    login_url = "https://services.mywhoosh.com/http-service/api/login"
    api_root = "https://service14.mywhoosh.com/v2"
    coaching_api_root = "https://coaching.mywhoosh.com/api/v2"
    page_size = 50
    access_token_key = "mywhoosh_access_token"
    token_expiry_key = "mywhoosh_token_expiry"
    device_id_key = "mywhoosh_device_id"

    def __init__(self, config: AppConfig, state_db: StateDB):
        super().__init__(config)
        self.state_db = state_db
        self.session.headers.update({"Accept": "application/json"})
        self._access_token_cache: str | None = None
        self._access_token_expiry: float | None = None

    def is_configured(self) -> bool:
        return bool(
            getattr(self.config, "mywhoosh_username", None)
            and getattr(self.config, "mywhoosh_password", None)
        )

    def authenticate(self) -> None:
        self._get_access_token()

    def list_activities(
        self,
        since: datetime | None,
        until: datetime | None,
        limit: int | None,
    ) -> list[Activity]:
        if limit is not None and limit <= 0:
            return []

        lower_bound = _as_utc(since)
        upper_bound = _as_utc(until)
        page_size = min(limit, self.page_size) if limit is not None else self.page_size
        activities: list[Activity] = []
        page = 1
        while True:
            payload = self._post_json(
                "/rider/profile/activities",
                {"type": "", "page": page, "sortDate": "DESC", "limit": page_size},
            )
            _ensure_success(payload, f"activity list page {page}")
            rows = _activity_rows(payload, page)
            for index, row in enumerate(rows):
                activity = _activity_from_record(row, page, index)
                if lower_bound is not None and (
                    activity.start_time is None or activity.start_time < lower_bound
                ):
                    continue
                if upper_bound is not None and (
                    activity.start_time is None or activity.start_time > upper_bound
                ):
                    continue
                activities.append(activity)
                if limit is not None and len(activities) >= limit:
                    return sorted(activities, key=_activity_sort_key)

            pagination = _pagination(payload)
            total_pages = pagination.get("totalPages", pagination.get("totalPage"))
            if total_pages is not None:
                try:
                    page_count = int(total_pages)
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("MyWhoosh totalPages must be an integer") from exc
                if page_count == 0 and not rows and page == 1:
                    break
                if page_count < 1:
                    raise RuntimeError("MyWhoosh totalPages must be positive")
                if page >= page_count:
                    break
            elif not rows or len(rows) < page_size:
                break
            page += 1

        return sorted(activities, key=_activity_sort_key)

    def download_fit(self, activity: Activity, output_dir: Path) -> Path:
        file_id = _file_id(activity)
        activity_dir = output_dir / self.name
        activity_dir.mkdir(parents=True, exist_ok=True)
        path = activity_dir / f"{safe_filename(activity.source_id)}.fit"
        if path.is_file() and path.stat().st_size >= 100 and fit_signature_ok(path):
            return path

        payload = self._post_json(
            "/rider/profile/download-activity-file", {"fileId": file_id}
        )
        _ensure_success(payload, "activity download")
        download_url = _download_url(payload, self.api_root)
        try:
            response = self.session.get(download_url, timeout=120)
            response.raise_for_status()
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            detail = f"HTTP {status}" if status is not None else "network error"
            raise RuntimeError(
                f"MyWhoosh FIT download failed for activity {activity.source_id}: {detail}"
            ) from exc

        content = response.content
        if not isinstance(content, bytes) or len(content) < 100:
            raise RuntimeError(
                f"MyWhoosh returned an empty FIT file for activity {activity.source_id}"
            )

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=activity_dir, prefix=f".{path.name}.", suffix=".tmp", delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(content)
            if not fit_signature_ok(temporary_path):
                raise RuntimeError(
                    f"MyWhoosh returned an invalid FIT file for activity {activity.source_id}"
                )
            temporary_path.replace(path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return path

    def list_workouts(self) -> list[dict[str, Any]]:
        response = self._coaching_request("GET", "/workout-builder/my-workouts")
        payload = _json_object(response, "workout list")
        _ensure_success(payload, "workout list")
        data = payload.get("data")
        if data is None:
            return []
        if isinstance(data, Mapping):
            for key in ("workouts", "results", "data"):
                rows = data.get(key)
                if isinstance(rows, list):
                    data = rows
                    break
        if not isinstance(data, list):
            raise RuntimeError("MyWhoosh workout list did not contain a workouts array")
        workouts: list[dict[str, Any]] = []
        for index, item in enumerate(data):
            if not isinstance(item, Mapping):
                raise RuntimeError(f"MyWhoosh workout item {index} must be an object")
            workouts.append(dict(item))
        return workouts

    def upload_workout(self, workout: Mapping[str, object]) -> dict[str, Any]:
        if not isinstance(workout, Mapping):
            raise ValueError("MyWhoosh workout data must be an object")
        token = self._get_access_token()
        user_id = _jwt_user_id(token)
        response = self._coaching_request(
            "POST",
            "/client/custom-workout-upload",
            token=token,
            json_body={
                "UserId": user_id,
                "SportsModeType": 0,
                "WorkoutsData": [dict(workout)],
            },
            accepted_statuses={200, 201},
        )
        return _optional_json_object(response, "workout upload")

    def delete_workout(self, workout_id: str | int) -> int:
        identifier = str(workout_id).strip()
        if not identifier.isascii() or not identifier.isdecimal():
            raise ValueError("MyWhoosh workout ID must be a positive integer")
        if int(identifier) <= 0:
            raise ValueError("MyWhoosh workout ID must be a positive integer")
        token = self._get_access_token()
        user_id = _jwt_user_id(token)
        path = (
            "/client/custom-workout-upload/"
            f"{quote(str(user_id), safe='')}/{quote(identifier, safe='')}"
        )
        response = self._coaching_request(
            "DELETE", path, token=token, accepted_statuses={200, 204}
        )
        return int(response.status_code)

    def _coaching_request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        json_body: dict[str, object] | None = None,
        accepted_statuses: set[int] | None = None,
    ) -> Any:
        if not path.startswith("/") or "?" in path or "#" in path:
            raise ValueError("Invalid MyWhoosh workout API path")
        access_token = token or self._get_access_token()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Source": "connect",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        request_method = getattr(self.session, method.lower(), None)
        if request_method is None:
            raise ValueError(f"Unsupported MyWhoosh workout API method: {method}")
        try:
            kwargs: dict[str, object] = {"headers": headers, "timeout": 30}
            if json_body is not None:
                kwargs["json"] = json_body
            response = request_method(f"{self.coaching_api_root}{path}", **kwargs)
        except requests.RequestException as exc:
            raise RuntimeError("MyWhoosh workout API request failed: network error") from exc

        status = getattr(response, "status_code", None)
        allowed = {200} if accepted_statuses is None else accepted_statuses
        if status not in allowed:
            try:
                response.raise_for_status()
            except requests.RequestException as exc:
                raise RuntimeError(
                    f"MyWhoosh workout API request failed: HTTP {status}"
                ) from exc
            raise RuntimeError(f"MyWhoosh workout API request failed: HTTP {status}")
        return response

    def _get_access_token(self) -> str:
        now = time.time()
        if self._access_token_cache and (
            self._access_token_expiry is None or self._access_token_expiry > now + 60
        ):
            return self._access_token_cache

        stored_token = self.state_db.get_value(self.access_token_key)
        stored_expiry = self.state_db.get_value(self.token_expiry_key)
        if stored_token and stored_expiry:
            try:
                expiry = float(stored_expiry)
            except ValueError:
                expiry = 0
            if math.isfinite(expiry) and expiry > now + 60:
                self._access_token_cache = stored_token
                self._access_token_expiry = expiry
                return stored_token

        username = getattr(self.config, "mywhoosh_username", None)
        password = getattr(self.config, "mywhoosh_password", None)
        if not username or not password:
            raise RuntimeError(
                "MyWhoosh credentials are not configured; set MYWHOOSH_USERNAME and "
                "MYWHOOSH_PASSWORD in .env"
            )

        device_id = self.state_db.get_value(self.device_id_key)
        if not device_id:
            device_id = str(uuid.uuid4())
            self.state_db.set_value(self.device_id_key, device_id)

        try:
            response = self.session.post(
                self.login_url,
                json={
                    "UserName": username,
                    "Password": password,
                    "Source": "connect",
                    "DeviceId": device_id,
                },
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            detail = f"HTTP {status}" if status is not None else "network error"
            raise RuntimeError(f"MyWhoosh login failed: {detail}") from exc

        result = _json_object(response, "login")
        token = result.get("AccessToken")
        if not isinstance(token, str) or not token.strip():
            raise RuntimeError("MyWhoosh login response did not contain an access token")
        token = token.strip()
        response_device_id = result.get("DeviceId")
        if isinstance(response_device_id, str) and response_device_id.strip():
            self.state_db.set_value(self.device_id_key, response_device_id.strip())

        expiry = _jwt_expiry(token)
        if expiry is not None and expiry <= now + 60:
            raise RuntimeError("MyWhoosh login returned an expired access token")
        self._access_token_cache = token
        self._access_token_expiry = expiry
        if expiry is not None:
            self.state_db.set_value(self.access_token_key, token)
            self.state_db.set_value(self.token_expiry_key, str(expiry))
        return token

    def _post_json(self, path: str, body: dict[str, object]) -> dict[str, Any]:
        for attempt in range(2):
            token = self._get_access_token()
            try:
                response = self.session.post(
                    f"{self.api_root}{path}",
                    json=body,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Source": "connect",
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                    timeout=30,
                )
            except requests.RequestException as exc:
                raise RuntimeError("MyWhoosh API request failed: network error") from exc

            if getattr(response, "status_code", None) == 401 and attempt == 0:
                self._access_token_cache = None
                self._access_token_expiry = None
                self.state_db.set_value(self.access_token_key, "")
                self.state_db.set_value(self.token_expiry_key, "0")
                continue
            try:
                response.raise_for_status()
            except requests.RequestException as exc:
                status = getattr(response, "status_code", None)
                detail = f"HTTP {status}" if status is not None else "request error"
                raise RuntimeError(f"MyWhoosh API request failed: {detail}") from exc
            return _json_object(response, path)
        raise RuntimeError("MyWhoosh authentication failed after retry")


def _json_object(response: Any, operation: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"MyWhoosh {operation} response was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"MyWhoosh {operation} response must be a JSON object")
    return payload


def _activity_rows(payload: Mapping[str, Any], page: int) -> list[Mapping[str, Any]]:
    data = payload.get("data")
    if isinstance(data, Mapping):
        rows = data.get("results", data.get("data"))
    elif isinstance(data, list):
        rows = data
    else:
        rows = payload.get("results")
    if not isinstance(rows, list):
        code = payload.get("code", payload.get("status"))
        if code is not None:
            raise RuntimeError(f"MyWhoosh activity list failed on page {page} (code {code})")
        raise RuntimeError(f"MyWhoosh activity page {page} did not contain a results list")
    result: list[Mapping[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise RuntimeError(f"MyWhoosh activity page {page} item {index} must be an object")
        result.append(row)
    return result


def _ensure_success(payload: Mapping[str, Any], operation: str) -> None:
    accepted = {"0", "200", "ok", "success", "true"}
    for field in ("code", "status"):
        value = payload.get(field)
        if value is None:
            continue
        if str(value).strip().casefold() not in accepted:
            raise RuntimeError(f"MyWhoosh {operation} failed ({field} {value})")


def _activity_from_record(row: Mapping[str, Any], page: int, index: int) -> Activity:
    raw_id = row.get("id") or row.get("activityId")
    if raw_id in (None, ""):
        raise RuntimeError(f"MyWhoosh activity page {page} item {index} is missing its ID")
    file_id = row.get("activityFileId") or row.get("userFileId")
    if file_id in (None, ""):
        raise RuntimeError(f"MyWhoosh activity {raw_id} is missing its FIT file ID")
    source_id = str(raw_id)
    name_value = row.get("title")
    name = str(name_value).strip() if name_value not in (None, "") else "MyWhoosh Activity"
    raw = dict(row)
    raw["fileId"] = str(file_id)
    raw["originalData"] = dict(row)
    return Activity(
        source="mywhoosh",
        source_id=source_id,
        name=name,
        sport_type=_sport_type(row.get("sportType")),
        start_time=parse_datetime(row.get("date") or row.get("startDateTime")),
        raw=raw,
    )


def _file_id(activity: Activity) -> str:
    value = activity.raw.get("fileId")
    if value in (None, ""):
        value = activity.raw.get("activityFileId", activity.raw.get("userFileId"))
    if value in (None, ""):
        raise RuntimeError(f"MyWhoosh activity {activity.source_id} is missing its FIT file ID")
    return str(value)


def _download_url(payload: Mapping[str, Any], base_url: str) -> str:
    value = payload.get("data")
    if not isinstance(value, str) or not value.strip():
        code = payload.get("code", payload.get("status"))
        if code is not None:
            raise RuntimeError(f"MyWhoosh download URL request failed (code {code})")
        raise RuntimeError("MyWhoosh download URL response did not contain a URL")
    url = urljoin(f"{base_url.rstrip('/')}/", value.strip())
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise RuntimeError("MyWhoosh returned an invalid activity download URL")
    return url


def _pagination(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    data = payload.get("data")
    if isinstance(data, Mapping):
        return data
    return payload


def _sport_type(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return {
        "ride": "cycling",
        "cycling": "cycling",
        "run": "running",
        "running": "running",
    }.get(value.strip().casefold())


def _jwt_expiry(token: str) -> float | None:
    claims = _jwt_claims(token)
    if claims is None:
        return None
    try:
        return float(claims["exp"])
    except (KeyError, TypeError, ValueError):
        return None


def _jwt_claims(token: str) -> Mapping[str, Any] | None:
    parts = token.split(".")
    if len(parts) < 2:
        return None
    try:
        encoded = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")))
    except (ValueError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(claims, Mapping):
        return None
    return claims


def _jwt_user_id(token: str) -> str | int:
    claims = _jwt_claims(token)
    user_id = claims.get("userId") if claims is not None else None
    if isinstance(user_id, bool) or not isinstance(user_id, (str, int)):
        raise RuntimeError("MyWhoosh access token did not contain a user ID")
    if not str(user_id).strip():
        raise RuntimeError("MyWhoosh access token did not contain a user ID")
    return user_id


def _optional_json_object(response: Any, operation: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except (TypeError, ValueError):
        if not getattr(response, "content", b""):
            return {}
        raise RuntimeError(f"MyWhoosh {operation} response was not valid JSON")
    if payload is None and not getattr(response, "content", b""):
        return {}
    if not isinstance(payload, Mapping):
        raise RuntimeError(f"MyWhoosh {operation} response must be a JSON object")
    return dict(payload)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _activity_sort_key(activity: Activity) -> tuple[bool, float, str]:
    if activity.start_time is None:
        return (True, 0.0, activity.source_id)
    return (False, -activity.start_time.timestamp(), activity.source_id)
