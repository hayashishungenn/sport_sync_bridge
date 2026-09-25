from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sport_sync_bridge.activity_library import LocalActivityLibrary
from sport_sync_bridge.cli import main
from sport_sync_bridge.formats import TrackPoint
from sport_sync_bridge.period_summary import (
    _activity_power_samples,
    _detect_intensity_model,
    calculate_period_summary,
    format_period_summary,
)
from sport_sync_bridge.state import StateDB
from tests.activity_fixtures import create_fit, create_gpx


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
        self.assertIsNone(result["avg_cadence"])
        self.assertEqual(result["avg_cadence_activity_count"], 0)
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

    def test_period_pr_changes_use_prior_history_and_include_three_percent_boundary(self) -> None:
        rows = [
            self._row("a" * 64, "running", "2025-12-31T08:00:00Z", "fit", 5000, 1700, 150),
            self._row("b" * 64, "running", "2026-01-01T08:00:00Z", "fit", 5150, 1650, 150),
            self._row("c" * 64, "running", "2026-01-02T08:00:00Z", "fit", 4850, 1800, 150),
            self._row("d" * 64, "running", "2026-01-03T08:00:00Z", "fit", 5300, 900, 150),
        ]

        result = calculate_period_summary(
            rows,
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 31),
        )

        self.assertEqual(
            result["pr_changes"],
            [
                {
                    "prType": "5K",
                    "oldValue": 1700.0,
                    "newValue": 1650.0,
                    "achievedAt": "2026-01-01T08:00:00+00:00",
                    "activityId": "b" * 64,
                    "improvementPct": (1700 - 1650) / 1700 * 100,
                }
            ],
        )
        self.assertIn("5K：27:30（提升 2.9%", format_period_summary(result, "txt"))

    def test_period_pr_changes_cover_recovered_running_cycling_and_swimming_targets(self) -> None:
        targets = (
            ("running", "5K", 5000),
            ("running", "10K", 10000),
            ("running", "halfMarathon", 21097.5),
            ("running", "marathon", 42195),
            ("cycling", "40K", 40000),
            ("swimming", "100m", 100),
            ("swimming", "400m", 400),
            ("swimming", "1500m", 1500),
        )
        rows: list[dict[str, object]] = []
        for index, (sport, _, distance) in enumerate(targets):
            previous = 2400 + index * 30
            rows.append(
                self._row(
                    f"{index * 2 + 1:064x}",
                    sport,
                    "2025-12-31T08:00:00Z",
                    "fit",
                    distance,
                    previous,
                    150,
                )
            )
            rows.append(
                self._row(
                    f"{index * 2 + 2:064x}",
                    sport,
                    "2026-01-01T08:00:00Z",
                    "fit",
                    distance,
                    previous - 10,
                    150,
                )
            )

        result = calculate_period_summary(
            rows,
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 31),
        )

        self.assertCountEqual([item["prType"] for item in result["pr_changes"]], [item[1] for item in targets])

    def test_period_first_personal_record_has_no_invented_baseline(self) -> None:
        result = calculate_period_summary(
            [self._row("a" * 64, "running", "2026-01-01T08:00:00Z", "fit", 5000, 1500, 150)],
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 31),
        )

        self.assertEqual(result["pr_changes"][0]["oldValue"], None)
        self.assertEqual(result["pr_changes"][0]["improvementPct"], None)
        self.assertIn("首次本地记录", format_period_summary(result, "txt"))

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

    def test_period_averages_cadence_for_running_and_cycling_activities(self) -> None:
        rows = [
            self._row("a" * 64, "running", "2026-01-01T08:00:00Z", "fit", 5000, 1500, 150, average_cadence=180),
            self._row("b" * 64, "cycling", "2026-01-02T08:00:00Z", "fit", 20000, 3600, 150, average_cadence=90),
            self._row(
                "c" * 64,
                "trail_running",
                "2026-01-03T08:00:00Z",
                "fit",
                5000,
                1500,
                150,
                average_cadence=170,
            ),
            self._row("d" * 64, "cycling", "2026-01-04T08:00:00Z", "fit", 20000, 3600, 150, average_cadence=0),
            self._row("e" * 64, "swimming", "2026-01-05T08:00:00Z", "fit", 1000, 1200, 140, average_cadence=75),
            self._row("f" * 64, "walking", "2026-01-06T08:00:00Z", "fit", 3000, 1800, 100, average_cadence=110),
            self._row("g" * 64, "running", "2026-01-07T08:00:00Z", "fit", 5000, 1500, 150),
        ]

        result = calculate_period_summary(
            rows,
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 7),
        )

        self.assertEqual(result["avg_cadence"], 110)
        self.assertEqual(result["avg_cadence_activity_count"], 4)
        self.assertIn("平均踏频：110.0（4 次有效活动）", format_period_summary(result, "txt"))

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

    def test_period_reconstructs_training_type_distribution(self) -> None:
        def heart_rate_zones(seconds_by_zone: tuple[float, ...]) -> list[dict[str, object]]:
            return [
                {
                    "heart_rate_zones": [
                        {"zone": zone, "seconds": seconds_by_zone[zone - 1]}
                        for zone in range(1, 6)
                    ]
                }
            ]

        rows = [
            self._row(
                "a" * 64,
                "running",
                "2026-01-01T08:00:00Z",
                "fit",
                5000,
                100,
                150,
                zones=heart_rate_zones((10, 200, 100, 0, 0)),
            ),
            self._row(
                "b" * 64,
                "cycling",
                "2026-01-02T08:00:00Z",
                "fit",
                20000,
                200,
                150,
                zones=heart_rate_zones((10, 100, 200, 0, 0)),
            ),
            self._row(
                "c" * 64,
                "cycling",
                "2026-01-03T08:00:00Z",
                "fit",
                20000,
                300,
                150,
                zones=heart_rate_zones((10, 100, 200, 300, 0)),
            ),
            self._row(
                "d" * 64,
                "cycling",
                "2026-01-04T08:00:00Z",
                "fit",
                20000,
                400,
                150,
                zones=heart_rate_zones((10, 100, 200, 300, 400)),
            ),
            self._row(
                "e" * 64,
                "walking",
                "2026-01-05T08:00:00Z",
                "fit",
                10200,
                500,
                100,
            ),
            self._row(
                "f" * 64,
                "running",
                "2026-01-06T08:00:00Z",
                "fit",
                5151,
                600,
                150,
            ),
        ]

        result = calculate_period_summary(
            rows,
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 31),
        )

        self.assertEqual(
            result["training_type_distribution"],
            {
                "easy": 100.0,
                "tempo": 200.0,
                "threshold": 300.0,
                "interval": 400.0,
                "race": 500.0,
                "mixed": 600.0,
            },
        )
        self.assertEqual(result["intensity_model"], "mixed")
        report = format_period_summary(result, "txt")
        self.assertIn("训练类型时长：easy 100秒", report)
        self.assertIn("强度模型：mixed", report)

    def test_training_type_race_distance_tolerance_is_three_percent(self) -> None:
        rows = [
            self._row("a" * 64, "running", "2026-01-01T08:00:00Z", "fit", 5149, 100, 150),
            self._row("b" * 64, "running", "2026-01-02T08:00:00Z", "fit", 5150, 200, 150),
            self._row("c" * 64, "walking", "2026-01-03T08:00:00Z", "fit", 9701, 300, 100),
            self._row("d" * 64, "walking", "2026-01-04T08:00:00Z", "fit", 9700, 400, 100),
            self._row("e" * 64, "hiking", "2026-01-05T08:00:00Z", "fit", 10_310, 500, 100),
        ]

        result = calculate_period_summary(
            rows,
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 31),
        )

        self.assertEqual(result["training_type_distribution"], {"race": 400.0, "mixed": 1100.0})

    def test_intensity_model_uses_aot_duration_ratio_thresholds(self) -> None:
        self.assertEqual(
            _detect_intensity_model({"easy": 61, "interval": 13, "mixed": 26}),
            "pyramidal",
        )
        self.assertEqual(
            _detect_intensity_model({"easy": 61, "interval": 14, "mixed": 25}),
            "mixed",
        )
        self.assertEqual(
            _detect_intensity_model({"easy": 60, "interval": 15, "mixed": 25}),
            "polarized",
        )
        self.assertEqual(
            _detect_intensity_model({"easy": 40, "interval": 20, "mixed": 40}),
            "mixed",
        )
        self.assertIsNone(_detect_intensity_model({}))

    def test_period_recalculates_heart_rate_and_speed_zones_from_fit_samples(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            activity_path = create_fit(
                Path(temporary) / "zones.fit",
                track_point_interval_seconds=1,
                heart_rate_values=(90, 110),
                speed_values=(5.0, 7.0),
                time_in_zone={
                    "hr_zone_high_boundary": [100, 150],
                    "speed_zone_high_boundary": [6, 8],
                },
            )
            zone_messages = [
                {
                    "heart_rate_zones": [
                        {"zone": 1, "high_boundary": 100},
                        {"zone": 2, "high_boundary": 150},
                    ],
                    "speed_zones": [
                        {"zone": 1, "high_boundary": 6},
                        {"zone": 2, "high_boundary": 8},
                    ],
                }
            ]
            result = calculate_period_summary(
                [
                    self._row(
                        "d" * 64,
                        "running",
                        "2026-01-02T03:04:00Z",
                        "fit",
                        100,
                        1,
                        100,
                        zones=zone_messages,
                        file_path=activity_path,
                    )
                ],
                date_from=date(2026, 1, 2),
                date_to=date(2026, 1, 2),
            )

        self.assertEqual(result["sampled_zone_time_s"]["heart_rate"], {1: 0.5, 2: 0.5})
        self.assertEqual(result["sampled_zone_time_s"]["speed"], {1: 0.5, 2: 0.5})
        self.assertEqual(result["sampled_zone_activity_count"], {"heart_rate": 1, "speed": 1})
        self.assertEqual(result["recorded_zone_time_s"]["heart_rate"], {})
        report = format_period_summary(result, "txt")
        self.assertIn("轨迹重算心率分区：Z1 0.5秒，Z2 0.5秒（1 次活动）", report)
        self.assertIn("轨迹重算速度分区：Z1 0.5秒，Z2 0.5秒（1 次活动）", report)

    def test_period_skips_ambiguous_thresholds_and_long_sample_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            activity_path = create_fit(
                Path(temporary) / "gap.fit",
                track_point_interval_seconds=31,
                time_in_zone={
                    "hr_zone_high_boundary": [100, 150],
                    "speed_zone_high_boundary": [6, 8],
                },
            )
            conflicting_zones = [
                {
                    "heart_rate_zones": [
                        {"zone": 1, "high_boundary": 100},
                        {"zone": 2, "high_boundary": 150},
                    ],
                    "speed_zones": [
                        {"zone": 1, "high_boundary": 6},
                        {"zone": 2, "high_boundary": 8},
                    ],
                },
                {
                    "heart_rate_zones": [
                        {"zone": 1, "high_boundary": 100},
                        {"zone": 2, "high_boundary": 160},
                    ],
                    "speed_zones": [
                        {"zone": 1, "high_boundary": 6},
                        {"zone": 2, "high_boundary": 8},
                    ],
                },
            ]
            result = calculate_period_summary(
                [
                    self._row(
                        "e" * 64,
                        "running",
                        "2026-01-02T03:04:00Z",
                        "fit",
                        100,
                        31,
                        100,
                        zones=conflicting_zones,
                        file_path=activity_path,
                    )
                ],
                date_from=date(2026, 1, 2),
                date_to=date(2026, 1, 2),
            )

        self.assertEqual(result["sampled_zone_time_s"], {"heart_rate": {}, "speed": {}})
        self.assertEqual(result["sampled_zone_activity_count"], {"heart_rate": 0, "speed": 0})

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

    def test_ftp_trend_uses_np_if_and_adaptive_window_maxima(self) -> None:
        rows = [
            self._row(
                "a" * 64, "cycling", "2026-01-01T08:00:00Z", "fit", 20000, 3600, 150,
                normalized_power=220, intensity_factor=1.1,
            ),
            self._row(
                "b" * 64, "cycling", "2026-01-21T08:00:00Z", "fit", 20000, 3600, 150,
                normalized_power=330, intensity_factor=1.1,
            ),
            self._row(
                "c" * 64, "cycling", "2026-04-10T08:00:00Z", "fit", 20000, 3600, 150,
                normalized_power=286, intensity_factor=1.1,
            ),
            self._row(
                "d" * 64, "cycling", "2026-04-11T08:00:00Z", "fit", 20000, 3600, 150,
                normalized_power=308, intensity_factor=1.1,
            ),
        ]

        result = calculate_period_summary(
            rows,
            date_from=date(2026, 1, 1),
            date_to=date(2026, 4, 11),
        )

        self.assertEqual(
            result["ftp_trend"],
            {
                "sample_count": 4,
                "window_days": 20,
                "first_window_best_w": 300.0,
                "last_window_best_w": 280.0,
                "period_best_w": 300.0,
                "change_w": -20.0,
            },
        )
        estimates = {
            item["activity_id"]: item["ftp_estimate_from_np_if_w"]
            for item in result["activity_log"]
        }
        self.assertAlmostEqual(estimates["a" * 64], 200.0)
        self.assertAlmostEqual(estimates["d" * 64], 280.0)
        self.assertIn("FTP趋势：300 W → 280 W（20 天窗口", format_period_summary(result, "txt"))

    def test_ftp_trend_filters_nonpositive_or_missing_intensity_factor(self) -> None:
        rows = [
            self._row(
                "a" * 64, "cycling", "2026-01-02T08:00:00Z", "fit", 20000, 3600, 150,
                normalized_power=275, intensity_factor=1.1,
            ),
            self._row(
                "b" * 64, "cycling", "2026-01-01T08:00:00Z", "fit", 20000, 3600, 150,
                normalized_power=220, intensity_factor=1.1,
            ),
            self._row(
                "c" * 64, "cycling", "2026-01-03T08:00:00Z", "fit", 20000, 3600, 150,
                normalized_power=300, intensity_factor=0,
            ),
            self._row(
                "d" * 64, "cycling", "2026-01-04T08:00:00Z", "fit", 20000, 3600, 150,
                normalized_power=300,
            ),
            self._row(
                "e" * 64, "cycling", "2026-01-05T08:00:00Z", "fit", 20000, 3600, 150,
                intensity_factor=1.1,
            ),
        ]

        result = calculate_period_summary(
            rows,
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 5),
        )

        self.assertEqual(result["ftp_trend"]["sample_count"], 2)
        self.assertIsNone(result["ftp_trend"]["window_days"])
        self.assertAlmostEqual(result["ftp_trend"]["single_value_w"], 200.0)
        self.assertIn("2 个有效样本，单值结果", format_period_summary(result, "txt"))

        no_estimates = calculate_period_summary(
            [self._row("f" * 64, "cycling", "2026-01-01T08:00:00Z", "fit", 20000, 3600, 150)],
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 1),
        )
        self.assertIsNone(no_estimates["ftp_trend"])

    def test_ftp_trend_uses_full_span_for_periods_under_fourteen_days(self) -> None:
        rows = [
            self._row(
                "a" * 64, "cycling", "2026-01-01T08:00:00Z", "fit", 20000, 3600, 150,
                normalized_power=220, intensity_factor=1.1,
            ),
            self._row(
                "b" * 64, "cycling", "2026-01-06T08:00:00Z", "fit", 20000, 3600, 150,
                normalized_power=330, intensity_factor=1.1,
            ),
            self._row(
                "c" * 64, "cycling", "2026-01-11T08:00:00Z", "fit", 20000, 3600, 150,
                normalized_power=286, intensity_factor=1.1,
            ),
        ]

        result = calculate_period_summary(
            rows,
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 11),
        )

        self.assertEqual(result["ftp_trend"]["window_days"], 10)

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
            self.assertEqual(payload["training_type_distribution"], {"race": 1500.0})
            self.assertEqual(payload["intensity_model"], "mixed")
            self.assertEqual(payload["sampled_zone_time_s"], {"heart_rate": {}, "speed": {}})
            self.assertEqual(payload["sampled_zone_activity_count"], {"heart_rate": 0, "speed": 0})
            self.assertEqual(payload["pr_changes"][0]["prType"], "5K")
            self.assertEqual(payload["pr_changes"][0]["newValue"], 1500.0)

    def test_library_period_cli_uses_np_if_from_imported_fit_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / ".data"
            fit_paths = [
                create_fit(
                    root / f"ride-{index}.fit",
                    start=datetime(2026, 1, day, 8, tzinfo=timezone.utc),
                    normalized_power=power,
                    intensity_factor=1.1,
                )
                for index, (day, power) in enumerate(
                    ((1, 220), (6, 330), (11, 286)),
                    start=1,
                )
            ]
            config = SimpleNamespace(
                data_dir=data_dir,
                db_path=data_dir / "sync_state.db",
                log_level="ERROR",
                log_path=data_dir / "sync.log",
            )
            state = StateDB(config.db_path)
            LocalActivityLibrary(state, data_dir).import_paths(fit_paths)
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
                                "2026-01-11",
                                "--format",
                                "json",
                            ]
                        )

            self.assertEqual(status, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["ftp_trend"]["window_days"], 10)
            self.assertEqual(payload["ftp_trend"]["first_window_best_w"], 300.0)
            self.assertEqual(payload["activity_log"][0]["ftp_estimate_from_np_if_w"], 260.0)

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
        intensity_factor: float | None = None,
        average_cadence: float | None = None,
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
        if intensity_factor is not None:
            summary["intensity_factor"] = intensity_factor
        if average_cadence is not None:
            summary["average_cadence_rpm"] = average_cadence
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
