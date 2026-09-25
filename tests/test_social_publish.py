from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from sport_sync_bridge.cli import main
from sport_sync_bridge.formats import ActivityFile, ActivityLap, TrackPoint
from sport_sync_bridge.social_feed import SocialFeedClient, SocialFeedError
from sport_sync_bridge.social_publish import build_activity_publish_data


class _PublishHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []

    def do_POST(self) -> None:
        body_length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(body_length))
        type(self).requests.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "payload": payload,
            }
        )
        body = json.dumps({"published": True, "id": "post-1"}).encode("utf-8")
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class SocialPublishTests(unittest.TestCase):
    def _activity(self, *, with_track: bool = True) -> ActivityFile:
        points = (
            [
                TrackPoint(
                    timestamp=datetime(2025, 2, 3, 3, 4, tzinfo=timezone.utc),
                    latitude=38.5,
                    longitude=-120.2,
                    elevation_m=100,
                ),
                TrackPoint(
                    timestamp=datetime(2025, 2, 3, 4, 4, tzinfo=timezone.utc),
                    latitude=40.7,
                    longitude=-120.95,
                    elevation_m=110,
                ),
            ]
            if with_track
            else []
        )
        return ActivityFile(
            name="Morning ride",
            sport_type="cycling",
            start_time=datetime(2025, 2, 3, 11, 4, 5, tzinfo=timezone(timedelta(hours=8))),
            elapsed_time_s=3610,
            timer_time_s=3600,
            distance_m=3600,
            laps=[ActivityLap(calories=250, track_points=points)],
            creator="Garmin Edge",
            average_heart_rate_bpm=150,
            average_power_w=210,
            normalized_power_w=225,
            average_cadence=85,
        )

    def test_build_publish_data_matches_aot_fields_and_encodes_route(self) -> None:
        payload = build_activity_publish_data(
            self._activity(),
            activity_id="garmin-activity-42",
            display_name="Runner",
            avatar_url="https://example.test/avatar.png",
            location_name="Test region",
        )

        self.assertEqual(
            set(payload),
            {
                "activityId",
                "displayName",
                "avatarUrl",
                "title",
                "sport",
                "subSport",
                "deviceName",
                "distanceMeters",
                "movingTimeSeconds",
                "elevationGainMeters",
                "avgSpeedMs",
                "summaryPolyline",
                "location",
                "locationName",
                "avgHeartRate",
                "avgPower",
                "normPower",
                "avgCadence",
                "calories",
                "startTime",
            },
        )
        self.assertEqual(payload["activityId"], "garmin-activity-42")
        self.assertEqual(payload["title"], "Morning ride")
        self.assertEqual(payload["distanceMeters"], 3600.0)
        self.assertEqual(payload["movingTimeSeconds"], 3600.0)
        self.assertEqual(payload["elevationGainMeters"], 10.0)
        self.assertEqual(payload["avgSpeedMs"], 1.0)
        self.assertEqual(payload["summaryPolyline"], "_p~iF~ps|U_ulLnnqC")
        self.assertEqual(payload["location"], {"lat": 38.5, "lng": -120.2})
        self.assertEqual(payload["avgHeartRate"], 150.0)
        self.assertEqual(payload["avgPower"], 210.0)
        self.assertEqual(payload["normPower"], 225.0)
        self.assertEqual(payload["avgCadence"], 85.0)
        self.assertEqual(payload["calories"], 250)
        self.assertEqual(payload["startTime"], "2025-02-03T03:04:05Z")

    def test_missing_gps_leaves_route_and_location_empty(self) -> None:
        payload = build_activity_publish_data(
            self._activity(with_track=False),
            activity_id="local-activity-1",
            display_name="Runner",
        )

        self.assertIsNone(payload["summaryPolyline"])
        self.assertIsNone(payload["location"])
        self.assertIsNone(payload["locationName"])

    def test_invalid_coordinates_are_rejected(self) -> None:
        activity = self._activity()
        activity.laps[0].track_points[0].latitude = 91
        with self.assertRaisesRegex(ValueError, "invalid GPS coordinates"):
            build_activity_publish_data(
                activity,
                activity_id="activity-1",
                display_name="Runner",
            )

    def test_client_posts_aot_publish_endpoint_and_returns_response(self) -> None:
        response = Mock(status_code=201)
        response.json.return_value = {"published": True, "id": "post-1"}
        post = Mock(return_value=response)
        client = SocialFeedClient(
            "https://social.example/api/",
            "session-token",
            post=post,
        )

        result = client.publish({"activityId": "activity-1", "title": "Ride"})

        self.assertEqual(result, {"published": True, "id": "post-1"})
        post.assert_called_once_with(
            "https://social.example/api/publish",
            json={"activityId": "activity-1", "title": "Ride"},
            headers={
                "Authorization": "Bearer session-token",
                "Content-Type": "application/json",
            },
            timeout=30,
        )

    def test_client_rejects_failed_publish_response(self) -> None:
        response = Mock(status_code=403)
        post = Mock(return_value=response)
        client = SocialFeedClient(
            "https://social.example/api",
            "session-token",
            post=post,
        )

        with self.assertRaisesRegex(SocialFeedError, "HTTP 403"):
            client.publish({"activityId": "activity-1"})

    def test_cli_dry_run_prints_payload_without_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            activity_path = Path(directory) / "ride.gpx"
            _write_gpx(activity_path)
            config = Mock(data_dir=Path(directory) / ".data")
            output = io.StringIO()
            with (
                patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
                patch("sport_sync_bridge.cli.ensure_directory"),
                patch("sport_sync_bridge.cli.configure_logging"),
                contextlib.redirect_stdout(output),
            ):
                result = main(
                    [
                        "social",
                        "publish",
                        str(activity_path),
                        "--activity-id",
                        "activity-1",
                        "--display-name",
                        "Runner",
                        "--dry-run",
                    ]
                )

        self.assertEqual(result, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["activityId"], "activity-1")
        self.assertEqual(payload["title"], "Morning ride")
        self.assertIsNotNone(payload["summaryPolyline"])

    def test_cli_publishes_to_local_server(self) -> None:
        _PublishHandler.requests = []
        server = HTTPServer(("127.0.0.1", 0), _PublishHandler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                activity_path = Path(directory) / "ride.gpx"
                _write_gpx(activity_path)
                output = io.StringIO()
                config = Mock(data_dir=Path(directory) / ".data")
                with (
                    patch.dict(
                        "os.environ",
                        {
                            "GARSYNC_SOCIAL_BASE_URL": f"http://127.0.0.1:{server.server_port}/social",
                            "TEST_SOCIAL_TOKEN": "local-test-token",
                        },
                        clear=True,
                    ),
                    patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
                    patch("sport_sync_bridge.cli.ensure_directory"),
                    patch("sport_sync_bridge.cli.configure_logging"),
                    contextlib.redirect_stdout(output),
                ):
                    result = main(
                        [
                            "social",
                            "publish",
                            str(activity_path),
                            "--activity-id",
                            "activity-1",
                            "--display-name",
                            "Runner",
                            "--token-env",
                            "TEST_SOCIAL_TOKEN",
                        ]
                    )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue()), {"published": True, "id": "post-1"})
        request = _PublishHandler.requests[0]
        parsed_url = urlsplit(str(request["path"]))
        self.assertEqual(parsed_url.path, "/social/publish")
        self.assertEqual(request["authorization"], "Bearer local-test-token")
        self.assertEqual(request["payload"]["activityId"], "activity-1")
        self.assertEqual(request["payload"]["displayName"], "Runner")
        self.assertIsNotNone(request["payload"]["summaryPolyline"])


def _write_gpx(path: Path) -> None:
    path.write_text(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        "<gpx xmlns=\"http://www.topografix.com/GPX/1/1\"><trk><name>Morning ride</name><type>cycling</type>"
        "<trkseg><trkpt lat=\"31.23\" lon=\"121.47\"><ele>10</ele><time>2026-01-02T03:04:00Z</time>"
        "</trkpt><trkpt lat=\"31.231\" lon=\"121.471\"><ele>11</ele>"
        "<time>2026-01-02T03:05:00Z</time></trkpt></trkseg></trk></gpx>",
        encoding="utf-8",
    )
