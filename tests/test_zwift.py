from __future__ import annotations

import contextlib
import io
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

from sport_sync_bridge.cli import build_parser, main as cli_main
from sport_sync_bridge.config import AppConfig
from sport_sync_bridge.engine import SyncEngine
from sport_sync_bridge.models import Activity
from sport_sync_bridge.zwift_api import ZwiftClient
from sport_sync_bridge.zwift_source import ZwiftSource
from tests.activity_fixtures import create_fit


class _Response:
    def __init__(self, payload: object = None, *, status_code: int = 200, content: bytes = b""):
        self.payload = payload
        self.status_code = status_code
        self.content = content

    def json(self) -> object:
        if self.payload is None:
            raise ValueError("no JSON payload")
        return self.payload


class _Session:
    def __init__(self, *, request_responses=None, post_responses=None, get_responses=None):
        self.headers: dict[str, str] = {}
        self.request_responses = list(request_responses or [])
        self.post_responses = list(post_responses or [])
        self.get_responses = list(get_responses or [])
        self.request_calls: list[tuple[str, str, dict[str, object]]] = []
        self.post_calls: list[tuple[str, dict[str, object]]] = []
        self.get_calls: list[tuple[str, dict[str, object]]] = []

    def request(self, method: str, url: str, **kwargs):
        self.request_calls.append((method, url, kwargs))
        return self.request_responses.pop(0)

    def post(self, url: str, **kwargs):
        self.post_calls.append((url, kwargs))
        return self.post_responses.pop(0)

    def get(self, url: str, **kwargs):
        self.get_calls.append((url, kwargs))
        return self.get_responses.pop(0)


class _State:
    def __init__(self):
        self.values: dict[str, str] = {}

    def get_value(self, key: str) -> str | None:
        return self.values.get(key)

    def set_value(self, key: str, value: str) -> None:
        self.values[key] = value


def _config(*, username: str | None = "rider@example.test", password: str | None = "test-password"):
    return SimpleNamespace(zwift_username=username, zwift_password=password)


class ZwiftClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = _State()
        self.client = ZwiftClient(_config(), self.state)

    def test_password_login_validates_profile_and_stores_tokens(self) -> None:
        session = _Session(
            post_responses=[
                _Response(
                    {
                        "access_token": "access-token",
                        "refresh_token": "refresh-token",
                        "expires_in": 3600,
                    }
                )
            ],
            request_responses=[_Response({"id": 12345})],
        )
        self.client.session = session

        self.assertEqual(self.client.authenticate(), "12345")
        token_url, token_kwargs = session.post_calls[0]
        self.assertEqual(token_url, self.client.token_url)
        self.assertEqual(
            token_kwargs["data"],
            {
                "client_id": "Zwift_Mobile_Link",
                "grant_type": "password",
                "username": "rider@example.test",
                "password": "test-password",
            },
        )
        profile_url = session.request_calls[0][1]
        profile_headers = session.request_calls[0][2]["headers"]
        self.assertEqual(profile_url, f"{self.client.api_root}/api/profiles/me")
        self.assertEqual(profile_headers["Authorization"], "Bearer access-token")
        self.assertEqual(self.state.get_value(self.client.access_token_key), "access-token")
        self.assertEqual(self.state.get_value(self.client.refresh_token_key), "refresh-token")
        self.assertEqual(self.state.get_value(self.client.player_id_key), "12345")

    def test_expired_access_token_refreshes_and_retries_unauthorized_api_call(self) -> None:
        self.state.set_value(self.client.access_token_key, "expired-access")
        self.state.set_value(self.client.refresh_token_key, "saved-refresh")
        self.state.set_value(self.client.expires_at_key, str(time.time() + 3600))
        self.state.set_value(self.client.player_id_key, "12345")
        session = _Session(
            request_responses=[_Response({"message": "expired"}, status_code=401), _Response([])],
            post_responses=[
                _Response(
                    {
                        "access_token": "fresh-access",
                        "refresh_token": "rotated-refresh",
                        "expires_in": 3600,
                    }
                )
            ],
        )
        self.client.session = session

        self.assertEqual(self.client.get_json("/api/profiles/12345/activities/"), [])
        self.assertEqual(
            [call[2]["headers"]["Authorization"] for call in session.request_calls],
            ["Bearer expired-access", "Bearer fresh-access"],
        )
        self.assertEqual(session.post_calls[0][1]["data"]["grant_type"], "refresh_token")
        self.assertEqual(self.state.get_value(self.client.refresh_token_key), "rotated-refresh")

    def test_login_does_not_store_tokens_when_profile_validation_fails(self) -> None:
        self.client.session = _Session(
            post_responses=[_Response({"access_token": "short-lived", "refresh_token": "refresh"})],
            request_responses=[_Response({"name": "profile missing id"})],
        )

        with self.assertRaisesRegex(RuntimeError, "player ID"):
            self.client.authenticate()
        self.assertIsNone(self.state.get_value(self.client.access_token_key))
        self.assertIsNone(self.state.get_value(self.client.refresh_token_key))

    def test_expired_access_token_without_refresh_or_credentials_is_not_configured(self) -> None:
        client = ZwiftClient(_config(username=None, password=None), self.state)
        self.state.set_value(client.access_token_key, "expired-access")
        self.state.set_value(client.expires_at_key, "0")

        self.assertFalse(client.is_configured())

    def test_logout_revokes_refresh_session_then_clears_local_tokens(self) -> None:
        for key in (
            self.client.access_token_key,
            self.client.refresh_token_key,
            self.client.expires_at_key,
            self.client.player_id_key,
        ):
            self.state.set_value(key, "saved-value")
        session = _Session(
            post_responses=[
                _Response({"access_token": "fresh-access", "refresh_token": "fresh-refresh", "expires_in": 3600}),
                _Response(status_code=204),
            ]
        )
        self.client.session = session

        self.client.logout()

        self.assertEqual(session.post_calls[0][1]["data"]["grant_type"], "refresh_token")
        self.assertEqual(session.post_calls[1][0], self.client.logout_url)
        self.assertEqual(session.post_calls[1][1]["data"]["refresh_token"], "fresh-refresh")
        self.assertTrue(all(self.state.get_value(key) == "" for key in self.state.values))

    def test_fit_download_uses_s3_without_bearer_and_validates_file(self) -> None:
        payload = bytearray(128)
        payload[0] = 14
        payload[8:12] = b".FIT"
        session = _Session(get_responses=[_Response(status_code=200, content=bytes(payload))])
        self.client.session = session
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "activity.fit"

            result = self.client.download_fit_file("zwift-fit-files", "activities/ride 1.fit", output)

            self.assertEqual(result, output)
            self.assertEqual(output.read_bytes(), bytes(payload))
        url, kwargs = session.get_calls[0]
        self.assertEqual(url, "https://zwift-fit-files.s3.amazonaws.com/activities/ride%201.fit")
        self.assertNotIn("Authorization", kwargs.get("headers", {}))

    def test_fit_download_rejects_bad_bucket_and_does_not_write_invalid_content(self) -> None:
        self.client.session = _Session(get_responses=[_Response(status_code=200, content=b"not a FIT file" * 20)])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "activity.fit"
            with self.assertRaisesRegex(ValueError, "bucket"):
                self.client.download_fit_file("../invalid", "activity.fit", output)
            with self.assertRaisesRegex(RuntimeError, "invalid FIT"):
                self.client.download_fit_file("zwift-fit-files", "activity.fit", output)
            self.assertFalse(output.exists())


class _SourceClient:
    def __init__(self, pages=None, details=None):
        self.player_id = "42"
        self.pages = list(pages or [])
        self.details = details or {}
        self.get_calls: list[tuple[str, dict[str, object] | None]] = []
        self.download_calls: list[tuple[object, object, Path]] = []

    def is_configured(self) -> bool:
        return True

    def authenticate(self) -> str:
        return self.player_id

    def get_json(self, path: str, *, params=None):
        self.get_calls.append((path, params))
        if "activities/" in path and path.endswith("/"):
            return self.pages.pop(0)
        return self.details

    def download_fit_file(self, bucket, object_key, output_path: Path) -> Path:
        self.download_calls.append((bucket, object_key, output_path))
        return output_path


