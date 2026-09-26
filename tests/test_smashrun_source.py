from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import requests

from sport_sync_bridge.cli import build_parser, main as cli_main
from sport_sync_bridge.config import AppConfig
from sport_sync_bridge.engine import SyncEngine
from sport_sync_bridge.formats import read_activity_file
from sport_sync_bridge.models import Activity
from sport_sync_bridge.smashrun_source import (
    SmashrunSource,
    _activity_file_from_detail,
    _activity_from_summary,
)
from sport_sync_bridge.state import StateDB
from sport_sync_bridge.utils import fit_signature_ok


class FakeResponse:
    def __init__(self, payload: object, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> object:
        return self.payload


class SmashrunSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.state_db = StateDB(self.root / "state.db")
        self.config = cast(AppConfig, SimpleNamespace(smashrun_access_token=None))
        self.source = SmashrunSource(self.config, self.state_db)

    def tearDown(self) -> None:
        self.state_db.close()
        self.temp_dir.cleanup()

    def test_token_is_validated_with_bearer_header_then_saved(self) -> None:
        with patch.object(self.source.session, "get", return_value=FakeResponse([])) as get:
            self.source.save_access_token(" personal-token ")

        call = get.call_args
        self.assertEqual(call.args[0], "https://api.smashrun.com/v1/my/activities/search/briefs")
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer personal-token")
        self.assertEqual(call.kwargs["params"], {"count": 1})
        self.assertNotIn("personal-token", call.args[0])
        self.assertEqual(self.state_db.get_value("smashrun_access_token"), "personal-token")
        self.assertTrue(self.source.is_configured())

    def test_invalid_token_is_not_saved(self) -> None:
        with patch.object(self.source.session, "get", return_value=FakeResponse({}, status_code=401)):
            with self.assertRaisesRegex(RuntimeError, "rejected the access token"):
                self.source.save_access_token("bad-token")
        self.assertIsNone(self.state_db.get_value("smashrun_access_token"))

    def test_lists_brief_activities_with_pagination_and_local_date_filter(self) -> None:
        self.state_db.set_value("smashrun_access_token", "token")
        self.source.page_size = 2
        requests_seen: list[dict[str, int]] = []

        def search(path: str, *, params: dict[str, int] | None = None, token: str | None = None) -> object:
            self.assertEqual(path, "/my/activities/search/briefs")
            self.assertEqual(token, "token")
            assert params is not None
            requests_seen.append(params)
            if params["page"] == 0:
                return [
                    {"runId": 3, "startTime": "2026-09-03T08:00:00Z"},
                    {"runId": 2, "startTime": "2026-09-02T08:00:00Z"},
                ]
            return [{"runId": 1, "startTime": "2026-09-01T08:00:00Z"}]

        with patch.object(self.source, "_request_json", side_effect=search):
            activities = self.source.list_activities(
                datetime(2026, 9, 1, tzinfo=timezone.utc),
                datetime(2026, 9, 2, 23, 59, tzinfo=timezone.utc),
                None,
            )

        self.assertEqual([activity.source_id for activity in activities], ["1", "2"])
        self.assertEqual(
            requests_seen[0]["fromDateUTC"],
            int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp()),
        )
        self.assertEqual([request["page"] for request in requests_seen], [0, 1])

    def test_detail_recordings_convert_to_fit_and_preserve_common_fields(self) -> None:
        self.state_db.set_value("smashrun_access_token", "token")
        activity = _activity_from_summary(
            {"runId": 42, "startTime": "2026-09-01T06:00:00Z", "name": "Morning run"},
            0,
        )
        detail = {
            "activityDetail": {
                "activityId": 42,
                "startDateTimeLocal": "2026-09-01T06:00:00+00:00",
                "distance": 0.1,
                "duration": 20,
                "heartRateAverage": 145,
                "heartRateMax": 160,
                "cadenceAverage": 84,
                "recordingKeys": [
                    "clock", "duration", "distance", "latitude", "longitude", "elevation",
                    "heartRate", "cadence", "power", "speed",
                ],
                "recordingValues": [
                    [0, 10, 20], [0, 10, 20], [0, 0.05, 0.1],
                    [25.0, 25.0001, 25.0002], [121.0, 121.0001, 121.0002],
                    [5, 6, 7], [130, 145, 160], [80, 84, 88], [100, 110, 120], [3.6, 7.2, 10.8],
                ],
                "laps": [
                    {"lapType": "general", "endTime": 10},
                    {"lapType": "work", "endTime": 20},
                ],
            }
        }
        with patch.object(self.source, "_request_json", return_value=detail):
            path = self.source.download_fit(activity, self.root / "downloads")

        self.assertTrue(fit_signature_ok(path))
        converted = read_activity_file(path)
        self.assertEqual(converted.sport_type, "running")
        self.assertAlmostEqual(converted.distance_m or 0, 100)
        self.assertEqual(len(converted.track_points), 3)
        self.assertAlmostEqual(converted.track_points[1].distance_m or 0, 50)
        self.assertEqual(converted.track_points[1].heart_rate_bpm, 145)
        self.assertAlmostEqual(converted.track_points[0].speed_mps or 0, 1)
        self.assertEqual(len(converted.laps), 2)

    def test_manual_activity_with_distance_series_can_be_written_without_gps(self) -> None:
        activity = Activity(
            source="smashrun",
            source_id="manual-1",
            name="Treadmill",
            sport_type="running",
            start_time=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
        self.state_db.set_value("smashrun_access_token", "token")
        detail = {
            "startDateTimeLocal": "2026-09-01T00:00:00Z",
            "distance": 1.0,
            "duration": 300,
            "recordingKeys": ["clock", "distance"],
            "recordingValues": [[0, 300], [0, 1.0]],
        }
        with patch.object(self.source, "_request_json", return_value=detail):
            path = self.source.download_fit(activity, self.root / "downloads")
        self.assertTrue(fit_signature_ok(path))
        converted = read_activity_file(path)
        self.assertEqual(converted.track_points[0].latitude, None)
        self.assertEqual(converted.track_points[-1].distance_m, 1000)

    def test_malformed_or_missing_time_series_fails_explicitly(self) -> None:
        activity = Activity("smashrun", "bad", "Bad run", "running", datetime.now(timezone.utc))
        with self.assertRaisesRegex(RuntimeError, "no recording arrays"):
            _activity_file_from_detail({}, activity)

        no_clock = {
            "startDateTimeLocal": "2026-09-01T00:00:00Z",
            "recordingKeys": ["distance"],
            "recordingValues": [[0.1]],
        }
        with self.assertRaisesRegex(RuntimeError, "no clock/time recording"):
            _activity_file_from_detail(no_clock, activity)

    def test_cli_exposes_smashrun_auth_and_sync_source(self) -> None:
        parser = build_parser()
        self.assertEqual(parser.parse_args(["smashrun-auth"]).command, "smashrun-auth")
        sync_args = parser.parse_args(["sync", "--source", "smashrun", "--dry-run"])
        self.assertEqual(sync_args.source, ["smashrun"])

    def test_engine_registers_smashrun_when_a_token_is_configured(self) -> None:
        with patch.dict(os.environ, {"SMASHRUN_ACCESS_TOKEN": "test-token"}):
            engine = SyncEngine(AppConfig.load(self.root))
        try:
            self.assertIn("smashrun", engine.sources)
        finally:
            engine.close()

    def test_sync_dry_run_reaches_the_smashrun_source_without_network_access(self) -> None:
        with patch.dict(
            os.environ,
            {
                "SMASHRUN_ACCESS_TOKEN": "test-token",
                "STRAVA_CLIENT_ID": "test-client",
                "STRAVA_CLIENT_SECRET": "test-secret",
            },
        ):
            config = AppConfig.load(self.root / "cli")
            with (
                patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
                patch("sport_sync_bridge.cli.configure_logging"),
                patch(
                    "sport_sync_bridge.smashrun_source.SmashrunSource.list_activities",
                    return_value=[
                        Activity(
                            source="smashrun",
                            source_id="dry-run-1",
                            name="Dry run",
                            sport_type="running",
                            start_time=datetime(2026, 9, 1, tzinfo=timezone.utc),
                        )
                    ],
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                result = cli_main(
                    ["sync", "--source", "smashrun", "--target", "strava", "--dry-run"]
                )
        self.assertEqual(result, 0)


if __name__ == "__main__":
    unittest.main()
