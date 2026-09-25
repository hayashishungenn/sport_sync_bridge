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
from sport_sync_bridge.ai_profile import (
    load_ai_athlete_profile,
    reset_ai_athlete_profile,
    save_ai_athlete_profile,
)
from sport_sync_bridge.cli import main
from sport_sync_bridge.state import StateDB
from tests.activity_fixtures import create_gpx


class AiAthleteProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = StateDB(self.root / "state.db")

    def tearDown(self) -> None:
        self.state.close()
        self.temporary.cleanup()

    def test_profile_defaults_partial_updates_clear_and_reset(self) -> None:
        self.assertEqual(load_ai_athlete_profile(self.state), {})
        saved = save_ai_athlete_profile(
            self.state,
            {"gender": "FEMALE", "age": 32, "weight_kg": 58.5, "ftp_w": 210},
        )
        self.assertEqual(
            saved,
            {"gender": "female", "age": 32, "weight_kg": 58.5, "ftp_w": 210},
        )
        self.assertEqual(
            save_ai_athlete_profile(self.state, {"height_cm": 165}, clear_fields=("ftp_w",)),
            {"gender": "female", "age": 32, "weight_kg": 58.5, "height_cm": 165},
        )
        self.assertEqual(reset_ai_athlete_profile(self.state), {})
        self.assertEqual(load_ai_athlete_profile(self.state), {})

    def test_profile_storage_rejects_malformed_json_unknown_fields_and_invalid_values(self) -> None:
        self.state.set_value("ai_athlete_profile", "[]")
        with self.assertRaisesRegex(ValueError, "JSON object"):
            load_ai_athlete_profile(self.state)
        self.state.set_value("ai_athlete_profile", '{"unknown":1}')
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            load_ai_athlete_profile(self.state)
        self.state.set_value("ai_athlete_profile", '{"age":true}')
        with self.assertRaisesRegex(ValueError, "integer"):
            load_ai_athlete_profile(self.state)
        self.state.set_value("ai_athlete_profile", "{}")

        for profile, message in (
            ({"gender": "unspecified"}, "female or male"),
            ({"gender": None}, "female or male"),
            ({"age": 0}, "between 1 and 120"),
            ({"max_hr_bpm": 251}, "between 30 and 250"),
            ({"weight_kg": float("inf")}, "finite number"),
            ({"ftp_w": "210"}, "must be a number"),
        ):
            with self.subTest(profile=profile), self.assertRaisesRegex(ValueError, message):
                save_ai_athlete_profile(self.state, profile)

        with self.assertRaisesRegex(ValueError, "Set at least one"):
            save_ai_athlete_profile(self.state, {})

    def test_cli_profile_management_persists_and_adds_only_configured_fields_to_prompt(self) -> None:
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
                    main(
                        [
                            "ai-profile",
                            "set",
                            "--gender",
                            "female",
                            "--age",
                            "32",
                            "--weight-kg",
                            "58.5",
                            "--ftp-w",
                            "210",
                        ]
                    ),
                    0,
                )
            self.assertEqual(
                json.loads(set_output.getvalue()),
                {"gender": "female", "age": 32, "weight_kg": 58.5, "ftp_w": 210.0},
            )

            show_output = io.StringIO()
            with contextlib.redirect_stdout(show_output):
                self.assertEqual(main(["ai-profile", "show"]), 0)
            self.assertEqual(json.loads(show_output.getvalue())["age"], 32)

            prompt_output = io.StringIO()
            with contextlib.redirect_stdout(prompt_output):
                self.assertEqual(
                    main(["ai-analysis", imported.fingerprint, "--prompt-only"]), 0
                )
            prompt = prompt_output.getvalue()
            self.assertIn("### Athlete Profile:", prompt)
            self.assertIn("gender: Female", prompt)
            self.assertIn("age: 32", prompt)
            self.assertIn("weight: 58.5 kg", prompt)
            self.assertIn("FTP: 210.0 W", prompt)
            self.assertNotIn("MaxHR:", prompt)

            clear_output = io.StringIO()
            with contextlib.redirect_stdout(clear_output):
                self.assertEqual(
                    main(["ai-profile", "set", "--clear", "ftp_w"]), 0
                )
            self.assertNotIn("ftp_w", json.loads(clear_output.getvalue()))

            reset_output = io.StringIO()
            with contextlib.redirect_stdout(reset_output):
                self.assertEqual(main(["ai-profile", "reset"]), 0)
            self.assertEqual(json.loads(reset_output.getvalue()), {})


if __name__ == "__main__":
    unittest.main()
