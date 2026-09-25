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
)
from tests.activity_fixtures import create_fit


class FitContinuityRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_removes_backward_and_over_48_hour_records_and_is_idempotent(self) -> None:
        start = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
        timestamps = (
            start,
            start + timedelta(seconds=60),
            start + timedelta(seconds=30),
            None,
            start + timedelta(seconds=60 + 172_800),
            start + timedelta(seconds=60 + 172_800 + 172_801),
        )
        source = self._create_fit(self.root / "source.fit", timestamps)
        original = source.read_bytes()
        output = self.root / "repaired.fit"

        repaired_path, removed = repair_fit_track_continuity(source, output)

        self.assertEqual(repaired_path, output.resolve())
        self.assertEqual(removed, 2)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(
            self._record_timestamps(repaired_path),
            [
                self._fit_timestamp(timestamps[0]),
                self._fit_timestamp(timestamps[1]),
                None,
                self._fit_timestamp(timestamps[4]),
            ],
        )
        self.assertTrue(Path(f"{repaired_path}.fit-repair.json").is_file())

        repeated_path, repeated_removed = repair_fit_track_continuity(
            repaired_path,
            self.root / "second-pass.fit",
        )
        self.assertEqual(repeated_path, repaired_path)
        self.assertEqual(repeated_removed, 0)
        self.assertFalse((self.root / "second-pass.fit").exists())

    def test_preserves_coordinate_marker_from_prior_normalization(self) -> None:
        source = create_fit(self.root / "coordinates.fit")
        normalized, changed = normalize_fit_coordinates(
            source,
            self.root / "normalized.fit",
            "gcj02_to_wgs84",
        )
        self.assertGreater(changed, 0)

        repaired, removed = repair_fit_track_continuity(normalized, self.root / "repaired.fit")

        self.assertEqual(removed, 0)
        self.assertTrue(Path(f"{repaired}.coord.json").is_file())
        second_path, second_changes = normalize_fit_coordinates(
            repaired,
            self.root / "second-coordinate-pass.fit",
            "gcj02_to_wgs84",
        )
        self.assertEqual(second_path, repaired)
        self.assertEqual(second_changes, 0)

    def test_cli_writes_output_and_reports_removed_records(self) -> None:
        start = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
        source = self._create_fit(
            self.root / "cli-source.fit",
            (start, start - timedelta(seconds=1)),
        )
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
                    "repair-fit-continuity",
                    str(source),
                    "--output",
                    str(output),
                ]
            )

        self.assertEqual(status, 0)
        self.assertTrue(output.is_file())
        self.assertIn("records_removed=1", captured.getvalue())
        self.assertIn(f"output={output.resolve()}", captured.getvalue())

    def test_rejects_corrupt_fit_input_without_creating_output(self) -> None:
        source = self.root / "corrupt.fit"
        source.write_bytes(b"not a FIT file")
        output = self.root / "repaired.fit"

        with self.assertRaisesRegex(RuntimeError, "Could not decode FIT file"):
            repair_fit_track_continuity(source, output)
        self.assertFalse(output.exists())

    def _create_fit(self, path: Path, timestamps: tuple[datetime | None, ...]) -> Path:
        builder = FitFileBuilder(auto_define=True)
        file_id = FileIdMessage()
        self._set_field(file_id, "type", FileType.ACTIVITY.value)
        self._set_field(file_id, "manufacturer", 999)
        self._set_field(file_id, "product", 123)
        builder.add(file_id)

        for index, timestamp in enumerate(timestamps):
            record = RecordMessage()
            if timestamp is not None:
                self._set_field(record, "timestamp", self._fit_timestamp(timestamp))
            self._set_field(record, "heart_rate", 140 + index)
            self._set_field(record, "position_lat", 31.23 + index * 0.001)
            self._set_field(record, "position_long", 121.47 + index * 0.001)
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
    def _fit_timestamp(value: datetime | None) -> int | None:
        if value is None:
            return None
        return round(value.timestamp() * 1000)

    @staticmethod
    def _record_timestamps(path: Path) -> list[int | None]:
        fit_file = FitFile.from_file(str(path))
        timestamps = []
        for record in fit_file.records:
            message = getattr(record, "message", None)
            if getattr(message, "name", None) != "record":
                continue
            field = message.get_field_by_name("timestamp")
            timestamps.append(field.get_value() if field is not None and field.is_valid() else None)
        return timestamps


if __name__ == "__main__":
    unittest.main()
