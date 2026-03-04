from __future__ import annotations

import hashlib
import time
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

import requests

from .config import AppConfig
from .models import Activity
from .utils import (
    fit_signature_ok,
    maybe_relative_url,
    parse_cookie_string,
    parse_datetime,
    safe_filename,
)


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)


class SourceAdapter(ABC):
    name: str

    def __init__(self, config: AppConfig):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    @abstractmethod
    def is_configured(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def authenticate(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def list_activities(
        self,
        since: datetime | None,
        until: datetime | None,
        limit: int | None,
    ) -> list[Activity]:
        raise NotImplementedError

    @abstractmethod
    def download_fit(self, activity: Activity, output_dir: Path) -> Path:
        raise NotImplementedError


class IGPSportSource(SourceAdapter):
    name = "igpsport"
    api_root = "https://prod.zh.igpsport.com"

    def __init__(self, config: AppConfig):
        super().__init__(config)
        self.access_token: str | None = config.igpsport_access_token

    def is_configured(self) -> bool:
        return bool(
            self.config.igpsport_access_token
            or (self.config.igpsport_username and self.config.igpsport_password)
        )

    def authenticate(self) -> None:
        if self.access_token:
            return

        if not self.config.igpsport_username or not self.config.igpsport_password:
            raise RuntimeError("IGPSPORT credentials are not configured")

        response = self.session.post(
            f"{self.api_root}/service/auth/account/login",
            json={
                "username": self.config.igpsport_username,
                "password": self.config.igpsport_password,
                "appId": "igpsport-web",
            },
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://app.zh.igpsport.com",
                "Referer": "https://app.zh.igpsport.com/",
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 0:
            raise RuntimeError(f"IGPSPORT login failed: {data.get('message', 'unknown error')}")
        self.access_token = data["data"]["access_token"]

    def list_activities(
        self,
        since: datetime | None,
        until: datetime | None,
        limit: int | None,
    ) -> list[Activity]:
        self.authenticate()
        activities: list[Activity] = []
        page = 1
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Authorization": f"Bearer {self.access_token}",
            "Origin": "https://app.zh.igpsport.com",
            "Referer": "https://app.zh.igpsport.com/",
            "timezone": "Asia/Shanghai",
            "qiwu-app-version": "1.0.0",
        }

        begin = since or None
        end = until or datetime.now(timezone.utc)

        while True:
            params = {
                "pageNo": page,
                "pageSize": 20,
                "reqType": 0,
                "sort": 1,
            }
            if begin or until:
                params["beginTime"] = (begin or datetime(2000, 1, 1, tzinfo=timezone.utc)).strftime("%Y-%m-%d")
                params["endTime"] = end.strftime("%Y-%m-%d")

            response = self.session.get(
                f"{self.api_root}/service/web-gateway/web-analyze/activity/queryMyActivity",
                params=params,
                headers=headers,
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") != 0:
                raise RuntimeError(f"IGPSPORT list failed: {payload.get('message', 'unknown error')}")

            data = payload.get("data") or {}
            rows = data.get("rows") or []
            if not rows:
                break

            for item in rows:
                activity = Activity(
                    source=self.name,
                    source_id=str(item.get("rideId")),
                    name=item.get("title") or f"IGPSPORT-{item.get('rideId')}",
                    sport_type=_igpsport_sport_type(item.get("exerciseType") or item.get("sportType")),
                    start_time=parse_datetime(
                        item.get("startDateTime")
                        or item.get("startTime")
                        or item.get("startTimeString")
                        or item.get("createdTime")
                    ),
                    raw=item,
                )
                if since and activity.start_time and activity.start_time < since:
                    continue
                if until and activity.start_time and activity.start_time > until:
                    continue
                activities.append(activity)
                if limit and len(activities) >= limit:
                    return sorted(activities, key=_activity_sort_key)

            if page >= int(data.get("totalPage") or 1):
                break
            page += 1
            time.sleep(0.2)

        return sorted(activities, key=_activity_sort_key)

    def download_fit(self, activity: Activity, output_dir: Path) -> Path:
        self.authenticate()
        activity_dir = output_dir / self.name
        activity_dir.mkdir(parents=True, exist_ok=True)
        path = activity_dir / f"{safe_filename(activity.source_id)}.fit"
        if path.exists() and path.stat().st_size > 100:
            return path

        response = self.session.get(
            f"{self.api_root}/service/web-gateway/web-analyze/activity/getDownloadUrl/{activity.source_id}",
            headers={"Authorization": f"Bearer {self.access_token}"},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise RuntimeError(
                f"IGPSPORT download URL failed for {activity.source_id}: {payload.get('message', 'unknown error')}"
            )

        download_url = payload.get("data")
        if not download_url:
            raise RuntimeError(f"IGPSPORT download URL missing for {activity.source_id}")

        file_response = self.session.get(download_url, timeout=120)
        file_response.raise_for_status()
        path.write_bytes(file_response.content)
        if path.stat().st_size < 100:
            raise RuntimeError(f"Downloaded FIT is too small for {activity.source_id}")
        return path


class OneLapSource(SourceAdapter):
    name = "onelap"
    sign_key = "fe9f8382418fcdeb136461cac6acae7b"

    def __init__(self, config: AppConfig):
        super().__init__(config)
        self.session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://u.onelap.cn",
                "Referer": "https://u.onelap.cn/analysis",
            }
        )
        self._authenticated = False

    def is_configured(self) -> bool:
        return bool(
            self.config.onelap_cookie
            or (self.config.onelap_username and self.config.onelap_password)
        )

    def authenticate(self) -> None:
        if self._authenticated:
            return

        if self.config.onelap_cookie:
            self._load_cookie_string(self.config.onelap_cookie)
            self._authenticated = True
            return

        if not self.config.onelap_username or not self.config.onelap_password:
            raise RuntimeError("OneLap credentials are not configured")

        password_md5 = hashlib.md5(self.config.onelap_password.encode("utf-8")).hexdigest()
        nonce = uuid.uuid4().hex[16:]
        timestamp = str(int(time.time()))
        signature_raw = (
            f"account={self.config.onelap_username}"
            f"&nonce={nonce}"
            f"&password={password_md5}"
            f"&timestamp={timestamp}"
            f"&key={self.sign_key}"
        )
        sign = hashlib.md5(signature_raw.encode("utf-8")).hexdigest()
        response = self.session.post(
            "https://www.onelap.cn/api/login",
            json={"account": self.config.onelap_username, "password": password_md5},
            headers={"nonce": nonce, "timestamp": timestamp, "sign": sign},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 200:
            raise RuntimeError(f"OneLap login failed: {payload.get('error') or payload.get('msg') or payload}")

        data = payload.get("data") or []
        first = data[0] if isinstance(data, list) and data else data
        if not isinstance(first, dict):
            raise RuntimeError("OneLap login did not return token data")

        token = first.get("token")
        refresh_token = first.get("refresh_token")
        uid = (first.get("userinfo") or {}).get("uid")
        if token:
            self.session.cookies.set("XSRF-TOKEN", token, domain=".onelap.cn")
            self.session.headers["X-XSRF-TOKEN"] = token
        if refresh_token:
            self.session.cookies.set("OTOKEN", refresh_token, domain=".onelap.cn")
        if uid:
            self.session.cookies.set("ouid", str(uid), domain=".onelap.cn")
        self._authenticated = True

    def list_activities(
        self,
        since: datetime | None,
        until: datetime | None,
        limit: int | None,
    ) -> list[Activity]:
        self.authenticate()
        response = self.session.get("https://u.onelap.cn/analysis/list", timeout=30)
        response.raise_for_status()
        payload = response.json()
        items = payload.get("data")
        if items is None:
            raise RuntimeError(f"OneLap list failed: {payload}")

        activities: list[Activity] = []
        for item in reversed(list(items)):
            source_id = str(item.get("fileKey") or item.get("created_at") or item.get("id"))
            activity = Activity(
                source=self.name,
                source_id=source_id,
                name=item.get("name") or f"{item.get('date', 'onelap')} ride",
                sport_type="cycling",
                start_time=parse_datetime(item.get("created_at") or item.get("date")),
                raw=item,
            )
            if since and activity.start_time and activity.start_time < since:
                continue
            if until and activity.start_time and activity.start_time > until:
                continue
            activities.append(activity)
            if limit and len(activities) >= limit:
                break

        return sorted(activities, key=_activity_sort_key)

    def download_fit(self, activity: Activity, output_dir: Path) -> Path:
        self.authenticate()
        activity_dir = output_dir / self.name
        activity_dir.mkdir(parents=True, exist_ok=True)
        path = activity_dir / f"{safe_filename(activity.source_id)}.fit"
        if path.exists() and path.stat().st_size > 100:
            return path

        raw_url = activity.raw.get("durl")
        if not raw_url:
            raise RuntimeError(f"OneLap activity {activity.source_id} has no durl")
        download_url = maybe_relative_url("https://u.onelap.cn", raw_url)

        response = self.session.get(download_url, timeout=120)
        if response.status_code >= 400 or "text/html" in response.headers.get("Content-Type", "").lower():
            response = self.session.post(download_url, timeout=120)
        response.raise_for_status()
        path.write_bytes(response.content)

        if path.stat().st_size < 100:
            raise RuntimeError(f"Downloaded FIT is too small for {activity.source_id}")
        if not fit_signature_ok(path) and path.stat().st_size < 1024:
            raise RuntimeError(f"Downloaded file does not look like FIT for {activity.source_id}")
        return path

    def _load_cookie_string(self, cookie_string: str) -> None:
        for key, value in parse_cookie_string(cookie_string).items():
            self.session.cookies.set(key, value, domain=".onelap.cn")
        xsrf_token = self.session.cookies.get("XSRF-TOKEN")
        if xsrf_token:
            self.session.headers["X-XSRF-TOKEN"] = xsrf_token


def _igpsport_sport_type(value: object) -> str | None:
    mapping = {
        0: "cycling",
        1: "running",
        2: "walking",
        3: "hiking",
        4: "swimming",
        6: "indoor_cycling",
    }
    try:
        return mapping.get(int(value))
    except (TypeError, ValueError):
        return None


def _activity_sort_key(activity: Activity) -> tuple[datetime, str]:
    return (activity.start_time or datetime(1970, 1, 1, tzinfo=timezone.utc), activity.source_id)
