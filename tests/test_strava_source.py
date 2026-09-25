from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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
from sport_sync_bridge.strava_source import StravaSource


class _Response:
    def __init__(self, payload: object = None, status_code: int = 200):
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
        self.get_calls: list[tuple[str, dict[str, object]]] = []
        self.headers: dict[str, str] = {}

    def get(self, url: str, **kwargs: object) -> _Response:
        self.get_calls.append((url, kwargs))
        return self.responses.pop(0)


class _TokenProvider:
    def __init__(self) -> None:
        self.calls: list[bool] = []

    def is_configured(self) -> bool:
        return True

    def get_access_token(self, *, force_refresh: bool = False) -> str:
        self.calls.append(force_refresh)
        return "refreshed-token" if force_refresh else "access-token"


class StravaSourceTests(unittest.TestCase):
    def _source(self) -> StravaSource:
        config = cast(AppConfig, SimpleNamespace())
        return StravaSource(config, _TokenProvider())

    def test_lists_activities_across_pages_in_chronological_order(self) -> None:
        source = self._source()
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        first_page = [
            {
                "id": identifier,
                "name": f"Ride {identifier}",
                "sport_type": "Ride",
                "start_date": (base + timedelta(days=identifier)).isoformat(),
            }
            for identifier in range(100, 0, -1)
        ]
        second_page = [{"id": 0, "type": "Run", "start_date": base.isoformat()}]
        source.session = _FakeSession([_Response(first_page), _Response(second_page)])

        activities = source.list_activities(None, None, None)

        self.assertEqual(len(activities), 101)
        self.assertEqual([activities[0].source_id, activities[-1].source_id], ["0", "100"])
        self.assertEqual(activities[0].sport_type, "running")
        self.assertEqual(activities[-1].sport_type, "cycling")
        self.assertEqual(source.session.get_calls[0][0], "https://www.strava.com/api/v3/athlete/activities")
        self.assertEqual(source.session.get_calls[0][1]["params"], {"per_page": 100, "page": 1})
        self.assertEqual(source.session.get_calls[1][1]["params"], {"per_page": 100, "page": 2})

    def test_applies_time_range_and_limit_to_activity_listing(self) -> None:
        source = self._source()
        item = {
            "id": 123,
            "type": "Run",
            "start_date": "2026-01-03T10:00:00Z",
        }
        source.session = _FakeSession([_Response([item])])
        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        until = datetime(2026, 1, 4, tzinfo=timezone.utc)

        activities = source.list_activities(since, until, 1)

        self.assertEqual([activity.source_id for activity in activities], ["123"])
        self.assertEqual(
            source.session.get_calls[0][1]["params"],
            {"per_page": 1, "page": 1, "after": int(since.timestamp()), "before": int(until.timestamp()) + 1},
        )

    def test_refreshes_a_401_and_retries_the_api_request(self) -> None:
        source = self._source()
        source.session = _FakeSession([_Response(status_code=401), _Response([])])

        self.assertEqual(source.list_activities(None, None, 1), [])

        self.assertEqual(source.token_provider.calls, [False, True])
        self.assertEqual(
            [call[1]["headers"]["Authorization"] for call in source.session.get_calls],
            ["Bearer access-token", "Bearer refreshed-token"],
        )

    def test_download_builds_and_caches_fit_with_stream_values(self) -> None:
        source = self._source()
        activity = Activity(
            source="strava",
            source_id="12345",
            name="Morning ride",
            sport_type="cycling",
            start_time=datetime(2026, 8, 5, 7, tzinfo=timezone.utc),
            raw={"id": 12345, "sport_type": "Ride"},
        )
        detail = {
            "id": 12345,
            "name": "Morning ride",
            "sport_type": "Ride",
            "start_date": "2026-08-05T07:00:00Z",
            "elapsed_time": 5,
            "moving_time": 4,
            "distance": 10,
            "average_heartrate": 130,
            "max_heartrate": 150,
            "average_watts": 200,
            "max_watts": 350,
            "weighted_average_watts": 210,
            "calories": 123,
        }
        streams = {
            "time": {"data": [0, 2, 4]},
            "distance": {"data": [0, 5, 10]},
            "latlng": {"data": [[22.3, 114.1], [22.31, 114.11], [22.32, 114.12]]},
            "altitude": {"data": [15, 16, 17]},
            "velocity_smooth": {"data": [3.2, 3.5, 3.8]},
            "heartrate": {"data": [100, 120, 140]},
            "cadence": {"data": [80, 82, 84]},
            "watts": {"data": [150, 200, 250]},
        }
        source.session = _FakeSession([_Response(detail), _Response(streams)])

        with tempfile.TemporaryDirectory() as directory:
            path = source.download_fit(activity, Path(directory))
            parsed = read_activity_file(path)
            self.assertEqual(path.suffix, ".fit")
            self.assertEqual(parsed.sport_type, "cycling")
            self.assertEqual(parsed.distance_m, 10)
            self.assertEqual(parsed.average_heart_rate_bpm, 130)
            self.assertEqual(parsed.average_power_w, 200)
            self.assertEqual(parsed.laps[0].calories, 123)
            self.assertEqual(len(parsed.track_points), 3)
            self.assertAlmostEqual(parsed.track_points[1].latitude, 22.31, places=6)
            self.assertEqual(parsed.track_points[1].speed_mps, 3.5)
            self.assertEqual(parsed.track_points[1].heart_rate_bpm, 120)
            self.assertEqual(parsed.track_points[1].cadence_rpm, 82)
            self.assertEqual(parsed.track_points[1].power_w, 200)
            self.assertEqual(source.download_fit(activity, Path(directory)), path)

        self.assertEqual(len(source.session.get_calls), 2)
        self.assertEqual(
            source.session.get_calls[1][1]["params"],
            {"keys": ",".join(source.stream_keys), "key_by_type": "true"},
        )

    def test_rejects_streams_without_time_data(self) -> None:
        source = self._source()
        activity = Activity(source="strava", source_id="123", name="Ride")
        source.session = _FakeSession([_Response({"start_date": "2026-01-01T00:00:00Z"}), _Response({})])

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "no time stream"):
                source.download_fit(activity, Path(directory))
            self.assertFalse((Path(directory) / "strava" / "123.fit").exists())

    def test_cli_and_engine_register_strava_as_an_optional_source(self) -> None:
        self.assertEqual(build_parser().parse_args(["sync", "--source", "strava"]).source, ["strava"])
        self.assertEqual(build_parser().parse_args(["check", "--source", "strava"]).source, ["strava"])

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "SYNC_DATA_DIR": str(Path(directory) / "data"),
                "SYNC_SOURCES": "strava",
                "SYNC_TARGETS": "strava",
                "STRAVA_CLIENT_ID": "test-client",
                "STRAVA_CLIENT_SECRET": "test-secret",
            },
        ):
            config = AppConfig.load(Path(directory))
            engine = SyncEngine(config)
            try:
                self.assertIn("strava", engine.sources)
                activity = Activity(
                    source="strava",
                    source_id="123",
                    name="Ride",
                    start_time=datetime(2026, 8, 5, 7, tzinfo=timezone.utc),
                )
                source = engine.sources["strava"]
                source.list_activities = lambda **_: [activity]
                source.download_fit = lambda *_: self.fail("same-platform sync should not download")
                engine.targets["strava"].upload_file = lambda *_: self.fail("same-platform sync should not upload")
                self.assertEqual(engine.sync_once(sources=["strava"], targets=["strava"]), 0)
            finally:
                engine.close()

    def test_cli_dry_run_scans_strava_without_authenticating_a_target(self) -> None:
        session = _FakeSession([_Response([])])
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "SYNC_DATA_DIR": str(Path(directory) / "data"),
                "SYNC_SOURCES": "strava",
                "SYNC_TARGETS": "garmin",
                "STRAVA_CLIENT_ID": "test-client",
                "STRAVA_CLIENT_SECRET": "test-secret",
                "STRAVA_ACCESS_TOKEN": "test-access-token",
                "STRAVA_REFRESH_TOKEN": "",
                "STRAVA_EXPIRES_AT": "4102444800",
                "GARMIN_EMAIL": "test@example.invalid",
                "GARMIN_PASSWORD": "test-password",
            },
        ), patch.object(requests, "Session", return_value=session), patch(
            "sport_sync_bridge.cli.configure_logging"
        ):
            self.assertEqual(
                cli_main(["sync", "--source", "strava", "--target", "garmin", "--dry-run"]),
                0,
            )

        self.assertEqual(len(session.get_calls), 1)
        self.assertEqual(session.get_calls[0][0], "https://www.strava.com/api/v3/athlete/activities")


if __name__ == "__main__":
    unittest.main()
