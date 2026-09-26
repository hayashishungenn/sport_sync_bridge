from __future__ import annotations

import base64
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

from sport_sync_bridge.cli import build_parser
from sport_sync_bridge.config import AppConfig
from sport_sync_bridge.engine import SyncEngine
from sport_sync_bridge.models import Activity
from sport_sync_bridge.mywhoosh_source import MyWhooshSource
from sport_sync_bridge.utils import fit_signature_ok
from tests.activity_fixtures import create_fit


class _Response:
    def __init__(self, payload: object | None = None, *, content: bytes = b"", status: int = 200):
        self.payload = payload
        self.content = content
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self) -> object:
        if self.payload is None:
            raise ValueError("missing JSON")
        return self.payload


class _Session:
    def __init__(self, *, post_responses=None, get_responses=None):
        self.headers: dict[str, str] = {}
        self.post_responses = list(post_responses or [])
        self.get_responses = list(get_responses or [])
        self.post_calls: list[tuple[str, dict]] = []
        self.get_calls: list[tuple[str, dict]] = []

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


def _token(expiry: float | None = None) -> str:
    claims = {"exp": expiry or time.time() + 3600, "userId": "account-1"}
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


class MyWhooshSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.state = _State()
        self.config = SimpleNamespace(mywhoosh_username="rider@example.test", mywhoosh_password="secret")
        self.source = MyWhooshSource(self.config, self.state)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_login_uses_apk_request_fields_and_caches_token_without_password(self) -> None:
        token = _token()
        session = _Session(post_responses=[_Response({"AccessToken": token, "DeviceId": "server-device"})])
        self.source.session = session

        self.source.authenticate()
        self.source.authenticate()

        self.assertEqual(len(session.post_calls), 1)
        url, kwargs = session.post_calls[0]
        self.assertEqual(url, MyWhooshSource.login_url)
        login_body = kwargs["json"]
        self.assertEqual(
            {key: value for key, value in login_body.items() if key != "DeviceId"},
            {"UserName": "rider@example.test", "Password": "secret", "Source": "connect"},
        )
        self.assertRegex(login_body["DeviceId"], r"^[0-9a-f-]{36}$")
        self.assertEqual(self.state.get_value(MyWhooshSource.device_id_key), "server-device")
        self.assertEqual(self.state.get_value(MyWhooshSource.access_token_key), token)
        self.assertNotIn("secret", json.dumps(self.state.values))

    def test_authentication_requires_credentials(self) -> None:
        self.source.config = SimpleNamespace(mywhoosh_username=None, mywhoosh_password=None)
        with self.assertRaisesRegex(RuntimeError, "MYWHOOSH_USERNAME"):
            self.source.authenticate()

    def test_lists_pages_maps_sports_and_filters_date_range(self) -> None:
        token = _token()
        login = _Response({"AccessToken": token, "DeviceId": "device-1"})
        first_page = _Response(
            {
                "data": {
                    "results": [
                        {
                            "id": 100,
                            "activityFileId": "file-100",
                            "title": "Morning ride",
                            "date": "2026-09-24T07:00:00Z",
                            "sportType": "ride",
                        }
                    ],
                    "currentPage": 1,
                    "totalPages": 2,
                }
            }
        )
        second_page = _Response(
            {
                "data": {
                    "results": [
                        {
                            "activityId": 99,
                            "userFileId": "file-99",
                            "title": "Old run",
                            "startDateTime": "2026-09-20T07:00:00Z",
                            "sportType": "run",
                        }
                    ],
                    "currentPage": 2,
                    "totalPages": 2,
                }
            }
        )
        session = _Session(post_responses=[login, first_page, second_page])
        self.source.session = session

        activities = self.source.list_activities(
            datetime(2026, 9, 24, tzinfo=timezone.utc),
            datetime(2026, 9, 24, 23, 59, tzinfo=timezone.utc),
            None,
        )

        self.assertEqual([item.source_id for item in activities], ["100"])
        activity = activities[0]
        self.assertEqual(activity.name, "Morning ride")
        self.assertEqual(activity.sport_type, "cycling")
        self.assertEqual(activity.raw["fileId"], "file-100")
        self.assertEqual(len(session.post_calls), 3)
        self.assertEqual(session.post_calls[1][0], f"{MyWhooshSource.api_root}/rider/profile/activities")
        self.assertEqual(session.post_calls[1][1]["json"], {"type": "", "page": 1, "sortDate": "DESC", "limit": 50})
        self.assertEqual(session.post_calls[2][1]["json"]["page"], 2)
        self.assertEqual(session.post_calls[1][1]["headers"]["Source"], "connect")

    def test_limit_zero_does_not_contact_api(self) -> None:
        self.source.session = _Session()
        self.assertEqual(self.source.list_activities(None, None, 0), [])
        self.assertEqual(self.source.session.post_calls, [])

    def test_empty_account_with_zero_pages_returns_no_activities(self) -> None:
        self.source.session = _Session(
            post_responses=[
                _Response({"AccessToken": _token(), "DeviceId": "device-1"}),
                _Response({"data": {"results": [], "currentPage": 0, "totalPages": 0}}),
            ]
        )
        self.assertEqual(self.source.list_activities(None, None, None), [])

    def test_rejects_api_error_envelope_even_if_results_is_empty(self) -> None:
        self.source.session = _Session(
            post_responses=[
                _Response({"AccessToken": _token(), "DeviceId": "device-1"}),
                _Response({"code": 500, "data": {"results": []}}),
            ]
        )
        with self.assertRaisesRegex(RuntimeError, "activity list page 1 failed"):
            self.source.list_activities(None, None, None)

    def test_activity_requires_a_file_id(self) -> None:
        self.source.session = _Session(
            post_responses=[_Response({"AccessToken": _token(), "DeviceId": "device-1"}), _Response({"data": {"results": [{"id": 12}]}})]
        )
        with self.assertRaisesRegex(RuntimeError, "FIT file ID"):
            self.source.list_activities(None, None, 1)

    def test_retries_one_unauthorized_api_request_with_a_fresh_login(self) -> None:
        old_token = _token()
        new_token = _token()
        self.state.set_value(MyWhooshSource.access_token_key, old_token)
        self.state.set_value(MyWhooshSource.token_expiry_key, str(time.time() + 3600))
        session = _Session(
            post_responses=[
                _Response(status=401),
                _Response({"AccessToken": new_token, "DeviceId": "device-1"}),
                _Response({"data": {"results": [], "currentPage": 1, "totalPages": 1}}),
            ]
        )
        self.source.session = session

        self.assertEqual(self.source.list_activities(None, None, None), [])
        self.assertEqual(session.post_calls[0][0], f"{MyWhooshSource.api_root}/rider/profile/activities")
        self.assertEqual(session.post_calls[1][0], MyWhooshSource.login_url)
        self.assertEqual(session.post_calls[2][1]["headers"]["Authorization"], f"Bearer {new_token}")

    def test_downloads_and_validates_fit_without_forwarding_bearer_to_file_host(self) -> None:
        fit_path = create_fit(self.root / "source.fit")
        fit_bytes = fit_path.read_bytes()
        self.state.set_value(MyWhooshSource.access_token_key, _token())
        self.state.set_value(MyWhooshSource.token_expiry_key, str(time.time() + 3600))
        session = _Session(
            post_responses=[_Response({"data": "https://cdn.example.test/activity.fit"})],
            get_responses=[_Response(content=fit_bytes)],
        )
        self.source.session = session
        activity = Activity(
            source="mywhoosh",
            source_id="100",
            name="ride",
            raw={"fileId": "file-100"},
        )

        output = self.source.download_fit(activity, self.root / "downloads")

        self.assertTrue(fit_signature_ok(output))
        self.assertEqual(output.read_bytes(), fit_bytes)
        self.assertEqual(session.post_calls[0][1]["json"], {"fileId": "file-100"})
        self.assertEqual(session.get_calls[0][0], "https://cdn.example.test/activity.fit")
        self.assertNotIn("headers", session.get_calls[0][1])

    def test_rejects_insecure_download_url(self) -> None:
        self.state.set_value(MyWhooshSource.access_token_key, _token())
        self.state.set_value(MyWhooshSource.token_expiry_key, str(time.time() + 3600))
        self.source.session = _Session(
            post_responses=[_Response({"data": "http://cdn.example.test/activity.fit"})]
        )
        activity = Activity("mywhoosh", "100", "ride", raw={"fileId": "file-100"})
        with self.assertRaisesRegex(RuntimeError, "invalid activity download URL"):
            self.source.download_fit(activity, self.root / "downloads")

    def test_rejects_corrupt_download_and_removes_temporary_file(self) -> None:
        self.state.set_value(MyWhooshSource.access_token_key, _token())
        self.state.set_value(MyWhooshSource.token_expiry_key, str(time.time() + 3600))
        self.source.session = _Session(
            post_responses=[_Response({"data": "https://cdn.example.test/activity.fit"})],
            get_responses=[_Response(content=b"not a FIT file" * 20)],
        )
        activity = Activity("mywhoosh", "100", "ride", raw={"fileId": "file-100"})

        with self.assertRaisesRegex(RuntimeError, "invalid FIT file"):
            self.source.download_fit(activity, self.root / "downloads")

        activity_dir = self.root / "downloads" / "mywhoosh"
        self.assertEqual(list(activity_dir.iterdir()), [])


