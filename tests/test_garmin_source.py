from __future__ import annotations

import io
import os
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import garminconnect

from sport_sync_bridge.cli import build_parser, main as cli_main
from sport_sync_bridge.config import AppConfig
from sport_sync_bridge.engine import SyncEngine
from sport_sync_bridge.garmin_source import GarminSource
from sport_sync_bridge.models import Activity


def _fit_bytes() -> bytes:
    data = bytearray(128)
    data[0] = 14
    data[8:12] = b".FIT"
    return bytes(data)


def _zip_bytes(name: str, content: bytes) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(name, content)
    return output.getvalue()


def _empty_zip_bytes() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w"):
        pass
    return output.getvalue()


class _GarminClient:
    def __init__(self, pages: dict[int, object] | None = None, download: bytes | None = None):
        self.pages = pages or {}
        self.download = download
        self.activity_calls: list[tuple[int, int]] = []
        self.download_calls: list[tuple[str, object]] = []

    def get_activities(self, start: int = 0, limit: int = 20) -> object:
        self.activity_calls.append((start, limit))
        return self.pages.get(start, [])

    def download_activity(self, activity_id: str, *, dl_fmt: object) -> bytes:
        self.download_calls.append((activity_id, dl_fmt))
        if self.download is None:
            raise AssertionError("Unexpected Garmin download")
        return self.download


class _GarminTarget:
    def __init__(self, client: _GarminClient | None = None, configured: bool = True):
        self.client = client
        self.configured = configured
        self.authenticate_calls = 0

    def is_configured(self) -> bool:
        return self.configured

    def authenticate(self) -> None:
        self.authenticate_calls += 1


