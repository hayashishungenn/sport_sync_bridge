from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import requests

from sport_sync_bridge.cli import build_parser
from sport_sync_bridge.config import AppConfig
from sport_sync_bridge.concept2_source import Concept2Source
from sport_sync_bridge.engine import SyncEngine
from sport_sync_bridge.models import Activity
from sport_sync_bridge.state import StateDB


class _Response:
    def __init__(
        self,
        *,
        payload: object = None,
        content: bytes = b"",
        status_code: int = 200,
    ):
        self.payload = payload
        self.content = content
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> object:
        return self.payload


class _FakeSession:
    def __init__(
        self,
        *,
        get_responses: list[_Response] | None = None,
        post_responses: list[_Response] | None = None,
        delete_responses: list[_Response] | None = None,
    ):
        self.headers: dict[str, str] = {}
        self.get_responses = list(get_responses or [])
        self.post_responses = list(post_responses or [])
        self.delete_responses = list(delete_responses or [])
        self.get_calls: list[tuple[str, dict[str, object]]] = []
        self.post_calls: list[tuple[str, dict[str, object]]] = []
        self.delete_calls: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, **kwargs: object) -> _Response:
        self.get_calls.append((url, kwargs))
        return self.get_responses.pop(0)

    def post(self, url: str, **kwargs: object) -> _Response:
        self.post_calls.append((url, kwargs))
        return self.post_responses.pop(0)

    def delete(self, url: str, **kwargs: object) -> _Response:
        self.delete_calls.append((url, kwargs))
        return self.delete_responses.pop(0)


class _MemoryState:
    def __init__(self, values: dict[str, str] | None = None):
        self.values = dict(values or {})

    def get_value(self, key: str) -> str | None:
        return self.values.get(key)

    def set_value(self, key: str, value: str) -> None:
        self.values[key] = value


def _fit_bytes() -> bytes:
    data = bytearray(128)
    data[0] = 14
    data[8:12] = b".FIT"
    return bytes(data)


