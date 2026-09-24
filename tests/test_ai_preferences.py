from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sport_sync_bridge.activity_library import LocalActivityLibrary
from sport_sync_bridge.ai_preferences import (
    load_ai_analysis_preferences,
    reset_ai_analysis_preferences,
    save_ai_analysis_preferences,
)
from sport_sync_bridge.cli import main
from sport_sync_bridge.state import StateDB
from tests.activity_fixtures import create_gpx


class AiAnalysisPreferenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = StateDB(self.root / "state.db")

    def tearDown(self) -> None:
        self.state.close()
        self.temporary.cleanup()

    def test_preferences_default_persist_partial_update_and_reset(self) -> None:
        self.assertEqual(
            load_ai_analysis_preferences(self.state),
            {"focus": "performance", "detail": "normal"},
        )
        self.assertEqual(
            save_ai_analysis_preferences(self.state, focus="recovery", detail="brief"),
            {"focus": "recovery", "detail": "brief"},
        )
        self.assertEqual(
            save_ai_analysis_preferences(self.state, detail="detailed"),
            {"focus": "recovery", "detail": "detailed"},
        )
        self.assertEqual(
            reset_ai_analysis_preferences(self.state),
            {"focus": "performance", "detail": "normal"},
        )
        with self.assertRaisesRegex(ValueError, "at least one"):
            save_ai_analysis_preferences(self.state)
        with self.assertRaisesRegex(ValueError, "must be"):
            save_ai_analysis_preferences(self.state, focus="unknown")

    def test_saved_preferences_apply_to_prompt_and_explicit_values_override(self) -> None:
        activity_path = create_gpx(self.root / "activity.gpx")
        library = LocalActivityLibrary(self.state, self.root / ".data")
        imported = library.import_paths([activity_path])[0]
        config = SimpleNamespace(
            data_dir=self.root / ".data",
            db_path=self.state.path,
            log_level="INFO",
            log_path=self.root / "sync.log",
        )
        with (
            patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
            patch("sport_sync_bridge.cli.configure_logging"),
        ):
            set_output = io.StringIO()
            with contextlib.redirect_stdout(set_output):
                self.assertEqual(
                    main(["ai-settings", "set", "--focus", "recovery", "--detail", "brief"]),
                    0,
                )
            self.assertEqual(
                json.loads(set_output.getvalue()),
                {"focus": "recovery", "detail": "brief"},
            )

            settings_output = io.StringIO()
            with contextlib.redirect_stdout(settings_output):
                self.assertEqual(main(["ai-settings", "show"]), 0)
            self.assertEqual(
                json.loads(settings_output.getvalue()),
                {"focus": "recovery", "detail": "brief"},
            )

            saved_prompt_output = io.StringIO()
            with contextlib.redirect_stdout(saved_prompt_output):
                self.assertEqual(main(["ai-analysis", imported.fingerprint, "--prompt-only"]), 0)
            saved_prompt = saved_prompt_output.getvalue()
            self.assertIn("重点评估恢复状态", saved_prompt)
            self.assertIn("使用纯文本", saved_prompt)

            override_prompt_output = io.StringIO()
            with contextlib.redirect_stdout(override_prompt_output):
                self.assertEqual(
                    main(
                        [
                            "ai-analysis",
                            imported.fingerprint,
                            "--prompt-only",
                            "--focus",
                            "health",
                            "--detail",
                            "detailed",
                        ]
                    ),
                    0,
                )

        self.assertIn("重点关注训练负荷的影响和长期健康价值", override_prompt_output.getvalue())
        self.assertIn("训练分析覆盖 3-5 个维度", override_prompt_output.getvalue())
        self.assertEqual(
            load_ai_analysis_preferences(self.state),
            {"focus": "recovery", "detail": "brief"},
        )

        reset_output = io.StringIO()
        with (
            patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
            patch("sport_sync_bridge.cli.configure_logging"),
            contextlib.redirect_stdout(reset_output),
        ):
            self.assertEqual(main(["ai-settings", "reset"]), 0)
        self.assertEqual(
            json.loads(reset_output.getvalue()),
            {"focus": "performance", "detail": "normal"},
        )

    def test_invalid_saved_preferences_are_not_silently_ignored(self) -> None:
        self.state.set_value("ai_analysis_preferences", '{"focus":"unsupported"}')
        with self.assertRaisesRegex(ValueError, "focus must be"):
            load_ai_analysis_preferences(self.state)


if __name__ == "__main__":
    unittest.main()