class GarminSourceTests(unittest.TestCase):
    def _source(self, client: _GarminClient | None = None) -> GarminSource:
        config = cast(AppConfig, SimpleNamespace())
        return GarminSource(config, _GarminTarget(client))

    def test_lists_filtered_activities_across_pages_and_sorts_them(self) -> None:
        client = _GarminClient(
            pages={
                0: [
                    {
                        "activityId": 3,
                        "activityName": "Evening ride",
                        "activityType": {"typeKey": "road_biking"},
                        "startTimeGMT": "2026-08-05T18:00:00Z",
                    },
                    {
                        "activityId": 2,
                        "activityName": "Morning run",
                        "activityType": {"typeKey": "running"},
                        "startTimeGMT": "2026-08-03T07:00:00Z",
                    },
                ],
                2: [
                    {
                        "activityId": 1,
                        "activityType": {"typeKey": "walking"},
                        "startTimeGMT": "2026-08-01T07:00:00Z",
                    },
                    {
                        "activityId": 4,
                        "activityType": {"typeKey": "lap_swimming"},
                        "startTimeGMT": "2026-08-08T07:00:00Z",
                    },
                ],
            }
        )
        source = self._source(client)
        since = datetime(2026, 8, 2, tzinfo=timezone.utc)
        until = datetime(2026, 8, 6, tzinfo=timezone.utc)

        with patch.object(GarminSource, "page_size", 2):
            activities = source.list_activities(since, until, None)

        self.assertEqual([activity.source_id for activity in activities], ["2", "3"])
        self.assertEqual([activity.sport_type for activity in activities], ["running", "cycling"])
        self.assertEqual(activities[0].name, "Morning run")
        self.assertEqual(client.activity_calls, [(0, 2), (2, 2), (4, 2)])

    def test_stops_after_requested_number_of_activities(self) -> None:
        client = _GarminClient(
            pages={
                0: [
                    {"activityId": 9, "startTimeGMT": "2026-08-05T00:00:00Z"},
                    {"activityId": 8, "startTimeGMT": "2026-08-04T00:00:00Z"},
                ]
            }
        )
        source = self._source(client)

        activities = source.list_activities(None, None, 1)

        self.assertEqual([activity.source_id for activity in activities], ["9"])
        self.assertEqual(client.activity_calls, [(0, GarminSource.page_size)])

    def test_rejects_malformed_activity_responses(self) -> None:
        cases = (
            (None, "JSON list"),
            ([None], "JSON object"),
            ([{"startTimeGMT": "2026-08-05T00:00:00Z"}], "missing its ID"),
            ([{"activityId": 3}], "missing a valid start time"),
        )
        for payload, message in cases:
            with self.subTest(payload=payload):
                source = self._source(_GarminClient(pages={0: payload}))
                with self.assertRaisesRegex(RuntimeError, message):
                    source.list_activities(None, None, None)

    def test_downloads_original_archive_extracts_fit_and_caches_it(self) -> None:
        client = _GarminClient(download=_zip_bytes("nested/activity.fit", _fit_bytes()))
        source = self._source(client)
        activity = Activity(source="garmin", source_id="ride/42", name="Ride")

        with tempfile.TemporaryDirectory() as directory:
            path = source.download_fit(activity, Path(directory))
            self.assertEqual(path.read_bytes(), _fit_bytes())
            self.assertEqual(path.name, "ride_42.fit")
            self.assertEqual(source.download_fit(activity, Path(directory)), path)

        self.assertEqual(len(client.download_calls), 1)
        self.assertEqual(client.download_calls[0][0], "ride/42")
        self.assertEqual(
            client.download_calls[0][1],
            garminconnect.Garmin.ActivityDownloadFormat.ORIGINAL,
        )

    def test_rejects_download_without_a_valid_single_fit_file(self) -> None:
        downloads = (
            _zip_bytes("activity.fit", b"x" * 128),
            _zip_bytes("readme.txt", b"no activity"),
            _empty_zip_bytes(),
            b"not a ZIP",
        )
        for downloaded in downloads:
            with self.subTest(size=len(downloaded)), tempfile.TemporaryDirectory() as directory:
                source = self._source(_GarminClient(download=downloaded))
                activity = Activity(source="garmin", source_id="123", name="Run")
                with self.assertRaises(RuntimeError):
                    source.download_fit(activity, Path(directory))
                self.assertFalse((Path(directory) / "garmin" / "123.fit").exists())

    def test_cli_and_engine_register_garmin_as_an_optional_source(self) -> None:
        self.assertEqual(build_parser().parse_args(["sync", "--source", "garmin"]).source, ["garmin"])
        self.assertEqual(build_parser().parse_args(["check", "--source", "garmin"]).source, ["garmin"])

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "SYNC_DATA_DIR": str(Path(directory) / "data"),
                "SYNC_SOURCES": "garmin",
                "SYNC_TARGETS": "garmin",
                "GARMIN_EMAIL": "test@example.invalid",
                "GARMIN_PASSWORD": "test-password",
            },
        ):
            engine = SyncEngine(AppConfig.load(Path(directory)))
            try:
                self.assertIn("garmin", engine.sources)
                self.assertIs(engine.sources["garmin"].target, engine.targets["garmin"])
            finally:
                engine.close()

    def test_cli_dry_run_scans_garmin_without_connecting_to_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "SYNC_DATA_DIR": str(Path(directory) / "data"),
                "SYNC_SOURCES": "garmin",
                "SYNC_TARGETS": "strava",
                "GARMIN_EMAIL": "test@example.invalid",
                "GARMIN_PASSWORD": "test-password",
                "STRAVA_CLIENT_ID": "test-client",
                "STRAVA_CLIENT_SECRET": "test-secret",
            },
        ), patch(
            "sport_sync_bridge.garmin_source.GarminSource.list_activities",
            return_value=[Activity(source="garmin", source_id="123", name="Morning run")],
        ) as list_activities, patch("sport_sync_bridge.cli.configure_logging"):
            self.assertEqual(
                cli_main(["sync", "--source", "garmin", "--target", "strava", "--dry-run"]),
                0,
            )

        list_activities.assert_called_once()