class Concept2SourceTests(unittest.TestCase):
    def _source(
        self,
        *,
        state: _MemoryState | None = None,
        access_token: str | None = "access-token",
        scope: str | None = None,
    ) -> Concept2Source:
        config = SimpleNamespace(
            concept2_client_id="client-id",
            concept2_client_secret="client-secret",
            concept2_api_root="https://log.concept2.com",
            concept2_allow_production_writes=False,
            concept2_redirect_uri="http://localhost/callback",
            concept2_access_token=access_token,
            concept2_refresh_token=None,
            concept2_expires_at=None,
            concept2_scope=scope,
        )
        return Concept2Source(cast(AppConfig, config), cast(StateDB, state or _MemoryState()))

    def test_config_loads_concept2_oauth_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "CONCEPT2_CLIENT_ID": "client-id",
                "CONCEPT2_CLIENT_SECRET": "client-secret",
                "CONCEPT2_API_ROOT": "https://log-dev.concept2.com",
                "CONCEPT2_ALLOW_PRODUCTION_WRITES": "true",
                "CONCEPT2_REDIRECT_URI": "http://localhost/callback",
                "CONCEPT2_ACCESS_TOKEN": "access-token",
                "CONCEPT2_REFRESH_TOKEN": "refresh-token",
                "CONCEPT2_EXPIRES_AT": "1234567890",
                "CONCEPT2_SCOPE": "user:read,results:read",
            },
        ):
            config = AppConfig.load(Path(directory))

        self.assertEqual(config.concept2_client_id, "client-id")
        self.assertEqual(config.concept2_client_secret, "client-secret")
        self.assertEqual(config.concept2_api_root, "https://log-dev.concept2.com")
        self.assertTrue(config.concept2_allow_production_writes)
        self.assertEqual(config.concept2_redirect_uri, "http://localhost/callback")
        self.assertEqual(config.concept2_access_token, "access-token")
        self.assertEqual(config.concept2_refresh_token, "refresh-token")
        self.assertEqual(config.concept2_expires_at, "1234567890")
        self.assertEqual(config.concept2_scope, "user:read,results:read")

    def test_builds_read_only_and_opt_in_write_authorization_urls(self) -> None:
        source = self._source()

        read_url = urlparse(source.build_authorize_url())
        write_url = urlparse(source.build_authorize_url(write=True))

        self.assertEqual(read_url.path, "/oauth/authorize")
        self.assertEqual(parse_qs(read_url.query)["scope"], ["user:read,results:read"])
        self.assertEqual(parse_qs(write_url.query)["scope"], ["user:read,results:write"])
        self.assertEqual(parse_qs(read_url.query)["redirect_uri"], ["http://localhost/callback"])

    def test_exchanges_code_and_persists_rotating_tokens(self) -> None:
        state = _MemoryState()
        source = self._source(state=state, access_token=None)
        session = _FakeSession(
            post_responses=[
                _Response(
                    payload={
                        "access_token": "new-access",
                        "refresh_token": "new-refresh",
                        "expires_in": 3600,
                        "scope": "user:read,results:read",
                    }
                )
            ]
        )
        session.headers.update(source.session.headers)
        source.session = cast(requests.Session, session)

        source.exchange_code(" auth-code ")

        url, kwargs = session.post_calls[0]
        self.assertEqual(url, "https://log.concept2.com/oauth/access_token")
        self.assertEqual(kwargs["data"]["code"], "auth-code")
        self.assertEqual(kwargs["data"]["scope"], "user:read,results:read")
        self.assertEqual(state.get_value("concept2_access_token"), "new-access")
        self.assertEqual(state.get_value("concept2_refresh_token"), "new-refresh")
        self.assertGreater(float(state.get_value("concept2_expires_at") or 0), 0)

        write_state = _MemoryState()
        write_source = self._source(state=write_state, access_token=None)
        write_session = _FakeSession(
            post_responses=[
                _Response(
                    payload={
                        "access_token": "write-access",
                        "scope": "user:read,results:write",
                    }
                )
            ]
        )
        write_session.headers.update(write_source.session.headers)
        write_source.session = cast(requests.Session, write_session)
        write_source.exchange_code("write-code", write=True)
        self.assertEqual(write_session.post_calls[0][1]["data"]["scope"], "user:read,results:write")
        self.assertEqual(write_state.get_value("concept2_scope"), "user:read,results:write")

    def test_refreshes_expiring_access_token_before_api_request(self) -> None:
        state = _MemoryState(
            {
                "concept2_access_token": "old-access",
                "concept2_refresh_token": "old-refresh",
                "concept2_expires_at": "1",
                "concept2_scope": "user:read,results:read",
            }
        )
        source = self._source(state=state, access_token=None)
        session = _FakeSession(
            post_responses=[
                _Response(
                    payload={
                        "access_token": "rotated-access",
                        "refresh_token": "rotated-refresh",
                        "expires_in": 3600,
                        "scope": "user:read,results:read",
                    }
                )
            ],
            get_responses=[_Response(payload={"data": {"id": 42}})],
        )
        source.session = cast(requests.Session, session)

        source.authenticate()

        self.assertEqual(session.post_calls[0][1]["data"]["grant_type"], "refresh_token")
        self.assertEqual(session.get_calls[0][1]["headers"]["Authorization"], "Bearer rotated-access")
        self.assertEqual(state.get_value("concept2_refresh_token"), "rotated-refresh")

    def test_lists_filtered_results_across_pages_and_maps_sport_types(self) -> None:
        source = self._source()
        session = _FakeSession(
            get_responses=[
                _Response(payload={"data": {"id": 42}}),
                _Response(
                    payload={
                        "data": [
                            {
                                "id": 7,
                                "date": "2026-09-02 09:00:00",
                                "date_utc": "2026-09-02T09:00:00Z",
                                "type": "bike",
                                "distance": 5000,
                            }
                        ],
                        "meta": {"pagination": {"current_page": 1, "total_pages": 2}},
                    }
                ),
                _Response(
                    payload={
                        "data": [
                            {
                                "id": 8,
                                "date": "2026-09-03 09:00:00",
                                "date_utc": "2026-09-03T09:00:00Z",
                                "type": "rower",
                                "distance": 2000,
                            }
                        ],
                        "meta": {"pagination": {"current_page": 2, "total_pages": 2}},
                    }
                ),
            ]
        )
        session.headers.update(source.session.headers)
        source.session = cast(requests.Session, session)
        since = datetime(2026, 9, 1, tzinfo=timezone.utc)
        until = datetime(2026, 9, 5, tzinfo=timezone.utc)

        activities = source.list_activities(since, until, None)

        self.assertEqual([activity.source_id for activity in activities], ["7", "8"])
        self.assertEqual([activity.sport_type for activity in activities], ["cycling", "rowing"])
        self.assertEqual(activities[0].name, "Concept2 bike 5000m")
        self.assertEqual(session.headers["Accept"], "application/vnd.c2logbook.v1+json")
        profile_url, _ = session.get_calls[0]
        first_page_url, first_page_kwargs = session.get_calls[1]
        second_page_kwargs = session.get_calls[2][1]
        self.assertEqual(profile_url, "https://log.concept2.com/api/users/me")
        self.assertEqual(first_page_url, "https://log.concept2.com/api/users/me/results")
        self.assertEqual(
            first_page_kwargs["params"],
            {"number": 250, "from": "2026-09-01 00:00:00", "to": "2026-09-05 00:00:00", "page": 1},
        )
        self.assertEqual(second_page_kwargs["params"]["page"], 2)

    def test_stops_at_limit_and_returns_empty_without_authentication(self) -> None:
        source = self._source()
        source.session = cast(requests.Session, _FakeSession())

        self.assertEqual(source.list_activities(None, None, 0), [])
        self.assertEqual(source.session.get_calls, [])

    def test_rejects_invalid_results_payloads_and_missing_ids(self) -> None:
        cases = (
            ([], "JSON object"),
            ({"data": None}, "data list"),
            ({"data": [None]}, "JSON object"),
            ({"data": [{}]}, "missing its ID"),
        )
        for payload, message in cases:
            with self.subTest(message=message):
                source = self._source()
                source.session = cast(
                    requests.Session,
                    _FakeSession(
                        get_responses=[
                            _Response(payload={"data": {"id": 42}}),
                            _Response(payload=payload),
                        ]
                    ),
                )
                with self.assertRaisesRegex(RuntimeError, message):
                    source.list_activities(None, None, None)

    def test_downloads_and_caches_fit_export(self) -> None:
        source = self._source()
        session = _FakeSession(get_responses=[_Response(content=_fit_bytes())])
        source.session = cast(requests.Session, session)
        activity = Activity(source="concept2", source_id="42", name="Row")

        with tempfile.TemporaryDirectory() as directory:
            path = source.download_fit(activity, Path(directory))
            self.assertEqual(path.read_bytes(), _fit_bytes())
            self.assertEqual(path.name, "42.fit")
            self.assertEqual(source.download_fit(activity, Path(directory)), path)

        self.assertEqual(len(session.get_calls), 1)
        self.assertEqual(
            session.get_calls[0][0],
            "https://log.concept2.com/api/users/me/results/42/export/fit",
        )
        self.assertEqual(
            session.get_calls[0][1]["headers"]["Accept"], "application/octet-stream"
        )

    def test_download_rejects_missing_export_and_invalid_fit(self) -> None:
        activity = Activity(source="concept2", source_id="42", name="Row")
        responses = (
            (_Response(status_code=404), "FIT export is unavailable"),
            (_Response(content=b"not a FIT" * 20), "not a valid FIT"),
        )
        for response, message in responses:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                source = self._source()
                source.session = cast(requests.Session, _FakeSession(get_responses=[response]))
                with self.assertRaisesRegex(RuntimeError, message):
                    source.download_fit(activity, Path(directory))
                self.assertFalse((Path(directory) / "concept2" / "42.fit").exists())

    def test_delete_requires_write_scope_and_encodes_result_id(self) -> None:
        unknown_scope = self._source(scope=None)
        unknown_scope.api_root = unknown_scope.development_api_root
        unknown_session = _FakeSession()
        unknown_scope.session = cast(requests.Session, unknown_session)
        with self.assertRaisesRegex(RuntimeError, "scope is unknown"):
            unknown_scope.delete_result("42")
        self.assertEqual(unknown_session.delete_calls, [])

        read_only = self._source(scope="user:read,results:read")
        read_only.api_root = read_only.development_api_root
        read_session = _FakeSession()
        read_only.session = cast(requests.Session, read_session)
        with self.assertRaisesRegex(RuntimeError, "read-only"):
            read_only.delete_result("42")
        self.assertEqual(read_session.delete_calls, [])

        writable = self._source(scope="user:read,results:write")
        writable.api_root = writable.development_api_root
        write_session = _FakeSession(delete_responses=[_Response(payload={"message": "deleted"})])
        writable.session = cast(requests.Session, write_session)
        writable.delete_result("42/a")
        self.assertEqual(
            write_session.delete_calls[0][0],
            "https://log-dev.concept2.com/api/users/me/results/42%2Fa",
        )

    def test_production_delete_requires_explicit_approval_setting(self) -> None:
        source = self._source(scope="user:read,results:write")
        source.session = cast(requests.Session, _FakeSession())
        with self.assertRaisesRegex(RuntimeError, "approval before production writes"):
            source.delete_result("42")
        self.assertEqual(source.session.delete_calls, [])

    def test_cli_registers_concept2_sync_and_explicit_delete_commands(self) -> None:
        parser = build_parser()
        self.assertEqual(
            parser.parse_args(["sync", "--source", "concept2"]).source,
            ["concept2"],
        )
        self.assertEqual(
            parser.parse_args(["check", "--source", "concept2"]).source,
            ["concept2"],
        )
        self.assertTrue(parser.parse_args(["concept2-auth-url", "--write"]).write)
        delete_args = parser.parse_args(["concept2-delete", "--activity-id", "42"])
        self.assertEqual(delete_args.activity_id, "42")
        self.assertFalse(delete_args.yes)

    def test_engine_registers_configured_concept2_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "SYNC_DATA_DIR": str(Path(directory) / "data"),
                "SYNC_SOURCES": "concept2",
                "SYNC_TARGETS": "",
                "CONCEPT2_CLIENT_ID": "client-id",
                "CONCEPT2_CLIENT_SECRET": "client-secret",
            },
        ):
            config = AppConfig.load(Path(directory))
            engine = SyncEngine(config)
            try:
                self.assertIn("concept2", engine.sources)
                self.assertIsInstance(engine.get_concept2_source(), Concept2Source)
            finally:
                engine.close()


if __name__ == "__main__":
    unittest.main()
