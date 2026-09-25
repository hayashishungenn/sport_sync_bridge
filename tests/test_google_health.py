from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from sport_sync_bridge.cli import build_parser
from sport_sync_bridge.config import AppConfig
from sport_sync_bridge.engine import SyncEngine
from sport_sync_bridge.formats import read_activity_file
from sport_sync_bridge.google_health import (
    GOOGLE_HEALTH_SCOPES,
    GoogleHealthClient,
    GoogleHealthSource,
)
from sport_sync_bridge.models import Activity
from sport_sync_bridge.state import StateDB
from sport_sync_bridge.utils import fit_signature_ok


class GoogleHealthTests(unittest.TestCase):
    def _state_db(self) -> StateDB:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state_db = StateDB(Path(directory.name) / "state.db")
        self.addCleanup(state_db.close)
        return state_db

    def _client(self, state_db: StateDB) -> GoogleHealthClient:
        config = cast(
            AppConfig,
            SimpleNamespace(
                google_health_client_id="client-id",
                google_health_client_secret="client-secret",
                google_health_redirect_uri="https://www.google.com",
            ),
        )
        return GoogleHealthClient(config, state_db)

    def _source(self) -> tuple[GoogleHealthSource, GoogleHealthClient]:
        config = cast(AppConfig, SimpleNamespace())
        client = cast(GoogleHealthClient, SimpleNamespace())
        return GoogleHealthSource(config, client), client

    def test_authorization_url_requests_offline_activity_and_location_read_access(self) -> None:
        state_db = self._state_db()
        client = self._client(state_db)
        authorization_url = client.build_authorize_url()
        query = parse_qs(urlparse(authorization_url).query)

        self.assertEqual(urlparse(authorization_url).netloc, "accounts.google.com")
        self.assertEqual(query["client_id"], ["client-id"])
        self.assertEqual(query["redirect_uri"], ["https://www.google.com"])
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(set(query["scope"][0].split()), set(GOOGLE_HEALTH_SCOPES))
        self.assertEqual(query["access_type"], ["offline"])
        self.assertEqual(query["include_granted_scopes"], ["true"])
        self.assertEqual(query["prompt"], ["consent"])
        code_verifier = state_db.get_value(client.oauth_code_verifier_key) or ""
        challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["code_challenge"], [challenge.rstrip(b"=").decode()])
        self.assertEqual(state_db.get_value(client.oauth_state_key), query["state"][0])

    def test_exchange_rejects_a_state_that_does_not_match(self) -> None:
        state_db = self._state_db()
        client = self._client(state_db)
        state_db.set_value(client.oauth_state_key, "expected-state")

        with patch("google_auth_oauthlib.flow.Flow.from_client_config") as create_flow:
            with self.assertRaisesRegex(RuntimeError, "state does not match"):
                client.exchange_code("temporary-code", "wrong-state")

        create_flow.assert_not_called()

    def test_exchange_saves_only_local_token_material_after_matching_state(self) -> None:
        state_db = self._state_db()
        client = self._client(state_db)
        state_db.set_value(client.oauth_state_key, "expected-state")
        credentials = SimpleNamespace(
            token="access-token",
            refresh_token="refresh-token",
            expiry=None,
            granted_scopes=list(GOOGLE_HEALTH_SCOPES),
            scopes=list(GOOGLE_HEALTH_SCOPES),
        )
        flow = SimpleNamespace(credentials=credentials, fetch_token=Mock())
        state_db.set_value(client.oauth_code_verifier_key, "test-code-verifier")

        with patch("google_auth_oauthlib.flow.Flow.from_client_config", return_value=flow) as create_flow:
            result = client.exchange_code("temporary-code", "expected-state")

        flow.fetch_token.assert_called_once_with(code="temporary-code")
        self.assertEqual(create_flow.call_args.kwargs["state"], "expected-state")
        self.assertEqual(create_flow.call_args.kwargs["code_verifier"], "test-code-verifier")
        self.assertEqual(result["scopes"], list(GOOGLE_HEALTH_SCOPES))
        self.assertEqual(state_db.get_value(client.oauth_state_key), "")
        self.assertEqual(state_db.get_value(client.oauth_code_verifier_key), "")
        stored = json.loads(state_db.get_value(client.credentials_key) or "{}")
        self.assertEqual(stored["refresh_token"], "refresh-token")
        self.assertNotIn("client_secret", stored)
        self.assertNotIn("access_token", result)

    def test_exchange_rejects_partial_scope_consent(self) -> None:
        state_db = self._state_db()
        client = self._client(state_db)
        state_db.set_value(client.oauth_state_key, "expected-state")
        credentials = SimpleNamespace(
            token="access-token",
            refresh_token="refresh-token",
            expiry=None,
            granted_scopes=[GOOGLE_HEALTH_SCOPES[0]],
            scopes=list(GOOGLE_HEALTH_SCOPES),
        )
        flow = SimpleNamespace(credentials=credentials, fetch_token=Mock())

        with patch("google_auth_oauthlib.flow.Flow.from_client_config", return_value=flow):
            with self.assertRaisesRegex(RuntimeError, "both activity and location"):
                client.exchange_code("temporary-code", "expected-state")

        self.assertIsNone(state_db.get_value(client.credentials_key))

    def test_api_uses_google_authorized_session_and_persists_refreshed_access_token(self) -> None:
        state_db = self._state_db()
        client = self._client(state_db)
        state_db.set_value(
            client.credentials_key,
            json.dumps(
                {
                    "token": "expired-access-token",
                    "refresh_token": "refresh-token",
                    "expiry": None,
                    "scopes": list(GOOGLE_HEALTH_SCOPES),
                }
            ),
        )
        response = SimpleNamespace(
            status_code=200,
            json=lambda: {"dataPoints": [], "nextPageToken": ""},
        )
        captured: dict[str, object] = {}

        class FakeAuthorizedSession:
            def __init__(self, credentials: object):
                self.credentials = credentials

            def get(self, url: str, *, params: object, timeout: int) -> SimpleNamespace:
                captured["request"] = (url, params, timeout)
                setattr(self.credentials, "token", "refreshed-access-token")
                setattr(self.credentials, "expiry", datetime(2026, 9, 26, tzinfo=timezone.utc))
                return response

            def close(self) -> None:
                captured["closed"] = True

        with patch("google.auth.transport.requests.AuthorizedSession", FakeAuthorizedSession):
            payload = client.get_json(
                "/users/me/dataTypes/exercise/dataPoints",
                params={"pageSize": 25},
            )

        self.assertEqual(payload["dataPoints"], [])
        self.assertEqual(
            captured["request"],
            (
                "https://health.googleapis.com/v4/users/me/dataTypes/exercise/dataPoints",
                {"pageSize": 25},
                30,
            ),
        )
        self.assertTrue(captured["closed"])
        stored = json.loads(state_db.get_value(client.credentials_key) or "{}")
        self.assertEqual(stored["token"], "refreshed-access-token")
        self.assertEqual(stored["refresh_token"], "refresh-token")
        self.assertNotIn("client_secret", stored)

    def test_expired_or_revoked_refresh_token_prompts_for_reauthorization(self) -> None:
        state_db = self._state_db()
        client = self._client(state_db)
        state_db.set_value(
            client.credentials_key,
            json.dumps(
                {
                    "token": "expired-access-token",
                    "refresh_token": "expired-refresh-token",
                    "expiry": None,
                    "scopes": list(GOOGLE_HEALTH_SCOPES),
                }
            ),
        )

        from google.auth.exceptions import RefreshError

        credentials = SimpleNamespace(
            valid=False,
            refresh=Mock(side_effect=RefreshError("invalid_grant: Token has been expired or revoked")),
        )
        with patch("google.oauth2.credentials.Credentials.from_authorized_user_info", return_value=credentials):
            with self.assertRaisesRegex(RuntimeError, "authorize again"):
                client.authenticate()

    def test_activity_listing_pages_and_stops_after_the_requested_start_time(self) -> None:
        source, client = self._source()
        client.get_json = Mock(
            side_effect=[
                {
                    "dataPoints": [
                        _exercise("124", "2026-01-04T08:00:00Z", "BIKING"),
                    ],
                    "nextPageToken": "page-2",
                },
                {
                    "dataPoints": [
                        _exercise("123", "2026-01-03T08:00:00Z", "RUNNING"),
                        _exercise("122", "2026-01-02T08:00:00Z", "HIKING"),
                    ],
                    "nextPageToken": "page-3",
                },
            ]
        )

        activities = source.list_activities(
            datetime(2026, 1, 3, tzinfo=timezone.utc),
            datetime(2026, 1, 5, tzinfo=timezone.utc),
            None,
        )

        self.assertEqual([activity.source_id for activity in activities], ["123", "124"])
        self.assertEqual([activity.sport_type for activity in activities], ["running", "cycling"])
        self.assertEqual(client.get_json.call_count, 2)
        self.assertEqual(client.get_json.call_args_list[0].kwargs["params"], {"pageSize": 25})
        self.assertEqual(
            client.get_json.call_args_list[1].kwargs["params"],
            {"pageSize": 25, "pageToken": "page-2"},
        )

    def test_download_exports_tcx_as_cached_fit(self) -> None:
        source, client = self._source()
        client.get_bytes = Mock(return_value=_tcx_activity())
        activity = Activity(
            source="fitbit",
            source_id="12345",
            name="Morning run",
            sport_type="running",
            start_time=datetime(2026, 1, 3, 8, tzinfo=timezone.utc),
        )

        with tempfile.TemporaryDirectory() as directory:
            path = source.download_fit(activity, Path(directory))
            self.assertEqual(path.suffix, ".fit")
            self.assertTrue(fit_signature_ok(path))
            parsed = read_activity_file(path)
            self.assertEqual(parsed.sport_type, "running")
            self.assertEqual(len(parsed.track_points), 2)
            self.assertAlmostEqual(parsed.track_points[0].latitude, 22.3, places=5)

            self.assertEqual(source.download_fit(activity, Path(directory)), path)

        client.get_bytes.assert_called_once_with(
            "/users/me/dataTypes/exercise/dataPoints/12345:exportExerciseTcx",
            params={"alt": "media"},
        )

    def test_download_rejects_export_without_a_gps_track(self) -> None:
        source, client = self._source()
        client.get_bytes = Mock(return_value=_tcx_activity(with_position=False))
        activity = Activity(source="fitbit", source_id="12345", name="Indoor run")

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "No GPS track points"):
                source.download_fit(activity, Path(directory))
            self.assertFalse((Path(directory) / "fitbit" / "12345.fit").exists())

    def test_sync_cli_accepts_fitbit_source_and_google_health_oauth_commands(self) -> None:
        parser = build_parser()

        sync_args = parser.parse_args(["sync", "--source", "fitbit", "--target", "garmin"])
        oauth_args = parser.parse_args(["google-health-exchange", "--code", "one-time", "--state", "state"])

        self.assertEqual(sync_args.source, ["fitbit"])
        self.assertEqual(oauth_args.command, "google-health-exchange")

    def test_engine_registers_fitbit_after_local_oauth_credentials_are_saved(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "GOOGLE_HEALTH_CLIENT_ID": "client-id",
                "GOOGLE_HEALTH_CLIENT_SECRET": "client-secret",
                "GOOGLE_HEALTH_REDIRECT_URI": "https://www.google.com",
                "SYNC_DATA_DIR": ".data",
            },
            clear=False,
        ):
            root_dir = Path(directory)
            config = AppConfig.load(root_dir)
            state_db = StateDB(config.db_path)
            state_db.set_value(
                GoogleHealthClient.credentials_key,
                json.dumps({"refresh_token": "local-refresh-token"}),
            )
            state_db.close()

            engine = SyncEngine(config)
            try:
                self.assertIn("fitbit", engine.sources)
            finally:
                engine.close()


