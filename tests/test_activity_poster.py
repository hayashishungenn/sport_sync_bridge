from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from sport_sync_bridge.activity_poster import (
    POSTER_LAYOUTS,
    POSTER_RATIOS,
    _power_points,
    _select_metrics,
    write_activity_poster,
)
from sport_sync_bridge.cli import main
from sport_sync_bridge.formats import TrackPoint, read_activity_file
from tests.activity_fixtures import create_fit, create_gpx


class ActivityPosterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.activity_path = create_gpx(self.root / "晨间骑行.gpx")
        self.activity = read_activity_file(self.activity_path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_writes_jpeg_with_requested_dimensions_and_track(self) -> None:
        output = self.root / "posters" / "ride.jpg"

        result = write_activity_poster(
            self.activity,
            output,
            ratio="square",
            user="测试用户",
        )

        self.assertEqual(result.output_path, output.resolve())
        self.assertEqual((result.width, result.height), POSTER_RATIOS["square"])
        self.assertEqual(len(result.warnings), 0)
        with Image.open(output) as poster:
            self.assertEqual(poster.format, "JPEG")
            self.assertEqual(poster.size, POSTER_RATIOS["square"])
            self.assertGreater(len(poster.getcolors(maxcolors=2_000_000) or []), 100)

    def test_all_confirmed_layouts_render(self) -> None:
        for layout in POSTER_LAYOUTS:
            with self.subTest(layout=layout):
                output = self.root / f"{layout}.jpg"
                result = write_activity_poster(self.activity, output, layout=layout)
                self.assertTrue(output.is_file())
                self.assertGreater(output.stat().st_size, 1000)
                self.assertEqual(result.width, POSTER_RATIOS["portrait"][0])

    def test_all_aspect_ratios_render(self) -> None:
        self.assertEqual(POSTER_RATIOS["portrait"], (1080, 1440))
        self.assertEqual(set(POSTER_RATIOS), {"portrait", "square"})
        for ratio, dimensions in POSTER_RATIOS.items():
            with self.subTest(ratio=ratio):
                output = self.root / f"{ratio}.jpg"
                result = write_activity_poster(self.activity, output, ratio=ratio)
                self.assertEqual((result.width, result.height), dimensions)
                with Image.open(output) as poster:
                    self.assertEqual(poster.size, dimensions)

    def test_missing_track_still_produces_indoor_poster(self) -> None:
        activity_path = create_fit(self.root / "indoor.fit", with_position=False)
        activity = read_activity_file(activity_path)
        start = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
        activity.laps[0].track_points = [
            TrackPoint(timestamp=start + timedelta(seconds=second), power_w=power)
            for second, power in ((0, 100), (5, 100), (10, 500), (15, 600), (60, 200))
        ]
        output = self.root / "indoor.jpg"

        result = write_activity_poster(activity, output, layout="indoor")

        self.assertTrue(output.is_file())
        self.assertEqual(result.warnings, ())
        with Image.open(output) as poster:
            self.assertEqual(poster.format, "JPEG")

    def test_power_curve_uses_maximum_average_by_duration(self) -> None:
        start = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
        points = [
            TrackPoint(timestamp=start + timedelta(seconds=second), power_w=power)
            for second, power in ((0, 100), (5, 100), (10, 500), (15, 600))
        ]

        self.assertEqual(_power_points(points), [(10.0, 400.0)])

    def test_poster_has_one_selected_stat_and_swim_pace_per_100m(self) -> None:
        summary = {
            "distance_m": 1000.0,
            "timer_time_s": 500.0,
            "average_speed_mps": 2.0,
            "sport_type": "swimming",
        }

        metrics = _select_metrics(summary, "pace")

        self.assertEqual([metric.label for metric in metrics], ["距离", "运动时间", "平均配速"])
        self.assertEqual(metrics[-1].value, "0'50''")
        self.assertEqual(metrics[-1].unit, "/100m")

    def test_background_photo_is_composited(self) -> None:
        photo_path = self.root / "background.png"
        Image.new("RGB", (120, 80), (20, 90, 190)).save(photo_path)
        output = self.root / "photo.jpg"

        result = write_activity_poster(
            self.activity,
            output,
            photo_path=photo_path,
            watermark=None,
            show_track=False,
        )

        self.assertTrue(output.is_file())
        self.assertEqual(result.warnings, ())

    def test_rejects_invalid_output_layout_and_color(self) -> None:
        with self.assertRaisesRegex(ValueError, "must use a .jpg"):
            write_activity_poster(self.activity, self.root / "poster.png")
        with self.assertRaisesRegex(ValueError, "Unsupported poster layout"):
            write_activity_poster(self.activity, self.root / "poster.jpg", layout="unknown")
        with self.assertRaisesRegex(ValueError, "Unsupported poster metric"):
            write_activity_poster(self.activity, self.root / "poster.jpg", metric="heart_rate")
        with self.assertRaisesRegex(ValueError, "Invalid track color"):
            write_activity_poster(self.activity, self.root / "poster.jpg", track_color="not-a-color")

    def test_cli_imports_activity_and_exports_poster(self) -> None:
        config = SimpleNamespace(
            data_dir=self.root / ".data",
            db_path=self.root / "state.db",
            log_level="INFO",
            log_path=self.root / "sync.log",
        )
        stdout = io.StringIO()
        with patch("sport_sync_bridge.cli.AppConfig.load", return_value=config), patch(
            "sport_sync_bridge.cli.configure_logging"
        ):
            with contextlib.redirect_stdout(stdout):
                imported = main(["library", "import", str(self.activity_path)])
                activity_id = stdout.getvalue().splitlines()[0].split('"')[3]
                exported = main(
                    [
                        "library",
                        "poster",
                        activity_id,
                        "--output",
                        str(self.root / "cli-poster.jpg"),
                        "--layout",
                        "classic_orange",
                        "--ratio",
                        "square",
                        "--no-watermark",
                    ]
                )

        self.assertEqual((imported, exported), (0, 0))
        self.assertIn("size=1080x1080", stdout.getvalue())
        with Image.open(self.root / "cli-poster.jpg") as poster:
            self.assertEqual(poster.format, "JPEG")
            self.assertEqual(poster.size, (1080, 1080))


if __name__ == "__main__":
    unittest.main()
