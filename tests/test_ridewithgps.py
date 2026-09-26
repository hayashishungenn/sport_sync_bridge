from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import requests

from sport_sync_bridge.cli import build_parser, main as cli_main
from sport_sync_bridge.config import AppConfig
from sport_sync_bridge.engine import SyncEngine
from sport_sync_bridge.models import Activity, UploadResult
from sport_sync_bridge.ridewithgps_api import RideWithGPSClient
from sport_sync_bridge.ridewithgps_source import RideWithGPSSource
from sport_sync_bridge.ridewithgps_target import RideWithGPSTarget
from sport_sync_bridge.state import StateDB
from sport_sync_bridge.utils import fit_signature_ok
from tests.activity_fixtures import create_fit


class _Response:
    def __init__(
        self,
        payload: object | None = None,
        *,
        content: bytes = b"",
        status_code: int = 200,
    ):
        self.payload = payload
        self.content = content
        self.status_code = status_code
        self.headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> object:
        if self.payload is None:
            raise ValueError("response did not contain JSON")
        return self.payload


class _Session:
    def __init__(self, *, request_responses=None, post_responses=None):
        self.headers: dict[str, str] = {}
        self.request_responses = list(request_responses or [])
        self.post_responses = list(post_responses or [])
        self.request_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.post_calls: list[tuple[str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs):
        self.request_calls.append((method, url, kwargs))
        return self.request_responses.pop(0)

    def post(self, url: str, **kwargs):
        self.post_calls.append((url, kwargs))
        return self.post_responses.pop(0)


class _StateDB:
    def __init__(self):
        self.values: dict[str, str] = {}

    def get_value(self, key: str) -> str | None:
        return self.values.get(key)

    def set_value(self, key: str, value: str) -> None:
        self.values[key] = value


class RideWithGPSApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = _StateDB()
        self.config = SimpleNamespace(
            ridewithgps_client_id="client-id",
            ridewithgps_client_secret="client-secret",
            ridewithgps_redirect_uri="http://localhost/callback",
            ridewithgps_access_token=None,
        )
        self.client = RideWithGPSClient(self.config, self.state)

    def test_authorization_url_uses_registered_redirect_uri(self) -> None:
        parsed = urlparse(self.client.build_authorize_url())
        self.assertEqual(parsed.netloc, "ridewithgps.com")
        self.assertEqual(
            parse_qs(parsed.query),
            {
                "client_id": ["client-id"],
                "redirect_uri": ["http://localhost/callback"],
                "response_type": ["code"],
            },
        )

    def test_exchange_validates_user_before_saving_access_token(self) -> None:
        session = _Session(
            post_responses=[_Response({"access_token": "test-access-token", "scope": "user"})],
            request_responses=[_Response({"user": {"id": 42}})],
        )
        self.client.session = session

        result = self.client.exchange_code(" code-value ")

        self.assertEqual(result, {"user_id": 42, "scope": "user"})
        self.assertEqual(self.state.get_value("ridewithgps_access_token"), "test-access-token")
        self.assertEqual(self.state.get_value("ridewithgps_user_id"), "42")
        url, post_kwargs = session.post_calls[0]
        self.assertEqual(url, "https://ridewithgps.com/oauth/token.json")
        self.assertEqual(post_kwargs["data"]["code"], "code-value")
        self.assertEqual(
            session.request_calls[0][2]["headers"]["Authorization"],
            "Bearer test-access-token",
        )

    def test_rejected_access_token_is_not_saved(self) -> None:
        session = _Session(
            post_responses=[_Response({"access_token": "bad-token"})],
            request_responses=[_Response({"errors": ["Unauthorized"]}, status_code=401)],
        )
        self.client.session = session

        with self.assertRaisesRegex(RuntimeError, "rejected the access token"):
            self.client.exchange_code("code")

        self.assertIsNone(self.state.get_value("ridewithgps_access_token"))

    def test_delete_requires_positive_numeric_trip_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive integer"):
            self.client.delete_trip("../other-user")
        self.assertFalse(self.client.session is None)


class RideWithGPSSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.state = _StateDB()
        self.state.set_value("ridewithgps_access_token", "test-token")
        self.config = SimpleNamespace(
            ridewithgps_client_id="client-id",
            ridewithgps_client_secret="client-secret",
            ridewithgps_redirect_uri="http://localhost/",
            ridewithgps_access_token=None,
        )
        self.client = RideWithGPSClient(self.config, self.state)
        self.source = RideWithGPSSource(self.config, self.client)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_lists_paginated_trips_and_filters_by_date(self) -> None:
        self.source.page_size = 2
        pages = [
            {
                "trips": [
                    {
                        "id": 103,
                        "name": "Morning ride",
                        "departed_at": "2026-09-03T08:00:00Z",
                        "activity_type": "cycling:road",
                    },
                    {
                        "id": 102,
                        "name": "Evening run",
                        "departed_at": "2026-09-02T08:00:00Z",
                        "activity_type": "running:generic",
                    },
                ],
                "meta": {"pagination": {"page_count": 2}},
            },
            {
                "trips": [
                    {
                        "id": 101,
                        "name": "Old ride",
                        "departed_at": "2026-09-01T08:00:00Z",
                        "activity_type": "cycling:gravel",
                    }
                ],
                "meta": {"pagination": {"page_count": 2}},
            },
        ]
        calls: list[dict[str, object]] = []

        def get_json(path: str, *, params: dict[str, object] | None = None):
            self.assertEqual(path, "/trips.json")
            calls.append(params or {})
            return pages.pop(0)

        with patch.object(self.client, "get_json", side_effect=get_json):
            activities = self.source.list_activities(
                datetime(2026, 9, 2, tzinfo=timezone.utc),
                datetime(2026, 9, 2, 23, 59, tzinfo=timezone.utc),
                None,
            )

        self.assertEqual([item.source_id for item in activities], ["102"])
        self.assertEqual(activities[0].sport_type, "running")
        self.assertEqual(activities[0].name, "Evening run")
        self.assertEqual(calls, [{"page": 1, "page_size": 2}, {"page": 2, "page_size": 2}])

    def test_missing_trip_fields_fail_instead_of_creating_invalid_activity(self) -> None:
        with patch.object(self.client, "get_json", return_value={"trips": [{"id": 1}]}):
            with self.assertRaisesRegex(RuntimeError, "missing a valid departed_at"):
                self.source.list_activities(None, None, 1)

    def test_downloads_and_validates_original_fit(self) -> None:
        fit_path = create_fit(self.root / "fixture.fit")
        fit_payload = fit_path.read_bytes()
        activity = Activity(
            source="ridewithgps",
            source_id="456",
            name="Imported trip",
            sport_type="cycling",
            start_time=datetime(2026, 9, 2, tzinfo=timezone.utc),
        )
        session = _Session(request_responses=[_Response(content=fit_payload)])
        self.client.session = session

        downloaded = self.source.download_fit(activity, self.root / "downloads")

        self.assertTrue(fit_signature_ok(downloaded))
        self.assertEqual(downloaded.read_bytes(), fit_payload)
        self.assertEqual(session.request_calls[0][0], "GET")
        self.assertEqual(
            session.request_calls[0][1], "https://ridewithgps.com/api/v1/trips/456.fit"
        )


class RideWithGPSTargetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        state = _StateDB()
        state.set_value("ridewithgps_access_token", "test-token")
        config = SimpleNamespace(
            ridewithgps_client_id="client-id",
            ridewithgps_client_secret="client-secret",
            ridewithgps_redirect_uri="http://localhost/",
            ridewithgps_access_token=None,
        )
        self.client = RideWithGPSClient(config, state)
        self.target = RideWithGPSTarget(self.client)
        self.activity = Activity(
            source="local",
            source_id="local-1",
            name="Test activity",
            sport_type="cycling",
            start_time=datetime(2026, 9, 2, tzinfo=timezone.utc),
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_upload_waits_for_async_trip_and_returns_remote_id(self) -> None:
        fit_path = create_fit(self.root / "activity.fit")
        responses = [
            _Response({"task": {"id": 77, "status": "pending"}}, status_code=202),
            _Response(
                {
                    "task": {
                        "id": 77,
                        "status": "completed",
                        "items": [{"item_type": "trip", "item_id": 88}],
                        "errors": [],
                    }
                }
            ),
        ]
        with patch.object(self.client, "api_request", side_effect=responses) as request:
            with patch("sport_sync_bridge.ridewithgps_target.time.sleep"):
                result = self.target.upload_file(fit_path, self.activity, "local:local-1")

        self.assertEqual(result, UploadResult(status="success", remote_id="88"))
        self.assertEqual(request.call_args_list[0].args[:2], ("POST", "/trips.json"))
        self.assertEqual(request.call_args_list[0].kwargs["data"], {"name": "Test activity"})
        self.assertEqual(request.call_args_list[0].kwargs["files"]["file"][0], "activity.fit")
        self.assertEqual(request.call_args_list[1].args[:2], ("GET", "/tasks/77.json"))

    def test_duplicate_upload_is_reported_as_duplicate(self) -> None:
        fit_path = create_fit(self.root / "activity.fit")
        responses = [
            _Response({"task": {"id": 77}}, status_code=202),
            _Response(
                {
                    "task": {
                        "status": "completed",
                        "items": [],
                        "errors": [{"code": "duplicate", "trip_id": 55}],
                    }
                }
            ),
        ]
        with patch.object(self.client, "api_request", side_effect=responses):
            with patch("sport_sync_bridge.ridewithgps_target.time.sleep"):
                result = self.target.upload_file(fit_path, self.activity, "local:local-1")
        self.assertEqual(result.status, "duplicate")
        self.assertEqual(result.remote_id, "55")

    def test_rejects_unsupported_or_invalid_input_without_request(self) -> None:
        unsupported = self.root / "activity.kml"
        unsupported.write_text("<kml/>", encoding="utf-8")
        with patch.object(self.client, "api_request") as request:
            result = self.target.upload_file(unsupported, self.activity, "local:local-1")
        self.assertEqual(result.status, "failed")
        request.assert_not_called()

        invalid_fit = self.root / "invalid.fit"
        invalid_fit.write_bytes(b"not FIT")
        with patch.object(self.client, "api_request") as request:
            result = self.target.upload_file(invalid_fit, self.activity, "local:local-1")
        self.assertEqual(result.status, "failed")
        request.assert_not_called()


class RideWithGPSCliTests(unittest.TestCase):
    def test_cli_exposes_sync_source_target_and_safe_delete(self) -> None:
        parser = build_parser()
        self.assertEqual(parser.parse_args(["ridewithgps-auth-url"]).command, "ridewithgps-auth-url")
        source_args = parser.parse_args(["sync", "--source", "ridewithgps", "--dry-run"])
        self.assertEqual(source_args.source, ["ridewithgps"])
        target_args = parser.parse_args(["sync", "--target", "ridewithgps", "--dry-run"])
        self.assertEqual(target_args.target, ["ridewithgps"])
        delete_args = parser.parse_args(["ridewithgps-delete", "--trip-id", "123"])
        self.assertEqual(delete_args.trip_id, "123")

    def test_engine_registers_source_and_target_when_client_credentials_are_configured(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(
                os.environ,
                {
                    "RIDEWITHGPS_CLIENT_ID": "test-client",
                    "RIDEWITHGPS_CLIENT_SECRET": "test-secret",
                },
            ):
                engine = SyncEngine(AppConfig.load(Path(temp_dir)))
            try:
                self.assertIn("ridewithgps", engine.sources)
                self.assertIn("ridewithgps", engine.targets)
            finally:
                engine.close()

    def test_sync_dry_run_reaches_ridewithgps_without_network_access(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(
                os.environ,
                {
                    "RIDEWITHGPS_CLIENT_ID": "test-client",
                    "RIDEWITHGPS_CLIENT_SECRET": "test-secret",
                    "RIDEWITHGPS_ACCESS_TOKEN": "test-token",
                    "STRAVA_CLIENT_ID": "strava-client",
                    "STRAVA_CLIENT_SECRET": "strava-secret",
                },
            ):
                config = AppConfig.load(Path(temp_dir))
            activity = Activity(
                source="ridewithgps",
                source_id="12",
                name="Dry run trip",
                sport_type="cycling",
                start_time=datetime(2026, 9, 2, tzinfo=timezone.utc),
            )
            with (
                patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
                patch("sport_sync_bridge.cli.configure_logging"),
                patch("sport_sync_bridge.ridewithgps_source.RideWithGPSSource.list_activities", return_value=[activity]),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = cli_main(
                    ["sync", "--source", "ridewithgps", "--target", "strava", "--dry-run"]
                )
            self.assertEqual(result, 0)

    def test_delete_requires_exact_typed_id_before_calling_api(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(
                os.environ,
                {
                    "RIDEWITHGPS_CLIENT_ID": "test-client",
                    "RIDEWITHGPS_CLIENT_SECRET": "test-secret",
                    "RIDEWITHGPS_ACCESS_TOKEN": "test-token",
                },
            ):
                config = AppConfig.load(Path(temp_dir))
            with (
                patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
                patch("sport_sync_bridge.cli.configure_logging"),
                patch("builtins.input", return_value="123"),
                patch.object(RideWithGPSClient, "delete_trip") as delete_trip,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = cli_main(["ridewithgps-delete", "--trip-id", "123"])
            self.assertEqual(result, 0)
            delete_trip.assert_called_once_with("123")

            with (
                patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
                patch("sport_sync_bridge.cli.configure_logging"),
                patch("builtins.input", return_value="wrong-id"),
                patch.object(RideWithGPSClient, "delete_trip") as delete_trip,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = cli_main(["ridewithgps-delete", "--trip-id", "123"])
            self.assertEqual(result, 1)
            delete_trip.assert_not_called()


if __name__ == "__main__":
    unittest.main()
