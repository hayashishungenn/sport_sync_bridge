from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sport_sync_bridge.cli import build_parser, main
from sport_sync_bridge.health import (
    format_health_summary_text,
    import_intervals_icu_wellness,
    summarize_health,
)
from sport_sync_bridge.health_sources import (
    fetch_intervals_icu_wellness,
    validate_intervals_wellness_date_range,
)
from sport_sync_bridge.state import StateDB


class _FakeWellnessClient:
    def __init__(self, response: object):
        self.response = response
        self.requested_range: tuple[str, str] | None = None

    def list_wellness(self, start_date: str, end_date: str) -> object:
        self.requested_range = (start_date, end_date)
        return self.response


class IntervalsIcuWellnessTests(unittest.TestCase):
    def test_fetch_validates_and_requests_the_inclusive_range(self) -> None:
        client = _FakeWellnessClient([{"id": "2026-08-03", "weight": 72.4}])

        records = fetch_intervals_icu_wellness(client, "2026-08-03", "2026-08-05")

        self.assertEqual(client.requested_range, ("2026-08-03", "2026-08-05"))
        self.assertEqual(records, [{"id": "2026-08-03", "weight": 72.4}])

    def test_rejects_bad_ranges_and_records_before_import(self) -> None:
        client = _FakeWellnessClient([])
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            validate_intervals_wellness_date_range("20260803", "2026-08-04")
        with self.assertRaisesRegex(ValueError, "on or after"):
            fetch_intervals_icu_wellness(client, "2026-08-04", "2026-08-03")
        self.assertIsNone(client.requested_range)

        for response, message in (({"records": []}, "must be a list"), ([None], "must be an object"),
                                  ([{"id": "not-a-date"}], "YYYY-MM-DD"),
                                  ([{"id": "2026-08-06"}], "outside the requested date range")):
            with self.subTest(response=response):
                with self.assertRaisesRegex(ValueError, message):
                    fetch_intervals_icu_wellness(
                        _FakeWellnessClient(response), "2026-08-03", "2026-08-05"
                    )

    def test_imports_all_model_fields_and_is_idempotent_for_identical_records(self) -> None:
        record = {
            "id": "2026-08-03",
            "weight": 72.4,
            "bodyFat": 19.8,
            "restingHR": 47,
            "hrv": 62.5,
            "hrvSDNN": 74.2,
            "sleepSecs": 25200,
            "sleepScore": 82,
            "sleepQuality": 2,
            "spO2": 97.5,
            "systolic": 120,
            "diastolic": 78,
            "steps": 8500,
            "respiration": 14.2,
            "hydrationVolume": 1.75,
            "comments": "Easy day",
        }
        with tempfile.TemporaryDirectory() as directory:
            state = StateDB(Path(directory) / "health.db")
            try:
                self.assertEqual(import_intervals_icu_wellness(state, [record]), 15)
                self.assertEqual(import_intervals_icu_wellness(state, [record]), 15)
                rows = state.list_health_observations()
                self.assertEqual(len(rows), 15)
                summary = summarize_health(state)
            finally:
                state.close()

        latest = summary["latest"]
        self.assertEqual(latest["weight_kg"]["value"], 72.4)
        self.assertEqual(latest["body_fat_percent"]["unit"], "%")
        self.assertEqual(latest["hrv_sdnn_ms"]["unit"], "ms")
        self.assertEqual(latest["sleep_hours"]["value"], 7.0)
        self.assertEqual(latest["sleep_quality_score"]["value"], 2.0)
        self.assertEqual(latest["hydration_l"]["value"], 1.75)
        self.assertEqual(latest["wellness_comment"]["value"], "Easy day")
        report = format_health_summary_text(summary)
        self.assertIn("健康备注：Easy day", report)
        self.assertNotIn("健康备注：Easy day text", report)

    def test_import_rejects_bad_dates_or_non_numeric_measurements(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateDB(Path(directory) / "health.db")
            try:
                with self.assertRaisesRegex(ValueError, "valid date ID"):
                    import_intervals_icu_wellness(state, [{"id": "bad-date", "weight": 72}])
                with self.assertRaisesRegex(ValueError, "Invalid value for health metric"):
                    import_intervals_icu_wellness(
                        state,
                        [
                            {"id": "2026-08-02", "weight": 71.0},
                            {"id": "2026-08-03", "weight": "unknown"},
                        ],
                    )
                self.assertEqual(state.list_health_observations(), [])
            finally:
                state.close()

    def test_cli_fetches_and_stores_wellness_records(self) -> None:
        self.assertEqual(
            build_parser().parse_args(
                [
                    "health",
                    "fetch-intervals-wellness",
                    "--start-date",
                    "2026-08-03",
                    "--end-date",
                    "2026-08-03",
                ]
            ).health_action,
            "fetch-intervals-wellness",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = SimpleNamespace(
                data_dir=root / ".data",
                db_path=root / "state.db",
                log_level="INFO",
                log_path=root / "sync.log",
            )
            source = _FakeWellnessClient([{"id": "2026-08-03", "weight": 72.4}])
            output = io.StringIO()
            with (
                patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
                patch("sport_sync_bridge.cli.configure_logging"),
                patch("sport_sync_bridge.cli.IntervalsIcuSource", return_value=source),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(
                    main(
                        [
                            "health",
                            "fetch-intervals-wellness",
                            "--start-date",
                            "2026-08-03",
                            "--end-date",
                            "2026-08-03",
                        ]
                    ),
                    0,
                )

            self.assertEqual(source.requested_range, ("2026-08-03", "2026-08-03"))
            self.assertIn("records_fetched=1", output.getvalue())
            self.assertIn("observations_processed=1", output.getvalue())
            state = StateDB(config.db_path)
            try:
                self.assertEqual(len(state.list_health_observations()), 1)
            finally:
                state.close()


if __name__ == "__main__":
    unittest.main()
