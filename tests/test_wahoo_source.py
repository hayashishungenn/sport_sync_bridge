from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

from sport_sync_bridge.cli import build_parser
from sport_sync_bridge.config import AppConfig
from sport_sync_bridge.engine import SyncEngine
from sport_sync_bridge.models import Activity
from sport_sync_bridge.wahoo_source import WahooSource
from sport_sync_bridge.wahoo_target import WahooTarget


class _Response:
    def __init__(self, payload=None, *, content: bytes = b"", status_code: int = 200):
        self.payload = payload
        self.content = content
        self.status_code = status_code

    def json(self):
        if self.payload is None:
            raise ValueError("response did not contain JSON")
        return self.payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class _Session:
    def __init__(self, get_responses=None):
        self.get_responses = list(get_responses or [])
        self.get_calls: list[tuple[str, dict]] = []
        self.headers: dict[str, str] = {}

    def get(self, url: str, **kwargs):
        self.get_calls.append((url, kwargs))
        return self.get_responses.pop(0)


class _StateDB:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get_value(self, key: str):
        return self.values.get(key)

    def set_value(self, key: str, value: str) -> None:
        self.values[key] = value


class WahooSourceTests(unittest.TestCase):
    def _config(self, **overrides):
        values = {
            "wahoo_client_id": "client-id",
            "wahoo_client_secret": "client-secret",
            "wahoo_redirect_uri": "http://localhost/",
            "wahoo_access_token": "access-token",
            "wahoo_refresh_token": None,
            "wahoo_expires_at": "4102444800",
            "wahoo_scope": "user_read workouts_read workouts_write",
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def _source(self, *, config=None, state=None, session=None):
        state_db = state or _StateDB()
        target = WahooTarget(config or self._config(), state_db)
        target.session = session or _Session()
        source = WahooSource(config or self._config(), target)
        return source, target

    def test_lists_completed_workouts_and_maps_sports(self) -> None:
        session = _Session(
            [
                _Response(
                    {
                        "workouts": [
                            {
                                "id": 123,
                                "starts": "2025-01-02T03:04:05Z",
                                "workout_type_id": 4,
                                "name": "Planned name",
                                "workout_summary": {
                                    "name": "Trail workout",
                                    "file": {"url": "https://files.wahoo.test/123.fit"},
                                },
                            },
                            {"id": 122, "workout_summary": None},
                            {"id": 121, "workout_summary": {}},
                        ],
                        "total": 3,
                        "page": 1,
                        "per_page": 30,
                    }
                )
            ]
        )
        source, _ = self._source(session=session)

        activities = source.list_activities(
            datetime(2025, 1, 1, tzinfo=timezone.utc),
            datetime(2025, 1, 3, tzinfo=timezone.utc),
            None,
        )

        self.assertEqual(len(activities), 1)
        self.assertEqual(activities[0].source, "wahoo")
        self.assertEqual(activities[0].source_id, "123")
        self.assertEqual(activities[0].name, "Trail workout")
        self.assertEqual(activities[0].sport_type, "trail_run")
        self.assertEqual(activities[0].start_time, datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
        self.assertEqual(session.get_calls[0][1]["params"], {"page": 1, "per_page": 30})

    def test_paginates_workouts_in_descending_order(self) -> None:
        def row(workout_id: int, starts: str) -> dict:
            return {
                "id": workout_id,
                "starts": starts,
                "workout_type_id": 15,
                "workout_summary": {"file": {"url": f"https://files.wahoo.test/{workout_id}.fit"}},
            }

        session = _Session(
            [
                _Response({"workouts": [row(2, "2025-01-02T03:04:05Z")], "total": 2}),
                _Response({"workouts": [row(1, "2025-01-01T03:04:05Z")], "total": 2}),
            ]
        )
        source, _ = self._source(session=session)
        source.page_size = 1

        activities = source.list_activities(None, None, None)

        self.assertEqual([item.source_id for item in activities], ["2", "1"])
        self.assertEqual(len(session.get_calls), 2)

    def test_downloads_fit_without_forwarding_wahoo_authorization(self) -> None:
        source, target = self._source()
        activity = Activity(
            source="wahoo",
            source_id="123",
            name="Ride",
            raw={"workout_summary": {"file": {"url": "https://files.wahoo.test/123.fit"}}},
        )
        response = _Response(content=b"\x00" * 8 + b".FIT" + b"\x00" * 32)

        with tempfile.TemporaryDirectory() as directory, patch(
            "sport_sync_bridge.wahoo_source.requests.get", return_value=response
        ) as download:
            path = source.download_fit(activity, Path(directory))
            cached = source.download_fit(activity, Path(directory))
            self.assertTrue(path.is_file())
            self.assertEqual(path.read_bytes(), response.content)

        self.assertEqual(path, cached)
        self.assertEqual(target.session.get_calls, [])
        download.assert_called_once_with("https://files.wahoo.test/123.fit", timeout=120)

    def test_fetches_missing_summary_and_rejects_insecure_file_url(self) -> None:
        source, target = self._source(
            session=_Session(
                [
                    _Response(
                        {"workout_summary": {"file": {"url": "https://files.wahoo.test/123.fit"}}}
                    )
                ]
            )
        )
        activity = Activity(source="wahoo", source_id="123", name="Ride", raw={})
        response = _Response(content=b"\x00" * 8 + b".FIT" + b"\x00" * 32)

        with tempfile.TemporaryDirectory() as directory, patch(
            "sport_sync_bridge.wahoo_source.requests.get", return_value=response
        ) as download:
            path = source.download_fit(activity, Path(directory))
            self.assertTrue(path.is_file())
            self.assertEqual(path.read_bytes(), response.content)

        self.assertEqual(
            target.session.get_calls[0][0],
            "https://api.wahooligan.com/v1/workouts/123/workout_summary",
        )
        self.assertEqual(
            target.session.get_calls[0][1]["headers"]["Authorization"], "Bearer access-token"
        )
        download.assert_called_once_with("https://files.wahoo.test/123.fit", timeout=120)

        insecure = Activity(
            source="wahoo",
            source_id="124",
            name="Ride",
            raw={"workout_summary": {"file": {"url": "http://files.wahoo.test/124.fit"}}},
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "sport_sync_bridge.wahoo_source.requests.get"
        ) as download:
            with self.assertRaisesRegex(RuntimeError, "invalid FIT file URL"):
                source.download_fit(insecure, Path(directory))
        download.assert_not_called()

    def test_requires_read_scope_and_registers_source_in_cli_and_engine(self) -> None:
        read_only_source, _ = self._source(
            config=self._config(wahoo_scope="user_read workouts_read")
        )
        read_only_source.authenticate()

        source, _ = self._source(config=self._config(wahoo_scope="user_read workouts_write"))
        with self.assertRaisesRegex(RuntimeError, "workouts_read"):
            source.authenticate()

        self.assertEqual(build_parser().parse_args(["sync", "--source", "wahoo"]).source, ["wahoo"])
        self.assertEqual(build_parser().parse_args(["check", "--source", "wahoo"]).source, ["wahoo"])

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "SYNC_DATA_DIR": str(Path(directory) / "data"),
                "SYNC_SOURCES": "wahoo",
                "SYNC_TARGETS": "wahoo",
                "WAHOO_CLIENT_ID": "test-client",
                "WAHOO_CLIENT_SECRET": "test-secret",
            },
        ):
            config = AppConfig.load(Path(directory))
            engine = SyncEngine(config)
            try:
                self.assertIn("wahoo", engine.sources)
                self.assertIn("wahoo", engine.targets)
            finally:
                engine.close()


if __name__ == "__main__":
    unittest.main()
