from __future__ import annotations

import base64
import contextlib
import io
import os
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sport_sync_bridge.cli import build_parser, main as cli_main
from sport_sync_bridge.config import AppConfig
from sport_sync_bridge.engine import SyncEngine
from sport_sync_bridge.giant_api import GiantClient
from sport_sync_bridge.giant_source import GiantSource
from sport_sync_bridge.giant_target import GiantTarget
from sport_sync_bridge.models import Activity
from tests.activity_fixtures import create_fit


def _fit_bytes() -> bytes:
    with tempfile.TemporaryDirectory() as directory:
        return create_fit(Path(directory) / "activity.fit").read_bytes()


class _Response:
    def __init__(
        self,
        payload: object | None = None,
        *,
        status_code: int = 200,
        content: bytes = b"",
    ):
        self.payload = payload
        self.status_code = status_code
        self.content = content

    def json(self):
        if self.payload is None:
            raise ValueError("no JSON body")
        return self.payload


class _Session:
    def __init__(self, *, get_responses=None, post_responses=None):
        self.get_responses = list(get_responses or [])
        self.post_responses = list(post_responses or [])
        self.get_calls: list[tuple[str, dict[str, object]]] = []
        self.post_calls: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, **kwargs):
        self.get_calls.append((url, kwargs))
        return self.get_responses.pop(0)

    def post(self, url: str, **kwargs):
        self.post_calls.append((url, kwargs))
        return self.post_responses.pop(0)


class _State:
    def __init__(self):
        self.values: dict[str, str] = {}

    def get_value(self, key: str) -> str | None:
        return self.values.get(key)

    def set_value(self, key: str, value: str) -> None:
        self.values[key] = value


def _config(*, username: str | None = "rider@example.test", password: str | None = "secret"):
    return SimpleNamespace(
        giant_username=username,
        giant_password=password,
        giant_app_version="4.1.3",
        giant_device_os_version="25",
        giant_device_model="sport-sync-bridge",
    )


class GiantClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = _State()
        self.client = GiantClient(_config(), self.state)

    def test_login_saves_app_token_and_generates_a_local_device_id(self) -> None:
        self.client.session = _Session(
            post_responses=[
                _Response(
                    {
                        "success": True,
                        "retval": {"_id": "user-7", "access_token": "app-token"},
                    }
                )
            ]
        )

        self.assertEqual(self.client.authenticate(), "user-7")
        url, kwargs = self.client.session.post_calls[0]
        self.assertEqual(url, f"{self.client.api_root}/v3/user/profile/login")
        fields = kwargs["data"]
        self.assertEqual(fields["account"], "rider@example.test")
        self.assertEqual(fields["password"], "secret")
        self.assertEqual(fields["app_version"], "4.1.3")
        self.assertEqual(fields["device_os"], "android")
        self.assertEqual(fields["device_os_version"], "25")
        self.assertEqual(fields["model"], "sport-sync-bridge")
        self.assertRegex(fields["device_id"], re.compile(r"^[0-9a-f]{16}$"))
        self.assertNotEqual(fields["device_id"], "8f8c8c8c8c8c8c8c")
        self.assertEqual(self.state.get_value(self.client.access_token_key), "app-token")
        self.assertEqual(
            base64.b64decode(self.client._auth_headers()["Authorization"].removeprefix("Basic ")),
            b"user-7:app-token",
        )

    def test_business_auth_failure_relogs_and_retries_once(self) -> None:
        self.state.set_value(self.client.user_id_key, "user-7")
        self.state.set_value(self.client.access_token_key, "old-token")
        session = _Session(
            get_responses=[
                _Response({"errCode": "E0001", "success": False}),
                _Response({"success": True, "retval": []}),
            ],
            post_responses=[
                _Response(
                    {
                        "success": True,
                        "retval": {"_id": "user-7", "access_token": "new-token"},
                    }
                )
            ],
        )
        self.client.session = session

        self.assertEqual(self.client.get_json("/v4/cycling")["retval"], [])
        auth_headers = [kwargs["headers"]["Authorization"] for _, kwargs in session.get_calls]
        self.assertEqual(
            [base64.b64decode(value.removeprefix("Basic ")) for value in auth_headers],
            [b"user-7:old-token", b"user-7:new-token"],
        )
        self.assertEqual(len(session.post_calls), 1)

    def test_fit_download_does_not_send_app_credentials_to_external_host(self) -> None:
        self.state.set_value(self.client.user_id_key, "user-7")
        self.state.set_value(self.client.access_token_key, "app-token")
        session = _Session(
            get_responses=[_Response(status_code=200, content=_fit_bytes())]
        )
        self.client.session = session
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "activity.fit"
            result = self.client.download_fit_file("https://cdn.example.test/ride.fit", output)

            self.assertEqual(result.read_bytes(), _fit_bytes())
        self.assertNotIn("Authorization", session.get_calls[0][1]["headers"])

    def test_fit_download_rejects_non_https_urls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "invalid FIT URL"):
                self.client.download_fit_file(
                    "http://cdn.example.test/ride.fit", Path(directory) / "ride.fit"
                )

    def test_fit_download_does_not_send_auth_to_nonstandard_port(self) -> None:
        self.state.set_value(self.client.user_id_key, "user-7")
        self.state.set_value(self.client.access_token_key, "app-token")
        session = _Session(get_responses=[_Response(status_code=200, content=_fit_bytes())])
        self.client.session = session
        with tempfile.TemporaryDirectory() as directory:
            self.client.download_fit_file(
                "https://rideapp.giant.com.cn:8443/ride.fit",
                Path(directory) / "activity.fit",
            )

        self.assertNotIn("Authorization", session.get_calls[0][1]["headers"])

    def test_web_upload_uses_reidlifetoken_and_fit_multipart_fields(self) -> None:
        session = _Session(
            post_responses=[
                _Response({"status": 1, "user_token": "web-token"}),
                _Response({"status": 1, "retval": {"id": 88}}),
            ]
        )
        self.client.session = session
        with tempfile.TemporaryDirectory() as directory:
            fit_path = Path(directory) / "ride.fit"
            fit_path.write_bytes(_fit_bytes())

            response = self.client.upload_fit_file(fit_path)

            self.assertEqual(response.status_code, 200)
        login_url, login_kwargs = session.post_calls[0]
        self.assertEqual(login_url, self.client.web_login_url)
        self.assertEqual(login_kwargs["data"], {"username": "rider@example.test", "password": "secret"})
        upload_url, upload_kwargs = session.post_calls[1]
        self.assertEqual(upload_url, self.client.upload_url)
        self.assertEqual(
            upload_kwargs["data"],
            {"token": "web-token", "device": "bike_computer", "brand": "garmin"},
        )
        self.assertIn("files[]", upload_kwargs["files"])

    def test_web_upload_relogs_when_giant_returns_a_nonterminal_failure_status(self) -> None:
        session = _Session(
            post_responses=[
                _Response({"status": 1, "user_token": "old-web-token"}),
                _Response({"status": 0, "message": "expired"}),
                _Response({"status": 1, "user_token": "new-web-token"}),
                _Response({"status": 1, "message": "uploaded"}),
            ]
        )
        self.client.session = session
        with tempfile.TemporaryDirectory() as directory:
            fit_path = Path(directory) / "ride.fit"
            fit_path.write_bytes(_fit_bytes())

            response = self.client.upload_fit_file(fit_path)

        self.assertEqual(response.json()["status"], 1)
        self.assertEqual(len(session.post_calls), 4)
        self.assertEqual(session.post_calls[1][1]["data"]["token"], "old-web-token")
        self.assertEqual(session.post_calls[3][1]["data"]["token"], "new-web-token")


