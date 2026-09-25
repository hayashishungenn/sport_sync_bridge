from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sport_sync_bridge.cli import build_parser, main
from sport_sync_bridge.health import (
    import_garmin_user_summaries,
    list_garmin_user_summaries,
    summarize_health,
    summarize_health_for_activity,
)
from sport_sync_bridge.health_sources import (
    fetch_garmin_user_summaries,
    validate_garmin_user_summary_date_range,
)
from sport_sync_bridge.state import StateDB


class _FakeGarminClient:
    def __init__(self, responses: dict[str, object]):
        self.responses = responses
        self.requested_dates: list[str] = []

    def get_user_summary(self, cdate: str) -> object:
        self.requested_dates.append(cdate)
        return self.responses[cdate]


class GarminUserSummaryTests(unittest.TestCase):
    def test_fetches_inclusive_dates_and_fills_calendar_date(self) -> None:
        client = _FakeGarminClient(
            {
                "2026-08-03": {"totalSteps": 7000},
                "2026-08-04": {"calendarDate": "2026-08-04", "totalSteps": 8000},
            }
        )

        records = fetch_garmin_user_summaries(client, "2026-08-03", "2026-08-04")

        self.assertEqual(client.requested_dates, ["2026-08-03", "2026-08-04"])
        self.assertEqual(
            records,
            [
                {"totalSteps": 7000, "calendarDate": "2026-08-03"},
                {"calendarDate": "2026-08-04", "totalSteps": 8000},
            ],
        )

    def test_rejects_invalid_ranges_dates_and_response_shapes(self) -> None:
        client = _FakeGarminClient({})
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            validate_garmin_user_summary_date_range("20260803", "2026-08-04")
        with self.assertRaisesRegex(ValueError, "on or after"):
            fetch_garmin_user_summaries(client, "2026-08-04", "2026-08-03")
        self.assertEqual(client.requested_dates, [])

        with self.assertRaisesRegex(ValueError, "must be an object"):
            fetch_garmin_user_summaries(
                _FakeGarminClient({"2026-08-03": []}), "2026-08-03", "2026-08-03"
            )
        with self.assertRaisesRegex(ValueError, "does not match requested date"):
            fetch_garmin_user_summaries(
                _FakeGarminClient(
                    {"2026-08-03": {"calendarDate": "2026-08-02", "totalSteps": 7000}}
                ),
                "2026-08-03",
                "2026-08-03",
            )
        with self.assertRaisesRegex(RuntimeError, "does not support"):
            fetch_garmin_user_summaries(object(), "2026-08-03", "2026-08-03")

    def test_imports_raw_record_and_normalizes_daily_metrics_idempotently(self) -> None:
        record = {
            "calendarDate": "2026-08-03",
            "totalSteps": 9000,
            "dailyStepGoal": 10000,
            "floorsAscended": 20,
            "floorsDescended": 18,
            "userFloorsAscendedGoal": 10,
            "restingHeartRate": 48,
            "minHeartRate": 42,
            "maxHeartRate": 151,
            "lastSevenDaysAvgRestingHeartRate": 49,
            "averageStressLevel": 24,
            "maxStressLevel": 71,
            "bodyBatteryMostRecentValue": 76,
            "bodyBatteryHighestValue": 98,
            "bodyBatteryLowestValue": 35,
            "bodyBatteryChargedValue": 60,
            "bodyBatteryDrainedValue": 42,
            "averageSpo2": 97,
            "lowestSpo2": 92,
            "sleepingSeconds": 25200,
            "avgWakingRespirationValue": 14,
            "latestRespirationValue": 13,
            "highestRespirationValue": 21,
            "hrvWeeklyAverage": 54,
            "totalDistanceMeters": 5200,
            "totalKilocalories": 2100,
            "activeKilocalories": 550,
            "bmrKilocalories": 1500,
            "wellnessKilocalories": 1800,
            "hrvStatus": "UNBALANCED",
            "wellnessStartTimeLocal": "2026-08-03T00:00:00",
            "wellnessEndTimeLocal": "2026-08-03T23:59:59",
            "unmappedField": {"kept": True},
        }
        with tempfile.TemporaryDirectory() as directory:
            state = StateDB(Path(directory) / "health.db")
            try:
                first = import_garmin_user_summaries(state, [record])
                second = import_garmin_user_summaries(state, [record])
                rows = state.list_health_observations()
                summary = summarize_health(state)
                raw = list_garmin_user_summaries(state, "2026-08-03", "2026-08-03")
            finally:
                state.close()

        self.assertEqual(first, {"summaries_stored": 1, "observations_processed": 29})
        self.assertEqual(second, {"summaries_stored": 0, "observations_processed": 29})
        self.assertEqual(len(rows), 29)
        latest = summary["latest"]
        self.assertEqual(latest["steps"]["value"], 9000)
        self.assertEqual(latest["sleep_hours"]["value"], 7.0)
        self.assertEqual(latest["distance_km"]["value"], 5.2)
        self.assertEqual(latest["hrv_status"]["value"], "UNBALANCED")
        self.assertEqual(latest["body_battery"]["value"], 76)
        self.assertEqual(raw["record_count"], 1)
        saved = raw["records"][0]["summary"]
        self.assertEqual(saved["unmappedField"], {"kept": True})
        self.assertEqual(saved["wellnessEndTimeLocal"], "2026-08-03T23:59:59")

    def test_validation_failure_does_not_partially_store_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateDB(Path(directory) / "health.db")
            try:
                with self.assertRaisesRegex(ValueError, "Invalid value for health metric"):
                    import_garmin_user_summaries(
                        state,
                        [
                            {"calendarDate": "2026-08-03", "totalSteps": 5000},
                            {"calendarDate": "2026-08-04", "totalSteps": "invalid"},
                        ],
                    )
                self.assertEqual(state.list_health_observations(), [])
                self.assertEqual(
                    list_garmin_user_summaries(state, "2026-08-03", "2026-08-04")[
                        "record_count"
                    ],
                    0,
                )
            finally:
                state.close()

    def test_database_failure_rolls_back_raw_and_normalized_summaries_together(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateDB(Path(directory) / "health.db")
            try:
                state.connection.executescript(
                    "CREATE TRIGGER reject_garmin_steps "
                    "BEFORE INSERT ON health_observations "
                    "WHEN NEW.metric = 'steps' "
                    "BEGIN SELECT RAISE(ABORT, 'test database failure'); END;"
                )
                with self.assertRaisesRegex(sqlite3.IntegrityError, "test database failure"):
                    import_garmin_user_summaries(
                        state,
                        [{"calendarDate": "2026-08-03", "totalSteps": 9000}],
                    )
                self.assertEqual(state.list_health_observations(), [])
                self.assertEqual(
                    list_garmin_user_summaries(state, "2026-08-03", "2026-08-03")[
                        "record_count"
                    ],
                    0,
                )
            finally:
                state.close()

    def test_daily_rollup_is_not_treated_as_pre_activity_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateDB(Path(directory) / "health.db")
            try:
                import_garmin_user_summaries(
                    state, [{"calendarDate": "2026-08-03", "totalSteps": 9000}]
                )
                same_day = summarize_health_for_activity(
                    state,
                    "2026-08-03T09:00:00+00:00",
                    "2026-08-03T10:00:00+00:00",
                )
                next_day = summarize_health_for_activity(
                    state,
                    "2026-08-04T09:00:00+00:00",
                    "2026-08-04T10:00:00+00:00",
                )
            finally:
                state.close()

        self.assertNotIn("steps", same_day["before_activity"])
        self.assertEqual(same_day["after_activity"]["steps"]["value"], 9000)
        self.assertEqual(next_day["before_activity"]["steps"]["value"], 9000)

    def test_cli_fetches_persists_and_displays_garmin_summaries(self) -> None:
        fetch_args = [
            "health",
            "fetch-garmin-summary",
            "--start-date",
            "2026-08-03",
            "--end-date",
            "2026-08-03",
        ]
        self.assertEqual(build_parser().parse_args(fetch_args).health_action, "fetch-garmin-summary")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = SimpleNamespace(
                data_dir=root / ".data",
                db_path=root / "state.db",
                log_level="INFO",
                log_path=root / "sync.log",
            )
            client = _FakeGarminClient(
                {"2026-08-03": {"totalSteps": 9000, "calendarDate": "2026-08-03"}}
            )
            authenticate = Mock()
            target = SimpleNamespace(client=client, authenticate=authenticate)
            output = io.StringIO()
            with (
                patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
                patch("sport_sync_bridge.cli.configure_logging"),
                patch("sport_sync_bridge.cli.GarminTarget", return_value=target),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(main(fetch_args), 0)

            authenticate.assert_called_once_with()
            self.assertIn("summaries_fetched=1", output.getvalue())
            self.assertIn("summaries_stored=1", output.getvalue())
            self.assertIn("observations_processed=1", output.getvalue())

            output = io.StringIO()
            with (
                patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
                patch("sport_sync_bridge.cli.configure_logging"),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(
                    main(
                        [
                            "health",
                            "summaries",
                            "--start-date",
                            "2026-08-03",
                            "--end-date",
                            "2026-08-03",
                        ]
                    ),
                    0,
                )

        result = json.loads(output.getvalue())
        self.assertEqual(result["record_count"], 1)
        self.assertEqual(result["records"][0]["summary"]["totalSteps"], 9000)


if __name__ == "__main__":
    unittest.main()
