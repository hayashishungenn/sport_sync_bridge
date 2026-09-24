from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sport_sync_bridge.activity_library import LocalActivityLibrary
from sport_sync_bridge.activity_analysis import build_ai_analysis_prompt, summarize_activity
from sport_sync_bridge.cli import main
from sport_sync_bridge.formats import read_activity_file
from sport_sync_bridge.state import StateDB
from sport_sync_bridge.training_balance import (
    calculate_training_balance,
    format_training_balance,
    _zone_for_tsb,
)
from tests.activity_fixtures import create_fit, create_gpx


class TrainingBalanceTests(unittest.TestCase):
    def test_hr_tss_uses_garsync_formula_and_fit_tss_takes_precedence(self) -> None:
        result = calculate_training_balance(
            [self._activity("2026-01-01", average_heart_rate_bpm=150, timer_time_s=3600)],
            resting_hr=60,
            threshold_hr=180,
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 1),
        )
        history = result["history"]
        self.assertIsInstance(history, list)
        self.assertEqual(history[0]["training_stress_score"], 56.25)
        self.assertEqual(result["load_source_counts"], {"hr_tss": 1})

        fit_tss = calculate_training_balance(
            [self._activity("2026-01-01", training_stress_score=100, average_heart_rate_bpm=150, timer_time_s=3600)],
            threshold_hr=180,
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 1),
        )
        self.assertEqual(fit_tss["history"][0]["training_stress_score"], 100.0)
        self.assertEqual(fit_tss["load_source_counts"], {"fit_tss": 1})

        zero_fit_tss = calculate_training_balance(
            [self._activity("2026-01-01", training_stress_score=0, average_heart_rate_bpm=150, timer_time_s=3600)],
            threshold_hr=180,
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 1),
        )
        self.assertEqual(zero_fit_tss["history"][0]["training_stress_score"], 0.0)
        self.assertEqual(zero_fit_tss["load_source_counts"], {"fit_tss": 1})

    def test_ctl_atl_smooth_daily_load_and_older_activities_seed_range(self) -> None:
        result = calculate_training_balance(
            [
                self._activity("2026-01-01", training_stress_score=100),
                self._activity("2026-01-03", training_stress_score=50),
            ],
            date_from=date(2026, 1, 3),
            date_to=date(2026, 1, 4),
        )
        history = result["history"]
        self.assertEqual(len(history), 2)
        self.assertAlmostEqual(history[0]["ctl"], round((100 / 42) * (41 / 42) ** 2 + 50 / 42, 3))
        self.assertAlmostEqual(history[0]["atl"], round((100 / 7) * (6 / 7) ** 2 + 50 / 7, 3))
        self.assertEqual(history[0]["activity_count"], 1)
        self.assertEqual(result["scored_activity_count"], 1)

    def test_hr_intensity_is_clamped_and_missing_inputs_remain_unscored(self) -> None:
        result = calculate_training_balance(
            [
                self._activity("2026-01-01", average_heart_rate_bpm=300, timer_time_s=3600),
                self._activity("2026-01-01", average_heart_rate_bpm=50, timer_time_s=3600),
                self._activity("2026-01-01", average_heart_rate_bpm=150),
            ],
            resting_hr=60,
            threshold_hr=180,
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 1),
        )
        self.assertEqual(result["history"][0]["training_stress_score"], 400.0)
        self.assertEqual(result["scored_activity_count"], 2)
        self.assertEqual(result["unscored_activity_count"], 1)

    def test_offset_timestamps_use_utc_calendar_date(self) -> None:
        result = calculate_training_balance(
            [
                {
                    "start_time": "2026-01-02T00:30:00+09:00",
                    "summary_json": json.dumps({"training_stress_score": 50}),
                }
            ],
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 1),
        )
        self.assertEqual(result["history"][0]["training_stress_score"], 50.0)

    def test_zone_boundaries_match_garsync_thresholds(self) -> None:
        cases = (
            (-30.001, "high_risk"),
            (-30, "optimal"),
            (-10, "optimal"),
            (-9.999, "grey"),
            (5, "grey"),
            (5.001, "fresh"),
            (25, "fresh"),
            (25.001, "transitional"),
        )
        for tsb, expected in cases:
            with self.subTest(tsb=tsb):
                self.assertEqual(_zone_for_tsb(tsb), expected)

    def test_date_and_heart_rate_validation(self) -> None:
        with self.assertRaisesRegex(ValueError, "must exceed"):
            calculate_training_balance([], resting_hr=180, threshold_hr=180)
        with self.assertRaisesRegex(ValueError, "must not be after"):
            calculate_training_balance([], date_from=date(2026, 1, 2), date_to=date(2026, 1, 1))

    def test_csv_and_text_reports_expose_daily_balance(self) -> None:
        result = calculate_training_balance(
            [self._activity("2026-01-01", training_stress_score=100)],
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 1),
        )
        self.assertIn("date,activity_count,training_stress_score,ctl,atl,tsb,zone", format_training_balance(result, "csv"))
        self.assertIn("CTL=", format_training_balance(result, "txt"))

    def test_ai_analysis_context_includes_fit_training_stress_score(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            activity = read_activity_file(
                create_fit(
                    Path(temporary) / "ai-training-load.fit",
                    training_stress_score=72.5,
                )
            )
            summary = summarize_activity(activity)
            prompt = build_ai_analysis_prompt(summary, [], "今天的训练负荷如何？")

            self.assertEqual(summary["training_stress_score"], 72.5)
            self.assertIn("训练压力分（TSS）：72.5", prompt)

    def test_library_balance_cli_reads_sqlite_rows_and_runs_without_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / ".data"
            config = SimpleNamespace(
                data_dir=data_dir,
                db_path=data_dir / "sync_state.db",
                log_level="ERROR",
                log_path=data_dir / "sync.log",
            )
            state = StateDB(config.db_path)
            LocalActivityLibrary(state, data_dir).import_paths([create_gpx(root / "ride.gpx")])
            state.close()

            output = io.StringIO()
            with patch("sport_sync_bridge.cli.AppConfig.load", return_value=config):
                with patch("sport_sync_bridge.cli.configure_logging"):
                    with contextlib.redirect_stdout(output):
                        status = main(
                            [
                                "library",
                                "balance",
                                "--threshold-hr",
                                "180",
                                "--from",
                                "2026-01-02",
                                "--to",
                                "2026-01-02",
                            ]
                        )

            self.assertEqual(status, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["load_source_counts"], {"hr_tss": 1})
            self.assertEqual(payload["history"][0]["date"], "2026-01-02")

    @staticmethod
    def _activity(activity_date: str, **summary: float) -> dict[str, str]:
        return {
            "start_time": f"{activity_date}T12:00:00+00:00",
            "summary_json": json.dumps(summary),
        }

if __name__ == "__main__":
    unittest.main()
