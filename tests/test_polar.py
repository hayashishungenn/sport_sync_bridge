from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import requests

from sport_sync_bridge.cli import build_parser
from sport_sync_bridge.config import AppConfig
from sport_sync_bridge.engine import SyncEngine
from sport_sync_bridge.models import Activity
from sport_sync_bridge.polar_api import PolarClient
from sport_sync_bridge.polar_source import PolarSource


class _Response:
    def __init__(self, payload=None, status_code: int = 200, content: bytes | None = None):
        self.payload = payload
        self.status_code = status_code
        self.content = (
            content
            if content is not None
            else json.dumps(payload).encode("utf-8")
            if payload is not None
            else b""
        )

    def json(self):
        if self.payload is None:
            raise ValueError("response did not contain JSON")
        return self.payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class _Session:
    def __init__(self, *, get=None, post=None):
        self.responses = {"get": list(get or []), "post": list(post or [])}
        self.calls: list[tuple[str, str, dict]] = []
        self.headers: dict[str, str] = {}

    def _request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses[method].pop(0)

    def get(self, url: str, **kwargs):
        return self._request("get", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self._request("post", url, **kwargs)


class _StateDB:
    def __init__(self, values=None):
        self.values: dict[str, str] = dict(values or {})

    def get_value(self, key: str):
        return self.values.get(key)

    def set_value(self, key: str, value: str) -> None:
        self.values[key] = value


class PolarAccessLinkTests(unittest.TestCase):
    def _config(self, **overrides):
        values = {
            "polar_client_id": "polar-client",
            "polar_client_secret": "polar-secret",
            "polar_redirect_uri": "http://localhost/callback",
            "polar_access_token": None,
            "polar_member_id": None,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def _client(self, *, config=None, state=None, session=None):
        client = PolarClient(config or self._config(), state or _StateDB())
        client.session = session or _Session()
        return client

    def _registered_state(self):
        return _StateDB(
            {
                "polar_access_token": "access-token",
                "polar_x_user_id": "123",
                "polar_registered_user_id": "123",
                "polar_registered_member_id": "sport_sync_bridge_123",
            }
        )

    def test_builds_oauth_url_and_persists_csrf_state(self) -> None:
        state = _StateDB()
        client = self._client(state=state)

        parsed = urlparse(client.build_authorize_url())
        query = parse_qs(parsed.query)

        self.assertEqual(parsed.geturl().split("?")[0], PolarClient.authorization_url)
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["client_id"], ["polar-client"])
        self.assertEqual(query["scope"], ["accesslink.read_all"])
        self.assertEqual(query["redirect_uri"], ["http://localhost/callback"])
        self.assertEqual(query["state"], [state.values[PolarClient.oauth_state_key]])

    def test_rejects_mismatched_state_before_token_request(self) -> None:
        state = _StateDB({PolarClient.oauth_state_key: "expected"})
        session = _Session()
        client = self._client(state=state, session=session)

        with self.assertRaisesRegex(ValueError, "state"):
            client.exchange_code("authorization-code", "wrong")
        self.assertEqual(session.calls, [])

    def test_exchanges_code_registers_user_and_persists_token(self) -> None:
        state = _StateDB({PolarClient.oauth_state_key: "expected"})
        session = _Session(
            post=[
                _Response({"access_token": "new-token", "x_user_id": 123, "expires_in": 31535999}),
                _Response({"polar-user-id": 456, "member-id": "sport_sync_bridge_123"}),
            ]
        )
        client = self._client(state=state, session=session)

        payload = client.exchange_code(" code ", "expected")

        self.assertEqual(payload["access_token"], "new-token")
        self.assertEqual(session.calls[0][1], PolarClient.token_url)
        self.assertEqual(session.calls[0][2]["auth"], ("polar-client", "polar-secret"))
        self.assertEqual(
            session.calls[0][2]["data"],
            {
                "grant_type": "authorization_code",
                "code": "code",
                "redirect_uri": "http://localhost/callback",
            },
        )
        self.assertEqual(session.calls[1][1], f"{PolarClient.api_root}/users")
        self.assertEqual(session.calls[1][2]["json"], {"member-id": "sport_sync_bridge_123"})
        self.assertEqual(
            session.calls[1][2]["headers"]["Authorization"], "Bearer new-token"
        )
        self.assertEqual(state.values["polar_access_token"], "new-token")
        self.assertEqual(state.values["polar_registered_user_id"], "123")
        self.assertEqual(state.values["polar_api_user_id"], "456")
        self.assertEqual(state.values[PolarClient.oauth_state_key], "")

    def test_registration_is_idempotent_for_the_same_polar_account(self) -> None:
        session = _Session()
        client = self._client(state=self._registered_state(), session=session)

        client.register_user()

        self.assertEqual(session.calls, [])

    def test_does_not_accept_an_unverified_registration_conflict(self) -> None:
        state = _StateDB({"polar_access_token": "token", "polar_x_user_id": "123"})
        client = self._client(
            config=self._config(polar_member_id="custom-member"),
            state=state,
            session=_Session(post=[_Response(status_code=409)]),
        )

        with self.assertRaisesRegex(RuntimeError, "already registered"):
            client.register_user()

    def test_recovers_existing_default_registration_after_local_state_loss(self) -> None:
        state = _StateDB({"polar_access_token": "token", "polar_x_user_id": "123"})
        client = self._client(
            state=state,
            session=_Session(post=[_Response(status_code=409)]),
        )

        client.register_user()

        self.assertEqual(state.values["polar_registered_user_id"], "123")
        self.assertEqual(state.values["polar_registered_member_id"], "sport_sync_bridge_123")

    def test_lists_and_filters_exercises_using_timezone_offset(self) -> None:
        rows = [
            {
                "id": "run-1",
                "sport": "RUNNING",
                "start_time": "2026-09-24T09:00:00",
                "start_time_utc_offset": 120,
            },
            {
                "id": "old-run",
                "sport": "RUNNING",
                "start_time": "2026-09-24T07:00:00",
                "start_time_utc_offset": 120,
            },
        ]
        session = _Session(get=[_Response(rows)])
        client = self._client(state=self._registered_state(), session=session)
        source = PolarSource(self._config(), client)

        activities = source.list_activities(
            datetime(2026, 9, 24, 6, 30, tzinfo=timezone.utc),
            datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc),
            limit=10,
        )

        self.assertEqual([activity.source_id for activity in activities], ["run-1"])
        self.assertEqual(activities[0].sport_type, "running")
        self.assertEqual(activities[0].start_time.utcoffset().total_seconds(), 7200)
        self.assertEqual(session.calls[0][1], f"{PolarClient.api_root}/exercises")

    def test_downloads_and_caches_valid_fit_data(self) -> None:
        fit_data = bytearray(64)
        fit_data[8:12] = b".FIT"
        session = _Session(get=[_Response(content=bytes(fit_data))])
        client = self._client(state=self._registered_state(), session=session)
        source = PolarSource(self._config(), client)
        activity = Activity(source="polar", source_id="fit/id", name="Polar RUNNING")

        with tempfile.TemporaryDirectory() as directory:
            path = source.download_fit(activity, Path(directory))
            cached = source.download_fit(activity, Path(directory))

            self.assertEqual(path, cached)
            self.assertEqual(path.read_bytes(), bytes(fit_data))
        self.assertEqual(len(session.calls), 1)
        self.assertIn("fit%2Fid/fit", session.calls[0][1])

    def test_rejects_invalid_fit_without_leaving_partial_file(self) -> None:
        source = PolarSource(
            self._config(),
            self._client(
                state=self._registered_state(),
                session=_Session(get=[_Response(content=b"not FIT")]),
            ),
        )
        activity = Activity(source="polar", source_id="exercise-1", name="Polar RUNNING")

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "invalid FIT"):
                source.download_fit(activity, Path(directory))
            self.assertEqual(list(Path(directory).rglob("*fit*")), [])

    def test_engine_registers_polar_when_a_token_is_configured(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"SYNC_DATA_DIR": ".data", "POLAR_ACCESS_TOKEN": "local-token"},
        ):
            config = AppConfig.load(Path(directory))
            engine = SyncEngine(config)
            try:
                self.assertIn("polar", engine.sources)
                with patch.object(engine.sources["polar"], "list_activities", return_value=[]):
                    count = engine.sync_once(
                        sources=["polar"],
                        targets=[],
                        dry_run=True,
                    )
                self.assertEqual(count, 0)
            finally:
                engine.close()

    def test_cli_accepts_polar_source_and_oauth_state(self) -> None:
        parser = build_parser()

        sync_args = parser.parse_args(
            ["sync", "--source", "polar", "--target", "garmin", "--dry-run"]
        )
        exchange_args = parser.parse_args(
            ["polar-exchange", "--code", "auth-code", "--state", "csrf-state"]
        )
        check_args = parser.parse_args(
            ["check", "--source", "polar", "--target", "hammerhead"]
        )

        self.assertEqual(sync_args.source, ["polar"])
        self.assertEqual(exchange_args.state, "csrf-state")
        self.assertEqual(check_args.source, ["polar"])
        self.assertEqual(check_args.target, ["hammerhead"])


if __name__ == "__main__":
    unittest.main()
