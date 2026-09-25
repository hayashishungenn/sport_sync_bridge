from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fit_tool.fit_file import FitFile

from sport_sync_bridge.formats import TrackPoint
from sport_sync_bridge.virtual_ride import (
    PowerSegment,
    RideCourse,
    RideCourseError,
    RideSession,
    demo_ride_course,
    intensity_multiplier_from_percent,
    load_ride_course,
    write_ride_activity_fit,
)


class VirtualRideTests(unittest.TestCase):
    def test_demo_course_interpolates_power_and_uses_dart_rounding(self) -> None:
        course = demo_ride_course()

        self.assertEqual(course.duration_s, 600)
        self.assertEqual(course.target_power_at(0), 110)
        self.assertEqual(course.target_power_at(150), 125)
        self.assertEqual(course.target_power_at(300), 165)
        self.assertEqual(course.target_power_at(600), 0)
        self.assertEqual(PowerSegment(0, 2, 100, 101).target_power_at(1), 101)
        self.assertEqual(course.target_power_at(150, 1.1), 138)

    def test_session_supports_intensity_skip_and_pause_state(self) -> None:
        course = RideCourse(
            (
                PowerSegment(0, 10, 100, 100, "first"),
                PowerSegment(10, 20, 200, 200, "second"),
            )
        )
        session = RideSession(course, intensity=0.1)

        self.assertEqual(session.decrease_intensity(), 10)
        self.assertEqual(session.increase_intensity(), 15)
        session.advance(2.5)
        self.assertTrue(session.skip_interval())
        self.assertEqual(session.wall_elapsed_s, 2.5)
        self.assertEqual(session.course_elapsed_s, 10)
        self.assertEqual(session.current_target_power_w, 30)
        self.assertTrue(session.toggle_pause())
        self.assertFalse(session.toggle_pause())

    def test_session_advances_very_short_segments_without_stalling(self) -> None:
        session = RideSession(RideCourse((PowerSegment(0, 1e-10, 100, 100),)))

        session.advance(1e-10)

        self.assertIsNone(session.current_segment)
        self.assertEqual(session.wall_elapsed_s, 1e-10)

    def test_loads_direct_segments_json(self) -> None:
        payload = {
            "name": "Short test",
            "segments": [
                {
                    "start_time_s": 0,
                    "end_time_s": 30,
                    "start_power_w": 120,
                    "end_power_w": 150,
                    "label": "warmup",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "course.json"
            source.write_text(json.dumps(payload), encoding="utf-8")

            course = load_ride_course(source)

        self.assertEqual(course.name, "Short test")
        self.assertEqual(course.target_power_at(15), 135)

    def test_loads_ai_workout_targets_repeats_and_fallback(self) -> None:
        payload = {
            "name": "Mixed targets",
            "sportType": "cycling",
            "steps": [
                {"intensity": "active", "duration": "10min", "target": "200 W"},
                {
                    "repeat": 2,
                    "steps": [
                        {"intensity": "interval", "duration": "5min", "target": "90% FTP"}
                    ],
                },
                {"intensity": "active", "duration": "1km", "target": "power zone 4"},
                {"intensity": "recovery", "duration": "open", "target": "180 bpm"},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "workout.json"
            source.write_text(json.dumps(payload), encoding="utf-8")

            course = load_ride_course(source, ftp_watts=200)

        self.assertEqual(course.name, "Mixed targets")
        self.assertEqual([segment.duration_s for segment in course.segments], [600, 300, 300, 120, 300])
        self.assertEqual([segment.start_power_w for segment in course.segments], [200, 180, 180, 380, 100])
        self.assertEqual(len(course.warnings), 1)
        self.assertIn("50% FTP fallback", course.warnings[0])

    def test_loads_generated_fit_metadata_sidecar(self) -> None:
        payload = {
            "type": "workout",
            "name": "Generated cycling workout",
            "sportType": "CYCLING",
            "steps": [{"intensity": "active", "duration": "2min", "target": "150 W"}],
        }
        with tempfile.TemporaryDirectory() as directory:
            fit_path = Path(directory) / "generated.fit"
            Path(f"{fit_path}.meta").write_text(json.dumps(payload), encoding="utf-8")

            course = load_ride_course(fit_path)

        self.assertEqual(course.name, "Generated cycling workout")
        self.assertEqual(course.duration_s, 120)
        self.assertEqual(course.target_power_at(0), 150)

    def test_writes_trackless_virtual_ride_fit_without_inventing_power(self) -> None:
        start = datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)
        samples = [
            TrackPoint(timestamp=start, power_w=145, speed_mps=7.5, heart_rate_bpm=132),
            TrackPoint(timestamp=start + timedelta(seconds=5), power_w=150, speed_mps=8.0),
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "ride.fit"

            written = write_ride_activity_fit(
                "Indoor ride",
                start,
                10,
                samples,
                output,
                timer_time_s=8,
            )
            decoded = FitFile.from_file(str(written))
            messages = [record.message for record in decoded.records if not record.is_definition]
            records = [message for message in messages if message.name == "record"]
            session = next(message for message in messages if message.name == "session")

            self.assertEqual(len(records), 3)
            self.assertEqual(records[0].power, 145)
            self.assertEqual(records[0].heart_rate, 132)
            self.assertIsNone(records[0].position_lat)
            self.assertEqual(session.total_elapsed_time, 10)
            self.assertEqual(session.total_timer_time, 8)
            before = output.read_bytes()
            with self.assertRaisesRegex(RideCourseError, "already exists"):
                write_ride_activity_fit("Indoor ride", start, 10, samples, output, timer_time_s=8)
            self.assertEqual(output.read_bytes(), before)

    def test_rejects_bad_course_json_and_invalid_segments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "bad.json"
            source.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(RideCourseError, "valid JSON"):
                load_ride_course(source)

            source.write_text(
                json.dumps(
                    {
                        "segments": [
                            {"start_time_s": 1, "end_time_s": 2, "start_power_w": 100, "end_power_w": 100}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RideCourseError, "start at 0"):
                load_ride_course(source)

    def test_requires_ftp_for_percentage_targets_and_rejects_extreme_intensity(self) -> None:
        payload = {
            "name": "FTP workout",
            "sportType": "cycling",
            "steps": [{"intensity": "interval", "duration": "1min", "target": "90% FTP"}],
        }
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "workout.json"
            source.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RideCourseError, "supply --ftp"):
                load_ride_course(source)

        with self.assertRaisesRegex(RideCourseError, "at least 10"):
            intensity_multiplier_from_percent(9.9)
        with self.assertRaisesRegex(RideCourseError, "exceeds 32767"):
            PowerSegment(0, 1, 20_000, 20_000).target_power_at(0, 2)


if __name__ == "__main__":
    unittest.main()
