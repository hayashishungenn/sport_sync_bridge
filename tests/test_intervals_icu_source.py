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

from sport_sync_bridge.cli import build_parser
from sport_sync_bridge.config import AppConfig
from sport_sync_bridge.engine import SyncEngine
from sport_sync_bridge.intervals_icu import IntervalsIcuSource
from sport_sync_bridge.models import Activity


class _Response:
    def __init__(self, *, payload: object = None, content: bytes = b"", status_code: int = 200):
        self.payload = payload
        self.content = content
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> object:
        return self.payload


class _FakeSession:
    def __init__(self, *, get_responses: list[_Response] | None = None, post_responses: list[_Response] | None = None):
        self.get_responses = list(get_responses or [])
        self.post_responses = list(post_responses or [])
        self.get_calls: list[tuple[str, dict[str, object]]] = []
        self.post_calls: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, **kwargs: object) -> _Response:
        self.get_calls.append((url, kwargs))
        return self.get_responses.pop(0)

    def post(self, url: str, **kwargs: object) -> _Response:
        self.post_calls.append((url, kwargs))
        return self.post_responses.pop(0)


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


class IntervalsIcuSourceTests(unittest.TestCase):
    def _source(self, athlete_id: str = "i123", api_key: str = "test-key") -> IntervalsIcuSource:
        config = SimpleNamespace(
            intervals_icu_athlete_id=athlete_id,
            intervals_icu_api_key=api_key,
        )
        return IntervalsIcuSource(cast(AppConfig, config))

    def test_configuration_requires_both_credentials_and_config_loads_them(self) -> None:
        self.assertFalse(self._source(athlete_id="", api_key="test-key").is_configured())
        self.assertFalse(self._source(athlete_id="i123", api_key="").is_configured())
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"INTERVALS_ICU_ATHLETE_ID": "i123", "INTERVALS_ICU_API_KEY": "test-key"},
        ):
            config = AppConfig.load(Path(directory))
        self.assertEqual(config.intervals_icu_athlete_id, "i123")
        self.assertEqual(config.intervals_icu_api_key, "test-key")

    def test_lists_activity_range_skips_strava_duplicates_and_maps_sports(self) -> None:
        source = self._source()
        source.session = _FakeSession(
            get_responses=[
                _Response(payload={"id": "i123"}),
                _Response(
                    payload=[
                        {
                            "id": 3,
                            "source": "DEVICE",
                            "type": "Run",
                            "name": "Evening run",
                            "start_date": "2026-08-05T18:00:00Z",
                        },
                        {
                            "id": 2,
                            "source": "STRAVA",
                            "type": "Ride",
                            "start_date": "2026-08-04T18:00:00Z",
                        },
                        {
                            "id": 1,
                            "source": "DEVICE",
                            "type": "VirtualRide",
                            "name": "Morning ride",
                            "start_date": "2026-08-03T18:00:00Z",
                        },
                    ]
                ),
            ]
        )
        since = datetime(2026, 8, 1, tzinfo=timezone.utc)
        until = datetime(2026, 8, 8, tzinfo=timezone.utc)

        activities = source.list_activities(since, until, 5)

        self.assertEqual([activity.source_id for activity in activities], ["1", "3"])
        self.assertEqual([activity.sport_type for activity in activities], ["cycling", "running"])
        self.assertEqual(activities[0].name, "Morning ride")
        profile_url, profile_kwargs = source.session.get_calls[0]
        self.assertEqual(profile_url, "https://intervals.icu/api/v1/athlete/i123/profile")
        self.assertEqual(profile_kwargs["auth"], ("API_KEY", "test-key"))
        list_url, list_kwargs = source.session.get_calls[1]
        self.assertEqual(list_url, "https://intervals.icu/api/v1/athlete/i123/activities")
        self.assertEqual(
            list_kwargs["params"],
            {"oldest": "2026-08-01T00:00:00", "newest": "2026-08-08T00:00:00", "limit": 5},
        )

    def test_activity_response_must_be_a_list_of_objects_with_ids(self) -> None:
        for payload, message in (({"activities": []}, "JSON list"), ([None], "JSON object"), ([{}], "missing its ID")):
            with self.subTest(payload=payload):
                source = self._source()
                source.session = _FakeSession(
                    get_responses=[_Response(payload={"id": "i123"}), _Response(payload=payload)]
                )
                with self.assertRaisesRegex(RuntimeError, message):
                    source.list_activities(None, None, None)

    def test_download_posts_activity_id_and_caches_valid_fit(self) -> None:
        source = self._source()
        expected = _fit_bytes()
        source.session = _FakeSession(post_responses=[_Response(content=_zip_bytes("ride.fit", expected))])
        source._authenticated = True
        activity = Activity(source="intervals_icu", source_id="ride/42", name="Ride")
        with tempfile.TemporaryDirectory() as directory:
            path = source.download_fit(activity, Path(directory))
            self.assertEqual(path.read_bytes(), expected)
            self.assertEqual(path.name, "ride_42.fit")
            self.assertEqual(source.session.post_calls[0][0], "https://intervals.icu/api/v1/athlete/i123/download-fit-files")
            self.assertEqual(source.session.post_calls[0][1]["data"], {"ids": "ride/42"})
            self.assertEqual(source.download_fit(activity, Path(directory)), path)
        self.assertEqual(len(source.session.post_calls), 1)

    def test_download_rejects_empty_or_invalid_archives_and_non_fit_content(self) -> None:
        responses = [
            _Response(content=_empty_zip_bytes()),
            _Response(content=b"not a zip archive"),
            _Response(content=_zip_bytes("activity.fit", b"x" * 128)),
        ]
        for response, message in zip(responses, ("Empty ZIP", "Invalid ZIP", "not a valid FIT")):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                source = self._source()
                source.session = _FakeSession(post_responses=[response])
                source._authenticated = True
                activity = Activity(source="intervals_icu", source_id="42", name="Ride")
                with self.assertRaisesRegex(RuntimeError, message):
                    source.download_fit(activity, Path(directory))
                self.assertFalse((Path(directory) / "intervals_icu" / "42.fit").exists())

    def test_sync_and_check_cli_accept_intervals_icu_source(self) -> None:
        self.assertEqual(
            build_parser().parse_args(["sync", "--source", "intervals_icu"]).source,
            ["intervals_icu"],
        )
        self.assertEqual(
            build_parser().parse_args(["check", "--source", "intervals_icu"]).source,
            ["intervals_icu"],
        )

    def test_engine_registers_configured_intervals_icu_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "SYNC_DATA_DIR": str(Path(directory) / "data"),
                "SYNC_SOURCES": "intervals_icu",
                "SYNC_TARGETS": "",
                "INTERVALS_ICU_ATHLETE_ID": "i123",
                "INTERVALS_ICU_API_KEY": "test-key",
            },
        ):
            config = AppConfig.load(Path(directory))
            engine = SyncEngine(config)
            try:
                self.assertIn("intervals_icu", engine.sources)
                source = engine.sources["intervals_icu"]
                source.session = _FakeSession(
                    get_responses=[_Response(payload={"id": "i123"}), _Response(payload=[])]
                )
                self.assertEqual(
                    engine.sync_once(sources=["intervals_icu"], targets=[], dry_run=True),
                    0,
                )
                self.assertEqual(len(source.session.get_calls), 2)
            finally:
                engine.close()


if __name__ == "__main__":
    unittest.main()