class GiantSourceTests(unittest.TestCase):
    def test_lists_year_month_activities_and_preserves_summary_fields(self) -> None:
        september = datetime(2026, 9, 15, 8, tzinfo=timezone.utc).timestamp()
        client = Mock()
        client.is_configured.return_value = True
        client.authenticate.return_value = "user-7"
        client.get_json.side_effect = [
            {"success": True, "retval": [2025, 2026]},
            {"success": True, "retval": [1, 9]},
            {
                "success": True,
                "retval": [
                    {
                        "id": 17,
                        "started_at": september,
                        "finished_at": september + 3600,
                        "seconds": 3600,
                        "title": "Evening ride",
                        "distance": 25000,
                        "avgHR": 142,
                        "avgSpeed": 25.2,
                        "device": {"name": "Ride computer"},
                    }
                ],
            },
        ]
        source = GiantSource(_config(), client)

        activities = source.list_activities(
            datetime(2026, 9, 1, tzinfo=timezone.utc),
            datetime(2026, 9, 30, 23, 59, tzinfo=timezone.utc),
            None,
        )

        self.assertEqual([activity.source_id for activity in activities], ["17"])
        activity = activities[0]
        self.assertEqual(activity.name, "Evening ride")
        self.assertEqual(activity.sport_type, "cycling")
        self.assertEqual(activity.start_time, datetime(2026, 9, 15, 8, tzinfo=timezone.utc))
        self.assertEqual(activity.raw["distance"], 25000)
        self.assertEqual(activity.raw["avgHR"], 142)
        self.assertEqual(activity.raw["device"]["name"], "Ride computer")
        self.assertEqual(
            [call.args[0] for call in client.get_json.call_args_list],
            ["/v4/cycling", "/v4/cycling/2026", "/v1.1/cycling/2026/9"],
        )

    def test_download_fetches_detail_fit_url(self) -> None:
        client = Mock()
        client.is_configured.return_value = True
        client.authenticate.return_value = "user-7"
        client.get_json.return_value = {
            "success": True,
            "retval": {"fitUrl": "https://cdn.example.test/17.fit"},
        }
        client.download_fit_file.return_value = Path("cache/giant/17.fit")
        source = GiantSource(_config(), client)
        activity = Activity(source="giant", source_id="17", name="Evening ride")

        result = source.download_fit(activity, Path("cache"))

        self.assertEqual(result, Path("cache/giant/17.fit"))
        client.get_json.assert_called_once_with("/v3.2/cycling/17")
        client.download_fit_file.assert_called_once_with(
            "https://cdn.example.test/17.fit", Path("cache/giant/17.fit")
        )

    def test_missing_activity_timestamp_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "started_at"):
            from sport_sync_bridge.giant_source import _activity_from_record

            _activity_from_record({"id": 17, "title": "Ride"}, 2026, 9, 0)


