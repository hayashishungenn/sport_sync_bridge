from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from sport_sync_bridge.config import AppConfig
from sport_sync_bridge.engine import SyncEngine
from sport_sync_bridge.formats import read_activity_file
from sport_sync_bridge.models import Activity
from sport_sync_bridge.state import StateDB
from sport_sync_bridge.withings_source import WithingsClient, WithingsSource


class _Response:
    def __init__(self, payload: object, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> object:
        return self.payload


class _FakeSession:
    def __init__(self, responses: list[_Response]):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class WithingsSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db = StateDB(self.root / "state.db")
        config = SimpleNamespace(
            withings_client_id="client-id",
            withings_client_secret="client-secret",
            withings_redirect_uri="http://localhost/callback",
        )
        self.config = cast(AppConfig, config)
        self.client = WithingsClient(self.config, self.db)

    def tearDown(self) -> None:
        self.db.close()
        self.temp_dir.cleanup()

    def test_authorization_url_uses_user_activity_scope_and_exchange_checks_state(self) -> None:
        url = self.client.build_authorize_url()
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["scope"], ["user.activity"])
        self.assertEqual(query["redirect_uri"], [self.config.withings_redirect_uri])
        state = query["state"][0]
        self.client.session = _FakeSession(
            [
                _Response(
                    {
                        "status": 0,
                        "body": {
                            "access_token": "access-1",
                            "refresh_token": "refresh-1",
                            "expires_in": 10800,
                            "scope": "user.activity",
                        },
                    }
                )
            ]
        )

        with self.assertRaisesRegex(RuntimeError, "state does not match"):
            self.client.exchange_code("short-lived-code", "wrong-state")
        self.assertEqual(self.client.session.calls, [])

        result = self.client.exchange_code("short-lived-code", state)
        self.assertEqual(result["scope"], "user.activity")
        url, kwargs = self.client.session.calls[0]
        self.assertEqual(url, f"{self.client.api_root}/v2/oauth2")
        self.assertEqual(
            kwargs["data"],
            {
                "action": "requesttoken",
                "client_id": "client-id",
                "client_secret": "client-secret",
                "grant_type": "authorization_code",
                "code": "short-lived-code",
                "redirect_uri": "http://localhost/callback",
            },
        )
        saved = json.loads(self.db.get_value(self.client.credentials_key) or "{}")
        self.assertEqual(saved["refresh_token"], "refresh-1")
        self.assertEqual(self.db.get_value(self.client.oauth_state_key), "")

    def test_refresh_replaces_the_rotated_refresh_token(self) -> None:
        self.client._save_credentials(
            {
                "access_token": "expired-access",
                "refresh_token": "old-refresh",
                "expires_at": int(time.time()) - 1,
                "scope": "user.activity",
            }
        )
        session = _FakeSession(
            [
                _Response(
                    {
                        "status": 0,
                        "body": {
                            "access_token": "fresh-access",
                            "refresh_token": "rotated-refresh",
                            "expires_in": 10800,
                            "scope": "user.activity",
                        },
                    }
                )
            ]
        )
        self.client.session = session

        self.assertEqual(self.client.access_token(), "fresh-access")
        request = session.calls[0][1]
        self.assertEqual(request["data"]["grant_type"], "refresh_token")
        self.assertEqual(request["data"]["refresh_token"], "old-refresh")
        saved = json.loads(self.db.get_value(self.client.credentials_key) or "{}")
        self.assertEqual(saved["refresh_token"], "rotated-refresh")

    def test_refresh_rejects_a_response_without_a_new_refresh_token(self) -> None:
        self.client._save_credentials(
            {
                "access_token": "expired-access",
                "refresh_token": "old-refresh",
                "expires_at": int(time.time()) - 1,
            }
        )
        self.client.session = _FakeSession(
            [_Response({"status": 0, "body": {"access_token": "fresh-access", "expires_in": 10800}})]
        )

        with self.assertRaisesRegex(RuntimeError, "did not rotate"):
            self.client.access_token()
        saved = json.loads(self.db.get_value(self.client.credentials_key) or "{}")
        self.assertEqual(saved["refresh_token"], "old-refresh")

    def test_lists_workouts_and_generates_fit_with_gps_and_intraday_heart_rate(self) -> None:
        start = datetime(2026, 9, 1, 5, 0, tzinfo=timezone.utc)
        end = datetime(2026, 9, 1, 5, 30, tzinfo=timezone.utc)
        start_epoch = int(start.timestamp())
        workout = {
            "id": 321,
            "name": "Morning run",
            "category": 1,
            "startdate": start_epoch,
            "enddate": int(end.timestamp()),
            "data": {
                "distance": 5000,
                "calories": 320,
                "hr_average": 151,
                "hr_max": 178,
            },
        }
        session = _FakeSession(
            [
                _Response({"status": 0, "body": {"series": [workout]}}),
                _Response(
                    {
                        "status": 0,
                        "body": {
                            "series": [
                                {
                                    "data": {
                                        "gps": {str(start_epoch + 60): [22.3, 114.2, 18]}
                                    }
                                }
                            ]
                        },
                    }
                ),
                _Response(
                    {
                        "status": 0,
                        "body": {
                            "series": {
                                str(start_epoch + 60): {"heart_rate": 145},
                                str(start_epoch + 120): {"heart_rate": 156},
                            }
                        },
                    }
                ),
            ]
        )
        self.client.session = session
        self.client._save_credentials(
            {
                "access_token": "usable-access",
                "refresh_token": "refresh",
                "expires_at": int(time.time()) + 3600,
                "scope": "user.activity",
            }
        )
        source = WithingsSource(self.config, self.client)

        activities = source.list_activities(start, end, 5)
        self.assertEqual(len(activities), 1)
        self.assertEqual(activities[0].sport_type, "running")
        self.assertEqual(activities[0].source_id, "321")
        list_fields = session.calls[0][1]["data"]
        self.assertEqual(list_fields["action"], "getworkouts")
        self.assertEqual(list_fields["startdateymd"], "2026-09-01")

        output = source.download_fit(activities[0], self.root / "downloads")
        converted = read_activity_file(output)
        self.assertEqual(converted.sport_type, "running")
        self.assertEqual(converted.distance_m, 5000)
        self.assertEqual(converted.average_heart_rate_bpm, 151)
        self.assertEqual(len(converted.track_points), 2)
        self.assertAlmostEqual(converted.track_points[0].latitude or 0, 22.3, places=4)
        self.assertEqual(converted.track_points[0].heart_rate_bpm, 145)
        self.assertEqual(converted.track_points[1].heart_rate_bpm, 156)
        self.assertEqual(session.calls[1][1]["data"]["data_fields"], "gps")
        self.assertEqual(session.calls[2][1]["data"]["action"], "getintradayactivity")

    def test_engine_registers_authorized_withings_source(self) -> None:
        with patch.dict(
            os.environ,
            {
                "SYNC_DATA_DIR": ".data",
                "SYNC_SOURCES": "withings",
                "WITHINGS_CLIENT_ID": "client-id",
                "WITHINGS_CLIENT_SECRET": "client-secret",
                "WITHINGS_REDIRECT_URI": "http://localhost/callback",
            },
            clear=False,
        ):
            config = AppConfig.load(self.root)
        state_db = StateDB(config.db_path)
        state_db.set_value(
            WithingsClient.credentials_key,
            json.dumps({"refresh_token": "local-refresh", "scope": "user.activity"}),
        )
        state_db.close()

        engine = SyncEngine(config)
        try:
            self.assertIn("withings", engine.sources)
            self.assertEqual(config.withings_redirect_uri, "http://localhost/callback")
        finally:
            engine.close()


if __name__ == "__main__":
    unittest.main()
