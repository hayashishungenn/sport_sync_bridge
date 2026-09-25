from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from sport_sync_bridge.activity_library import LocalActivityLibrary
from sport_sync_bridge.formats import read_activity_file
from sport_sync_bridge.huawei_archive import parse_huawei_activity_json
from sport_sync_bridge.state import StateDB
from tests.activity_fixtures import create_gpx


class HuaweiArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = StateDB(self.root / "state.db")
        self.library = LocalActivityLibrary(self.state, self.root / ".data")

    def tearDown(self) -> None:
        self.state.close()
        self.temporary.cleanup()

    def test_parses_grouped_motion_records_and_preserves_activity_fields(self) -> None:
        payload = _huawei_payload(
            [
                _activity_record(4, 1_767_323_040_000, "run-a"),
                _activity_record(5, 1_767_323_640_000, "run-b"),
            ],
            invalid_part_time_map=True,
        )

        activities = parse_huawei_activity_json(payload, "export", required=True)

        self.assertIsNotNone(activities)
        assert activities is not None
        self.assertEqual(len(activities), 2)
        first = activities[0]
        self.assertEqual(first.sport_type, "running")
        self.assertEqual(first.start_time, datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc))
        self.assertEqual(first.elapsed_time_s, 600)
        self.assertEqual(first.distance_m, 1_000)
        self.assertEqual(first.laps[0].calories, 250)
        self.assertEqual(first.average_heart_rate_bpm, 150)
        self.assertEqual(first.maximum_heart_rate_bpm, 180)
        self.assertEqual(len(first.track_points), 2)
        self.assertEqual(first.track_points[0].latitude, 31.23)
        self.assertEqual(first.track_points[0].longitude, 121.47)
        self.assertEqual(first.track_points[0].elevation_m, 10)
        self.assertEqual(first.track_points[0].heart_rate_bpm, 145)
        self.assertEqual(activities[1].sport_type, "walking")

    def test_parses_flat_activity_records(self) -> None:
        record = _activity_record(4, 1_767_323_040_000, "flat")
        payload = json.dumps([record]).encode()

        activities = parse_huawei_activity_json(payload, "export")

        self.assertIsNotNone(activities)
        assert activities is not None
        self.assertEqual(len(activities), 1)
        self.assertEqual(activities[0].track_points[-1].timestamp.timestamp(), 1_767_323_045)

    def test_preview_and_import_huawei_archive_zip_without_importing_other_json(self) -> None:
        archive_path = self.root / "huawei-export.zip"
        payload = _huawei_payload([_activity_record(4, 1_767_323_040_000, "archive")])
        gpx_path = create_gpx(self.root / "other-activity.gpx")
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr(
                "Motion path detail data & description/motion path detail data.json",
                payload,
            )
            archive.writestr("Health detail data/sleep.json", b"not activity JSON")
            archive.writestr("__MACOSX/._motion path detail data.json", b"ignored")
            archive.writestr("Activities/other-activity.gpx", gpx_path.read_bytes())

        previews = self.library.preview_paths([archive_path])

        self.assertEqual(len(previews), 2)
        self.assertEqual(previews[0].file_format, "gpx")
        self.assertEqual(previews[0].summary["track_point_count"], 2)
        self.assertEqual(self.state.list_local_activities(), [])

        imported = self.library.import_paths([archive_path])

        self.assertEqual(len(imported), 2)
        self.assertEqual(imported[0].sport_type, "running")
        self.assertEqual(imported[0].file_format, "gpx")
        self.assertIn("motion path detail data.json", imported[0].source_label)
        stored = self.state.get_local_activity(imported[0].fingerprint)
        activity = read_activity_file(Path(stored["file_path"]))
        self.assertEqual(activity.track_points[0].heart_rate_bpm, 145)
        self.assertEqual(archive_path.exists(), True)

    def test_recursive_huawei_export_folder_skips_other_json_and_keeps_activity_files(self) -> None:
        export_dir = self.root / "huawei-export"
        motion_dir = export_dir / "Motion path detail data & description"
        motion_dir.mkdir(parents=True)
        (motion_dir / "motion path detail data.json").write_bytes(
            _huawei_payload([_activity_record(4, 1_767_323_040_000, "folder run")])
        )
        health_dir = export_dir / "Health detail data"
        health_dir.mkdir()
        (health_dir / "sleep.json").write_bytes(b"not activity JSON")
        gpx_path = create_gpx(export_dir / "Activities" / "other-activity.gpx")

        previews = self.library.preview_paths([export_dir], recursive=True)

        self.assertEqual(len(previews), 2)
        self.assertEqual({preview.sport_type for preview in previews}, {"running", "cycling"})
        self.assertEqual(self.state.list_local_activities(), [])

        imported = self.library.import_paths([export_dir], recursive=True)

        self.assertEqual(len(imported), 2)
        self.assertEqual({result.sport_type for result in imported}, {"running", "cycling"})
        self.assertTrue(any("motion path detail data.json" in result.source_label for result in imported))
        self.assertTrue(any(str(gpx_path) in result.source_label for result in imported))

    def test_direct_motion_detail_file_expands_multiple_activities(self) -> None:
        source = self.root / "motion path detail data.json"
        source.write_bytes(
            _huawei_payload(
                [
                    _activity_record(4, 1_767_323_040_000, "first"),
                    _activity_record(5, 1_767_323_640_000, "second"),
                ]
            )
        )

        results = self.library.import_paths([source])

        self.assertEqual([result.sport_type for result in results], ["running", "walking"])
        self.assertEqual(len(self.state.list_local_activities()), 2)

    def test_invalid_recognized_huawei_json_fails_without_partial_import(self) -> None:
        source = self.root / "motion path detail data.json"
        source.write_text("{not json", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "Huawei motion-detail JSON is invalid"):
            self.library.import_paths([source])

        self.assertEqual(self.state.list_local_activities(), [])
        self.assertFalse(self.library.file_dir.exists())

    def test_invalid_later_activity_coordinates_fail_before_importing_earlier_records(self) -> None:
        source = self.root / "motion path detail data.json"
        first = _activity_record(4, 1_767_323_040_000, "first")
        second = _activity_record(5, 1_767_323_640_000, "invalid second")
        second["attribute"] = str(second["attribute"]).replace("lat=31.23", "lat=95")
        source.write_bytes(_huawei_payload([first, second]))

        with self.assertRaisesRegex(ValueError, "coordinates"):
            self.library.import_paths([source])

        self.assertEqual(self.state.list_local_activities(), [])
        self.assertFalse(self.library.file_dir.exists())

    def test_cli_preview_and_import_huawei_archive_without_account_access(self) -> None:
        archive_path = self.root / "cli-huawei.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr(
                "Motion path detail data & description/motion path detail data.json",
                _huawei_payload([_activity_record(4, 1_767_323_040_000, "CLI run")]),
            )
        project_root = Path(__file__).resolve().parents[1]
        data_dir = self.root / "cli-data"
        env = os.environ.copy()
        env["SYNC_DATA_DIR"] = str(data_dir)

        preview = subprocess.run(
            [sys.executable, str(project_root / "sync.py"), "library", "preview", str(archive_path)],
            cwd=project_root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertIn("CLI run", preview.stdout)
        preview_state = StateDB(data_dir / "sync_state.db")
        try:
            self.assertEqual(preview_state.list_local_activities(), [])
        finally:
            preview_state.close()

        imported = subprocess.run(
            [sys.executable, str(project_root / "sync.py"), "library", "import", str(archive_path)],
            cwd=project_root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(imported.returncode, 0, imported.stderr)
        self.assertIn('"name": "CLI run"', imported.stdout)
        self.assertIn("imported=1", imported.stdout)
        self.assertEqual(len(list((data_dir / "local_imports").glob("*.gpx"))), 1)
        imported_state = StateDB(data_dir / "sync_state.db")
        try:
            self.assertEqual(len(imported_state.list_local_activities()), 1)
        finally:
            imported_state.close()


def _huawei_payload(records: list[dict[str, object]], *, invalid_part_time_map: bool = False) -> bytes:
    grouped = {"recordDay": 20260102, "timeZone": "+0000", "motionPathData": records}
    if invalid_part_time_map:
        grouped["partTimeMap"] = {}
    text = json.dumps([grouped], separators=(",", ":"))
    if invalid_part_time_map:
        text = text.replace('"partTimeMap":{}', '"partTimeMap":{brokenKey: 3}')
    return text.encode()


def _activity_record(sport_type: int, start_time_ms: int, label: str) -> dict[str, object]:
    start_seconds = start_time_ms / 1000
    detail = "\n".join(
        [
            f"tp=lbs;k={start_seconds};lat=31.23;lon=121.47;alt=10;t={start_seconds};hr=145",
            f"tp=lbs;k={start_seconds + 5};lat=31.231;lon=121.471;alt=11;t={start_seconds + 5};hr=155",
        ]
    )
    simplify = json.dumps(
        {
            "avgHeartRate": 150,
            "maxHeartRate": 180,
            "totalCalories": 250,
            "totalDistance": 1_000,
            "totalTime": 600_000,
        },
        separators=(",", ":"),
    )
    return {
        "sportType": sport_type,
        "startTime": start_time_ms,
        "endTime": start_time_ms + 600_000,
        "totalTime": 600_000,
        "totalDistance": 1_000,
        "totalCalories": 250,
        "attribute": f"HW_EXT_TRACK_DETAIL@is{detail}&&HW_EXT_TRACK_SIMPLIFY@is{simplify}",
        "name": label,
    }


if __name__ == "__main__":
    unittest.main()