class GiantTargetTests(unittest.TestCase):
    def test_maps_successful_fit_upload_response(self) -> None:
        client = Mock()
        client.is_configured.return_value = True
        client.upload_fit_file.return_value = _Response(
            {"status": 1, "retval": {"id": 88}}
        )
        target = GiantTarget(client)
        activity = Activity(source="local", source_id="ride-1", name="Ride")
        with tempfile.TemporaryDirectory() as directory:
            fit_path = Path(directory) / "ride.fit"
            fit_path.write_bytes(_fit_bytes())

            result = target.upload_file(fit_path, activity, "local:ride-1")

        self.assertEqual(result.status, "success")
        self.assertEqual(result.remote_id, "88")

    def test_rejects_non_fit_upload(self) -> None:
        client = Mock()
        target = GiantTarget(client)
        result = target.upload_file(
            Path("ride.tcx"),
            Activity(source="local", source_id="ride-1", name="Ride"),
            "local:ride-1",
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("FIT", result.message)
        client.upload_fit_file.assert_not_called()

    def test_saved_web_token_configures_target_without_app_api_token(self) -> None:
        state = _State()
        state.set_value(GiantClient.web_token_key, "web-token")
        client = GiantClient(_config(username=None, password=None), state)

        self.assertTrue(GiantTarget(client).is_configured())


class GiantIntegrationTests(unittest.TestCase):
    def test_config_cli_and_engine_register_giant_without_network_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "GIANT_USERNAME": "rider@example.test",
                "GIANT_PASSWORD": "secret",
                "SYNC_SOURCES": "giant",
                "SYNC_TARGETS": "giant",
                "SYNC_DATA_DIR": str(Path(directory) / "data"),
            },
            clear=True,
        ):
            config = AppConfig.load(Path(directory))
            engine = SyncEngine(config)
            try:
                self.assertIn("giant", engine.sources)
                self.assertIn("giant", engine.targets)
                engine.sources["giant"].list_activities = Mock(return_value=[])
                self.assertEqual(
                    engine.sync_once(
                        sources=["giant"],
                        targets=["giant"],
                        dry_run=True,
                        target_formats={"giant": "fit"},
                    ),
                    0,
                )
            finally:
                engine.close()

        args = build_parser().parse_args(
            ["sync", "--source", "giant", "--target", "giant", "--dry-run"]
        )
        self.assertEqual(args.source, ["giant"])
        self.assertEqual(args.target, ["giant"])
        self.assertEqual(build_parser().parse_args(["giant-auth"]).command, "giant-auth")
        self.assertEqual(build_parser().parse_args(["giant-logout"]).command, "giant-logout")

    def test_cli_dry_run_dispatches_giant_without_authentication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = SimpleNamespace(
                data_dir=Path(directory) / "data",
                log_level="INFO",
                log_path=Path(directory) / "sync.log",
            )
            fake_engine = SimpleNamespace(
                sources={"giant": object()},
                targets={"giant": object()},
                sync_once=Mock(return_value=0),
                close=Mock(),
            )
            output = io.StringIO()
            with (
                patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
                patch("sport_sync_bridge.cli.configure_logging"),
                patch("sport_sync_bridge.cli.SyncEngine", return_value=fake_engine),
                contextlib.redirect_stdout(output),
            ):
                result = cli_main(
                    [
                        "sync",
                        "--source",
                        "giant",
                        "--target",
                        "giant",
                        "--format",
                        "giant=fit",
                        "--dry-run",
                    ]
                )

        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue().strip(), "done=0")
        self.assertEqual(fake_engine.sync_once.call_args.kwargs["target_formats"], {"giant": "fit"})
        self.assertTrue(fake_engine.sync_once.call_args.kwargs["dry_run"])
        fake_engine.close.assert_called_once_with()

    def test_giant_target_only_accepts_fit_format(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "GIANT_USERNAME": "rider@example.test",
                "GIANT_PASSWORD": "secret",
                "SYNC_DATA_DIR": str(Path(directory) / "data"),
            },
            clear=True,
        ):
            engine = SyncEngine(AppConfig.load(Path(directory)))
            try:
                with self.assertRaisesRegex(ValueError, "Unsupported target format mapping"):
                    engine.sync_once(
                        sources=["giant"],
                        targets=["giant"],
                        dry_run=True,
                        target_formats={"giant": "tcx"},
                    )
            finally:
                engine.close()


if __name__ == "__main__":
    unittest.main()
