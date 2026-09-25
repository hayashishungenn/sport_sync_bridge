from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from sport_sync_bridge.cli import _run_fitbit_health_fetch, build_parser
from sport_sync_bridge.config import AppConfig
from sport_sync_bridge.engine import SyncEngine
from sport_sync_bridge.formats import read_activity_file
from sport_sync_bridge.google_health import (
    GOOGLE_HEALTH_ACTIVITY_SCOPES,
    GOOGLE_HEALTH_HEALTH_DATA_TYPES,
    GOOGLE_HEALTH_SCOPES,
    GoogleHealthClient,
    GoogleHealthSource,
)
from sport_sync_bridge.health import (
    import_google_health_data_points,
    summarize_health_for_activity,
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
            with self.assertRaisesRegex(RuntimeError, "activity, location, sleep"):
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

    def test_loading_existing_credentials_does_not_widen_the_saved_scopes(self) -> None:
        state_db = self._state_db()
        client = self._client(state_db)
        stored_scopes = list(GOOGLE_HEALTH_ACTIVITY_SCOPES)
        state_db.set_value(
            client.credentials_key,
            json.dumps({"refresh_token": "refresh-token", "scopes": stored_scopes}),
        )

        with patch(
            "google.oauth2.credentials.Credentials.from_authorized_user_info",
            return_value=SimpleNamespace(),
        ) as create_credentials:
            client._load_credentials()

        self.assertEqual(create_credentials.call_args.kwargs["scopes"], stored_scopes)

    def test_health_data_point_listing_uses_inclusive_dates_and_paginates(self) -> None:
        state_db = self._state_db()
        client = self._client(state_db)
        sleep_scope = next(
            scope for scope in GOOGLE_HEALTH_SCOPES if scope.endswith("sleep.readonly")
        )
        state_db.set_value(
            client.credentials_key,
            json.dumps({"scopes": [sleep_scope]}),
        )
        client.get_json = Mock(
            side_effect=[
                {"dataPoints": [{"sleep": {"id": "one"}}], "nextPageToken": "page-2"},
                {"dataPoints": [{"sleep": {"id": "two"}}]},
            ]
        )

        records = client.list_health_data_points("sleep", "2026-01-01", "2026-01-02")

        self.assertEqual(len(records), 2)
        self.assertEqual(client.get_json.call_count, 2)
        self.assertEqual(
            client.get_json.call_args_list[0].kwargs["params"],
            {
                "pageSize": 25,
                "filter": (
                    'sleep.interval.civil_end_time >= "2026-01-01" '
                    'AND sleep.interval.civil_end_time < "2026-01-03"'
                ),
            },
        )
        self.assertEqual(
            client.get_json.call_args_list[1].kwargs["params"]["pageToken"],
            "page-2",
        )

    def test_health_data_point_listing_requires_scope_and_valid_date_range(self) -> None:
        state_db = self._state_db()
        client = self._client(state_db)
        state_db.set_value(client.credentials_key, json.dumps({"scopes": []}))

        with self.assertRaisesRegex(RuntimeError, "missing Google Health read-only permissions"):
            client.list_health_data_points("weight", "2026-01-01", "2026-01-01")

        with self.assertRaisesRegex(ValueError, "on or after"):
            client.list_health_data_points("sleep", "2026-01-02", "2026-01-01")

    def test_each_health_data_type_uses_its_documented_filter_and_scope(self) -> None:
        state_db = self._state_db()
        client = self._client(state_db)
        cases = (
            ("sleep", "sleep.readonly", "sleep.interval.civil_end_time", 25),
            (
                "weight",
                "health_metrics_and_measurements.readonly",
                "weight.sample_time.civil_time",
                10000,
            ),
            (
                "steps",
                "activity_and_fitness.readonly",
                "steps.interval.civil_start_time",
                10000,
            ),
            (
                "heart-rate",
                "health_metrics_and_measurements.readonly",
                "heart_rate.sample_time.civil_time",
                10000,
            ),
        )

        for data_type, scope_suffix, filter_field, page_size in cases:
            scope = next(
                scope for scope in GOOGLE_HEALTH_SCOPES if scope.endswith(scope_suffix)
            )
            state_db.set_value(
                client.credentials_key,
                json.dumps({"scopes": [scope]}),
            )
            client.get_json = Mock(return_value={"dataPoints": []})

            self.assertEqual(
                client.list_health_data_points(data_type, "2026-01-01", "2026-01-02"),
                [],
            )

            client.get_json.assert_called_once_with(
                f"/users/me/dataTypes/{data_type}/dataPoints",
                params={
                    "pageSize": page_size,
                    "filter": (
                        f'{filter_field} >= "2026-01-01" '
                        f'AND {filter_field} < "2026-01-03"'
                    ),
                },
            )

    def test_health_data_points_import_into_shared_observations_and_sleep_context(self) -> None:
        state_db = self._state_db()
        records = {
            "sleep": [_sleep_record()],
            "weight": [
                {
                    "weight": {
                        "sampleTime": {"physicalTime": "2026-01-04T06:45:00Z"},
                        "weightGrams": 72_345.5,
                    }
                }
            ],
            "steps": [
                {
                    "steps": {
                        "interval": {
                            "startTime": "2026-01-04T06:00:00Z",
                            "endTime": "2026-01-04T07:00:00Z",
                        },
                        "count": "1234",
                    }
                }
            ],
            "heart-rate": [
                {
                    "heartRate": {
                        "sampleTime": {"physicalTime": "2026-01-04T06:50:00Z"},
                        "beatsPerMinute": "65",
                    }
                }
            ],
        }

        processed = import_google_health_data_points(state_db, records)
        duplicate_processed = import_google_health_data_points(state_db, records)
        observations = state_db.list_health_observations()
        by_metric = {str(row["metric"]): row for row in observations}

        self.assertEqual(processed, 11)
        self.assertEqual(duplicate_processed, 11)
        self.assertEqual(len(observations), 11)
        self.assertAlmostEqual(by_metric["weight_kg"]["value"], 72.3455)
        self.assertEqual(by_metric["steps"]["value"], 1234)
        self.assertEqual(by_metric["pulse_bpm"]["value"], 65)
        self.assertEqual(by_metric["sleep_hours"]["value"], 6.5)
        self.assertEqual(by_metric["time_in_bed_hours"]["value"], 7)
        self.assertEqual(by_metric["deep_sleep_seconds"]["value"], 5400)
        self.assertEqual(by_metric["sleep_latency_seconds"]["value"], 600)

        activity_health = summarize_health_for_activity(
            state_db,
            datetime(2026, 1, 4, 8, tzinfo=timezone.utc),
            datetime(2026, 1, 4, 9, tzinfo=timezone.utc),
        )
        sleep_context = activity_health["sleep_before_activity"]
        self.assertIsInstance(sleep_context, dict)
        self.assertEqual(sleep_context["source"], "Google Health")
        self.assertEqual(sleep_context["sleep_hours"], 6.5)

    def test_invalid_health_data_point_batch_is_rejected_before_writing(self) -> None:
        state_db = self._state_db()
        records = {
            "steps": [
                {
                    "steps": {
                        "interval": {
                            "startTime": "2026-01-04T06:00:00Z",
                            "endTime": "2026-01-04T07:00:00Z",
                        },
                        "count": "1234",
                    }
                }
            ],
            "heart-rate": [
                {
                    "heartRate": {
                        "sampleTime": {"physicalTime": "2026-01-04T06:50:00Z"},
                        "beatsPerMinute": "301",
                    }
                }
            ],
        }

        with self.assertRaisesRegex(ValueError, "outside the supported range"):
            import_google_health_data_points(state_db, records)

        self.assertEqual(state_db.list_health_observations(), [])

    def test_sleep_stage_segments_provide_metrics_when_summary_is_missing(self) -> None:
        state_db = self._state_db()
        record = {
            "sleep": {
                "interval": {
                    "startTime": "2026-01-03T23:00:00Z",
                    "endTime": "2026-01-04T03:20:00Z",
                },
                "stages": [
                    {
                        "type": "DEEP",
                        "startTime": "2026-01-03T23:00:00Z",
                        "endTime": "2026-01-04T00:00:00Z",
                    },
                    {
                        "type": "LIGHT",
                        "startTime": "2026-01-04T00:00:00Z",
                        "endTime": "2026-01-04T02:00:00Z",
                    },
                    {
                        "type": "REM",
                        "startTime": "2026-01-04T02:00:00Z",
                        "endTime": "2026-01-04T03:00:00Z",
                    },
                    {
                        "type": "AWAKE",
                        "startTime": "2026-01-04T03:00:00Z",
                        "endTime": "2026-01-04T03:10:00Z",
                    },
                    {
                        "type": "RESTLESS",
                        "startTime": "2026-01-04T03:10:00Z",
                        "endTime": "2026-01-04T03:20:00Z",
                    },
                ],
            }
        }

        self.assertEqual(
            import_google_health_data_points(state_db, {"sleep": [record]}),
            6,
        )
        observations = {
            str(row["metric"]): row["value"]
            for row in state_db.list_health_observations()
        }
        self.assertEqual(observations["sleep_hours"], 4)
        self.assertEqual(observations["deep_sleep_seconds"], 3600)
        self.assertEqual(observations["light_sleep_seconds"], 7200)
        self.assertEqual(observations["rem_sleep_seconds"], 3600)
        self.assertEqual(observations["awake_sleep_seconds"], 600)
        self.assertEqual(observations["restless_sleep_seconds"], 600)

    def test_fitbit_health_fetch_cli_selects_dataset_and_imports_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = cast(AppConfig, SimpleNamespace(db_path=Path(directory) / "state.db"))
            args = build_parser().parse_args(
                [
                    "health",
                    "fetch-fitbit",
                    "--dataset",
                    "steps",
                    "--start-date",
                    "2026-01-01",
                    "--end-date",
                    "2026-01-02",
                ]
            )
            client = Mock()
            client.list_health_data_points.return_value = [
                {
                    "steps": {
                        "interval": {
                            "startTime": "2026-01-01T09:00:00Z",
                            "endTime": "2026-01-01T10:00:00Z",
                        },
                        "count": "42",
                    }
                }
            ]
            output = io.StringIO()
            with patch("sport_sync_bridge.cli.GoogleHealthClient", return_value=client):
                with redirect_stdout(output):
                    result = _run_fitbit_health_fetch(args, config)

            self.assertEqual(result, 0)
            client.list_health_data_points.assert_called_once_with(
                "steps", "2026-01-01", "2026-01-02"
            )
            self.assertIn("data_points_fetched=1", output.getvalue())
            state_db = StateDB(config.db_path)
            try:
                self.assertEqual(
                    state_db.list_health_observations("steps")[0]["value"],
                    42,
                )
            finally:
                state_db.close()

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
        health_args = parser.parse_args(
            [
                "health",
                "fetch-fitbit",
                "--dataset",
                "sleep",
                "--start-date",
                "2026-01-01",
                "--end-date",
                "2026-01-02",
            ]
        )

        self.assertEqual(sync_args.source, ["fitbit"])
        self.assertEqual(oauth_args.command, "google-health-exchange")
        self.assertEqual(health_args.dataset, ["sleep"])
        self.assertEqual(
            set(GOOGLE_HEALTH_HEALTH_DATA_TYPES),
            {"sleep", "weight", "steps", "heart-rate"},
        )

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
                json.dumps(
                    {
                        "refresh_token": "local-refresh-token",
                        "scopes": list(GOOGLE_HEALTH_ACTIVITY_SCOPES),
                    }
                ),
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


def _sleep_record() -> dict[str, object]:
    return {
        "name": "users/me/dataTypes/sleep/dataPoints/session-1",
        "sleep": {
            "interval": {
                "startTime": "2026-01-03T23:00:00Z",
                "endTime": "2026-01-04T06:00:00Z",
            },
            "summary": {
                "minutesInSleepPeriod": "420",
                "minutesAsleep": "390",
                "minutesAwake": "20",
                "minutesToFallAsleep": "10",
                "minutesAfterWakeUp": "5",
                "stagesSummary": [
                    {"type": "DEEP", "minutes": "90", "count": "2"},
                    {"type": "LIGHT", "minutes": "180", "count": "3"},
                    {"type": "REM", "minutes": "120", "count": "2"},
                    {"type": "AWAKE", "minutes": "20", "count": "2"},
                ],
            },
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
