from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import requests


FRIENDS_ENDPOINT = "/v2/friend"


class NakamaFriendsError(ValueError):
    pass


class NakamaFriendsClient:
    def __init__(
        self,
        base_url: str,
        auth_token: str,
        *,
        get: Callable[..., Any] | None = None,
        post: Callable[..., Any] | None = None,
        delete: Callable[..., Any] | None = None,
    ) -> None:
        if not isinstance(base_url, str):
            raise NakamaFriendsError("Nakama base URL must be a string")
        normalized_url = base_url.strip()
        try:
            parsed_url = urlsplit(normalized_url)
        except ValueError as exc:
            raise NakamaFriendsError("Nakama base URL is invalid") from exc
        if (
            parsed_url.scheme not in {"http", "https"}
            or not parsed_url.netloc
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise NakamaFriendsError(
                "Nakama base URL must be an HTTP(S) origin or path without credentials"
            )
        if not isinstance(auth_token, str) or not auth_token.strip():
            raise NakamaFriendsError("a non-empty Nakama session token is required")

        self._url = f"{normalized_url.rstrip('/')}{FRIENDS_ENDPOINT}"
        self._auth_token = auth_token.strip()
        self._get = get if get is not None else requests.get
        self._post = post if post is not None else requests.post
        self._delete = delete if delete is not None else requests.delete

    def list_friends(
        self,
        *,
        limit: int = 2000,
        cursor: str | None = None,
        state: int | None = None,
    ) -> dict[str, object]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise NakamaFriendsError("limit must be a positive integer")

        params: dict[str, str | int] = {"limit": limit}
        if cursor is not None:
            if not isinstance(cursor, str) or not cursor.strip():
                raise NakamaFriendsError("cursor must be a non-empty string")
            params["cursor"] = cursor.strip()
        if state is not None:
            if not isinstance(state, int) or isinstance(state, bool) or state < 0:
                raise NakamaFriendsError("state must be a non-negative integer")
            params["state"] = state

        response = self._request(self._get, method="GET", params=params)
        try:
            payload = response.json()
        except ValueError as exc:
            raise NakamaFriendsError("Nakama friends response was not valid JSON") from exc
        if not isinstance(payload, dict):
            raise NakamaFriendsError("Nakama friends response must be a JSON object")
        return payload

    def add_friend(self, user_id: str) -> None:
        self._change_friend(self._post, "POST", user_id)

    def remove_friend(self, user_id: str) -> None:
        self._change_friend(self._delete, "DELETE", user_id)

    def _change_friend(
        self, request: Callable[..., Any], method: str, user_id: str
    ) -> None:
        if not isinstance(user_id, str) or not user_id.strip():
            raise NakamaFriendsError("user ID must be a non-empty string")
        self._request(request, method=method, json_body={"ids": [user_id.strip()]})

    def _request(
        self,
        request: Callable[..., Any],
        *,
        method: str,
        params: dict[str, str | int] | None = None,
        json_body: dict[str, list[str]] | None = None,
    ) -> Any:
        request_options: dict[str, object] = {
            "headers": {
                "Authorization": f"Bearer {self._auth_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            "timeout": 30,
        }
        if params is not None:
            request_options["params"] = params
        if json_body is not None:
            request_options["json"] = json_body

        try:
            response = request(self._url, **request_options)
        except requests.RequestException as exc:
            raise NakamaFriendsError("Nakama friends request failed") from exc
        if not 200 <= response.status_code < 300:
            raise NakamaFriendsError(
                f"{method} {FRIENDS_ENDPOINT} failed with HTTP {response.status_code}"
            )
        return response
