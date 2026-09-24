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
from sport_sync_bridge.cli import main
from sport_sync_bridge.state import StateDB
from sport_sync_bridge.vdot import (
    analyze_running_activities,
    calculate_vdot,
    format_vdot_report,
    predict_race_times,
    training_paces,
)


class VDOTTests(unittest.TestCase):
    def test_calculate_vdot_uses_daniels_formula_reference(self) -> None:
        self.assertAlmostEqual(calculate_vdot(5000, 1500), 38.3, places=1)

    def test_calculator_minimum_is_exclusive(self) -> None:
        self.assertIsNone(calculate_vdot(200, 300, minimum_value=200))
        self.assertIsNone(calculate_vdot(300, 60, minimum_value=60))
        self.assertIsNotNone(calculate_vdot(201, 61, minimum_value=60))

    def test_race_predictions_invert_the_calculator(self) -> None:
        vdot = calculate_vdot(5000, 1500)
        self.assertIsNotNone(vdot)
        predictions = predict_race_times(vdot)
        distances = {
            "1mi": 1609.344,
            "3k": 3000,
            "5k": 5000,
            "10k": 10000,
            "half_marathon": 21097.5,
            "full_marathon": 42195,
        }
        self.assertEqual(set(predictions), set(distances))
        self.assertAlmostEqual(predictions["5k"], 1500, delta=0.001)
        for name, seconds in predictions.items():
            self.assertIsNotNone(seconds, name)
            self.assertAlmostEqual(
                calculate_vdot(distances[name], seconds, minimum_value=0),
                vdot,
                places=7,
            )

    def test_training_paces_use_five_apk_ranges(self) -> None:
        paces = training_paces(38.3)
        self.assertEqual(list(paces), ["easy", "marathon", "threshold", "interval", "repetition"])
        for pace in paces.values():
            self.assertLess(pace["min_seconds_per_km"], pace["max_seconds_per_km"])
        self.assertGreater(paces["easy"]["min_seconds_per_km"], paces["threshold"]["max_seconds_per_km"])

    def test_daily_curve_uses_best_run_and_excludes_non_running_or_short_activities(self) -> None:
        rows = [
            self._row("a" * 64, "running", "2026-01-02T08:00:00+00:00", 5000, 1500),
            self._row("b" * 64, "running", "2026-01-02T18:00:00+00:00", 10000, 3300),
            self._row("c" * 64, "cycling", "2026-01-03T08:00:00+00:00", 5000, 1200),
            self._row("d" * 64, "running", "2026-01-03T08:00:00+00:00", 200, 70),
        ]
        report = analyze_running_activities(rows)

        self.assertEqual(report["running_activity_count"], 3)
        self.assertEqual(report["eligible_activity_count"], 2)
        self.assertEqual(len(report["daily_trend"]), 1)
        self.assertEqual(report["daily_trend"][0]["activity_count"], 2)
        self.assertEqual(report["latest"]["activity_id"], "a" * 64)
        self.assertEqual(report["best"]["activity_id"], "a" * 64)
        self.assertEqual(len(report["latest"]["race_predictions"]), 6)
        self.assertIn("Easy Run", format_vdot_report(report, "txt"))

    def test_date_filter_and_reversed_range_validation(self) -> None:
        rows = [self._row("a" * 64, "running", "2026-01-02T08:00:00+00:00", 5000, 1500)]
        report = analyze_running_activities(rows, date_from=date(2026, 1, 3))
        self.assertEqual(report["running_activity_count"], 0)
        with self.assertRaisesRegex(ValueError, "must not be after"):
            analyze_running_activities(rows, date_from=date(2026, 1, 3), date_to=date(2026, 1, 2))

    def test_library_vdot_cli_reads_imported_json_without_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / ".data"
            activity_path = root / "run.json"
            activity_path.write_text(
                json.dumps(
                    {
                        "name": "Test run",
                        "sport_type": "running",
                        "start_time": "2026-01-02T08:00:00+00:00",
                        "distance_m": 5000,
                        "timer_time_s": 1500,
                    }
                ),
                encoding="utf-8",
            )
            config = SimpleNamespace(
                data_dir=data_dir,
                db_path=data_dir / "sync_state.db",
                log_level="ERROR",
                log_path=data_dir / "sync.log",
            )
            state = StateDB(config.db_path)
            LocalActivityLibrary(state, data_dir).import_paths([activity_path])
            state.close()

            output = io.StringIO()
            with patch("sport_sync_bridge.cli.AppConfig.load", return_value=config):
                with patch("sport_sync_bridge.cli.configure_logging"):
                    with contextlib.redirect_stdout(output):
                        status = main(["library", "vdot", "--from", "2026-01-01"])

            self.assertEqual(status, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["eligible_activity_count"], 1)
            self.assertAlmostEqual(payload["latest"]["vdot"], 38.3, places=1)
            self.assertEqual(payload["latest"]["date"], "2026-01-02")

    @staticmethod
    def _row(
        fingerprint: str,
        sport: str,
        start_time: str,
        distance_m: float,
        duration_s: float,
    ) -> dict[str, object]:
        return {
            "fingerprint": fingerprint,
            "name": "Run",
            "sport_type": sport,
            "start_time": start_time,
            "summary_json": json.dumps(
                {
                    "distance_m": distance_m,
                    "timer_time_s": duration_s,
                }
            ),
        }


if __name__ == "__main__":
    unittest.main()
