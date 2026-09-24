from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sport_sync_bridge.activity_charts import write_activity_charts
from sport_sync_bridge.activity_library import LocalActivityLibrary
from sport_sync_bridge.cli import main
from sport_sync_bridge.formats import ActivityFile, ActivityLap, TrackPoint, read_activity_file
from sport_sync_bridge.state import StateDB
from tests.activity_fixtures import create_gpx


class ActivityChartTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.activity_path = create_gpx(self.root / "ride.gpx")
        self.activity = read_activity_file(self.activity_path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_writes_offline_charts_without_gps_coordinates(self) -> None:
        output = self.root / "charts" / "ride.html"

        result = write_activity_charts(self.activity, output)

        page = output.read_text(encoding="utf-8")
        self.assertEqual(result.output_path, output.resolve())
        self.assertEqual(result.point_count, 2)
        self.assertEqual(result.chart_count, 5)
        self.assertEqual(result.x_axis, "time")
        self.assertIn("心率", page)
        self.assertIn("速度", page)
        self.assertIn("海拔", page)
        self.assertIn("功率", page)
        self.assertIn("踏频", page)
        self.assertIn("M 76.00", page)
        self.assertNotIn("31.23", page)
        self.assertNotIn("121.47", page)
        self.assertNotIn("https://", page)

    def test_escapes_activity_title_as_html_text(self) -> None:
        self.activity.name = '<script>alert("x")</script>'
        output = self.root / "safe.html"

        write_activity_charts(self.activity, output)

        page = output.read_text(encoding="utf-8")
        self.assertNotIn('<script>alert("x")', page)
        self.assertIn("&lt;script&gt;", page)

    def test_uses_distance_when_track_timestamps_are_missing(self) -> None:
        activity = ActivityFile(
            laps=[
                ActivityLap(
                    track_points=[
                        TrackPoint(distance_m=0, heart_rate_bpm=120),
                        TrackPoint(distance_m=100, heart_rate_bpm=130),
                    ]
                )
            ]
        )

        result = write_activity_charts(activity, self.root / "distance.html")

        self.assertEqual(result.x_axis, "distance")
        self.assertIn("0.10 km", (self.root / "distance.html").read_text(encoding="utf-8"))

        invalid_distance = ActivityFile(
            laps=[
                ActivityLap(
                    track_points=[
                        TrackPoint(distance_m=float("nan"), heart_rate_bpm=120),
                        TrackPoint(distance_m=float("inf"), heart_rate_bpm=130),
                    ]
                )
            ]
        )
        result = write_activity_charts(invalid_distance, self.root / "sample.html")
        self.assertEqual(result.x_axis, "sample")

    def test_rejects_empty_unmeasured_and_non_html_activities(self) -> None:
        empty = ActivityFile()
        with self.assertRaisesRegex(ValueError, "no track samples"):
            write_activity_charts(empty, self.root / "empty.html")

        unmeasured = ActivityFile(laps=[ActivityLap(track_points=[TrackPoint(), TrackPoint()])])
        with self.assertRaisesRegex(ValueError, "no chartable"):
            write_activity_charts(unmeasured, self.root / "unmeasured.html")

        with self.assertRaisesRegex(ValueError, "must use .html or .htm"):
            write_activity_charts(self.activity, self.root / "ride.png")

    def test_cli_exports_charts_for_an_imported_activity(self) -> None:
        data_dir = self.root / ".data"
        db_path = data_dir / "state.db"
        state = StateDB(db_path)
        try:
            imported = LocalActivityLibrary(state, data_dir).import_paths([self.activity_path])[0]
        finally:
            state.close()
        config = SimpleNamespace(
            data_dir=data_dir,
            db_path=db_path,
            log_level="INFO",
            log_path=self.root / "sync.log",
        )
        stdout = io.StringIO()
        output = self.root / "cli-chart.html"
        with patch("sport_sync_bridge.cli.AppConfig.load", return_value=config), patch(
            "sport_sync_bridge.cli.configure_logging"
        ), contextlib.redirect_stdout(stdout):
            exit_code = main(["library", "chart", imported.fingerprint[:12], "--output", str(output)])

        self.assertEqual(exit_code, 0)
        self.assertIn("charts=5", stdout.getvalue())
        self.assertTrue(output.is_file())
