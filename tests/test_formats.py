from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from sport_sync_bridge.formats import convert_activity_file, _read_fit, _read_gpx, _read_tcx
from tests.activity_fixtures import START, create_fit, create_gpx, create_tcx


class FormatConversionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fit_path = create_fit(self.root / "source.fit")
        self.gpx_path = create_gpx(self.root / "source.gpx")
        self.tcx_path = create_tcx(self.root / "source.tcx")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_all_six_format_directions_preserve_common_fields(self) -> None:
        cases = (
            (self.fit_path, "gpx", _read_gpx),
            (self.fit_path, "tcx", _read_tcx),
            (self.gpx_path, "fit", _read_fit),
            (self.gpx_path, "tcx", _read_tcx),
            (self.tcx_path, "fit", _read_fit),
            (self.tcx_path, "gpx", _read_gpx),
        )
        for source_path, target_format, read_result in cases:
            with self.subTest(source=source_path.suffix, target=target_format):
                output_path = self.root / f"converted-{source_path.stem}.{target_format}"
                result = convert_activity_file(source_path, output_path, target_format)
                converted = read_result(result.output_path)
                points = converted.track_points
                self.assertEqual(len(points), 2)
                self.assertAlmostEqual(points[0].latitude or 0, 31.23, places=4)
                self.assertAlmostEqual(points[0].longitude or 0, 121.47, places=4)
                self.assertAlmostEqual(points[0].elevation_m or 0, 10.0, places=2)
                self.assertIsNotNone(points[0].timestamp)
                self.assertEqual(points[0].heart_rate_bpm, 150)
                self.assertEqual(points[0].cadence_rpm, 80)
                self.assertEqual(points[0].power_w, 200)
                self.assertAlmostEqual(points[0].speed_mps or 0, 5.0, places=2)
                self.assertAlmostEqual(points[-1].distance_m or 0, 100.0, places=2)
                self.assertEqual(converted.sport_type, "cycling")
                self.assertEqual(len(converted.laps), 1)
                if source_path.suffix in {".gpx", ".tcx"} and target_format in {"gpx", "tcx"}:
                    self.assertEqual(converted.name, "Evening ride")
                if source_path.suffix == ".fit":
                    self.assertTrue(any("avg_speed" in loss for loss in result.losses))
                if source_path.suffix == ".fit" and target_format == "tcx":
                    self.assertEqual(converted.laps[0].elapsed_time_s, 61.0)
                    self.assertTrue(any("timer time" in loss for loss in result.losses))
                    self.assertTrue(any("Activity-level elapsed" in loss for loss in result.losses))

    def test_lap_summary_loss_is_reported_for_gpx(self) -> None:
        output_path = self.root / "summary.gpx"
        result = convert_activity_file(self.tcx_path, output_path, "gpx")
        self.assertTrue(any("lap summary" in loss for loss in result.losses))
        self.assertEqual(len(_read_gpx(output_path).laps), 1)

    def test_unmodeled_tcx_lap_fields_are_reported_as_conversion_losses(self) -> None:
        source_path = self.root / "with-extra-lap-field.tcx"
        source_path.write_text(
            self.tcx_path.read_text(encoding="utf-8").replace(
                "<TotalTimeSeconds>60</TotalTimeSeconds>",
                "<TotalTimeSeconds>60</TotalTimeSeconds><BeginLatitude>31.23</BeginLatitude>",
            ),
            encoding="utf-8",
        )

        result = convert_activity_file(source_path, self.root / "extra-field.gpx", "gpx")

        self.assertTrue(any("BeginLatitude" in loss for loss in result.losses))

    def test_fit_sport_values_use_the_installed_profile_enum(self) -> None:
        from fit_tool.profile.profile_type import Sport

        basketball = create_fit(self.root / "basketball.fit", sport=Sport.BASKETBALL.value)
        self.assertEqual(_read_fit(basketball).sport_type, "basketball")
        output_path = self.root / "basketball.gpx"
        convert_activity_file(basketball, output_path, "gpx")
        self.assertEqual(_read_gpx(output_path).sport_type, "basketball")

    def test_fit_session_keeps_start_end_elapsed_timer_and_distance_summaries(self) -> None:
        activity = _read_fit(self.fit_path)
        self.assertEqual(activity.start_time, START)
        self.assertEqual(activity.end_time, START + timedelta(minutes=1))
        self.assertEqual(activity.elapsed_time_s, 62.0)
        self.assertEqual(activity.timer_time_s, 59.0)
        self.assertEqual(activity.distance_m, 100.0)

    def test_fit_session_keeps_heart_rate_and_training_stress_score(self) -> None:
        source_path = create_fit(
            self.root / "training-load.fit",
            average_heart_rate=150,
            maximum_heart_rate=180,
            training_stress_score=72.5,
        )
        activity = _read_fit(source_path)

        self.assertEqual(activity.average_heart_rate_bpm, 150)
        self.assertEqual(activity.maximum_heart_rate_bpm, 180)
        self.assertEqual(activity.training_stress_score, 72.5)

        for target_format in ("gpx", "tcx"):
            with self.subTest(target=target_format):
                result = convert_activity_file(
                    source_path,
                    self.root / f"training-load.{target_format}",
                    target_format,
                )
                self.assertTrue(any("training stress score" in loss for loss in result.losses))

    def test_fit_session_keeps_power_and_training_effect_metrics(self) -> None:
        source_path = create_fit(
            self.root / "training-metrics.fit",
            average_power=210,
            maximum_power=620,
            normalized_power=245,
            intensity_factor=0.82,
            aerobic_training_effect=3.7,
            anaerobic_training_effect=2.1,
            training_stress_score=72.5,
        )
        activity = _read_fit(source_path)

        self.assertEqual(activity.average_power_w, 210)
        self.assertEqual(activity.maximum_power_w, 620)
        self.assertEqual(activity.normalized_power_w, 245)
        self.assertAlmostEqual(activity.intensity_factor or 0, 0.82)
        self.assertAlmostEqual(activity.aerobic_training_effect or 0, 3.7)
        self.assertAlmostEqual(activity.anaerobic_training_effect or 0, 2.1)

        for target_format in ("gpx", "tcx"):
            with self.subTest(target=target_format):
                result = convert_activity_file(
                    source_path,
                    self.root / f"training-metrics.{target_format}",
                    target_format,
                )
                for label in (
                    "activity-level average power",
                    "activity-level maximum power",
                    "normalized power",
                    "intensity factor",
                    "aerobic training effect",
                    "anaerobic training effect",
                    "training stress score",
                ):
                    self.assertTrue(any(label in loss for loss in result.losses), label)

    def test_fit_writer_preserves_activity_summary_metrics(self) -> None:
        from sport_sync_bridge.formats import _write_fit

        activity = _read_gpx(self.gpx_path)
        activity.average_power_w = 205
        activity.maximum_power_w = 410
        activity.normalized_power_w = 220
        activity.intensity_factor = 0.74
        activity.aerobic_training_effect = 3.2
        activity.anaerobic_training_effect = 1.8
        activity.training_stress_score = 64.0
        output_path = self.root / "written-summary.fit"
        output_path.write_bytes(_write_fit(activity))

        converted = _read_fit(output_path)
        self.assertEqual(converted.average_power_w, 205)
        self.assertEqual(converted.maximum_power_w, 410)
        self.assertEqual(converted.normalized_power_w, 220)
        self.assertAlmostEqual(converted.intensity_factor or 0, 0.74)
        self.assertAlmostEqual(converted.aerobic_training_effect or 0, 3.2)
        self.assertAlmostEqual(converted.anaerobic_training_effect or 0, 1.8)
        self.assertEqual(converted.training_stress_score, 64.0)

    def test_tcx_uses_activity_heart_rate_summary_for_one_lap(self) -> None:
        source_path = create_fit(
            self.root / "summary-only-hr.fit",
            with_heart_rate=False,
            average_heart_rate=155,
            maximum_heart_rate=177,
        )
        result = convert_activity_file(source_path, self.root / "summary-only-hr.tcx", "tcx")

        self.assertFalse(any("activity-level average heart rate" in loss for loss in result.losses))
        activity = _read_tcx(result.output_path)
        self.assertEqual(activity.laps[0].average_heart_rate, 155)
        self.assertEqual(activity.laps[0].maximum_heart_rate, 177)

    def test_gpx_fit_and_tcx_gpx_round_trips_keep_track_measurements(self) -> None:
        intermediate_fit = self.root / "round-trip.fit"
        intermediate_tcx = self.root / "round-trip.tcx"
        final_gpx = self.root / "round-trip.gpx"
        convert_activity_file(self.gpx_path, intermediate_fit, "fit")
        convert_activity_file(intermediate_fit, intermediate_tcx, "tcx")
        convert_activity_file(intermediate_tcx, final_gpx, "gpx")

        activity = _read_gpx(final_gpx)
        self.assertEqual(len(activity.track_points), 2)
        self.assertEqual(activity.track_points[-1].heart_rate_bpm, 151)
        self.assertEqual(activity.track_points[-1].cadence_rpm, 81)
        self.assertAlmostEqual(activity.track_points[-1].distance_m or 0, 100.0)
        self.assertAlmostEqual(activity.track_points[-1].power_w or 0, 201.0)

    def test_gpx_to_tcx_keeps_track_distance_without_fabricating_calories(self) -> None:
        output_path = self.root / "from-gpx.tcx"
        convert_activity_file(self.gpx_path, output_path, "tcx")
        activity = _read_tcx(output_path)
        self.assertIsNone(activity.laps[0].calories)
        self.assertAlmostEqual(activity.track_points[-1].distance_m or 0, 100.0)

    def test_same_format_copy_validates_track_and_preserves_bytes(self) -> None:
        original = self.fit_path.read_bytes()
        output_path = self.root / "copy.fit"
        convert_activity_file(self.fit_path, output_path, "fit")
        self.assertEqual(output_path.read_bytes(), original)

        trackless = create_fit(self.root / "trackless.fit", with_track=False)
        with self.assertRaisesRegex(ValueError, "No GPS track points"):
            convert_activity_file(trackless, self.root / "trackless-copy.fit", "fit")

    def test_malformed_and_trackless_xml_files_fail(self) -> None:
        malformed = self.root / "malformed.gpx"
        malformed.write_text("<gpx", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Could not parse XML"):
            convert_activity_file(malformed, self.root / "bad.tcx", "tcx")

        trackless_gpx = create_gpx(self.root / "trackless.gpx", with_track=False)
        with self.assertRaisesRegex(ValueError, "No GPX tracks or routes"):
            convert_activity_file(trackless_gpx, self.root / "trackless.tcx", "tcx")

        trackless_tcx = create_tcx(self.root / "trackless.tcx", with_track=False)
        with self.assertRaisesRegex(ValueError, "No TCX track points"):
            convert_activity_file(trackless_tcx, self.root / "trackless.gpx", "gpx")

        malformed_fit = self.root / "malformed.fit"
        malformed_fit.write_bytes(b"not a FIT file")
        with self.assertRaisesRegex(ValueError, "Could not decode FIT"):
            convert_activity_file(malformed_fit, self.root / "malformed.gpx", "gpx")

    def test_tcx_requires_timestamps_for_fit_and_tcx_outputs(self) -> None:
        no_time_gpx = self.root / "no-time.gpx"
        no_time_gpx.write_text(
            '<gpx><trk><trkseg><trkpt lat="31.23" lon="121.47"/></trkseg></trk></gpx>',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "timestamp"):
            convert_activity_file(no_time_gpx, self.root / "no-time.tcx", "tcx")
        with self.assertRaisesRegex(ValueError, "timestamp"):
            convert_activity_file(no_time_gpx, self.root / "no-time.fit", "fit")


if __name__ == "__main__":
    unittest.main()
