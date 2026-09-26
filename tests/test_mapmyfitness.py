from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import requests

from sport_sync_bridge.cli import build_parser, main as cli_main
from sport_sync_bridge.config import AppConfig
from sport_sync_bridge.engine import SyncEngine
from sport_sync_bridge.formats import convert_activity_file, read_activity_file
from sport_sync_bridge.mapmyfitness_api import MapMyFitnessClient
from sport_sync_bridge.mapmyfitness_source import MapMyFitnessSource
from sport_sync_bridge.models import Activity, UploadResult
from sport_sync_bridge.utils import fit_signature_ok


class _Response:
    def __init__(self, payload: object | None = None, *, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> object:
        if self.payload is None:
            raise ValueError("response did not contain JSON")
        return self.payload


class _Session:
    def __init__(self, *, request_responses=None, post_responses=None):
        self.headers: dict[str, str] = {}
        self.request_responses = list(request_responses or [])
        self.post_responses = list(post_responses or [])
        self.request_calls: list[tuple[str, str, dict[str, object]]] = []
        self.post_calls: list[tuple[str, dict[str, object]]] = []

    def request(self, method: str, url: str, **kwargs):
        self.request_calls.append((method, url, kwargs))
        return self.request_responses.pop(0)

    def post(self, url: str, **kwargs):
        self.post_calls.append((url, kwargs))
        return self.post_responses.pop(0)


class _State:
    def __init__(self):
        self.values: dict[str, str] = {}

    def get_value(self, key: str) -> str | None:
        return self.values.get(key)

    def set_value(self, key: str, value: str) -> None:
        self.values[key] = value


class MapMyFitnessClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = _State()
        self.config = SimpleNamespace(
            mapmyfitness_client_id="client-id",
            mapmyfitness_client_secret="client-secret",
            mapmyfitness_redirect_uri="http://localhost:12345/callback",
        )
        self.client = MapMyFitnessClient(self.config, self.state)

    def test_authorization_url_uses_registered_redirect_uri(self) -> None:
        parsed = urlparse(self.client.build_authorize_url())
        self.assertEqual(parsed.netloc, "www.mapmyfitness.com")
        self.assertEqual(
            parse_qs(parsed.query),
            {
                "client_id": ["client-id"],
                "response_type": ["code"],
                "redirect_uri": ["http://localhost:12345/callback"],
            },
        )

    def test_exchange_saves_tokens_only_after_validating_user(self) -> None:
        session = _Session(
            post_responses=[
                _Response(
                    {
                        "access_token": "test-access-token",
                        "refresh_token": "test-refresh-token",
                        "expires_in": 3600,
                        "scope": "workout",
                    }
                )
            ],
            request_responses=[_Response({"id": 42})],
        )
        self.client.session = session

        result = self.client.exchange_code(" code-value ")

        self.assertEqual(result["user_id"], "42")
        self.assertEqual(result["scope"], "workout")
        self.assertEqual(self.state.get_value("mapmyfitness_access_token"), "test-access-token")
        self.assertEqual(self.state.get_value("mapmyfitness_refresh_token"), "test-refresh-token")
        self.assertEqual(self.state.get_value("mapmyfitness_user_id"), "42")
        token_url, token_kwargs = session.post_calls[0]
        self.assertEqual(token_url, self.client.token_url)
        self.assertEqual(token_kwargs["data"]["code"], "code-value")
        self.assertEqual(token_kwargs["headers"]["Api-Key"], "client-id")
        api_call = session.request_calls[0][2]
        self.assertEqual(api_call["headers"]["Api-Key"], "client-id")
        self.assertEqual(api_call["headers"]["Authorization"], "Bearer test-access-token")

    def test_retries_once_after_unauthorized_response_with_refresh_token(self) -> None:
        self.state.set_value("mapmyfitness_access_token", "expired-access-token")
        self.state.set_value("mapmyfitness_refresh_token", "stored-refresh-token")
        self.client.session = _Session(
            request_responses=[
                _Response({"detail": "expired"}, status_code=401),
                _Response({"id": 42}),
            ],
            post_responses=[
                _Response(
                    {
                        "access_token": "new-access-token",
                        "refresh_token": "new-refresh-token",
                        "expires_in": 3600,
                    }
                )
            ],
        )

        self.assertEqual(self.client.get_json("/user/self/"), {"id": 42})
        self.assertEqual(len(self.client.session.request_calls), 2)
        self.assertEqual(
            self.client.session.request_calls[0][2]["headers"]["Authorization"],
            "Bearer expired-access-token",
        )
        self.assertEqual(
            self.client.session.request_calls[1][2]["headers"]["Authorization"],
            "Bearer new-access-token",
        )


class MapMyFitnessSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.state = _State()
        self.state.set_value("mapmyfitness_access_token", "test-token")
        self.state.set_value("mapmyfitness_user_id", "42")
        self.config = SimpleNamespace(
            mapmyfitness_client_id="client-id",
            mapmyfitness_client_secret="client-secret",
            mapmyfitness_redirect_uri="http://localhost:12345/callback",
        )
        self.client = MapMyFitnessClient(self.config, self.state)
        self.source = MapMyFitnessSource(self.config, self.client)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_lists_offset_pages_with_date_filters_and_resolves_sport_types(self) -> None:
        pages = {
            0: {
                "_embedded": {
                    "workouts": [
                        {
                            "name": "Old ride",
                            "start_datetime": "2026-08-31T08:00:00Z",
                            "_links": {
                                "self": [{"id": "101"}],
                                "activity_type": [{"id": "11"}],
                            },
                        },
                        {
                            "name": "Morning run",
                            "start_datetime": "2026-09-02T08:00:00Z",
                            "_links": {
                                "self": [{"id": "102"}],
                                "activity_type": [{"id": "12"}],
                            },
                        },
                    ]
                },
                "total_count": 3,
            },
            2: {
                "_embedded": {
                    "workouts": [
                        {
                            "name": "Evening ride",
                            "start_datetime": "2026-09-03T08:00:00Z",
                            "_links": {
                                "self": [{"id": "103"}],
                                "activity_type": [{"id": "11"}],
                            },
                        }
                    ]
                },
                "total_count": 3,
            },
        }
        calls: list[tuple[str, dict[str, object] | None]] = []

        def get_json(path: str, *, params=None):
            calls.append((path, params))
            if path == "/workout/":
                return pages[params["offset"]]
            if path == "/activity_type/11/":
                return {"short_name": "ride"}
            if path == "/activity_type/12/":
                return {"short_name": "run"}
            raise AssertionError(f"unexpected API path: {path}")

        with (
            patch.object(self.client, "authenticate"),
            patch.object(self.client, "get_json", side_effect=get_json),
        ):
            activities = self.source.list_activities(
                datetime(2026, 9, 1, tzinfo=timezone.utc),
                datetime(2026, 9, 4, tzinfo=timezone.utc),
                2,
            )

        self.assertEqual([item.source_id for item in activities], ["102", "103"])
        self.assertEqual([item.sport_type for item in activities], ["running", "cycling"])
        page_calls = [(path, params) for path, params in calls if path == "/workout/"]
        self.assertEqual([call[1]["offset"] for call in page_calls], [0, 2])
        self.assertEqual([call[1]["limit"] for call in page_calls], [2, 1])
        self.assertEqual(page_calls[0][1]["user"], "42")
        self.assertEqual(page_calls[0][1]["started_after"], "2026-09-01T00:00:00Z")
        self.assertEqual(page_calls[0][1]["started_before"], "2026-09-04T00:00:00Z")
        self.assertEqual(page_calls[0][1]["order_by"], "-start_datetime")

    def test_download_writes_tcx_with_position_and_sensor_only_trackpoints(self) -> None:
        start = datetime(2026, 9, 2, 8, tzinfo=timezone.utc)
        workout = {
            "name": "MapMy ride",
            "start_datetime": start.isoformat(),
            "aggregates": {
                "elapsed_time_total": 3,
                "active_time_total": 2.5,
                "distance_total": 20,
                "metabolic_energy_total": 4184,
                "heartrate_avg": 110,
                "heartrate_max": 120,
                "cadence_avg": 80,
                "cadence_max": 90,
            },
            "time_series": {
                "position": [
                    [0, {"lat": 45.0, "lng": -122.0, "elevation": 10}],
                    [2, {"lat": 45.001, "lng": -122.001, "elevation": 11}],
                ],
                "distance": [[0, 0], [1, 10], [2, 20]],
                "heartrate": [[0, 100], [1, 110], [2, 120]],
                "speed": [[0, 1.1], [2, 2.2]],
                "cadence": [[2, 80]],
                "power": [[2, 180]],
            },
        }
        activity = Activity(
            source="mapmyfitness",
            source_id="99",
            name="MapMy ride",
            sport_type="cycling",
            start_time=start,
        )
        with patch.object(self.client, "get_json", return_value=workout) as get_json:
            path = self.source.download_fit(activity, self.root)

        self.assertEqual(path.suffix, ".tcx")
        self.assertEqual(get_json.call_args.kwargs["params"], {"field_set": "time_series"})
        parsed = read_activity_file(path)
        self.assertEqual(parsed.sport_type, "cycling")
        self.assertEqual(parsed.distance_m, 20)
        self.assertEqual(parsed.laps[0].calories, 1)
        points = parsed.track_points
        self.assertEqual(len(points), 3)
        self.assertEqual(points[0].latitude, 45.0)
        self.assertEqual(points[0].heart_rate_bpm, 100)
        self.assertIsNone(points[1].latitude)
        self.assertEqual(points[1].distance_m, 10)
        self.assertEqual(points[1].heart_rate_bpm, 110)
        self.assertEqual(points[2].power_w, 180)
        self.assertEqual(points[2].speed_mps, 2.2)
        self.assertEqual(points[2].cadence_rpm, 80)

        converted_fit = self.root / "roundtrip.fit"
        result = convert_activity_file(path, converted_fit, "fit")
        self.assertFalse(any("samples without GPS" in loss for loss in result.losses))
        fit_activity = read_activity_file(converted_fit)
        self.assertEqual(len(fit_activity.track_points), 3)
        self.assertEqual(fit_activity.track_points[1].heart_rate_bpm, 110)

    def test_rejects_malformed_time_series_and_workouts_without_gps(self) -> None:
        malformed = {
            "start_datetime": "2026-09-02T08:00:00Z",
            "time_series": {"position": [[0]]},
        }
        with (
            patch.object(self.client, "get_json", return_value=malformed),
            self.assertRaisesRegex(ValueError, "offset, value"),
        ):
            self.source.download_fit(
                Activity(
                    source="mapmyfitness",
                    source_id="1",
                    name="Bad",
                    start_time=datetime(2026, 9, 2, tzinfo=timezone.utc),
                ),
                self.root,
            )

    def test_sync_converts_mapmy_tcx_to_target_fit_before_upload(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            with patch.dict(
                os.environ,
                {
                    "MAPMYFITNESS_CLIENT_ID": "client-id",
                    "MAPMYFITNESS_CLIENT_SECRET": "client-secret",
                    "MAPMYFITNESS_REDIRECT_URI": "http://localhost:12345/callback",
                    "SYNC_DATA_DIR": str(root_path / ".data"),
                },
                clear=False,
            ):
                config = AppConfig.load(root_path)
            engine = SyncEngine(config)

            class _Target:
                name = "garmin"

                def __init__(self):
                    self.uploads: list[tuple[Path, Activity, str | None]] = []

                def upload_file(self, path: Path, activity: Activity, *, external_id: str):
                    self.uploads.append((path, activity, external_id))
                    return UploadResult(status="success", remote_id="remote-activity")

            target = _Target()
            engine.targets = {"garmin": target}
            engine.state_db.set_value("mapmyfitness_user_id", "42")
            list_payload = {
                "_embedded": {
                    "workouts": [
                        {
                            "name": "MapMy ride",
                            "start_datetime": "2026-09-02T08:00:00Z",
                            "activity_type": {"short_name": "ride"},
                            "_links": {"self": [{"id": "555"}]},
                        }
                    ]
                },
                "total_count": 1,
            }
            detail_payload = {
                "name": "MapMy ride",
                "start_datetime": "2026-09-02T08:00:00Z",
                "aggregates": {"elapsed_time_total": 2, "distance_total": 15},
                "time_series": {
                    "position": [
                        [0, {"lat": 45.0, "lng": -122.0, "elevation": 10}],
                        [2, {"lat": 45.001, "lng": -122.001, "elevation": 11}],
                    ],
                    "distance": [[0, 0], [2, 15]],
                    "heartrate": [[0, 100], [2, 110]],
                },
            }

            def get_json(path: str, *, params=None):
                if path == "/workout/":
                    return list_payload
                if path == "/workout/555/":
                    return detail_payload
                raise AssertionError(f"unexpected API path: {path}")

            try:
                with (
                    patch.object(engine.mapmyfitness_client, "authenticate"),
                    patch.object(engine.mapmyfitness_client, "get_json", side_effect=get_json),
                ):
                    synced = engine.sync_once(
                        sources=["mapmyfitness"],
                        targets=["garmin"],
                        target_formats={"garmin": "fit"},
                    )
                self.assertEqual(synced, 1)
                self.assertEqual(len(target.uploads), 1)
                uploaded_path, uploaded_activity, external_id = target.uploads[0]
                self.assertEqual(uploaded_activity.source_id, "555")
                self.assertEqual(external_id, "mapmyfitness:555")
                self.assertEqual(uploaded_path.suffix, ".fit")
                self.assertTrue(fit_signature_ok(uploaded_path))
                self.assertEqual(uploaded_path.parent, config.converted_dir / "mapmyfitness" / "garmin")
                original_path = config.downloads_dir / "mapmyfitness" / "555.tcx"
                self.assertTrue(original_path.is_file())
                self.assertNotEqual(uploaded_path, original_path)
            finally:
                engine.close()

        no_track = {
            "start_datetime": "2026-09-02T08:00:00Z",
            "time_series": {"heartrate": [[0, 100]]},
        }
        with (
            patch.object(self.client, "get_json", return_value=no_track),
            self.assertRaisesRegex(ValueError, "no GPS position track"),
        ):
            self.source.download_fit(
                Activity(
                    source="mapmyfitness",
                    source_id="2",
                    name="No track",
                    start_time=datetime(2026, 9, 2, tzinfo=timezone.utc),
                ),
                self.root,
            )

    def test_engine_and_cli_register_mapmyfitness_source_and_auth_commands(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            with patch.dict(
                os.environ,
                {
                    "MAPMYFITNESS_CLIENT_ID": "client-id",
                    "MAPMYFITNESS_CLIENT_SECRET": "client-secret",
                    "MAPMYFITNESS_REDIRECT_URI": "http://localhost:12345/callback",
                    "SYNC_DATA_DIR": str(root_path / ".data"),
                },
                clear=False,
            ):
                config = AppConfig.load(root_path)
                engine = SyncEngine(config)
                try:
                    self.assertIn("mapmyfitness", engine.sources)
                finally:
                    engine.close()

        parser = build_parser()
        sync_args = parser.parse_args(["sync", "--source", "mapmyfitness"])
        exchange_args = parser.parse_args(["mapmyfitness-exchange", "--code", "sample-code"])
        self.assertEqual(sync_args.source, ["mapmyfitness"])
        self.assertEqual(exchange_args.command, "mapmyfitness-exchange")

        client = SimpleNamespace(
            build_authorize_url=lambda: "https://www.mapmyfitness.com/authorize",
            exchange_code=lambda code: {"user_id": "42", "scope": "workout", "access_token": "hidden"},
        )
        fake_engine = SimpleNamespace(mapmyfitness_client=client, close=lambda: None)
        fake_config = SimpleNamespace(data_dir=Path("."), log_level="INFO", log_path=Path("sync.log"))
        output = io.StringIO()
        with (
            patch("sport_sync_bridge.cli.AppConfig.load", return_value=fake_config),
            patch("sport_sync_bridge.cli.ensure_directory"),
            patch("sport_sync_bridge.cli.configure_logging"),
            patch("sport_sync_bridge.cli.SyncEngine", return_value=fake_engine),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(cli_main(["mapmyfitness-auth-url"]), 0)
        self.assertEqual(output.getvalue().strip(), "https://www.mapmyfitness.com/authorize")

        output = io.StringIO()
        with (
            patch("sport_sync_bridge.cli.AppConfig.load", return_value=fake_config),
            patch("sport_sync_bridge.cli.ensure_directory"),
            patch("sport_sync_bridge.cli.configure_logging"),
            patch("sport_sync_bridge.cli.SyncEngine", return_value=fake_engine),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(cli_main(["mapmyfitness-exchange", "--code", "sample-code"]), 0)
        self.assertNotIn("hidden", output.getvalue())


if __name__ == "__main__":
    unittest.main()
