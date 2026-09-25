from __future__ import annotations

import contextlib
import io
import json
import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sport_sync_bridge.cli import main
from sport_sync_bridge.running_dynamics import (
    RunningDynamicsAnalyzer,
    _PhonePositionDetector,
    analyze_running_dynamics_file,
    format_running_dynamics,
)


class RunningDynamicsTests(unittest.TestCase):
    def test_initial_stride_uses_apk_height_default_formula(self) -> None:
        summary = RunningDynamicsAnalyzer(height_cm=175).summary()

        self.assertAlmostEqual(summary.stride_length_m, 175 * 0.413 / 100)
        self.assertEqual(summary.cadence, 0)
        self.assertEqual(summary.total_steps, 0)

    def test_gps_segments_calibrate_stride_after_distance_and_step_thresholds(self) -> None:
        analyzer = RunningDynamicsAnalyzer(height_cm=175)
        initial_stride = analyzer.summary().stride_length_m

        analyzer.add_gps_segment(30, 40, 5, 3)
        self.assertEqual(analyzer.summary().stride_length_m, initial_stride)
        analyzer.add_gps_segment(30, 40, 5, 3)

        self.assertAlmostEqual(analyzer.summary().stride_length_m, initial_stride * 0.8 + 0.75 * 0.2)

    def test_gps_segments_skip_bad_accuracy_and_speed(self) -> None:
        analyzer = RunningDynamicsAnalyzer()
        initial_stride = analyzer.summary().stride_length_m

        analyzer.add_gps_segment(60, 80, 10.1, 3)
        analyzer.add_gps_segment(60, 80, 5, 15.1)

        self.assertEqual(analyzer.summary().stride_length_m, initial_stride)

    def test_cadence_uses_recent_step_window_and_resets_after_timeout(self) -> None:
        analyzer = RunningDynamicsAnalyzer()
        analyzer._on_step(1000, 1)
        analyzer._on_step(1500, 2)
        analyzer._on_step(2000, 3)

        self.assertEqual(analyzer.summary().cadence, 60)
        self.assertEqual(analyzer.summary().total_steps, 3)

        analyzer._check_cadence_timeout(4001)
        self.assertEqual(analyzer.summary().cadence, 0)

    def test_phone_position_filter_uses_apk_one_hertz_cutoff(self) -> None:
        detector = _PhonePositionDetector()

        for _ in range(200):
            detector.process_sample(1.0, 0.0, 0.0)

        self.assertGreater(detector._windows[0].mean, 0.95)

    def test_accelerometer_pipeline_detects_steps_and_emits_running_dynamics(self) -> None:
        analyzer = RunningDynamicsAnalyzer()

        for index in range(400):
            timestamp_ms = 1000 + index * 20
            vertical_acceleration = 9.8 + 4 * math.sin(2 * math.pi * 2 * index / 50)
            analyzer.add_accelerometer_sample(timestamp_ms, 0, 0, vertical_acceleration)

        summary = analyzer.summary()
        self.assertEqual(summary.accelerometer_samples, 400)
        self.assertEqual(summary.total_steps, 15)
        self.assertAlmostEqual(summary.cadence, 55.55555555555556)
        self.assertGreater(summary.vertical_oscillation_cm, 2)
        self.assertLess(summary.vertical_oscillation_cm, 25)
        self.assertAlmostEqual(
            summary.vertical_ratio_percent,
            summary.vertical_oscillation_cm / summary.stride_length_m,
        )

    def test_rejects_invalid_event_data_and_out_of_order_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            path.write_text(
                '\n'.join(
                    (
                        json.dumps({"type": "accelerometer", "timestamp_ms": 200, "x_mps2": 0, "y_mps2": 0, "z_mps2": 9.8}),
                        json.dumps({"type": "accelerometer", "timestamp_ms": 100, "x_mps2": 0, "y_mps2": 0, "z_mps2": 9.8}),
                    )
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "out of order"):
                analyze_running_dynamics_file(path)

    def test_file_reader_reports_json_line_and_requires_accelerometer_samples(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            path.write_text("{bad json\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "line 1"):
                analyze_running_dynamics_file(path)

            path.write_text(
                json.dumps(
                    {
                        "type": "gps_segment",
                        "timestamp_ms": 100,
                        "distance_m": 60,
                        "step_delta": 80,
                        "horizontal_accuracy_m": 5,
                        "speed_mps": 3,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "no accelerometer"):
                analyze_running_dynamics_file(path)

    def test_formats_json_and_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "type": "accelerometer",
                        "timestamp_ms": 1000,
                        "x_mps2": 0,
                        "y_mps2": 0,
                        "z_mps2": 9.8,
                    }
                ),
                encoding="utf-8",
            )
            summary = analyze_running_dynamics_file(path)

        json_result = json.loads(format_running_dynamics(summary, "json"))
        text_result = format_running_dynamics(summary, "txt")
        self.assertEqual(json_result["accelerometer_samples"], 1)
        self.assertEqual(json_result["gps_segments"], 0)
        self.assertIn("步幅：", text_result)

    def test_cli_runs_analysis_without_account_or_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / ".data"
            data_dir.mkdir()
            input_path = root / "run-sensors.jsonl"
            input_path.write_text(
                json.dumps(
                    {
                        "type": "accelerometer",
                        "timestamp_ms": 1000,
                        "x_mps2": 0,
                        "y_mps2": 0,
                        "z_mps2": 9.8,
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
            output = io.StringIO()
            with (
                patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
                patch("sport_sync_bridge.cli.configure_logging"),
                patch("sport_sync_bridge.cli.StateDB", side_effect=AssertionError("database not needed")),
                contextlib.redirect_stdout(output),
            ):
                status = main(["library", "running-dynamics", str(input_path)])

            self.assertEqual(status, 0)
            self.assertEqual(json.loads(output.getvalue())["accelerometer_samples"], 1)


if __name__ == "__main__":
    unittest.main()
