from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import requests

from sport_sync_bridge.cli import build_parser, main
from sport_sync_bridge.config import AppConfig
from sport_sync_bridge.engine import SyncEngine
from sport_sync_bridge.intervals_icu_target import IntervalsIcuTarget
from sport_sync_bridge.models import Activity


class _Response:
    def __init__(self, payload: object, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> object:
        return self.payload


class _Session:
    def __init__(self, upload_response: _Response):
        self.upload_response = upload_response
        self.get_calls: list[tuple[str, dict[str, object]]] = []
        self.post_calls: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, **kwargs: object) -> _Response:
        self.get_calls.append((url, kwargs))
        return _Response({"id": "i123"})

    def post(self, url: str, **kwargs: object) -> _Response:
        self.post_calls.append((url, kwargs))
        return self.upload_response


class IntervalsIcuTargetTests(unittest.TestCase):
    def _target(self, athlete_id: str = "i123", api_key: str = "test-key") -> IntervalsIcuTarget:
        config = SimpleNamespace(
            intervals_icu_athlete_id=athlete_id,
            intervals_icu_api_key=api_key,
        )
        return IntervalsIcuTarget(cast(AppConfig, config))

    def test_upload_uses_basic_auth_multipart_and_returns_activity_id(self) -> None:
        target = self._target(athlete_id="athlete/7")
        target.session = _Session(
            _Response(
                [{"id": "activity-9", "icu_athlete_id": "i123"}],
                status_code=201,
            )
        )
        activity = Activity("igpsport", "42", "Morning ride", "cycling")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ride.fit"
            path.write_bytes(b"FIT activity")
            result = target.upload_file(path, activity, external_id="igpsport:42")

        self.assertEqual(result.status, "success")
        self.assertEqual(result.remote_id, "activity-9")
        self.assertEqual(
            target.session.get_calls[0],
            (
                "https://intervals.icu/api/v1/athlete/athlete%2F7/profile",
                {"auth": ("API_KEY", "test-key"), "timeout": 30},
            ),
        )
        upload_url, upload_kwargs = target.session.post_calls[0]
        self.assertEqual(upload_url, "https://intervals.icu/api/v1/athlete/athlete%2F7/activities")
        self.assertEqual(upload_kwargs["auth"], ("API_KEY", "test-key"))
        self.assertEqual(
            upload_kwargs["params"],
            {"name": "Morning ride", "external_id": "igpsport:42"},
        )
        files = cast(dict[str, tuple[str, object, str]], upload_kwargs["files"])
        self.assertEqual(files["file"][0], "ride.fit")
        self.assertEqual(files["file"][2], "application/octet-stream")
        self.assertEqual(upload_kwargs["timeout"], 120)

    def test_upload_accepts_apk_object_result_shape(self) -> None:
        target = self._target()
        target.session = _Session(
            _Response(
                {
                    "id": "i123",
                    "icu_athlete_id": "i123",
                    "activities": [{"id": "activity-9", "icu_athlete_id": "i123"}],
                },
                status_code=201,
            )
        )
        activity = Activity("local", "42", "Ride")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ride.gpx"
            path.write_text("<gpx/>", encoding="utf-8")
            result = target.upload_file(path, activity, external_id="local:42")

        self.assertEqual(result.status, "success")
        self.assertEqual(result.remote_id, "activity-9")

    def test_http_200_is_reported_as_duplicate(self) -> None:
        target = self._target()
        target.session = _Session(_Response({}, status_code=200))
        activity = Activity("local", "42", "Ride")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ride.tcx"
            path.write_text("<TrainingCenterDatabase/>", encoding="utf-8")
            result = target.upload_file(path, activity, external_id="local:42")

        self.assertEqual(result.status, "duplicate")
        self.assertEqual(len(target.session.post_calls), 1)

    def test_rejects_unsupported_extension_without_network_calls(self) -> None:
        target = self._target()
        target.session = _Session(_Response({}, status_code=201))
        activity = Activity("local", "42", "Ride")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ride.zip"
            path.write_bytes(b"not an activity format")
            result = target.upload_file(path, activity, external_id="local:42")

        self.assertEqual(result.status, "failed")
        self.assertIn("FIT, GPX, TCX", result.message)
        self.assertEqual(target.session.get_calls, [])
        self.assertEqual(target.session.post_calls, [])

    def test_engine_and_cli_register_optional_intervals_target(self) -> None:
        parsed_sync = build_parser().parse_args(
            [
                "sync",
                "--source",
                "local",
                "--target",
                "intervals_icu",
                "--format",
                "intervals_icu=tcx",
            ]
        )
        self.assertEqual(parsed_sync.target, ["intervals_icu"])
        self.assertEqual(parsed_sync.target_formats, [("intervals_icu", "tcx")])
        self.assertEqual(
            build_parser().parse_args(["check", "--target", "intervals_icu"]).target,
            ["intervals_icu"],
        )

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "SYNC_DATA_DIR": str(Path(directory) / "data"),
                "SYNC_SOURCES": "local",
                "SYNC_TARGETS": "intervals_icu",
                "INTERVALS_ICU_ATHLETE_ID": "i123",
                "INTERVALS_ICU_API_KEY": "test-key",
            },
        ):
            config = AppConfig.load(Path(directory))
            engine = SyncEngine(config)
            try:
                self.assertIn("intervals_icu", engine.targets)
                self.assertEqual(
                    engine.sync_once(
                        sources=["local"],
                        targets=["intervals_icu"],
                        dry_run=True,
                        target_formats={"intervals_icu": "tcx"},
                    ),
                    0,
                )
            finally:
                engine.close()

            output = StringIO()
            with patch("sport_sync_bridge.cli.configure_logging"), redirect_stdout(output):
                exit_code = main(
                    [
                        "sync",
                        "--source",
                        "local",
                        "--target",
                        "intervals_icu",
                        "--format",
                        "intervals_icu=tcx",
                        "--dry-run",
                    ]
                )
            self.assertEqual(exit_code, 0)
            self.assertIn("done=0", output.getvalue())


if __name__ == "__main__":
    unittest.main()
