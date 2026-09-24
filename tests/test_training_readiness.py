from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sport_sync_bridge.cli import main
from sport_sync_bridge.state import StateDB
from sport_sync_bridge.training_readiness import (
    format_training_readiness_text,
    import_training_readiness_json,
    summarize_training_readiness,
)


class TrainingReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = StateDB(self.root / "state.db")

    def tearDown(self) -> None:
        self.state.close()
        self.temporary.cleanup()

    def test_import_preserves_model_fields_and_lists_them(self) -> None:
        payload = self._sample_record()
        source = self._write_json("readiness.json", payload)

        self.assertEqual(import_training_readiness_json(self.state, source), 1)
        summary = summarize_training_readiness(self.state)
        self.assertEqual(summary["record_count"], 1)
        record = summary["records"][0]
        self.assertEqual(record["calendar_date"], "2026-08-03")
        self.assertEqual(record["score"], 74.5)
        self.assertEqual(record["level"], "Moderate")
        self.assertEqual(record["data"], payload)
        self.assertIn("context_source", record["data"]["inputContext"])
        self.assertEqual(record["data"]["metadata"]["device"], "watch")

        text = format_training_readiness_text(summary)
        self.assertIn("训练准备度分数：74.5", text)
        self.assertIn("ACWR 因子反馈：急性负荷适中", text)
        self.assertIn("睡眠数据有效：是", text)

    def test_identical_payload_is_idempotent_across_json_formatting(self) -> None:
        payload = self._sample_record()
        source = self._write_json("readiness.json", payload)
        self.assertEqual(import_training_readiness_json(self.state, source), 1)

        source.write_text(json.dumps(payload, ensure_ascii=False, indent=4), encoding="utf-8")
        self.assertEqual(import_training_readiness_json(self.state, source), 0)
        self.assertEqual(summarize_training_readiness(self.state)["record_count"], 1)

    def test_invalid_record_array_is_rejected_without_partial_writes(self) -> None:
        valid = self._sample_record()
        invalid = {"calendarDate": "not-a-date", "score": 92}
        source = self._write_json("readiness.json", [valid, invalid])

        with self.assertRaisesRegex(ValueError, "invalid calendarDate"):
            import_training_readiness_json(self.state, source)

        self.assertEqual(summarize_training_readiness(self.state)["record_count"], 0)

    def test_cli_import_and_json_history_use_local_state(self) -> None:
        source = self._write_json("readiness.json", self._sample_record())
        config = SimpleNamespace(
            data_dir=self.root / ".data",
            db_path=self.root / "cli-state.db",
            log_level="INFO",
            log_path=self.root / "sync.log",
        )
        output = io.StringIO()
        with (
            patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
            patch("sport_sync_bridge.cli.configure_logging"),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(main(["health", "import-readiness", str(source)]), 0)
        self.assertIn("records_added=1", output.getvalue())

        output = io.StringIO()
        with (
            patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
            patch("sport_sync_bridge.cli.configure_logging"),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(main(["health", "readiness", "--format", "json"]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["records"][0]["data"]["score"], 74.5)

    def test_cli_fetches_garmin_readiness_into_the_local_history(self) -> None:
        config = SimpleNamespace(
            data_dir=self.root / ".data",
            db_path=self.root / "garmin-state.db",
            log_level="INFO",
            log_path=self.root / "sync.log",
        )
        client = SimpleNamespace(
            get_training_readiness=lambda cdate: [
                {**self._sample_record(), "calendarDate": cdate}
            ]
        )
        authenticate = Mock()
        target = SimpleNamespace(client=client, authenticate=authenticate)
        output = io.StringIO()
        with (
            patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
            patch("sport_sync_bridge.cli.configure_logging"),
            patch("sport_sync_bridge.cli.GarminTarget", return_value=target) as target_factory,
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(
                main(
                    [
                        "health",
                        "fetch-readiness",
                        "--start-date",
                        "2026-08-03",
                        "--end-date",
                        "2026-08-03",
                    ]
                ),
                0,
            )

        target_factory.assert_called_once_with(config)
        authenticate.assert_called_once_with()
        self.assertIn("records_fetched=1", output.getvalue())
        self.assertIn("records_added=1", output.getvalue())
        imported_state = StateDB(config.db_path)
        try:
            summary = summarize_training_readiness(imported_state)
        finally:
            imported_state.close()
        record = summary["records"][0]
        self.assertEqual(record["calendar_date"], "2026-08-03")
        self.assertEqual(record["source_id"], "garmin-local")
        self.assertEqual(record["source_label"], "Garmin Connect training readiness")

    def _sample_record(self) -> dict[str, object]:
        return {
            "id": "readiness-2026-08-03",
            "sourceId": "garmin-local",
            "calendarDate": "2026-08-03",
            "timestamp": "2026-08-03T05:00:00+08:00",
            "timestampLocal": "2026-08-03T05:00:00+08:00",
            "deviceId": "device-1",
            "level": "Moderate",
            "feedbackShort": "保持轻松训练",
            "feedbackLong": "近期睡眠和压力数据可用。",
            "score": 74.5,
            "sleepScore": 82,
            "recoveryTime": 12,
            "recoveryTimeFactorPercent": 80,
            "recoveryTimeFactorFeedback": "恢复时间正常",
            "acwrFactorPercent": 75,
            "acwrFactorFeedback": "急性负荷适中",
            "acuteLoad": 420,
            "stressHistoryFactorPercent": 72,
            "stressHistoryFactorFeedback": "压力稳定",
            "hrvFactorPercent": 76,
            "hrvFactorFeedback": "HRV 在基线范围",
            "hrvWeeklyAverage": 54,
            "sleepHistoryFactorPercent": 85,
            "sleepHistoryFactorFeedback": "睡眠充足",
            "validSleep": True,
            "inputContext": {"context_source": "daily health summary"},
            "metadata": {"device": "watch"},
        }

    def _write_json(self, name: str, value: object) -> Path:
        path = self.root / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path


if __name__ == "__main__":
    unittest.main()
