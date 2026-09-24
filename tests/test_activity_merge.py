from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sport_sync_bridge.activity_merge import merge_fit_files
from sport_sync_bridge.cli import main
from sport_sync_bridge.formats import read_activity_file
from tests.activity_fixtures import START, create_fit


class ActivityMergeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_merge_rebases_overlapping_timestamps_and_accumulates_distance(self) -> None:
        first = create_fit(self.root / "first.fit")
        second = create_fit(self.root / "second.fit")
        first_bytes = first.read_bytes()
        second_bytes = second.read_bytes()
        output = self.root / "merged.fit"

        result = merge_fit_files([first, second], output, name="Combined ride")
        activity = read_activity_file(output)

        self.assertEqual(result.records_before, 4)
        self.assertEqual(result.records_after, 4)
        self.assertFalse(result.decimated)
        self.assertEqual(len(activity.track_points), 4)
        self.assertEqual(activity.sport_type, "cycling")
        self.assertEqual(activity.distance_m, 200.0)
        self.assertEqual(activity.track_points[2].distance_m, 100.0)
        timestamps = [point.timestamp for point in activity.track_points]
        self.assertEqual(timestamps, sorted(timestamps))
        self.assertEqual(first.read_bytes(), first_bytes)
        self.assertEqual(second.read_bytes(), second_bytes)
        self.assertTrue(any("activity name" in loss.lower() for loss in result.losses))

    def test_merge_preserves_an_inter_activity_rest_lap(self) -> None:
        first = create_fit(self.root / "early.fit")
        second = create_fit(self.root / "late.fit", start=START + timedelta(minutes=10))
        output = self.root / "merged.fit"

        merge_fit_files([first, second], output)
        activity = read_activity_file(output)
        rest_laps = [lap for lap in activity.laps if not lap.track_points]

        self.assertEqual(len(rest_laps), 1)
        self.assertEqual(rest_laps[0].elapsed_time_s, 540.0)
        self.assertEqual(rest_laps[0].timer_time_s, 0.0)
        self.assertEqual(rest_laps[0].distance_m, 0.0)

    def test_merge_keeps_records_without_gps_coordinates(self) -> None:
        first = create_fit(self.root / "indoor-a.fit", with_position=False)
        second = create_fit(self.root / "indoor-b.fit", with_position=False)
        output = self.root / "indoor-merged.fit"

        merge_fit_files([first, second], output)
        activity = read_activity_file(output)

        self.assertEqual(len(activity.track_points), 4)
        self.assertTrue(all(point.latitude is None and point.longitude is None for point in activity.track_points))
        self.assertEqual(activity.track_points[0].heart_rate_bpm, 150.0)

    def test_merge_decimates_to_limit_and_keeps_endpoints(self) -> None:
        first = create_fit(self.root / "part-a.fit")
        second = create_fit(self.root / "part-b.fit")
        output = self.root / "decimated.fit"

        result = merge_fit_files([first, second], output, max_records=3)
        activity = read_activity_file(output)

        self.assertTrue(result.decimated)
        self.assertEqual(result.records_after, 3)
        self.assertEqual(len(activity.track_points), 3)
        self.assertEqual(activity.track_points[0].heart_rate_bpm, 150.0)
        self.assertEqual(activity.track_points[-1].heart_rate_bpm, 151.0)

    def test_merge_rejects_invalid_or_ambiguous_inputs(self) -> None:
        first = create_fit(self.root / "first.fit")
        no_timestamps = create_fit(self.root / "no-time.fit", with_timestamps=False)
        no_records = create_fit(self.root / "no-records.fit", with_track=False)
        different_sport = create_fit(self.root / "run.fit", sport=1)

        with self.assertRaisesRegex(ValueError, "at least two"):
            merge_fit_files([first], self.root / "one.fit")
        with self.assertRaisesRegex(ValueError, "timestamp"):
            merge_fit_files([first, no_timestamps], self.root / "bad-time.fit")
        with self.assertRaisesRegex(ValueError, "no record messages"):
            merge_fit_files([first, no_records], self.root / "empty.fit")
        with self.assertRaisesRegex(ValueError, "different sport types"):
            merge_fit_files([first, different_sport], self.root / "mixed.fit")
        with self.assertRaisesRegex(ValueError, "different from every input"):
            merge_fit_files([first, no_timestamps], first)

    def test_merge_cli_runs_without_account_engine(self) -> None:
        first = create_fit(self.root / "cli-a.fit")
        second = create_fit(self.root / "cli-b.fit")
        output_path = self.root / "cli-merged.fit"
        config = SimpleNamespace(
            data_dir=self.root / ".data",
            db_path=self.root / ".data" / "state.db",
            log_level="INFO",
            log_path=self.root / "sync.log",
        )
        stdout = io.StringIO()
        with (
            patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
            patch("sport_sync_bridge.cli.configure_logging"),
            patch("sport_sync_bridge.cli.SyncEngine", side_effect=AssertionError("engine is not needed")),
            contextlib.redirect_stdout(stdout),
        ):
            status = main(
                ["library", "merge", str(first), str(second), "--output", str(output_path), "--name", "CLI merge"]
            )

        self.assertEqual(status, 0)
        self.assertTrue(output_path.is_file())
        self.assertIn("merged=2", stdout.getvalue())
        self.assertIn("records=4/4", stdout.getvalue())
        self.assertIn("decimated=no", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
