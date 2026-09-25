from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import urlsplit

import requests


FEED_ENDPOINTS = {
    "user": "/feed/user",
    "nearby": "/feed/nearby",
    "newest": "/feed/newest",
    "hot": "/feed/hot",
    "follow": "/feed/follow",
}


class SocialFeedError(ValueError):
    pass


class SocialFeedClient:
    def __init__(
        self,
        base_url: str,
        auth_token: str,
        *,
        get: Callable[..., Any] | None = None,
    ) -> None:
        if not isinstance(base_url, str):
            raise SocialFeedError("social base URL must be a string")
        normalized_url = base_url.strip()
        try:
            parsed_url = urlsplit(normalized_url)
        except ValueError as exc:
            raise SocialFeedError("social base URL is invalid") from exc
        if (
            parsed_url.scheme not in {"http", "https"}
            or not parsed_url.netloc
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise SocialFeedError("social base URL must be an HTTP(S) origin or path without credentials")
        if not isinstance(auth_token, str) or not auth_token.strip():
            raise SocialFeedError("a non-empty Nakama session token is required")

        self._base_url = normalized_url.rstrip("/")
        self._auth_token = auth_token.strip()
        self._get = get if get is not None else requests.get

    def get_feed(
        self,
        kind: str,
        *,
        page: int = 0,
        limit: int = 40,
        user_id: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        following_ids: Sequence[str] | None = None,
    ) -> dict[str, object]:
        if not isinstance(kind, str) or kind not in FEED_ENDPOINTS:
            raise SocialFeedError(f"unsupported feed kind: {kind}")
        if not isinstance(page, int) or isinstance(page, bool) or page < 0:
            raise SocialFeedError("page must be zero or greater")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise SocialFeedError("limit must be greater than zero")

        params: dict[str, str | int | float] = {"page": page, "limit": limit}
        if kind == "user":
            if not isinstance(user_id, str) or not user_id.strip():
                raise SocialFeedError("the user feed requires --user-id")
            params["userId"] = user_id.strip()
        elif user_id is not None:
            raise SocialFeedError("--user-id is only valid for the user feed")

        if kind == "nearby":
            if latitude is None or longitude is None:
                raise SocialFeedError("the nearby feed requires --lat and --lng")
            if (
                isinstance(latitude, bool)
                or isinstance(longitude, bool)
                or not isinstance(latitude, (int, float))
                or not isinstance(longitude, (int, float))
                or not math.isfinite(latitude)
                or not math.isfinite(longitude)
                or not -90 <= latitude <= 90
                or not -180 <= longitude <= 180
            ):
                raise SocialFeedError("nearby feed coordinates are outside valid latitude/longitude ranges")
            params["lat"] = latitude
            params["lng"] = longitude
        elif latitude is not None or longitude is not None:
            raise SocialFeedError("--lat and --lng are only valid for the nearby feed")

        if following_ids is not None:
            if kind != "follow":
                raise SocialFeedError("following IDs are only valid for the follow feed")
            if isinstance(following_ids, (str, bytes)):
                raise SocialFeedError("following IDs must be a sequence of user ID strings")
            if not all(isinstance(value, str) for value in following_ids):
                raise SocialFeedError("following IDs must be a sequence of user ID strings")
            cleaned_ids = [value.strip() for value in following_ids if value.strip()]
            if cleaned_ids:
                params["userIds"] = ",".join(cleaned_ids)

        endpoint = FEED_ENDPOINTS[kind]
        try:
            response = self._get(
                f"{self._base_url}{endpoint}",
                params=params,
                headers={
                    "Authorization": f"Bearer {self._auth_token}",
                    "Content-Type": "application/json",
                },
                timeout=30,
            )
        except requests.RequestException as exc:
            raise SocialFeedError("social feed request failed") from exc

        if response.status_code != 200:
            raise SocialFeedError(f"GET {endpoint} failed with HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise SocialFeedError("social feed response was not valid JSON") from exc
        if not isinstance(payload, dict):
            raise SocialFeedError("social feed response must be a JSON object")
        return payload
