from __future__ import annotations

import contextlib
import io
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import requests

from sport_sync_bridge.cli import build_parser, main as cli_main
from sport_sync_bridge.config import AppConfig
from sport_sync_bridge.engine import SyncEngine
from sport_sync_bridge.models import Activity, UploadResult
from sport_sync_bridge.suunto_api import SuuntoClient
from sport_sync_bridge.suunto_source import SuuntoSource
from sport_sync_bridge.suunto_target import SuuntoTarget
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
        self.request_calls: list[tuple[str, str, dict[str, object]]] = []
        self.post_calls: list[tuple[str, dict[str, object]]] = []

    def request(self, method: str, url: str, **kwargs: object) -> _Response:
        self.request_calls.append((method, url, kwargs))
        return self.request_responses.pop(0)

    def post(self, url: str, **kwargs: object) -> _Response:
        self.post_calls.append((url, kwargs))
        return self.post_responses.pop(0)


class _State:
    def __init__(self):
        self.values: dict[str, str] = {}

    def get_value(self, key: str) -> str | None:
        return self.values.get(key)

    def set_value(self, key: str, value: str) -> None:
        self.values[key] = value


def _config(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "suunto_client_id": "user-client-id",
        "suunto_client_secret": "user-client-secret",
        "suunto_redirect_uri": "https://localhost/callback",
        "suunto_subscription_key": "user-subscription-key",
        "suunto_api_root": "https://cloudapi.suunto.com",
        "suunto_oauth_root": "https://cloudapi-oauth.suunto.com",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _ClientStub:
    def __init__(self, config: SimpleNamespace, *, json_values=None, responses=None):
        self.config = config
        self.json_values = list(json_values or [])
        self.responses = list(responses or [])
        self.api_calls: list[tuple[str, str, dict[str, object]]] = []
        self.json_calls: list[tuple[str, dict[str, object] | None]] = []

    def is_configured(self) -> bool:
        return True

    def authenticate(self) -> None:
        return None

    def get_json(self, path: str, *, params=None):
        self.json_calls.append((path, params))
        return self.json_values.pop(0)

    def api_request(self, method: str, path: str, **kwargs: object) -> _Response:
        self.api_calls.append((method, path, kwargs))
        return self.responses.pop(0)


class SuuntoClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = _State()
        self.config = _config()
        self.client = SuuntoClient(self.config, self.state)

    def test_authorize_url_stores_state_and_requests_workout_scope(self) -> None:
        query = parse_qs(urlparse(self.client.build_authorize_url()).query)

        self.assertEqual(
            query,
            {
                "response_type": ["code"],
                "client_id": ["user-client-id"],
                "redirect_uri": ["https://localhost/callback"],
                "scope": ["workouts"],
                "state": [self.state.get_value("suunto_oauth_state")],
            },
        )
        self.assertEqual(
            urlparse(self.client.build_authorize_url()).netloc,
            "cloudapi-oauth.suunto.com",
        )

    def test_exchange_uses_basic_auth_and_persists_tokens_without_returning_them(self) -> None:
        self.state.set_value("suunto_oauth_state", "expected-state")
        session = _Session(post_responses=[_Response({
            "access_token": "private-access",
            "refresh_token": "private-refresh",
            "expires_in": 86400,
            "scope": "workouts",
        })])
        self.client.session = session

        result = self.client.exchange_code(" code ", "expected-state")

        self.assertEqual(result, {"expires_in": 86400, "scope": "workouts"})
        self.assertEqual(self.state.get_value("suunto_access_token"), "private-access")
        self.assertEqual(self.state.get_value("suunto_refresh_token"), "private-refresh")
        self.assertEqual(self.state.get_value("suunto_oauth_state"), "")
        url, kwargs = session.post_calls[0]
        self.assertEqual(url, "https://cloudapi-oauth.suunto.com/oauth/token")
        self.assertEqual(kwargs["auth"], ("user-client-id", "user-client-secret"))
        self.assertEqual(
            kwargs["data"],
            {
                "grant_type": "authorization_code",
                "code": "code",
                "redirect_uri": "https://localhost/callback",
            },
        )
        self.assertNotIn("access_token", result)

    def test_exchange_rejects_mismatched_state_before_network_request(self) -> None:
        self.state.set_value("suunto_oauth_state", "expected-state")
        session = _Session()
        self.client.session = session

        with self.assertRaisesRegex(ValueError, "did not match"):
            self.client.exchange_code("code", "other-state")

        self.assertEqual(session.post_calls, [])

    def test_api_requests_send_bearer_and_subscription_headers(self) -> None:
        self.state.set_value("suunto_access_token", "private-access")
        self.state.set_value("suunto_expires_at", str(time.time() + 3600))
        session = _Session(request_responses=[_Response({"payload": []})])
        self.client.session = session

        self.assertEqual(self.client.get_json("/v3/workouts", params={"limit": 1}), {"payload": []})

        method, url, kwargs = session.request_calls[0]
        self.assertEqual((method, url), ("GET", "https://cloudapi.suunto.com/v3/workouts"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer private-access")
        self.assertEqual(kwargs["headers"]["Ocp-Apim-Subscription-Key"], "user-subscription-key")

    def test_unauthorized_request_refreshes_once_and_retries(self) -> None:
        self.state.set_value("suunto_access_token", "old-access")
        self.state.set_value("suunto_refresh_token", "old-refresh")
        self.state.set_value("suunto_expires_at", str(time.time() + 3600))
        session = _Session(
            post_responses=[_Response({
                "access_token": "new-access",
                "refresh_token": "rotated-refresh",
                "expires_in": 86400,
            })],
            request_responses=[_Response({}, status_code=401), _Response({"payload": []})],
        )
        self.client.session = session

        self.assertEqual(self.client.get_json("/v3/workouts"), {"payload": []})
        self.assertEqual(len(session.request_calls), 2)
        self.assertEqual(
            session.request_calls[0][2]["headers"]["Authorization"],
            "Bearer old-access",
        )
        self.assertEqual(
            session.request_calls[1][2]["headers"]["Authorization"],
            "Bearer new-access",
        )
        self.assertEqual(self.state.get_value("suunto_refresh_token"), "rotated-refresh")

    def test_insecure_api_roots_are_not_configured(self) -> None:
        client = SuuntoClient(_config(suunto_api_root="http://127.0.0.1"), self.state)

        self.assertFalse(client.is_configured())


class SuuntoSourceTests(unittest.TestCase):
    def test_lists_paged_workouts_and_maps_epoch_milliseconds(self) -> None:
        start_ms = int(datetime(2026, 9, 20, 7, tzinfo=timezone.utc).timestamp() * 1000)
        later_ms = int(datetime(2026, 9, 21, 8, tzinfo=timezone.utc).timestamp() * 1000)
        client = _ClientStub(
            _config(),
            json_values=[
                {"payload": {
                    "items": [
                        {"workoutKey": "w-2", "workoutName": "Ride", "activityType": "CYCLING", "startTime": later_ms},
                        {"workoutKey": "w-1", "workoutName": "Run", "activityType": "RUNNING", "startTime": start_ms},
                    ],
                    "hasMore": True,
                }},
                {"payload": {
                    "items": [
                        {"workoutKey": "w-3", "workoutName": "Swim", "activityType": "SWIMMING", "startTime": later_ms + 1},
                    ],
                    "hasMore": False,
                }},
            ],
        )
        source = SuuntoSource(_config(), client)
        since = datetime(2026, 9, 19, tzinfo=timezone.utc)
        until = datetime(2026, 9, 22, tzinfo=timezone.utc)

        activities = source.list_activities(since, until, 3)

        self.assertEqual(client.json_calls, [
            ("/v3/workouts", {
                "limit": 3,
                "offset": 0,
                "since": "2026-09-19T00:00:00Z",
                "until": "2026-09-22T00:00:00Z",
            }),
            ("/v3/workouts", {
                "limit": 1,
                "offset": 2,
                "since": "2026-09-19T00:00:00Z",
                "until": "2026-09-22T00:00:00Z",
            }),
        ])
        self.assertEqual([activity.source_id for activity in activities], ["w-1", "w-2", "w-3"])
        self.assertEqual(
            [activity.sport_type for activity in activities],
            ["running", "cycling", "swimming"],
        )
        self.assertEqual(activities[0].start_time, datetime(2026, 9, 20, 7, tzinfo=timezone.utc))

    def test_downloads_fit_and_uses_legacy_endpoint_after_v3_404(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fit_path = create_fit(Path(temporary) / "fixture.fit")
            client = _ClientStub(
                _config(),
                responses=[
                    _Response(status_code=404),
                    _Response(content=fit_path.read_bytes()),
                ],
            )
            source = SuuntoSource(_config(), client)

            output = source.download_fit(Activity("suunto", "workout/1", "Ride"), Path(temporary) / "download")

            self.assertTrue(output.is_file())
            self.assertEqual(output.read_bytes(), fit_path.read_bytes())
            self.assertEqual(
                [(call[0], call[1]) for call in client.api_calls],
                [
                    ("GET", "/v3/workouts/workout%2F1/fit"),
                    ("GET", "/v2/workout/exportFit/workout%2F1"),
                ],
            )

    def test_rejects_non_fit_download(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            client = _ClientStub(_config(), responses=[_Response(content=b"not FIT")])
            source = SuuntoSource(_config(), client)

            with self.assertRaisesRegex(RuntimeError, "invalid FIT signature"):
                source.download_fit(Activity("suunto", "w-1", "Run"), Path(temporary))


class SuuntoTargetTests(unittest.TestCase):
    def test_uploads_fit_without_forwarding_api_credentials_to_blob_storage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fit_path = create_fit(Path(temporary) / "ride.fit")
            client = _ClientStub(
                _config(),
                json_values=[
                    {"payload": {"id": "upload-1", "status": "PROCESSED", "workoutKey": "remote-1"}}
                ],
                responses=[
                    _Response({
                        "payload": {
                            "id": "upload-1",
                            "url": "https://storage.example/upload",
                            "method": "PUT",
                            "headers": {"x-ms-blob-type": "BlockBlob"},
                        }
                    }, status_code=201)
                ],
            )
            target = SuuntoTarget(client)
            blob_session = _Session(request_responses=[_Response(status_code=201)])
            target.upload_session = blob_session

            result = target.upload_file(fit_path, Activity("local", "1", "Morning Ride"), "local:1")

            self.assertEqual(
                result,
                UploadResult(
                    status="success",
                    remote_id="remote-1",
                    message="Suunto processed the uploaded workout",
                ),
            )
            self.assertEqual(client.api_calls[0][0:2], ("POST", "/v2/upload"))
            self.assertEqual(client.api_calls[0][2]["json"], {
                "description": "Morning Ride",
                "notifyUser": False,
            })
            self.assertEqual(blob_session.request_calls[0][0:2], ("PUT", "https://storage.example/upload"))
            self.assertEqual(blob_session.request_calls[0][2]["data"], fit_path.read_bytes())
            headers = blob_session.request_calls[0][2]["headers"]
            self.assertNotIn("Authorization", headers)
            self.assertNotIn("Ocp-Apim-Subscription-Key", headers)

    def test_rejects_unsupported_formats_and_bad_storage_urls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tcx_path = Path(temporary) / "ride.tcx"
            tcx_path.write_text("<TrainingCenterDatabase />", encoding="utf-8")
            target = SuuntoTarget(_ClientStub(_config()))
            result = target.upload_file(tcx_path, Activity("local", "1", "Ride"), "local:1")
            self.assertEqual(result.status, "failed")
            self.assertIn("FIT files only", result.message or "")

            fit_path = create_fit(Path(temporary) / "ride.fit")
            client = _ClientStub(
                _config(),
                responses=[
                    _Response({
                        "payload": {
                            "id": "upload-2",
                            "url": "http://storage.example/upload",
                            "method": "PUT",
                        }
                    }, status_code=201)
                ],
            )
            result = SuuntoTarget(client).upload_file(
                fit_path,
                Activity("local", "2", "Ride"),
                "local:2",
            )
            self.assertEqual(result.status, "failed")
            self.assertIn("HTTPS", result.message or "")


class SuuntoIntegrationTests(unittest.TestCase):
    def test_cli_accepts_suunto_source_and_fit_target_only(self) -> None:
        args = build_parser().parse_args(
            ["sync", "--source", "suunto", "--target", "suunto", "--format", "suunto=fit"]
        )
        self.assertEqual(args.source, ["suunto"])
        self.assertEqual(args.target, ["suunto"])
        check_args = build_parser().parse_args(
            ["check", "--source", "suunto", "--target", "suunto"]
        )
        self.assertEqual(check_args.source, ["suunto"])
        self.assertEqual(check_args.target, ["suunto"])

        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(["sync", "--format", "suunto=tcx"])

    def test_engine_registers_suunto_source_and_target_and_dry_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            "os.environ",
            {
                "SUUNTO_CLIENT_ID": "client-id",
                "SUUNTO_CLIENT_SECRET": "client-secret",
                "SUUNTO_REDIRECT_URI": "https://localhost/callback",
                "SUUNTO_SUBSCRIPTION_KEY": "subscription-key",
                "SYNC_DATA_DIR": str(Path(temporary) / ".data"),
                "SYNC_LOOKBACK_DAYS": "0",
            },
        ), patch("sport_sync_bridge.cli.configure_logging"), patch(
            "sport_sync_bridge.suunto_api.SuuntoClient.get_json",
            return_value={"payload": []},
        ), contextlib.redirect_stdout(io.StringIO()) as output:
            config = AppConfig.load(Path(temporary))
            engine = SyncEngine(config)
            try:
                self.assertIn("suunto", engine.sources)
                self.assertIn("suunto", engine.targets)
            finally:
                engine.close()

            status = cli_main(
                [
                    "sync",
                    "--dry-run",
                    "--source",
                    "suunto",
                    "--target",
                    "suunto",
                ]
            )

        self.assertEqual(status, 0)
        self.assertIn("done=0", output.getvalue())


if __name__ == "__main__":
    unittest.main()