def _exercise(source_id: str, start_time: str, exercise_type: str) -> dict[str, object]:
    return {
        "name": f"users/me/dataTypes/exercise/dataPoints/{source_id}",
        "exercise": {
            "interval": {"startTime": start_time},
            "exerciseType": exercise_type,
            "displayName": exercise_type.title(),
        },
    }


def _tcx_activity(*, with_position: bool = True) -> bytes:
    first_position = (
        "<Position><LatitudeDegrees>22.3</LatitudeDegrees><LongitudeDegrees>114.1</LongitudeDegrees></Position>"
        if with_position
        else ""
    )
    second_position = (
        "<Position><LatitudeDegrees>22.31</LatitudeDegrees><LongitudeDegrees>114.11</LongitudeDegrees></Position>"
        if with_position
        else ""
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">
  <Activities>
    <Activity Sport="Running">
      <Id>2026-01-03T08:00:00Z</Id>
      <Lap StartTime="2026-01-03T08:00:00Z">
        <TotalTimeSeconds>2</TotalTimeSeconds>
        <DistanceMeters>10</DistanceMeters>
        <Track>
          <Trackpoint><Time>2026-01-03T08:00:00Z</Time>{first_position}<AltitudeMeters>10</AltitudeMeters><DistanceMeters>0</DistanceMeters></Trackpoint>
          <Trackpoint><Time>2026-01-03T08:00:01Z</Time>{second_position}<AltitudeMeters>11</AltitudeMeters><DistanceMeters>10</DistanceMeters></Trackpoint>
        </Track>
      </Lap>
    </Activity>
  </Activities>
</TrainingCenterDatabase>""".encode("utf-8")


if __name__ == "__main__":
    unittest.main()
