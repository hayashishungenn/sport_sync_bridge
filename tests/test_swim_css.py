from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sport_sync_bridge.cli import main
from sport_sync_bridge.swim_css import (
    calculate_swim_css,
    calculate_swim_rest_seconds,
    format_swim_css,
    format_swim_rest,
    parse_swim_time,
)


class SwimCssTests(unittest.TestCase):
    def test_parses_seconds_and_clock_formats(self) -> None:
        self.assertEqual(parse_swim_time("160", "time"), 160)
        self.assertEqual(parse_swim_time("2:40.5", "time"), 160.5)
        self.assertEqual(parse_swim_time("1:02:40", "time"), 3760)

    def test_rejects_malformed_or_out_of_range_components(self) -> None:
        for value in ("", "2:60", "1:60:00", "-1:20", "1:2:3:4", "nan"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_swim_time(value, "time")

    def test_calculates_css_pace_and_pool_splits(self) -> None:
        result = calculate_swim_css(160, 340, pool_length_m=25)

        self.assertEqual(result["css_seconds_per_100m"], 90)
        self.assertEqual(result["css_pace_per_100m"], "1:30")
        self.assertAlmostEqual(result["css_speed_m_per_s"], 100 / 90)
        self.assertEqual(result["target_pool_length_s"], 22.5)
        self.assertEqual(result["target_splits_s"]["50m"], 45)

    def test_validates_times_and_supported_pool_lengths(self) -> None:
        with self.assertRaisesRegex(ValueError, "longer than"):
            calculate_swim_css(200, 200)
        with self.assertRaisesRegex(ValueError, "positive finite"):
            calculate_swim_css(float("nan"), 300)
        with self.assertRaisesRegex(ValueError, "25 or 50"):
            calculate_swim_css(160, 340, pool_length_m=33)

    def test_formats_json_and_text_reports(self) -> None:
        result = calculate_swim_css(160, 340, pool_length_m=50)

        self.assertEqual(json.loads(format_swim_css(result, "json"))["css_pace_per_100m"], "1:30")
        self.assertIn("50 米泳池目标：0:45", format_swim_css(result, "txt"))

    def test_calculates_garsync_default_swim_rests(self) -> None:
        expected = {25: 12, 50: 20, 100: 25, 200: 35, 400: 50, 800: 60, 1600: 120}
        for distance_m, rest_seconds in expected.items():
            with self.subTest(distance_m=distance_m):
                self.assertEqual(calculate_swim_rest_seconds(distance_m), rest_seconds)
        self.assertEqual(calculate_swim_rest_seconds(150), 11)

    def test_rejects_invalid_swim_rest_distance_and_format(self) -> None:
        for distance_m in (0, -25, True, 25.0):
            with self.subTest(distance_m=distance_m), self.assertRaises(ValueError):
                calculate_swim_rest_seconds(distance_m)
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            format_swim_rest(100, 25, "csv")

    def test_formats_swim_rest_reports(self) -> None:
        self.assertEqual(
            json.loads(format_swim_rest(100, 25, "json")),
            {"distance_m": 100, "rest_seconds": 25},
        )
        self.assertIn("默认休息时间：25 秒", format_swim_rest(100, 25, "txt"))

    def test_cli_runs_css_calculation_without_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / ".data"
            data_dir.mkdir()
            config = SimpleNamespace(
                data_dir=data_dir,
                db_path=data_dir / "sync_state.db",
                log_level="ERROR",
                log_path=data_dir / "sync.log",
            )
            output = io.StringIO()
            with (
                patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
                patch("sport_sync_bridge.cli.configure_logging"),
                contextlib.redirect_stdout(output),
            ):
                status = main(
                    [
                        "library",
                        "swim-css",
                        "--time-200m",
                        "2:40",
                        "--time-400m",
                        "5:40",
                        "--pool-length",
                        "50",
                    ]
                )

            self.assertEqual(status, 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result["css_seconds_per_100m"], 90)
            self.assertEqual(result["pool_length_m"], 50)

    def test_cli_runs_swim_rest_without_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / ".data"
            data_dir.mkdir()
            config = SimpleNamespace(
                data_dir=data_dir,
                db_path=data_dir / "sync_state.db",
                log_level="ERROR",
                log_path=data_dir / "sync.log",
            )
            output = io.StringIO()
            with (
                patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
                patch("sport_sync_bridge.cli.configure_logging"),
                contextlib.redirect_stdout(output),
            ):
                status = main(["library", "swim-rest", "100"])

            self.assertEqual(status, 0)
            self.assertEqual(json.loads(output.getvalue()), {"distance_m": 100, "rest_seconds": 25})


if __name__ == "__main__":
    unittest.main()
