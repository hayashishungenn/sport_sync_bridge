from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import os
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
from sport_sync_bridge.formats import convert_activity_file, read_activity_file
from sport_sync_bridge.models import Activity, UploadResult
from sport_sync_bridge.nolio_api import NolioClient
from sport_sync_bridge.nolio_source import NolioSource
from sport_sync_bridge.nolio_target import NolioTarget
from tests.activity_fixtures import create_fit, create_tcx


class _Response:
    def __init__(self, payload: object | None = None, *, content: bytes = b"", status_code: int = 200):
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


class _NolioStub:
    def __init__(self, config: SimpleNamespace, *, json_values=None, response: _Response | None = None):
        self.config = config
        self.json_values = list(json_values or [])
        self.response = response or _Response({})
        self.json_calls: list[tuple[str, dict[str, object] | None]] = []
        self.api_calls: list[tuple[str, str, dict[str, object]]] = []

    def is_configured(self) -> bool:
        return True

    def authenticate(self) -> dict[str, object]:
        return {"id": 1}

    def get_json(self, path: str, *, params: dict[str, object] | None = None) -> object:
        self.json_calls.append((path, params))
        return self.json_values.pop(0)

    def api_request(self, method: str, path: str, **kwargs: object) -> _Response:
        self.api_calls.append((method, path, kwargs))
        return self.response


def _config(*, athlete_id: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(nolio_athlete_id=athlete_id)


class NolioApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = _State()
        self.config = SimpleNamespace(
            nolio_client_id="client-id",
            nolio_client_secret="client-secret",
            nolio_redirect_uri="https://localhost/callback",
            nolio_athlete_id=None,
        )
        self.client = NolioClient(self.config, self.state)

    def test_authorize_url_saves_random_csrf_state(self) -> None:
        parsed = urlparse(self.client.build_authorize_url())
        query = parse_qs(parsed.query)

        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "www.nolio.io")
        self.assertEqual(query["client_id"], ["client-id"])
        self.assertEqual(query["redirect_uri"], ["https://localhost/callback"])
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["state"], [self.state.get_value("nolio_oauth_state")])

    def test_exchange_uses_basic_auth_and_stores_tokens_without_returning_them(self) -> None:
        self.state.set_value("nolio_oauth_state", "expected-state")
        session = _Session(post_responses=[_Response({
            "access_token": "private-access",
            "refresh_token": "private-refresh",
            "expires_in": 86400,
            "scope": "read write",
        })])
        self.client.session = session

        result = self.client.exchange_code(" code ", "expected-state")

        self.assertEqual(result, {"expires_in": 86400, "scope": "read write"})
        self.assertEqual(self.state.get_value("nolio_access_token"), "private-access")
        self.assertEqual(self.state.get_value("nolio_refresh_token"), "private-refresh")
        self.assertEqual(self.state.get_value("nolio_scope"), "read write")
        self.assertEqual(self.state.get_value("nolio_oauth_state"), "")
        url, kwargs = session.post_calls[0]
        self.assertEqual(url, "https://www.nolio.io/api/token/")
        self.assertEqual(kwargs["auth"], ("client-id", "client-secret"))
        self.assertEqual(
            kwargs["data"],
            {"grant_type": "authorization_code", "code": "code", "redirect_uri": "https://localhost/callback"},
        )
        self.assertNotIn("access_token", result)

    def test_exchange_rejects_mismatched_state_before_network_request(self) -> None:
        self.state.set_value("nolio_oauth_state", "expected-state")
        session = _Session()
        self.client.session = session

        with self.assertRaisesRegex(ValueError, "did not match"):
            self.client.exchange_code("code", "other-state")

        self.assertEqual(session.post_calls, [])

    def test_expiring_access_token_refreshes_and_persists_rotated_refresh_token(self) -> None:
        self.state.set_value("nolio_access_token", "old-access")
        self.state.set_value("nolio_refresh_token", "old-refresh")
        self.state.set_value("nolio_expires_at", str(time.time() + 1))
        session = _Session(
            post_responses=[_Response({
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 86400,
            })],
            request_responses=[_Response({"id": 1})],
        )
        self.client.session = session

        self.assertEqual(self.client.get_json("/get/user/"), {"id": 1})
        self.assertEqual(self.state.get_value("nolio_access_token"), "new-access")
        self.assertEqual(self.state.get_value("nolio_refresh_token"), "new-refresh")
        self.assertEqual(session.post_calls[0][1]["data"], {
            "grant_type": "refresh_token",
            "refresh_token": "old-refresh",
        })
        self.assertEqual(session.request_calls[0][2]["headers"]["Authorization"], "Bearer new-access")

    def test_unauthorized_request_refreshes_once_and_retries(self) -> None:
        self.state.set_value("nolio_access_token", "old-access")
        self.state.set_value("nolio_refresh_token", "old-refresh")
        self.state.set_value("nolio_expires_at", str(time.time() + 3600))
        session = _Session(
            post_responses=[_Response({
                "access_token": "new-access",
                "refresh_token": "rotated-refresh",
                "expires_in": 86400,
            })],
            request_responses=[_Response({}, status_code=401), _Response({"id": 1})],
        )
        self.client.session = session

        self.assertEqual(self.client.get_json("/get/user/"), {"id": 1})
        self.assertEqual(len(session.request_calls), 2)
        self.assertEqual(session.request_calls[0][2]["headers"]["Authorization"], "Bearer old-access")
        self.assertEqual(session.request_calls[1][2]["headers"]["Authorization"], "Bearer new-access")
        self.assertEqual(self.state.get_value("nolio_refresh_token"), "rotated-refresh")


class NolioSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_lists_workouts_with_documented_filters_and_sport_mapping(self) -> None:
        client = _NolioStub(
            _config(athlete_id="456"),
            json_values=[
                [
                    {"nolio_id": 12, "name": "Run", "sport": "Running", "date_start": "2026-09-25", "sport_id": 2},
                    {"nolio_id": 11, "name": "Bike", "sport": "Bike", "date_start": "2026-09-24", "sport_id": 14},
                ]
            ],
        )
        source = NolioSource(_config(athlete_id="456"), client)

        activities = source.list_activities(
            datetime(2026, 9, 24, tzinfo=timezone.utc),
            datetime(2026, 9, 25, tzinfo=timezone.utc),
            500,
        )

        self.assertEqual(client.json_calls, [("/get/training/", {
            "limit": 500,
            "from": "2026-09-24",
            "to": "2026-09-25",
            "athlete_id": 456,
        })])
        self.assertEqual([activity.source_id for activity in activities], ["11", "12"])
        self.assertEqual([activity.sport_type for activity in activities], ["cycling", "running"])
        self.assertEqual(activities[0].start_time, datetime(2026, 9, 24, tzinfo=timezone.utc))

    def test_uses_nolio_documented_default_list_limit(self) -> None:
        client = _NolioStub(_config(), json_values=[[]])
        source = NolioSource(_config(), client)

        self.assertEqual(source.list_activities(None, None, None), [])
        self.assertEqual(client.json_calls, [("/get/training/", {"limit": 30})])

    def test_downloads_fit_without_forwarding_api_authorization_header(self) -> None:
        fit_path = create_fit(self.root / "fixture.fit")
        activity = Activity("nolio", "123", "Ride", "cycling")
        client = _NolioStub(_config(), json_values=[{"file_url": "https://cdn.example/file.fit?signature=x"}])
        source = NolioSource(_config(), client)
        response = _Response(content=fit_path.read_bytes())

        with patch.object(source.session, "get", return_value=response) as get:
            output = source.download_fit(activity, self.root / "download")

        self.assertTrue(output.is_file())
        self.assertTrue(output.read_bytes()[8:12] == b".FIT")
        self.assertNotIn("Authorization", get.call_args.kwargs.get("headers", {}))
        self.assertEqual(client.json_calls[0], ("/get/training/info/", {"id": "123"}))

    def test_downloads_tcx_and_converts_it_to_pipeline_fit(self) -> None:
        tcx_path = create_tcx(self.root / "source.tcx")
        activity = Activity("nolio", "124", "Swim", "swimming")
        client = _NolioStub(_config(), json_values=[{"file_url": "https://cdn.example/file.tcx?signature=x"}])
        source = NolioSource(_config(), client)
        response = _Response(content=tcx_path.read_bytes())

        with patch.object(source.session, "get", return_value=response):
            output = source.download_fit(activity, self.root / "download")

        self.assertEqual(output.suffix, ".fit")
        self.assertTrue(output.read_bytes()[8:12] == b".FIT")
        self.assertEqual(read_activity_file(output).track_points[0].heart_rate_bpm, 150)

    def test_rejects_non_https_download_url(self) -> None:
        client = _NolioStub(_config(), json_values=[{"file_url": "http://cdn.example/file.fit"}])
        source = NolioSource(_config(), client)

        with self.assertRaisesRegex(RuntimeError, "HTTPS"):
            source.download_fit(Activity("nolio", "123", "Ride"), self.root / "download")

    def test_rejects_empty_or_malformed_workout_lists(self) -> None:
        source = NolioSource(_config(), _NolioStub(_config(), json_values=[{"training": []}]))

        with self.assertRaisesRegex(RuntimeError, "JSON array"):
            source.list_activities(None, None, None)

        source = NolioSource(_config(), _NolioStub(_config(), json_values=[[{"name": "missing id"}]]))
        with self.assertRaisesRegex(RuntimeError, "nolio_id"):
            source.list_activities(None, None, None)


class NolioTargetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = _config()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_uploads_fit_as_base64_with_stable_partner_id(self) -> None:
        fit_path = create_fit(self.root / "ride.fit")
        client = _NolioStub(self.config, response=_Response({"nolio_id": 818}, status_code=202))
        target = NolioTarget(client)

        result = target.upload_file(fit_path, Activity("local", "fingerprint", "Ride"), "local:fingerprint")

        method, path, kwargs = client.api_calls[0]
        payload = kwargs["json"]
        self.assertEqual((method, path), ("POST", "/upload/file/"))
        self.assertEqual(payload["format"], "fit")
        self.assertEqual(base64.b64decode(payload["data"]), fit_path.read_bytes())
        expected_partner_id = "sport_sync_bridge:" + hashlib.sha256(
            b"local:fingerprint"
        ).hexdigest()
        self.assertEqual(payload["id_partner"], expected_partner_id)
        self.assertEqual(result, UploadResult(status="success", remote_id="818", message="Nolio accepted the file for processing"))

    def test_uploads_tcx_and_classifies_documented_duplicate_response(self) -> None:
        fit_path = create_fit(self.root / "ride.fit")
        tcx_path = self.root / "ride.tcx"
        convert_activity_file(fit_path, tcx_path, "tcx")
        client = _NolioStub(self.config, response=_Response({"error": "Training already imported"}, status_code=400))
        target = NolioTarget(client)

        result = target.upload_file(tcx_path, Activity("local", "fingerprint", "Ride"), "local:fingerprint")

        self.assertEqual(client.api_calls[0][2]["json"]["format"], "tcx")
        self.assertEqual(base64.b64decode(client.api_calls[0][2]["json"]["data"]), tcx_path.read_bytes())
        self.assertEqual(result.status, "duplicate")

    def test_rejects_gpx_upload(self) -> None:
        gpx_path = self.root / "ride.gpx"
        gpx_path.write_text("<gpx />", encoding="utf-8")
        client = _NolioStub(self.config)
        target = NolioTarget(client)

        result = target.upload_file(gpx_path, Activity("local", "fingerprint", "Ride"), "local:fingerprint")

        self.assertEqual(result.status, "failed")
        self.assertIn("FIT and TCX", result.message or "")
        self.assertEqual(client.api_calls, [])

    def test_uploads_for_configured_athlete_with_numeric_api_identifier(self) -> None:
        fit_path = create_fit(self.root / "ride.fit")
        config = _config(athlete_id="456")
        client = _NolioStub(config, response=_Response({}, status_code=202))
        target = NolioTarget(client)

        target.upload_file(fit_path, Activity("local", "fingerprint", "Ride"), "local:fingerprint")

        self.assertEqual(client.api_calls[0][2]["json"]["athlete_id"], 456)

    def test_rejects_invalid_configured_athlete_identifier(self) -> None:
        fit_path = create_fit(self.root / "ride.fit")
        client = _NolioStub(_config(athlete_id="coach"))
        target = NolioTarget(client)

        result = target.upload_file(fit_path, Activity("local", "fingerprint", "Ride"), "local:fingerprint")

        self.assertEqual(result.status, "failed")
        self.assertIn("NOLIO_ATHLETE_ID", result.message or "")
        self.assertEqual(client.api_calls, [])


class NolioIntegrationTests(unittest.TestCase):
    def test_engine_registers_source_and_target_when_oauth_client_is_configured(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ,
            {
                "NOLIO_CLIENT_ID": "client-id",
                "NOLIO_CLIENT_SECRET": "client-secret",
                "NOLIO_REDIRECT_URI": "https://localhost/callback",
                "SYNC_DATA_DIR": str(Path(temporary) / ".data"),
            },
            clear=True,
        ):
            config = AppConfig.load(Path(temporary))
            engine = SyncEngine(config)
            try:
                self.assertIn("nolio", engine.sources)
                self.assertIn("nolio", engine.targets)
            finally:
                engine.close()

    def test_cli_selects_nolio_and_rejects_unsupported_gpx_target_format(self) -> None:
        args = build_parser().parse_args(
            ["sync", "--source", "nolio", "--target", "nolio", "--format", "nolio=tcx"]
        )
        self.assertEqual(args.source, ["nolio"])
        self.assertEqual(args.target, ["nolio"])
        check_args = build_parser().parse_args(["check", "--source", "nolio", "--target", "nolio"])
        self.assertEqual(check_args.source, ["nolio"])
        self.assertEqual(check_args.target, ["nolio"])

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
            build_parser().parse_args(["sync", "--format", "nolio=gpx"])
        self.assertIn("only support FIT and TCX", stderr.getvalue())

    def test_cli_dry_run_lists_nolio_without_network_auth_or_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ,
            {
                "NOLIO_CLIENT_ID": "client-id",
                "NOLIO_CLIENT_SECRET": "client-secret",
                "NOLIO_REDIRECT_URI": "https://localhost/callback",
                "SYNC_DATA_DIR": str(Path(temporary) / ".data"),
                "SYNC_LOOKBACK_DAYS": "0",
            },
        ), patch("sport_sync_bridge.cli.configure_logging"), patch(
            "sport_sync_bridge.nolio_api.NolioClient.get_json", return_value=[]
        ), contextlib.redirect_stdout(io.StringIO()) as output:
            status = cli_main(
                [
                    "sync",
                    "--dry-run",
                    "--source",
                    "nolio",
                    "--target",
                    "nolio",
                    "--format",
                    "nolio=tcx",
                ]
            )

        self.assertEqual(status, 0)
        self.assertIn("done=0", output.getvalue())


if __name__ == "__main__":
    unittest.main()