class MyWhooshIntegrationTests(unittest.TestCase):
    def test_config_loads_credentials_from_environment_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"MYWHOOSH_USERNAME": "rider", "MYWHOOSH_PASSWORD": "pass"},
            clear=True,
        ):
            config = AppConfig.load(Path(directory))
        self.assertEqual(config.mywhoosh_username, "rider")
        self.assertEqual(config.mywhoosh_password, "pass")

    def test_engine_registers_mywhoosh_when_credentials_are_configured(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "MYWHOOSH_USERNAME": "rider",
                "MYWHOOSH_PASSWORD": "pass",
                "SYNC_DATA_DIR": ".data",
                "SYNC_SOURCES": "mywhoosh",
            },
            clear=True,
        ):
            engine = SyncEngine(AppConfig.load(Path(directory)))
            try:
                self.assertIn("mywhoosh", engine.sources)
            finally:
                engine.close()

    def test_sync_and_check_commands_accept_mywhoosh_source(self) -> None:
        parser = build_parser()
        sync_args = parser.parse_args(["sync", "--source", "mywhoosh", "--dry-run"])
        check_args = parser.parse_args(["check", "--source", "mywhoosh"])
        self.assertEqual(sync_args.source, ["mywhoosh"])
        self.assertEqual(check_args.source, ["mywhoosh"])
