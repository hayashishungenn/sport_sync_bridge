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
from sport_sync_bridge.hammerhead_api import HammerheadClient
from sport_sync_bridge.hammerhead_source import HammerheadSource
from sport_sync_bridge.hammerhead_target import HammerheadTarget
from sport_sync_bridge.models import Activity, UploadResult


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
    def __init__(self, *, get=None, post=None, delete=None):
        self.responses = {
            "get": list(get or []),
            "post": list(post or []),
            "delete": list(delete or []),
        }
        self.calls: list[tuple[str, str, dict]] = []
        self.headers: dict[str, str] = {}

    def _request(self, method: str, url: str, **kwargs):
        if method == "post" and "files" in kwargs:
            kwargs["uploaded_content"] = kwargs["files"]["file"][1].read()
        self.calls.append((method, url, kwargs))
        return self.responses[method].pop(0)

    def get(self, url: str, **kwargs):
        return self._request("get", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self._request("post", url, **kwargs)

    def delete(self, url: str, **kwargs):
        return self._request("delete", url, **kwargs)


class _StateDB:
    def __init__(self):
        self.values: dict[str, str] = {}

    def get_value(self, key: str):
        return self.values.get(key)

    def set_value(self, key: str, value: str) -> None:
        self.values[key] = value


class HammerheadTests(unittest.TestCase):
    def _config(self, **overrides):
        values = {
            "hammerhead_client_id": "client-id",
            "hammerhead_client_secret": "client-secret",
            "hammerhead_redirect_uri": "http://localhost/",
            "hammerhead_access_token": "access-token",
            "hammerhead_refresh_token": None,
            "hammerhead_expires_at": "4102444800",
            "hammerhead_scope": "activity:read route:read route:write",
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def _client(self, config=None, session=None, state_db=None):
        client = HammerheadClient(config or self._config(), state_db or _StateDB())
        client.session = session or _Session()
        return client

    def test_builds_oauth_url_and_persists_random_state(self) -> None:
        state_db = _StateDB()
        client = self._client(state_db=state_db)

        parsed = urlparse(client.build_authorize_url())
        query = parse_qs(parsed.query)

        self.assertEqual(parsed.path, "/v1/auth/oauth/authorize")
        self.assertEqual(query["client_id"], ["client-id"])
        self.assertEqual(query["redirect_uri"], ["http://localhost/"])
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["scope"], ["activity:read route:read route:write"])
        self.assertEqual(query["state"], [state_db.values[client.oauth_state_key]])
        self.assertGreaterEqual(len(query["state"][0]), 32)

    def test_exchanges_code_only_after_state_matches_and_persists_tokens(self) -> None:
        state_db = _StateDB()
        state_db.set_value(HammerheadClient.oauth_state_key, "expected-state")
        session = _Session(
            post=[
                _Response(
                    {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 7200}
                )
            ]
        )
        client = self._client(session=session, state_db=state_db)

        with self.assertRaisesRegex(ValueError, "state"):
            client.exchange_code("code", "wrong-state")
        payload = client.exchange_code(" code ", "expected-state")

        self.assertEqual(payload["access_token"], "new-access")
        self.assertEqual(session.calls[0][1], HammerheadClient.token_url)
        self.assertEqual(
            session.calls[0][2]["data"],
            {
                "client_id": "client-id",
                "client_secret": "client-secret",
                "code": "code",
                "redirect_uri": "http://localhost/",
                "grant_type": "authorization_code",
            },
        )
        self.assertEqual(state_db.values["hammerhead_access_token"], "new-access")
        self.assertEqual(state_db.values["hammerhead_refresh_token"], "new-refresh")
        self.assertEqual(state_db.values[HammerheadClient.oauth_state_key], "")
        self.assertGreater(float(state_db.values["hammerhead_expires_at"]), datetime.now(timezone.utc).timestamp())

    def test_lists_activities_with_documented_pagination_and_date_filters(self) -> None:
        session = _Session(
            get=[
                _Response(
                    {
                        "data": [
                            {
                                "id": "ride-1",
                                "name": "Morning ride",
                                "createdAt": "2026-09-24T12:00:00Z",
                                "activityType": "MOUNTAIN_BIKE",
                            },
                            {"id": "old", "createdAt": "2026-09-23T12:00:00Z"},
                        ],
                        "totalPages": 2,
                    }
                ),
                _Response(
                    {
                        "data": [{"id": "future", "createdAt": "2026-09-25T00:00:01Z"}],
                        "totalPages": 2,
                    }
                ),
            ]
        )
        source = HammerheadSource(self._config(), self._client(session=session))

        activities = source.list_activities(
            datetime(2026, 9, 24, tzinfo=timezone.utc),
            datetime(2026, 9, 25, tzinfo=timezone.utc),
            None,
        )

        self.assertEqual([activity.source_id for activity in activities], ["ride-1"])
        self.assertEqual(activities[0].sport_type, "mountain_biking")
        self.assertEqual(session.calls[0][1], "https://api.hammerhead.io/v1/api/activities")
        self.assertEqual(
            session.calls[0][2]["params"],
            {"perPage": 100, "startDate": "2026-09-24", "page": 1},
        )
        self.assertEqual(session.calls[1][2]["params"]["page"], 2)

    def test_lists_routes_and_respects_limit(self) -> None:
        session = _Session(
            get=[
                _Response({"data": [{"id": "route-1"}, {"id": "route-2"}], "totalPages": 2}),
                _Response({"data": [{"id": "route-3"}], "totalPages": 2}),
            ]
        )
        source = HammerheadSource(self._config(), self._client(session=session))

        routes = source.list_routes(limit=2)

        self.assertEqual([route["id"] for route in routes], ["route-1", "route-2"])
        self.assertEqual(session.calls[0][1], "https://api.hammerhead.io/v1/api/routes")
        self.assertEqual(session.calls[0][2]["params"], {"perPage": 100, "page": 1})

    def test_keeps_unrecognized_activity_types_unclassified(self) -> None:
        session = _Session(get=[_Response({"data": [{"id": "ride-1", "activityType": "FUTURE_TYPE"}], "totalPages": 1})])
        source = HammerheadSource(self._config(), self._client(session=session))

        activities = source.list_activities(None, None, None)

        self.assertEqual(activities[0].sport_type, None)

    def test_downloads_and_caches_a_valid_fit_file(self) -> None:
        fit_data = bytearray(64)
        fit_data[8:12] = b".FIT"
        session = _Session(get=[_Response(content=bytes(fit_data))])
        source = HammerheadSource(self._config(), self._client(session=session))
        activity = Activity(source="hammerhead", source_id="ride-1", name="Ride")

        with tempfile.TemporaryDirectory() as directory:
            fit_path = source.download_fit(activity, Path(directory))
            cached_path = source.download_fit(activity, Path(directory))

            self.assertEqual(fit_path, cached_path)
            self.assertEqual(fit_path.read_bytes(), bytes(fit_data))
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(
            session.calls[0][1],
            "https://api.hammerhead.io/v1/api/activities/ride-1/file",
        )

    def test_rejects_invalid_download_without_leaving_a_partial_file(self) -> None:
        source = HammerheadSource(
            self._config(), self._client(session=_Session(get=[_Response(content=b"not a FIT")]))
        )
        activity = Activity(source="hammerhead", source_id="ride-1", name="Ride")

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "invalid FIT"):
                source.download_fit(activity, Path(directory))
            self.assertEqual(list(Path(directory).rglob("*fit*")), [])

    def test_uploads_route_file_and_returns_remote_id(self) -> None:
        session = _Session(post=[_Response({"id": "new-route"}, status_code=201)])
        target = HammerheadTarget(self._config(), self._client(session=session))
        activity = Activity(source="local", source_id="ride-1", name="Morning ride")

        with tempfile.TemporaryDirectory() as directory:
            gpx_path = Path(directory) / "morning.gpx"
            gpx_path.write_text("<gpx />", encoding="utf-8")
            result = target.upload_file(gpx_path, activity, external_id="local:ride-1")

        self.assertEqual(result, UploadResult(status="success", remote_id="new-route"))
        self.assertEqual(session.calls[0][1], "https://api.hammerhead.io/v1/api/routes/file")
        self.assertEqual(session.calls[0][2]["uploaded_content"], b"<gpx />")

    def test_rejects_unsupported_route_format(self) -> None:
        session = _Session()
        target = HammerheadTarget(self._config(), self._client(session=session))
        activity = Activity(source="local", source_id="ride-1", name="Morning ride")

        with tempfile.TemporaryDirectory() as directory:
            json_path = Path(directory) / "activity.json"
            json_path.write_text("{}", encoding="utf-8")
            result = target.upload_file(json_path, activity, external_id="local:ride-1")

        self.assertEqual(result.status, "failed")
        self.assertIn("FIT, GPX, or TCX", result.message or "")
        self.assertEqual(session.calls, [])

    def test_deletes_only_through_documented_route_endpoint(self) -> None:
        session = _Session(delete=[_Response(status_code=204)])
        target = HammerheadTarget(self._config(), self._client(session=session))

        target.delete_route("route/a b")

        self.assertEqual(
            session.calls[0][1],
            "https://api.hammerhead.io/v1/api/routes/route%2Fa%20b",
        )

    def test_refreshes_token_after_unauthorized_response(self) -> None:
        state_db = _StateDB()
        session = _Session(
            get=[_Response(status_code=401), _Response({"data": [], "totalPages": 1})],
            post=[_Response({"access_token": "fresh", "refresh_token": "rotated", "expires_in": 3600})],
        )
        client = self._client(
            config=self._config(
                hammerhead_access_token="expired",
                hammerhead_refresh_token="old-refresh",
                hammerhead_expires_at="4102444800",
            ),
            session=session,
            state_db=state_db,
        )

        response = client.api_request("get", "activities", params={"page": 1})

        response.raise_for_status()
        self.assertEqual(session.calls[0][2]["headers"]["Authorization"], "Bearer expired")
        self.assertEqual(session.calls[1][2]["data"]["grant_type"], "refresh_token")
        self.assertEqual(session.calls[2][2]["headers"]["Authorization"], "Bearer fresh")
        self.assertEqual(state_db.values["hammerhead_refresh_token"], "rotated")

    def test_engine_registers_hammerhead_as_source_and_route_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "SYNC_DATA_DIR": ".data",
                "HAMMERHEAD_CLIENT_ID": "client-id",
                "HAMMERHEAD_CLIENT_SECRET": "client-secret",
                "HAMMERHEAD_ACCESS_TOKEN": "access-token",
                "HAMMERHEAD_EXPIRES_AT": "4102444800",
            },
        ):
            config = AppConfig.load(Path(directory))
            engine = SyncEngine(config)
            try:
                self.assertIn("hammerhead", engine.sources)
                self.assertIn("hammerhead", engine.targets)
                with patch.object(engine.sources["hammerhead"], "list_activities", return_value=[]):
                    count = engine.sync_once(
                        sources=["hammerhead"],
                        targets=["hammerhead"],
                        dry_run=True,
                        target_formats={"hammerhead": "tcx"},
                    )
                self.assertEqual(count, 0)
            finally:
                engine.close()

    def test_cli_accepts_hammerhead_source_target_and_oauth_state(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            ["sync", "--source", "hammerhead", "--target", "hammerhead", "--format", "hammerhead=tcx", "--dry-run"]
        )
        exchange = parser.parse_args(
            ["hammerhead-exchange", "--code", "auth-code", "--state", "csrf-state"]
        )

        self.assertEqual(args.source, ["hammerhead"])
        self.assertEqual(args.target, ["hammerhead"])
        self.assertEqual(args.target_formats, [("hammerhead", "tcx")])
        self.assertEqual(exchange.state, "csrf-state")


if __name__ == "__main__":
    unittest.main()
