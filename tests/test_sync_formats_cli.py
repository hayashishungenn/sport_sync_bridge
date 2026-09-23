from __future__ import annotations

import contextlib
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sport_sync_bridge.cli import _target_format_map, build_parser, main
from sport_sync_bridge.engine import SyncEngine
from sport_sync_bridge.formats import _read_tcx
from sport_sync_bridge.models import Activity, UploadResult
from tests.activity_fixtures import create_fit, create_gpx


class _State:
    def __init__(self) -> None:
        self.recorded: list[dict[str, object]] = []

    def get_activity_row(self, source: str, source_id: str):
        return None

    def is_target_done(self, source: str, source_id: str, target: str) -> bool:
        return False

    def upsert_activity(self, **values: object) -> None:
        self.activity_row = values

    def record_target_result(self, **values: object) -> None:
        self.recorded.append(values)


class _Source:
    name = "igpsport"

    def __init__(self, activity: Activity, fit_path: Path) -> None:
        self.activity = activity
        self.fit_path = fit_path

    def list_activities(self, **kwargs: object) -> list[Activity]:
        return [self.activity]

    def download_fit(self, activity: Activity, output_dir: Path) -> Path:
        return self.fit_path


class _Target:
    def __init__(self) -> None:
        self.uploaded: list[Path] = []

    def upload_file(self, file_path: Path, activity: Activity, external_id: str) -> UploadResult:
        self.uploaded.append(file_path)
        return UploadResult(status="success")


class SyncFormatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_targets_receive_independent_requested_files_and_original_is_unchanged(self) -> None:
        fit_path = create_fit(self.root / "download.fit")
        original_hash = hashlib.sha256(fit_path.read_bytes()).hexdigest()
        activity = Activity("igpsport", "activity/42", "Morning ride", "cycling")
        engine = SyncEngine.__new__(SyncEngine)
        engine.config = SimpleNamespace(
            sources=["igpsport"],
            targets=["garmin", "strava"],
            lookback_days=0,
            downloads_dir=self.root / "downloads",
            repaired_dir=self.root / "repaired",
            converted_dir=self.root / "converted",
            coordinate_rules=(),
            igpsport_coord_mode="none",
            igpsport_coord_strict=True,
            onelap_coord_mode="none",
            onelap_coord_strict=True,
        )
        engine.state_db = _State()
        engine.sources = {"igpsport": _Source(activity, fit_path)}
        garmin = _Target()
        strava = _Target()
        engine.targets = {"garmin": garmin, "strava": strava}

        with self.assertLogs("sport_sync_bridge.engine", level="WARNING"):
            count = engine.sync_once(target_formats={"garmin": "fit", "strava": "tcx"})

        self.assertEqual(count, 2)
        self.assertNotEqual(fit_path.resolve(), garmin.uploaded[0])
        self.assertEqual(garmin.uploaded[0].suffix, ".fit")
        self.assertEqual(strava.uploaded[0].suffix, ".tcx")
        self.assertIn("converted", garmin.uploaded[0].parts)
        self.assertIn("converted", strava.uploaded[0].parts)
        self.assertNotEqual(garmin.uploaded[0], strava.uploaded[0])
        self.assertEqual(_read_tcx(strava.uploaded[0]).sport_type, "cycling")
        self.assertEqual(hashlib.sha256(fit_path.read_bytes()).hexdigest(), original_hash)
        self.assertEqual([item["status"] for item in engine.state_db.recorded], ["success", "success"])

    def test_conversion_failure_is_recorded_per_target_and_does_not_block_next_target(self) -> None:
        fit_path = create_fit(self.root / "download.fit", with_timestamps=False)
        activity = Activity("igpsport", "42", "Ride", "cycling")
        engine = SyncEngine.__new__(SyncEngine)
        engine.config = SimpleNamespace(
            sources=["igpsport"], targets=["garmin", "strava"], lookback_days=0,
            downloads_dir=self.root / "downloads",
            repaired_dir=self.root / "repaired", converted_dir=self.root / "converted",
            coordinate_rules=(), igpsport_coord_mode="none", igpsport_coord_strict=True,
            onelap_coord_mode="none", onelap_coord_strict=True,
        )
        engine.state_db = _State()
        engine.sources = {"igpsport": _Source(activity, fit_path)}
        garmin = _Target()
        strava = _Target()
        engine.targets = {"garmin": garmin, "strava": strava}

        count = engine.sync_once(target_formats={"garmin": "tcx", "strava": "fit"})

        self.assertEqual(count, 1)
        self.assertEqual(garmin.uploaded, [])
        self.assertEqual(strava.uploaded[0].suffix, ".fit")
        self.assertEqual(engine.state_db.recorded[0]["status"], "failed")
        self.assertIn("timestamp", str(engine.state_db.recorded[0]["message"]))
        self.assertEqual(engine.state_db.recorded[1]["status"], "success")

    def test_sync_defaults_both_targets_to_fit(self) -> None:
        fit_path = create_fit(self.root / "download.fit")
        activity = Activity("igpsport", "42", "Ride", "cycling")
        engine = SyncEngine.__new__(SyncEngine)
        engine.config = SimpleNamespace(
            sources=["igpsport"], targets=["garmin", "strava"], lookback_days=0,
            downloads_dir=self.root / "downloads", repaired_dir=self.root / "repaired",
            converted_dir=self.root / "converted", coordinate_rules=(),
            igpsport_coord_mode="none", igpsport_coord_strict=True,
            onelap_coord_mode="none", onelap_coord_strict=True,
        )
        engine.state_db = _State()
        engine.sources = {"igpsport": _Source(activity, fit_path)}
        garmin = _Target()
        strava = _Target()
        engine.targets = {"garmin": garmin, "strava": strava}

        count = engine.sync_once()

        self.assertEqual(count, 2)
        self.assertEqual(garmin.uploaded[0].suffix, ".fit")
        self.assertEqual(strava.uploaded[0].suffix, ".fit")
        self.assertNotEqual(garmin.uploaded[0], strava.uploaded[0])

    def test_format_argument_mapping_and_validation(self) -> None:
        args = build_parser().parse_args(
            ["sync", "--format", "garmin=fit", "--format", "strava=TCX", "--dry-run"]
        )
        self.assertEqual(_target_format_map(args.target_formats), {"garmin": "fit", "strava": "tcx"})
        default_args = build_parser().parse_args(["sync"])
        self.assertEqual(_target_format_map(default_args.target_formats), {})
        with self.assertRaises(ValueError):
            _target_format_map([("strava", "fit"), ("strava", "tcx")])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(["sync", "--format", "strava=zip"])

    def test_sync_dry_run_cli_smoke_uses_no_account_login(self) -> None:
        config = SimpleNamespace(data_dir=self.root / ".data", log_level="INFO", log_path=self.root / "sync.log")

        class _Engine:
            sources = {"igpsport": object()}
            targets = {"garmin": object(), "strava": object()}

            def __init__(self, _config: object) -> None:
                self.call: dict[str, object] | None = None

            def sync_once(self, **kwargs: object) -> int:
                self.call = kwargs
                return 0

            def close(self) -> None:
                return None

        output = io.StringIO()
        with (
            patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
            patch("sport_sync_bridge.cli.configure_logging"),
            patch("sport_sync_bridge.cli.SyncEngine", _Engine),
            contextlib.redirect_stdout(output),
        ):
            status = main(
                ["sync", "--dry-run", "--source", "igpsport", "--target", "strava", "--format", "strava=tcx"]
            )

        self.assertEqual(status, 0)
        self.assertIn("done=0", output.getvalue())

    def test_local_convert_command_runs_without_constructing_sync_engine(self) -> None:
        input_path = create_gpx(self.root / "input.gpx")
        output_path = self.root / "output.tcx"
        config = SimpleNamespace(
            data_dir=self.root / ".data",
            log_level="INFO",
            log_path=self.root / "sync.log",
            converted_dir=self.root / "converted",
            coordinate_rules=(),
            igpsport_coord_mode="none",
            onelap_coord_mode="none",
        )
        output = io.StringIO()
        with (
            patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
            patch("sport_sync_bridge.cli.configure_logging"),
            patch("sport_sync_bridge.cli.SyncEngine", side_effect=AssertionError("engine must not be created")),
            contextlib.redirect_stdout(output),
        ):
            status = main(["convert", str(input_path), "--to", "tcx", "--output", str(output_path)])

        self.assertEqual(status, 0)
        self.assertEqual(_read_tcx(output_path).track_points[0].heart_rate_bpm, 150)
        self.assertIn("converted=gpx->tcx", output.getvalue())


if __name__ == "__main__":
    unittest.main()
