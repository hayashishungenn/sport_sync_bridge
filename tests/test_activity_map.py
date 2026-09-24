from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sport_sync_bridge.activity_library import LocalActivityLibrary
from sport_sync_bridge.activity_map import write_route_map
from sport_sync_bridge.cli import main
from sport_sync_bridge.formats import TrackPoint, read_activity_file
from sport_sync_bridge.state import StateDB
from tests.activity_fixtures import create_gpx


class ActivityMapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.activity_path = create_gpx(self.root / "ride.gpx")
        self.activity = read_activity_file(self.activity_path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_writes_interactive_map_with_route_and_attribution(self) -> None:
        output = self.root / "maps" / "ride.html"

        result = write_route_map(self.activity, output)

        page = output.read_text(encoding="utf-8")
        self.assertEqual(result.output_path, output.resolve())
        self.assertEqual(result.point_count, 2)
        self.assertIn("L.polyline(activity.points", page)
        self.assertIn("https://tile.openstreetmap.org/{z}/{x}/{y}.png", page)
        self.assertIn("OpenStreetMap contributors", page)
        self.assertIn("[31.23,121.47]", page)
        self.assertIn("[31.231,121.471]", page)

    def test_title_is_inserted_as_data_not_executable_markup(self) -> None:
        self.activity.name = '</script><script>alert("x")</script>'
        output = self.root / "safe.html"

        write_route_map(self.activity, output)

        page = output.read_text(encoding="utf-8")
        self.assertNotIn('</script><script>alert("x")', page)
        self.assertIn("\\u003c/script", page)

    def test_accepts_single_point_and_rejects_missing_or_invalid_track(self) -> None:
        self.activity.laps[0].track_points = [self.activity.track_points[0]]
        result = write_route_map(self.activity, self.root / "single.html")
        self.assertEqual(result.point_count, 1)

        self.activity.laps[0].track_points = []
        with self.assertRaisesRegex(ValueError, "no GPS track"):
            write_route_map(self.activity, self.root / "empty.html")

        self.activity.laps[0].track_points = [TrackPoint(latitude=91, longitude=121)]
        with self.assertRaisesRegex(ValueError, "invalid GPS coordinates"):
            write_route_map(self.activity, self.root / "invalid.html")

        self.activity.laps[0].track_points = [TrackPoint(latitude=31.23)]
        with self.assertRaisesRegex(ValueError, "incomplete GPS coordinates"):
            write_route_map(self.activity, self.root / "incomplete.html")

    def test_requires_html_output(self) -> None:
        with self.assertRaisesRegex(ValueError, "must use .html or .htm"):
            write_route_map(self.activity, self.root / "route.gpx")

    def test_cli_exports_map_for_an_imported_activity(self) -> None:
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
        with patch("sport_sync_bridge.cli.AppConfig.load", return_value=config), patch(
            "sport_sync_bridge.cli.configure_logging"
        ), contextlib.redirect_stdout(stdout):
            exit_code = main(
                ["library", "map", imported.fingerprint[:12], "--output", str(self.root / "cli-map.html")]
            )

        self.assertEqual(exit_code, 0)
        self.assertIn("track_points=2", stdout.getvalue())
        self.assertTrue((self.root / "cli-map.html").is_file())
