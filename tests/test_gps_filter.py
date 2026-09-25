from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fit_tool.fit_file import FitFile
from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.profile_type import FileType

from sport_sync_bridge.cli import main
from sport_sync_bridge.fit_tools import (
    normalize_fit_coordinates,
    repair_fit_track_continuity,
    smooth_fit_gps_track,
)
from sport_sync_bridge.gps_filter import GpsTrackPoint, smooth_gps_track
from tests.activity_fixtures import create_fit


class GpsKalmanFilterTests(unittest.TestCase):
    def test_zero_process_noise_places_second_point_at_weighted_midpoint(self) -> None:
        points = [
            GpsTrackPoint(0, 0, 0, 10),
            GpsTrackPoint(0, 0.001, 1000, 10),
        ]

        smoothed = smooth_gps_track(points, q_metres_per_second=0, adaptive_q=False)

        self.assertEqual(smoothed[0], (0, 0))
        self.assertAlmostEqual(smoothed[1][0], 0, places=9)
        self.assertAlmostEqual(smoothed[1][1], 0.0005, places=7)

    def test_adaptive_turn_q_makes_filter_more_responsive(self) -> None:
        points = [
            GpsTrackPoint(0, 0, 0, 10, 6, None),
            GpsTrackPoint(0, 0.001, 1000, 10, 6, 0),
            GpsTrackPoint(0, 0.002, 2000, 10, 6, 90),
        ]

        adaptive = smooth_gps_track(points, adaptive_q=True)
        fixed = smooth_gps_track(points, adaptive_q=False)

        self.assertGreater(adaptive[-1][1], fixed[-1][1])

    def test_handles_longitude_wrap_and_rejects_invalid_q(self) -> None:
        points = [
            GpsTrackPoint(0, 179.999, 0, 5),
            GpsTrackPoint(0, -179.999, 1000, 5),
        ]

        smoothed = smooth_gps_track(points)

        self.assertLessEqual(abs(smoothed[-1][1]), 180)
        with self.assertRaisesRegex(ValueError, "Kalman Q"):
            smooth_gps_track(points, q_metres_per_second=-1)


class FitGpsSmoothingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_writes_smoothed_fit_preserves_other_fields_and_is_idempotent(self) -> None:
        source = self._create_fit(self.root / "source.fit")
        original = source.read_bytes()
        output = self.root / "smoothed.fit"

        smoothed_path, changed = smooth_fit_gps_track(
            source,
            output,
            q_metres_per_second=0,
            adaptive_q=False,
        )

        self.assertEqual(smoothed_path, output.resolve())
        self.assertGreater(changed, 0)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(self._record_field_values(output, "heart_rate"), [140, 141, 142])
        self.assertAlmostEqual(self._record_field_values(output, "position_long")[1], 0.0005, places=6)
        self.assertTrue(Path(f"{output}.gps-smoothing.json").is_file())

        repeated, repeated_count = smooth_fit_gps_track(output, self.root / "second.fit")

        self.assertEqual(repeated, output.resolve())
        self.assertEqual(repeated_count, 0)
        self.assertFalse((self.root / "second.fit").exists())

    def test_requires_fallback_accuracy_when_no_accuracy_is_stored(self) -> None:
        source = self._create_fit(self.root / "no-accuracy.fit", include_accuracy=False)

        with self.assertRaisesRegex(ValueError, "supply --accuracy-m"):
            smooth_fit_gps_track(source, self.root / "missing-fallback.fit")

        output, changed = smooth_fit_gps_track(
            source,
            self.root / "fallback.fit",
            accuracy_m=10,
            q_metres_per_second=0,
            adaptive_q=False,
        )

        self.assertGreater(changed, 0)
        self.assertTrue(output.is_file())

    def test_preserves_coordinate_and_continuity_markers(self) -> None:
        source = create_fit(self.root / "marked-source.fit")
        normalized, changed = normalize_fit_coordinates(
            source,
            self.root / "normalized.fit",
            "gcj02_to_wgs84",
        )
        self.assertGreater(changed, 0)
        repaired, removed = repair_fit_track_continuity(normalized, self.root / "repaired.fit")
        self.assertEqual(removed, 0)

        smoothed, _ = smooth_fit_gps_track(
            repaired,
            self.root / "marked-smoothed.fit",
            accuracy_m=5,
            q_metres_per_second=0,
            adaptive_q=False,
        )

        self.assertTrue(Path(f"{smoothed}.coord.json").is_file())
        self.assertTrue(Path(f"{smoothed}.fit-repair.json").is_file())
        normalized_again, coordinate_changes = normalize_fit_coordinates(
            smoothed,
            self.root / "second-normalization.fit",
            "gcj02_to_wgs84",
        )
        repaired_again, removed_again = repair_fit_track_continuity(
            smoothed,
            self.root / "second-repair.fit",
        )
        self.assertEqual((normalized_again, coordinate_changes), (smoothed, 0))
        self.assertEqual((repaired_again, removed_again), (smoothed, 0))

    def test_cli_runs_gps_smoothing_command(self) -> None:
        source = self._create_fit(self.root / "cli-source.fit")
        output = self.root / "cli-output.fit"
        config = SimpleNamespace(
            data_dir=self.root,
            log_level="INFO",
            log_path=self.root / "sync.log",
        )
        captured = io.StringIO()

        with (
            patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
            patch("sport_sync_bridge.cli.ensure_directory"),
            patch("sport_sync_bridge.cli.configure_logging"),
            redirect_stdout(captured),
        ):
            status = main(
                [
                    "library",
                    "smooth-gps",
                    str(source),
                    "--output",
                    str(output),
                    "--q",
                    "0",
                    "--no-adaptive-q",
                ]
            )

        self.assertEqual(status, 0)
        self.assertTrue(output.is_file())
        self.assertIn("records_smoothed=2", captured.getvalue())

    def _create_fit(self, path: Path, *, include_accuracy: bool = True) -> Path:
        builder = FitFileBuilder(auto_define=True)
        file_id = FileIdMessage()
        self._set_field(file_id, "type", FileType.ACTIVITY.value)
        self._set_field(file_id, "manufacturer", 999)
        self._set_field(file_id, "product", 123)
        builder.add(file_id)

        start = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
        for index in range(3):
            record = RecordMessage()
            self._set_field(record, "timestamp", round((start + timedelta(seconds=index)).timestamp() * 1000))
            self._set_field(record, "position_lat", 0.0)
            self._set_field(record, "position_long", index * 0.001)
            self._set_field(record, "heart_rate", 140 + index)
            self._set_field(record, "speed", 6.0)
            if include_accuracy:
                self._set_field(record, "gps_accuracy", 10.0)
            builder.add(record)

        path.parent.mkdir(parents=True, exist_ok=True)
        builder.build().to_file(str(path))
        return path

    @staticmethod
    def _set_field(message: object, name: str, value: object) -> None:
        field = message.get_field_by_name(name)
        if field is None:
            raise ValueError(f"Unknown FIT field: {name}")
        field.set_value(0, value)

    @staticmethod
    def _record_field_values(path: Path, field_name: str) -> list[object]:
        fit_file = FitFile.from_file(str(path))
        values = []
        for record in fit_file.records:
            message = getattr(record, "message", None)
            if getattr(message, "name", None) != "record":
                continue
            field = message.get_field_by_name(field_name)
            values.append(field.get_value() if field is not None and field.is_valid() else None)
        return values


if __name__ == "__main__":
    unittest.main()
