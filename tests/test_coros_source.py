from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from mcp.shared.auth import OAuthToken

from sport_sync_bridge.cli import build_parser
from sport_sync_bridge.config import AppConfig
from sport_sync_bridge.coros_source import (
    CorosMcpSource,
    CorosMcpTokenStorage,
    _activity_from_record,
    _build_tool_arguments,
    _collect_activity_rows,
    _extract_fit_bytes,
    _result_payload,
)
from sport_sync_bridge.engine import SyncEngine
from sport_sync_bridge.state import StateDB


class CorosSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.state_db = StateDB(self.root / "state.db")
        self.config = cast(
            AppConfig,
            SimpleNamespace(
                coros_mcp_url="https://mcp.coros.com/mcp",
                coros_mcp_timezone="Asia/Hong_Kong",
            ),
        )
        self.source = CorosMcpSource(self.config, self.state_db)

    def tearDown(self) -> None:
        self.state_db.close()
        self.temp_dir.cleanup()

    def test_tool_arguments_follow_server_schema_and_preserve_activity_id(self) -> None:
        start = datetime(2026, 9, 1, tzinfo=timezone.utc)
        end = datetime(2026, 9, 7, 23, 59, tzinfo=timezone.utc)
        query_tool = SimpleNamespace(
            name="querySportRecords",
            inputSchema={
                "type": "object",
                "properties": {
                    "startDate": {"type": "string", "format": "date"},
                    "endDate": {"type": "string", "format": "date"},
                    "sportTypeCodes": {"type": "array", "items": {"type": "integer"}},
                    "limit": {"type": "integer"},
                    "timezone": {"type": "string"},
                },
                "required": ["startDate", "endDate", "sportTypeCodes", "timezone"],
            },
        )

        arguments = _build_tool_arguments(
            query_tool,
            since=start,
            until=end,
            limit=12,
            timezone_name="Asia/Hong_Kong",
        )
        self.assertEqual(arguments, {
            "startDate": "2026-09-01",
            "endDate": "2026-09-07",
            "sportTypeCodes": [],
            "limit": 12,
            "timezone": "Asia/Hong_Kong",
        })

        download_tool = SimpleNamespace(
            name="downloadActivityFitFiles",
            inputSchema={
                "properties": {
                    "labelId": {"type": "string"},
                    "sportType": {"type": "integer"},
                },
                "required": ["labelId", "sportType"],
            },
        )
        self.assertEqual(
            _build_tool_arguments(
                download_tool,
                activity_id="record-1",
                sport_type_code=100,
            ),
            {"labelId": "record-1", "sportType": 100},
        )

    def test_unknown_required_tool_parameters_fail_explicitly(self) -> None:
        tool = SimpleNamespace(
            name="querySportRecords",
            inputSchema={"properties": {"cursor": {"type": "string"}}, "required": ["cursor"]},
        )
        with self.assertRaisesRegex(RuntimeError, "unsupported parameters: cursor"):
            _build_tool_arguments(tool, since=datetime.now(timezone.utc))

    def test_parses_structured_and_formatted_activity_records(self) -> None:
        formatted = (
            "LabelId: run-1 | SportType: Running | StartTime: 2026-09-01T06:00:00Z\n"
            "LabelId: ride-2 | SportType: Cycling | StartTime: 2026-09-02T07:00:00Z"
        )
        result = SimpleNamespace(isError=False, content=[SimpleNamespace(text=formatted)])
        rows = _collect_activity_rows(_result_payload(result))
        activities = [_activity_from_record(row) for row in rows]
        self.assertEqual([item.source_id for item in activities if item], ["run-1", "ride-2"])
        self.assertEqual([item.sport_type for item in activities if item], ["running", "cycling"])

        structured = _collect_activity_rows(
            {"data": {"records": [{"activityId": "a1", "startTime": "2026-09-01T00:00:00Z"}]}}
        )
        self.assertEqual(len(structured), 1)
        self.assertEqual(structured[0]["activityId"], "a1")

    def test_listing_calls_read_only_tool_and_applies_local_limit(self) -> None:
        query_tool = SimpleNamespace(
            name="querySportRecords",
            inputSchema={
                "properties": {
                    "startDate": {"type": "string", "format": "date"},
                    "endDate": {"type": "string", "format": "date"},
                    "sportTypeCodes": {"type": "array"},
                    "timezone": {"type": "string"},
                },
                "required": ["startDate", "endDate", "sportTypeCodes", "timezone"],
            },
        )

        class FakeSession:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object]]] = []

            async def list_tools(self) -> object:
                return SimpleNamespace(tools=[query_tool])

            async def call_tool(self, name: str, arguments: dict[str, object]) -> object:
                self.calls.append((name, arguments))
                return SimpleNamespace(
                    isError=False,
                    content=[
                        SimpleNamespace(
                            text=(
                                "LabelId: run-1 | SportType: Run | StartTime: 2026-09-01T06:00:00Z\n"
                                "LabelId: run-2 | SportType: Run | StartTime: 2026-09-02T06:00:00Z"
                            )
                        )
                    ],
                )

        fake_session = FakeSession()

        async def with_fake_session(operation: Any) -> Any:
            return await operation(fake_session)

        with patch.object(self.source, "_with_session", with_fake_session):
            activities = self.source.list_activities(
                datetime(2026, 9, 1, tzinfo=timezone.utc),
                datetime(2026, 9, 3, tzinfo=timezone.utc),
                1,
            )
        self.assertEqual(len(activities), 1)
        self.assertEqual(activities[0].source, "coros")
        self.assertEqual(activities[0].source_id, "run-1")
        self.assertEqual(fake_session.calls[0][0], "querySportRecords")

    def test_download_fit_writes_and_reuses_the_valid_local_copy(self) -> None:
        fit_file = bytearray(256)
        fit_file[0] = 14
        fit_file[8:12] = b".FIT"
        download_tool = SimpleNamespace(
            name="downloadActivityFitFiles",
            inputSchema={
                "properties": {"labelId": {"type": "string"}, "sportType": {"type": "integer"}},
                "required": ["labelId", "sportType"],
            },
        )

        class FakeSession:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object]]] = []

            async def list_tools(self) -> object:
                return SimpleNamespace(tools=[download_tool])

            async def call_tool(self, name: str, arguments: dict[str, object]) -> object:
                self.calls.append((name, arguments))
                return SimpleNamespace(
                    isError=False,
                    content=[
                        SimpleNamespace(resource=SimpleNamespace(blob=base64.b64encode(fit_file).decode("ascii")))
                    ],
                )

        fake_session = FakeSession()

        async def with_fake_session(operation: Any) -> Any:
            return await operation(fake_session)

        activity = _activity_from_record(
            {"labelId": "run-1", "sportType": 100, "startTime": "2026-09-01T06:00:00Z"}
        )
        if activity is None:
            self.fail("COROS activity record did not produce an activity")
        with patch.object(self.source, "_with_session", with_fake_session):
            path = self.source.download_fit(activity, self.root / "downloads")
            reused_path = self.source.download_fit(activity, self.root / "downloads")
        self.assertEqual(path, reused_path)
        self.assertEqual(path.read_bytes(), bytes(fit_file))
        self.assertEqual(fake_session.calls, [("downloadActivityFitFiles", {"labelId": "run-1", "sportType": 100})])
        self.assertEqual(len(json.loads(self.state_db.get_value("coros_mcp_fit_download_attempts") or "[]")), 1)

    def test_fit_binary_resource_is_base64_decoded_and_validated(self) -> None:
        fit_file = bytearray(256)
        fit_file[0] = 14
        fit_file[8:12] = b".FIT"
        result = SimpleNamespace(
            isError=False,
            content=[SimpleNamespace(resource=SimpleNamespace(blob=base64.b64encode(fit_file).decode("ascii")))],
        )
        self.assertEqual(_extract_fit_bytes(result), bytes(fit_file))

        bad = SimpleNamespace(
            isError=False,
            content=[SimpleNamespace(resource=SimpleNamespace(blob=base64.b64encode(b"not fit").decode("ascii")))],
        )
        self.assertIsNone(_extract_fit_bytes(bad))

    def test_fit_download_allowance_is_persisted_and_expired_attempts_are_removed(self) -> None:
        now = datetime.now(timezone.utc)
        self.state_db.set_value(
            "coros_mcp_fit_download_attempts",
            json.dumps([(now - timedelta(hours=1)).isoformat()] * 50),
        )
        with self.assertRaisesRegex(RuntimeError, "50 files per 24 hours"):
            self.source._reserve_fit_download_attempt()

        self.state_db.set_value(
            "coros_mcp_fit_download_attempts",
            json.dumps([(now - timedelta(hours=25)).isoformat()]),
        )
        self.source._reserve_fit_download_attempt()
        stored = json.loads(self.state_db.get_value("coros_mcp_fit_download_attempts") or "[]")
        self.assertEqual(len(stored), 1)

    def test_oauth_token_storage_round_trips_with_local_sqlite(self) -> None:
        storage = CorosMcpTokenStorage(self.state_db)
        token = OAuthToken(access_token="access", token_type="Bearer", refresh_token="refresh")
        asyncio.run(storage.set_tokens(token))
        loaded = asyncio.run(storage.get_tokens())
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.access_token, "access")
        self.assertEqual(loaded.refresh_token, "refresh")

    def test_parser_exposes_coros_auth_and_sync_source(self) -> None:
        auth_args = build_parser().parse_args(["coros-auth"])
        sync_args = build_parser().parse_args(
            ["sync", "--source", "coros", "--target", "strava", "--dry-run"]
        )
        self.assertEqual(auth_args.command, "coros-auth")
        self.assertEqual(sync_args.source, ["coros"])

    def test_engine_registers_coros_without_platform_client_credentials(self) -> None:
        with patch.dict(
            os.environ,
            {"SYNC_DATA_DIR": ".data", "SYNC_SOURCES": "coros", "COROS_MCP_URL": "https://mcp.coros.com/mcp"},
            clear=True,
        ):
            config = AppConfig.load(self.root)
        engine = SyncEngine(config)
        try:
            self.assertIn("coros", engine.sources)
            self.assertEqual(config.coros_mcp_url, "https://mcp.coros.com/mcp")
        finally:
            engine.close()


if __name__ == "__main__":
    unittest.main()
