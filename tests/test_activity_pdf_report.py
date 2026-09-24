from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sport_sync_bridge.activity_analysis import write_activity_report_pdf
from sport_sync_bridge.cli import main
from tests.activity_fixtures import create_gpx


class ActivityPdfReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_writes_chinese_activity_report_with_cjk_font(self) -> None:
        row = {
            "fingerprint": "a" * 64,
            "name": "晨间骑行",
            "sport_type": "cycling",
            "start_time": "2026-01-02T03:04:00+00:00",
            "summary_json": json.dumps(
                {
                    "distance_m": 12500,
                    "elapsed_time_s": 3600,
                    "timer_time_s": 3500,
                    "average_speed_mps": 3.57,
                    "average_heart_rate_bpm": 142,
                    "maximum_heart_rate_bpm": 168,
                    "average_cadence_rpm": 82,
                    "average_power_w": 185,
                    "maximum_power_w": 420,
                    "normalized_power_w": 198,
                    "intensity_factor": 0.72,
                    "aerobic_training_effect": 3.2,
                    "anaerobic_training_effect": 1.1,
                    "training_stress_score": 58,
                    "total_ascent_m": 220,
                    "time_in_zone_messages": [{"heart_rate_zones": [{"zone": 2, "seconds": 1200}]}],
                    "lap_count": 2,
                    "track_point_count": 1800,
                },
                ensure_ascii=False,
            ),
        }
        output = self.root / "reports" / "activity.pdf"

        result = write_activity_report_pdf([row], output)
        contents = result.read_bytes()

        self.assertEqual(result, output.resolve())
        self.assertTrue(contents.startswith(b"%PDF-"))
        self.assertIn(b"STSong-Light", contents)
        self.assertGreater(len(contents), 1000)

    def test_pdf_cli_requires_output_path(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            main(["library", "report", "--format", "pdf"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("requires --output", stderr.getvalue())

    def test_cli_exports_imported_activity_as_pdf(self) -> None:
        config = SimpleNamespace(
            data_dir=self.root / ".data",
            db_path=self.root / "state.db",
            log_level="INFO",
            log_path=self.root / "sync.log",
        )
        source = create_gpx(self.root / "晨间骑行.gpx")
        output = self.root / "reports" / "activity.pdf"
        stdout = io.StringIO()
        with patch("sport_sync_bridge.cli.AppConfig.load", return_value=config), patch(
            "sport_sync_bridge.cli.configure_logging"
        ):
            with contextlib.redirect_stdout(stdout):
                imported = main(["library", "import", str(source)])
                exported = main(
                    ["library", "report", "--format", "pdf", "--output", str(output)]
                )

        contents = output.read_bytes()
        self.assertEqual(imported, 0)
        self.assertEqual(exported, 0)
        self.assertIn("晨间骑行", stdout.getvalue())
        self.assertTrue(contents.startswith(b"%PDF-"))
        self.assertIn(b"STSong-Light", contents)

    def test_cli_writes_empty_activity_report_pdf(self) -> None:
        config = SimpleNamespace(
            data_dir=self.root / ".data",
            db_path=self.root / "state.db",
            log_level="INFO",
            log_path=self.root / "sync.log",
        )
        output = self.root / "empty.pdf"
        stdout = io.StringIO()
        with patch("sport_sync_bridge.cli.AppConfig.load", return_value=config), patch(
            "sport_sync_bridge.cli.configure_logging"
        ):
            with contextlib.redirect_stdout(stdout):
                result = main(["library", "report", "--format", "pdf", "--output", str(output)])

        self.assertEqual(result, 0)
        self.assertIn("written=", stdout.getvalue())
        self.assertTrue(output.read_bytes().startswith(b"%PDF-"))


if __name__ == "__main__":
    unittest.main()
