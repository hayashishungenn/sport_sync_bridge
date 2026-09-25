from __future__ import annotations

import base64
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
from sport_sync_bridge.models import Activity, UploadResult
from sport_sync_bridge.wahoo_target import WahooTarget


class _Response:
    def __init__(self, payload: object, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class _Session:
    def __init__(self, post_responses=None, get_responses=None):
        self.post_responses = list(post_responses or [])
        self.get_responses = list(get_responses or [])
        self.post_calls: list[tuple[str, dict]] = []
        self.get_calls: list[tuple[str, dict]] = []
        self.headers: dict[str, str] = {}

    def post(self, url: str, **kwargs):
        self.post_calls.append((url, kwargs))
        return self.post_responses.pop(0)

    def get(self, url: str, **kwargs):
        self.get_calls.append((url, kwargs))
        return self.get_responses.pop(0)


class _StateDB:
    def __init__(self):
        self.values: dict[str, str] = {}

    def get_value(self, key: str):
        return self.values.get(key)

    def set_value(self, key: str, value: str) -> None:
        self.values[key] = value


class WahooTargetTests(unittest.TestCase):
    def _config(self, **overrides):
        values = {
            "wahoo_client_id": "client-id",
            "wahoo_client_secret": "client-secret",
            "wahoo_redirect_uri": "http://localhost/",
            "wahoo_access_token": "access-token",
            "wahoo_refresh_token": None,
            "wahoo_expires_at": "4102444800",
            "wahoo_scope": None,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def _target(self, config=None, session=None, state_db=None):
        target = WahooTarget(config or self._config(), state_db or _StateDB())
        target.session = session or _Session()
        return target

    def test_builds_authorization_url_with_read_and_upload_scopes(self) -> None:
        target = self._target()

        parsed = urlparse(target.build_authorize_url())

        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "api.wahooligan.com")
        self.assertEqual(parsed.path, "/oauth/authorize")
        self.assertEqual(
            parse_qs(parsed.query),
            {
                "client_id": ["client-id"],
                "redirect_uri": ["http://localhost/"],
                "scope": ["user_read workouts_read workouts_write"],
                "response_type": ["code"],
            },
        )

    def test_exchanges_code_and_persists_rotating_tokens(self) -> None:
        state_db = _StateDB()
        session = _Session(
            post_responses=[
                _Response(
                    {
                        "access_token": "new-access",
                        "refresh_token": "new-refresh",
                        "expires_in": 7200,
                        "scope": "user_read workouts_read workouts_write",
                    }
                )
            ]
        )
        target = self._target(session=session, state_db=state_db)

        target.exchange_code(" auth-code ")

        self.assertEqual(session.post_calls[0][0], WahooTarget.token_url)
        self.assertEqual(
            session.post_calls[0][1]["data"],
            {
                "client_id": "client-id",
                "client_secret": "client-secret",
                "code": "auth-code",
                "redirect_uri": "http://localhost/",
                "grant_type": "authorization_code",
            },
        )
        self.assertEqual(state_db.values["wahoo_access_token"], "new-access")
        self.assertEqual(state_db.values["wahoo_refresh_token"], "new-refresh")
        self.assertEqual(state_db.values["wahoo_scope"], "user_read workouts_read workouts_write")
        self.assertGreater(float(state_db.values["wahoo_expires_at"]), datetime.now(timezone.utc).timestamp())

    def test_accepts_read_only_scope_for_source_but_rejects_target_upload(self) -> None:
        state_db = _StateDB()
        target = self._target(state_db=state_db)

        target._persist_token_payload(
            {
                "access_token": "access",
                "refresh_token": "refresh",
                "scope": "user_read workouts_read",
            }
        )

        self.assertEqual(state_db.values["wahoo_scope"], "user_read workouts_read")
        target.authenticate_for_scope("workouts_read")
        with self.assertRaisesRegex(RuntimeError, "workouts_write"):
            target.authenticate()

    def test_exchange_records_requested_scope_when_token_response_omits_it(self) -> None:
        state_db = _StateDB()
        session = _Session(post_responses=[_Response({"access_token": "new-access"})])
        target = self._target(session=session, state_db=state_db)

        target.exchange_code("auth-code")

        self.assertEqual(state_db.values["wahoo_scope"], "user_read workouts_read workouts_write")

    def test_uploads_base64_fit_and_polls_until_complete(self) -> None:
        session = _Session(
            post_responses=[_Response({"token": "upload-token"})],
            get_responses=[
                _Response({"status": "pending"}),
                _Response({"status": "complete", "workout_id": 785}),
            ],
        )
        target = self._target(session=session)
        activity = Activity(source="local", source_id="abc", name="Morning ride")

        with tempfile.TemporaryDirectory() as directory, patch("sport_sync_bridge.wahoo_target.time.sleep"):
            fit_path = Path(directory) / "ride.fit"
            fit_path.write_bytes(b"sample-fit-payload")

            result = target.upload_file(fit_path, activity, external_id="local:abc")

        self.assertEqual(result, UploadResult(status="success", remote_id="785"))
        self.assertEqual(session.post_calls[0][0], "https://api.wahooligan.com/v1/workout_file_uploads")
        self.assertEqual(
            session.post_calls[0][1]["data"],
            {
                "workout_file_upload[file]": "data:application/vnd.fit;base64,"
                + base64.b64encode(b"sample-fit-payload").decode("ascii"),
                "workout_file_upload[filename]": "ride.fit",
                "workout_file_upload[workout_name]": "Morning ride",
            },
        )
        self.assertEqual(
            session.get_calls[0][0],
            "https://api.wahooligan.com/v1/workout_file_uploads/upload-token",
        )
        self.assertEqual(session.post_calls[0][1]["headers"]["Authorization"], "Bearer access-token")

    def test_maps_duplicate_and_processing_error_statuses(self) -> None:
        duplicate_target = self._target(session=_Session(post_responses=[_Response({"status": "duplicate"})]))
        error_target = self._target(
            session=_Session(
                post_responses=[_Response({"token": "upload-token"})],
                get_responses=[_Response({"status": "error", "error": "invalid FIT"})],
            )
        )
        activity = Activity(source="local", source_id="abc", name="Morning ride")

        with tempfile.TemporaryDirectory() as directory, patch("sport_sync_bridge.wahoo_target.time.sleep"):
            fit_path = Path(directory) / "ride.fit"
            fit_path.write_bytes(b"sample-fit-payload")
            duplicate = duplicate_target.upload_file(fit_path, activity, external_id="local:abc")
            failed = error_target.upload_file(fit_path, activity, external_id="local:abc")

        self.assertEqual(duplicate.status, "duplicate")
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.message, "invalid FIT")

    def test_refreshes_expired_token_and_saves_rotated_refresh_token(self) -> None:
        state_db = _StateDB()
        session = _Session(
            post_responses=[
                _Response(
                    {
                        "access_token": "refreshed-access",
                        "refresh_token": "rotated-refresh",
                        "expires_in": 7200,
                    }
                )
            ]
        )
        target = self._target(
            config=self._config(
                wahoo_access_token="expired-access",
                wahoo_refresh_token="old-refresh",
                wahoo_expires_at="1",
            ),
            session=session,
            state_db=state_db,
        )

        target.authenticate()

        self.assertEqual(state_db.values["wahoo_access_token"], "refreshed-access")
        self.assertEqual(state_db.values["wahoo_refresh_token"], "rotated-refresh")
        self.assertEqual(session.post_calls[0][1]["data"]["grant_type"], "refresh_token")

    def test_retries_unauthorized_upload_with_refreshed_access_token(self) -> None:
        session = _Session(
            post_responses=[
                _Response({}, status_code=401),
                _Response(
                    {
                        "access_token": "refreshed-access",
                        "refresh_token": "rotated-refresh",
                        "expires_in": 7200,
                    }
                ),
                _Response({"token": "upload-token"}),
            ],
            get_responses=[_Response({"status": "complete", "workout_id": 786})],
        )
        target = self._target(
            config=self._config(wahoo_refresh_token="old-refresh"),
            session=session,
        )
        activity = Activity(source="local", source_id="abc", name="Morning ride")

        with tempfile.TemporaryDirectory() as directory:
            fit_path = Path(directory) / "ride.fit"
            fit_path.write_bytes(b"sample-fit-payload")
            result = target.upload_file(fit_path, activity, external_id="local:abc")

        self.assertEqual(result, UploadResult(status="success", remote_id="786"))
        upload_calls = [call for call in session.post_calls if call[0].endswith("/v1/workout_file_uploads")]
        self.assertEqual(
            [call[1]["headers"]["Authorization"] for call in upload_calls],
            ["Bearer access-token", "Bearer refreshed-access"],
        )

    def test_rejects_non_fit_upload_without_network_request(self) -> None:
        session = _Session()
        target = self._target(session=session)
        activity = Activity(source="local", source_id="abc", name="Morning ride")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ride.gpx"
            path.write_text("<gpx/>", encoding="utf-8")
            result = target.upload_file(path, activity, external_id="local:abc")

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.message, "Wahoo accepts FIT uploads only")
        self.assertEqual(session.post_calls, [])
        self.assertEqual(session.get_calls, [])

    def test_engine_and_cli_register_wahoo_and_limit_format_to_fit(self) -> None:
        self.assertEqual(
            build_parser().parse_args(["sync", "--target", "wahoo", "--format", "wahoo=fit"]).target,
            ["wahoo"],
        )
        self.assertEqual(build_parser().parse_args(["check", "--target", "wahoo"]).target, ["wahoo"])

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "SYNC_DATA_DIR": str(Path(directory) / "data"),
                "SYNC_SOURCES": "local",
                "SYNC_TARGETS": "wahoo",
                "WAHOO_CLIENT_ID": "test-client",
                "WAHOO_CLIENT_SECRET": "test-secret",
            },
        ):
            config = AppConfig.load(Path(directory))
            engine = SyncEngine(config)
            try:
                self.assertIn("wahoo", engine.targets)
                self.assertIn("wahoo", engine.sources)
                with self.assertRaisesRegex(ValueError, "Unsupported target format mapping"):
                    engine.sync_once(
                        sources=["local"],
                        targets=["wahoo"],
                        dry_run=True,
                        target_formats={"wahoo": "gpx"},
                    )
                self.assertEqual(
                    engine.sync_once(
                        sources=["local"],
                        targets=["wahoo"],
                        dry_run=True,
                        target_formats={"wahoo": "fit"},
                    ),
                    0,
                )
            finally:
                engine.close()


if __name__ == "__main__":
    unittest.main()
