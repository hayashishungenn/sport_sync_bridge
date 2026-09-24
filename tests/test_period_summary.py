from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sport_sync_bridge.activity_library import LocalActivityLibrary
from sport_sync_bridge.cli import main
from sport_sync_bridge.formats import TrackPoint
from sport_sync_bridge.period_summary import (
    _activity_power_samples,
    calculate_period_summary,
    format_period_summary,
)
from sport_sync_bridge.state import StateDB
from tests.activity_fixtures import create_gpx


class PeriodSummaryTests(unittest.TestCase):
    def test_period_totals_week_slices_and_recovered_highlights(self) -> None:
        rows = [
            self._row("a" * 64, "running", "2026-01-04T08:00:00+00:00", "fit", 10000, 3600, 150, tss=100),
            self._row("b" * 64, "running", "2026-01-05T08:00:00+00:00", "fit", 5000, 1200, 80, tss=80),
            self._row("c" * 64, "cycling", "2026-01-06T08:00:00+00:00", "gpx", 20000, 3600, 100, tss=50),
        ]
        result = calculate_period_summary(
            rows,
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 7),
        )

        self.assertEqual(result["activity_count"], 3)
        self.assertEqual(result["fit_file_count"], 2)
        self.assertIsNone(result["avg_norm_power_w"])
        self.assertEqual(result["avg_norm_power_activity_count"], 0)
        self.assertEqual(result["total_distance_m"], 35000)
        self.assertEqual(result["total_duration_s"], 8400)
        self.assertEqual(result["total_tss"], 230)
        self.assertEqual([item["week_start"] for item in result["weekly_slices"]], ["2025-12-29", "2026-01-05"])
        self.assertEqual([item["activity_count"] for item in result["weekly_slices"]], [1, 2])
        self.assertEqual(
            [item["highlight_type"] for item in result["key_activities"]],
            ["distance", "tss", "pace", "duration", "speed"],
        )
        self.assertEqual(result["key_activities"][0]["title"], "最长距离")
        self.assertIn("最高 TSS", format_period_summary(result, "txt"))

    def test_period_averages_positive_normalized_power_per_activity(self) -> None:
        rows = [
            self._row(
                "a" * 64,
                "cycling",
                "2026-01-01T08:00:00Z",
                "fit",
                20000,
                3600,
                150,
                normalized_power=250,
            ),
            self._row(
                "b" * 64,
                "cycling",
                "2026-01-02T08:00:00Z",
                "fit",
                20000,
                3600,
                150,
                normalized_power=300,
            ),
            self._row(
                "c" * 64,
                "cycling",
                "2026-01-03T08:00:00Z",
                "fit",
                20000,
                3600,
                150,
                normalized_power=0,
            ),
            self._row(
                "d" * 64,
                "cycling",
                "2026-01-04T08:00:00Z",
                "fit",
                20000,
                3600,
                150,
            ),
        ]

        result = calculate_period_summary(
            rows,
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 7),
        )

        self.assertEqual(result["avg_norm_power_w"], 275)
        self.assertEqual(result["avg_norm_power_activity_count"], 2)
        self.assertIn("平均 NP：275.0 W（2 次有效活动）", format_period_summary(result, "txt"))

    def test_hr_tss_fallback_is_optional_and_fit_tss_takes_precedence(self) -> None:
        rows = [
            self._row("a" * 64, "running", "2026-01-01T08:00:00Z", "fit", 5000, 3600, 150),
            self._row("b" * 64, "running", "2026-01-02T08:00:00Z", "fit", 5000, 3600, 150, tss=90),
        ]
        unscored = calculate_period_summary(rows, date_from=date(2026, 1, 1), date_to=date(2026, 1, 2))
        self.assertEqual(unscored["total_tss"], 90)
        self.assertEqual(unscored["unscored_tss_activity_count"], 1)

        scored = calculate_period_summary(
            rows,
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 2),
            resting_hr=60,
            threshold_hr=180,
        )
        self.assertEqual(scored["total_tss"], 146.25)
        self.assertEqual(scored["scored_tss_activity_count"], 2)

    def test_period_aggregates_recorded_time_in_zone_seconds(self) -> None:
        rows = [
            self._row(
                "a" * 64,
                "running",
                "2026-01-01T08:00:00Z",
                "fit",
                5000,
                1500,
                150,
                zones=[
                    {
                        "heart_rate_zones": [{"zone": 1, "seconds": 600}, {"zone": 2, "seconds": 300}],
                        "speed_zones": [{"zone": 2, "seconds": 500}],
                        "cadence_zones": [{"zone": 3, "seconds": 800}],
                        "power_zones": [{"zone": 1, "seconds": 450}],
                    },
                    {"heart_rate_zones": [{"zone": 1, "seconds": 60}]},
                ],
            ),
            self._row(
                "b" * 64,
                "running",
                "2026-01-03T08:00:00Z",
                "fit",
                5000,
                1500,
                150,
                zones=[{"heart_rate_zones": [{"zone": 1, "seconds": 120}]}],
            ),
            self._row(
                "c" * 64,
                "running",
                "2026-02-01T08:00:00Z",
                "fit",
                5000,
                1500,
                150,
                zones=[{"heart_rate_zones": [{"zone": 1, "seconds": 1000}]}],
            ),
        ]

        result = calculate_period_summary(
            rows,
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 31),
        )

        self.assertEqual(result["recorded_zone_time_s"]["heart_rate"], {1: 780.0, 2: 300.0})
        self.assertEqual(result["recorded_zone_time_s"]["speed"], {2: 500.0})
        self.assertEqual(result["recorded_zone_time_s"]["cadence"], {3: 800.0})
        self.assertEqual(result["recorded_zone_time_s"]["power"], {1: 450.0})

    def test_period_merges_sampled_power_curves_from_activity_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = create_gpx(root / "first.gpx")
            second = create_gpx(root / "second.gpx")
            second.write_text(
                second.read_text(encoding="utf-8").replace(
                    "<ssb:power>200</ssb:power>", "<ssb:power>300</ssb:power>"
                ).replace("<ssb:power>201</ssb:power>", "<ssb:power>400</ssb:power>"),
                encoding="utf-8",
            )
            rows = [
                self._row(
                    "a" * 64,
                    "cycling",
                    "2026-01-02T03:04:00Z",
                    "gpx",
                    100,
                    60,
                    150,
                    file_path=first,
                ),
                self._row(
                    "b" * 64,
                    "cycling",
                    "2026-01-03T03:04:00Z",
                    "gpx",
                    100,
                    60,
                    150,
                    file_path=second,
                ),
            ]

            result = calculate_period_summary(
                rows,
                date_from=date(2026, 1, 1),
                date_to=date(2026, 1, 7),
            )

        self.assertEqual(result["power_curve_w"], {60: 350})
        self.assertEqual(result["power_curve_sample_activity_count"], 2)
        self.assertEqual(result["power_curve_unavailable_activity_count"], 0)
        self.assertIn("60 秒 350 W", format_period_summary(result, "txt"))

    def test_sampled_power_curve_uses_maximum_sliding_window_mean(self) -> None:
        start = datetime.fromisoformat("2026-01-01T00:00:00+00:00")
        points = [
            TrackPoint(timestamp=start + timedelta(seconds=second), power_w=power)
            for second, power in ((0, 100), (5, 100), (10, 500), (15, 600))
        ]

        result = _activity_power_samples(points)

        self.assertEqual(result, {10: 400})

    def test_period_reports_activities_without_readable_power_source(self) -> None:
        rows = [self._row("a" * 64, "running", "2026-01-01T08:00:00Z", "fit", 5000, 1500, 150)]

        result = calculate_period_summary(
            rows,
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 1),
        )

        self.assertEqual(result["power_curve_w"], {})
        self.assertEqual(result["power_curve_sample_activity_count"], 0)
        self.assertEqual(result["power_curve_unavailable_activity_count"], 1)

    def test_running_vdot_trend_and_sport_filter(self) -> None:
        rows = [
            self._row("a" * 64, "running", "2026-01-01T08:00:00Z", "fit", 5000, 1500, 80),
            self._row("b" * 64, "cycling", "2026-01-01T09:00:00Z", "fit", 20000, 3600, 100),
            self._row("c" * 64, "running", "2026-01-03T08:00:00Z", "fit", 5000, 1400, 90),
        ]
        result = calculate_period_summary(
            rows,
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 3),
            sport="run",
        )
        self.assertEqual(result["activity_count"], 2)
        self.assertEqual(result["sport_filter"], "running")
        self.assertLess(result["vdot_start"], result["vdot_end"])
        self.assertEqual(result["vdot_max"], result["vdot_end"])

    def test_invalid_period_and_heart_rate_settings_fail(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive integer"):
            calculate_period_summary([], days=0)
        with self.assertRaisesRegex(ValueError, "must not be after"):
            calculate_period_summary([], date_from=date(2026, 1, 2), date_to=date(2026, 1, 1))
        with self.assertRaisesRegex(ValueError, "must exceed"):
            calculate_period_summary([], resting_hr=180, threshold_hr=180)

    def test_library_period_cli_uses_local_database(self) -> None:
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
                        status = main(
                            [
                                "library",
                                "period",
                                "--from",
                                "2026-01-01",
                                "--to",
                                "2026-01-03",
                                "--format",
                                "json",
                            ]
                        )

            self.assertEqual(status, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["activity_count"], 1)
            self.assertAlmostEqual(payload["vdot_max"], 38.3, places=1)

    def test_library_period_cli_rejects_invalid_dates(self) -> None:
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            with self.assertRaises(SystemExit) as raised:
                main(["library", "period", "--from", "not-a-date"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("invalid date/time", error.getvalue())

    @staticmethod
    def _row(
        fingerprint: str,
        sport: str,
        start_time: str,
        file_format: str,
        distance: float,
        duration: float,
        average_hr: float,
        *,
        tss: float | None = None,
        normalized_power: float | None = None,
        zones: list[dict[str, object]] | None = None,
        file_path: Path | None = None,
    ) -> dict[str, object]:
        summary: dict[str, object] = {
            "distance_m": distance,
            "timer_time_s": duration,
            "average_heart_rate_bpm": average_hr,
            "average_speed_mps": distance / duration,
        }
        if tss is not None:
            summary["training_stress_score"] = tss
        if normalized_power is not None:
            summary["normalized_power_w"] = normalized_power
        if zones is not None:
            summary["time_in_zone_messages"] = zones
        return {
            "fingerprint": fingerprint,
            "name": "Test activity",
            "sport_type": sport,
            "start_time": start_time,
            "file_format": file_format,
            "file_path": str(file_path) if file_path else None,
            "summary_json": json.dumps(summary),
        }


if __name__ == "__main__":
    unittest.main()