class ZwiftSourceTests(unittest.TestCase):
    def test_lists_offset_pages_filters_dates_and_preserves_summary_fields(self) -> None:
        client = _SourceClient(
            pages=[
                [
                    {"id": 1, "startDate": "2026-08-30T08:00:00Z", "sport": "CYCLING"},
                    {
                        "id_str": "2",
                        "startDate": "2026-09-02T08:00:00Z",
                        "sport": "RUNNING",
                        "name": "Morning run",
                        "distanceInMeters": 5000,
                        "movingTimeInMs": 1500000,
                        "avgHeartRate": 145,
                    },
                ],
                [
                    {
                        "id": 3,
                        "startDate": "2026-09-03T08:00:00Z",
                        "sport": "CYCLING",
                        "name": "Evening ride",
                        "avgWatts": 180,
                    }
                ],
            ]
        )
        source = ZwiftSource(cast(AppConfig, _config()), client)
        source.page_size = 2

        activities = source.list_activities(
            datetime(2026, 9, 1, tzinfo=timezone.utc),
            datetime(2026, 9, 4, tzinfo=timezone.utc),
            None,
        )

        self.assertEqual([activity.source_id for activity in activities], ["2", "3"])
        self.assertEqual([activity.sport_type for activity in activities], ["running", "cycling"])
        self.assertEqual(activities[0].raw["distanceInMeters"], 5000)
        self.assertEqual(activities[0].raw["avgHeartRate"], 145)
        self.assertEqual([call[1] for call in client.get_calls], [{"start": 0, "limit": 2}, {"start": 2, "limit": 2}])

    def test_download_reads_detail_and_delegates_s3_fit_file(self) -> None:
        client = _SourceClient(details={"fitFileBucket": "zwift-fit-files", "fitFileKey": "ride/10.fit"})
        source = ZwiftSource(cast(AppConfig, _config()), client)
        activity = Activity(source="zwift", source_id="10", name="Ride")
        with tempfile.TemporaryDirectory() as directory:
            result = source.download_fit(activity, Path(directory))

            self.assertEqual(result, Path(directory) / "zwift" / "10.fit")
        self.assertEqual(client.get_calls[0][0], "/api/profiles/42/activities/10")
        self.assertEqual(client.download_calls[0][:2], ("zwift-fit-files", "ride/10.fit"))

    def test_cli_exposes_zwift_source_and_auth_commands(self) -> None:
        parser = build_parser()
        self.assertEqual(parser.parse_args(["sync", "--source", "zwift"]).source, ["zwift"])
        self.assertEqual(parser.parse_args(["check", "--source", "zwift"]).source, ["zwift"])
        self.assertEqual(parser.parse_args(["zwift-auth"]).command, "zwift-auth")
        self.assertEqual(parser.parse_args(["zwift-logout"]).command, "zwift-logout")

    def test_auth_command_reports_player_id_without_printing_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = SimpleNamespace(
                data_dir=Path(directory),
                log_level="INFO",
                log_path=Path(directory) / "sync.log",
            )
            fake_engine = SimpleNamespace(
                zwift_client=SimpleNamespace(authenticate=Mock(return_value="12345")),
                close=Mock(),
            )
            output = io.StringIO()
            with (
                patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
                patch("sport_sync_bridge.cli.configure_logging"),
                patch("sport_sync_bridge.cli.SyncEngine", return_value=fake_engine),
                contextlib.redirect_stdout(output),
            ):
                result = cli_main(["zwift-auth"])

            self.assertEqual(result, 0)
            self.assertIn("player_id=12345", output.getvalue())
            self.assertNotIn("access-token", output.getvalue())
            fake_engine.close.assert_called_once_with()

    def test_engine_registers_zwift_and_passes_through_trackless_fit_for_upload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = AppConfig.load(root)
            config.zwift_username = "rider@example.test"
            config.zwift_password = "test-password"
            engine = SyncEngine(config)
            try:
                self.assertIn("zwift", engine.sources)
                source_path = create_fit(root / "zwift-indoor.fit", with_track=False)
                activity = Activity(
                    source="zwift",
                    source_id="indoor-ride-1",
                    name="Indoor ride",
                    sport_type="cycling",
                )
                upload_path, losses = engine._prepare_target_file(
                    source_path,
                    activity,
                    "strava",
                    "fit",
                )
                self.assertEqual(upload_path, config.converted_dir / "zwift" / "strava" / "indoor-ride-1.fit")
                self.assertEqual(upload_path.read_bytes(), source_path.read_bytes())
                self.assertEqual(losses, ())
            finally:
                engine.close()


if __name__ == "__main__":
    unittest.main()
