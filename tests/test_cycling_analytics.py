from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

from sport_sync_bridge.cli import _target_format_map, build_parser
from sport_sync_bridge.config import AppConfig
from sport_sync_bridge.cycling_analytics import (
    CyclingAnalyticsClient,
    CyclingAnalyticsSource,
    CyclingAnalyticsTarget,
)
from sport_sync_bridge.engine import SyncEngine
from sport_sync_bridge.models import Activity
from sport_sync_bridge.utils import fit_signature_ok
from tests.activity_fixtures import create_fit, create_gpx


class _Response:
    def __init__(
        self,
        payload: object,
        *,
        status_code: int = 200,
        content: bytes = b"",
        text: str = "",
    ) -> None:
        self.payload = payload
        self.status_code = status_code
        self.content = content
        self.text = text

    def json(self) -> object:
        return self.payload


class CyclingAnalyticsTests(unittest.TestCase):
    def _client(self, token: str = "test-token") -> CyclingAnalyticsClient:
        return CyclingAnalyticsClient(cast(AppConfig, SimpleNamespace(cycling_analytics_access_token=token)))

    def test_source_lists_and_filters_rides_using_utc_start_time(self) -> None:
        client = self._client()
        client.get_json = Mock(
            side_effect=[
                {"id": 7},
                {
                    "rides": [
                        {
                            "id": 2,
                            "title": "Virtual ride",
                            "utc_datetime": "2026-01-02T10:00:00Z",
                            "type": "cycling",
                            "subtype": "virtual",
                            "format": "fit",
                        },
                        {
                            "id": 1,
                            "title": "Older ride",
                            "utc_datetime": "2026-01-01T10:00:00Z",
                            "type": "cycling",
                            "format": "fit",
                        },
                    ]
                },
            ]
        )
        source = CyclingAnalyticsSource(
            cast(AppConfig, SimpleNamespace(cycling_analytics_access_token="test-token")), client
        )

        activities = source.list_activities(
            datetime(2026, 1, 2, tzinfo=timezone.utc),
            datetime(2026, 1, 3, tzinfo=timezone.utc),
            limit=1,
        )

        self.assertEqual(len(activities), 1)
        self.assertEqual(activities[0].source_id, "2")
        self.assertEqual(activities[0].name, "Virtual ride")
        self.assertEqual(activities[0].sport_type, "virtual_ride")
        self.assertEqual(activities[0].start_time, datetime(2026, 1, 2, 10, tzinfo=timezone.utc))

    def test_download_preserves_raw_fit_and_caches_valid_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source_file = create_fit(Path(directory) / "ride.fit")
            raw_fit = source_file.read_bytes()
            client = self._client()
            client.get_bytes = Mock(return_value=raw_fit)
            source = CyclingAnalyticsSource(
                cast(AppConfig, SimpleNamespace(cycling_analytics_access_token="test-token")), client
            )
            activity = Activity(
                source="cycling_analytics",
                source_id="ride/42",
                name="Ride",
                sport_type="cycling",
                raw={"format": "fit"},
            )

            first = source.download_fit(activity, Path(directory) / "downloads")
            second = source.download_fit(activity, Path(directory) / "downloads")

            self.assertEqual(first, second)
            self.assertTrue(fit_signature_ok(first))
            self.assertEqual(first.read_bytes(), raw_fit)
            client.get_bytes.assert_called_once_with("/ride/ride%2F42/raw")

    def test_download_converts_remote_gpx_to_fit_for_shared_sync_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            gpx_path = create_gpx(Path(directory) / "ride.gpx")
            client = self._client()
            client.get_bytes = Mock(return_value=gpx_path.read_bytes())
            source = CyclingAnalyticsSource(
                cast(AppConfig, SimpleNamespace(cycling_analytics_access_token="test-token")), client
            )
            activity = Activity(
                source="cycling_analytics",
                source_id="42",
                name="Ride",
                sport_type="cycling",
                raw={"format": "gpx"},
            )

            output = source.download_fit(activity, Path(directory) / "downloads")

            self.assertTrue(fit_signature_ok(output))
            self.assertEqual(output.suffix, ".fit")

    def test_upload_uses_documented_multipart_endpoint_and_polls_to_completion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fit_path = create_fit(Path(directory) / "ride.fit")
            client = self._client()
            response = _Response({"upload_id": 31, "status": "processing"}, status_code=200)
            client.request = Mock(return_value=response)
            client.get_json = Mock(return_value={"upload_id": 31, "status": "done", "ride_id": 88})
            target = CyclingAnalyticsTarget(client)
            target.upload_poll_interval_seconds = 0
            activity = Activity("igpsport", "42", "Morning ride", "cycling")

            with patch("sport_sync_bridge.cycling_analytics.time.sleep"):
                result = target.upload_file(fit_path, activity, external_id="igpsport:42")

            self.assertEqual(result.status, "success")
            self.assertEqual(result.remote_id, "88")
            args, kwargs = client.request.call_args
            self.assertEqual(args[:2], ("POST", "/me/upload"))
            self.assertEqual(kwargs["data"], {"filename": "ride.fit", "format": "fit", "title": "Morning ride"})
            self.assertEqual(kwargs["files"]["data"][0], "ride.fit")
            self.assertEqual(client.get_json.call_args.args, ("/me/upload/31",))

    def test_duplicate_upload_error_is_recorded_as_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fit_path = create_fit(Path(directory) / "ride.fit")
            client = self._client()
            client.request = Mock(
                return_value=_Response(
                    {"status": "error", "error_code": "duplicate_ride", "error": "duplicate_ride"}
                )
            )
            target = CyclingAnalyticsTarget(client)

            result = target.upload_file(fit_path, Activity("local", "1", "Ride"), "local:1")

            self.assertEqual(result.status, "duplicate")

    def test_delete_uses_encoded_ride_id(self) -> None:
        client = self._client()
        client.request = Mock(return_value=_Response({"success": "deleted"}))
        target = CyclingAnalyticsTarget(client)

        target.delete_ride("ride/42")

        self.assertEqual(client.request.call_args.args[:2], ("DELETE", "/ride/ride%2F42"))

    def test_cli_accepts_cycling_analytics_source_target_and_format(self) -> None:
        args = build_parser().parse_args(
            [
                "sync",
                "--source",
                "cycling_analytics",
                "--target",
                "cycling_analytics",
                "--format",
                "cycling_analytics=tcx",
            ]
        )
        self.assertEqual(_target_format_map(args.target_formats), {"cycling_analytics": "tcx"})

        delete_args = build_parser().parse_args(
            ["cycling-analytics-delete", "--ride-id", "88", "--yes"]
        )
        self.assertTrue(delete_args.yes)

    def test_engine_registers_source_and_target_when_token_is_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"CYCLING_ANALYTICS_ACCESS_TOKEN": "test-token"}
        ):
            config = AppConfig.load(Path(directory))
            engine = SyncEngine(config)
            try:
                self.assertIn("cycling_analytics", engine.sources)
                self.assertIn("cycling_analytics", engine.targets)
            finally:
                engine.close()


if __name__ == "__main__":
    unittest.main()
