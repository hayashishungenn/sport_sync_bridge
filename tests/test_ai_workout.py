from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fit_tool.fit_file import FitFile

from sport_sync_bridge.ai_workout import (
    build_ai_single_workout_prompts,
    normalize_ai_workout,
    write_ai_workout_fit,
)
from sport_sync_bridge.cli import main
from sport_sync_bridge.training import list_workout_templates


def _workout_response() -> str:
    return json.dumps(
        {
            "workouts": {
                "wo_1": {
                    "workoutId": "tempo_run",
                    "name": "Tempo Run",
                    "sportType": "running",
                    "steps": [
                        {"intensity": "warmup", "duration": "15min", "target": "zone1 HR"},
                        {
                            "repeat": 2,
                            "steps": [
                                {"intensity": "interval", "duration": "1000m", "target": "4:30/km"},
                                {"intensity": "recovery", "duration": "2min", "target": "easy jog"},
                            ],
                        },
                        {"intensity": "cooldown", "duration": "10min", "target": "zone1 HR"},
                    ],
                }
            }
        }
    )


class AISingleWorkoutTests(unittest.TestCase):
    def test_prompt_contains_sport_rules_and_single_workout_shape(self) -> None:
        system, user = build_ai_single_workout_prompts(
            sport="running",
            task="Tempo intervals",
            target_mode="pace",
            target_duration="45min",
            target_pace="4:30/km",
            language="en-US",
        )

        self.assertIn("exactly one workout", system)
        self.assertIn('"workouts":{"wo_1"', user)
        self.assertIn('"sportType":"running"', user)
        self.assertIn("Target Pace: 4:30/km", user)
        self.assertIn("single exact target value", system)

    def test_normalizes_nested_repeats_and_estimates_paced_distance(self) -> None:
        workout = normalize_ai_workout(_workout_response(), sport="running")

        self.assertEqual(workout["workoutId"], "tempo_run")
        self.assertEqual(workout["estimatedDuration"], 2280.0)
        self.assertEqual(workout["estimatedDistance"], 2000.0)
        self.assertEqual(workout["steps"][1]["repeat"], 2)

    def test_repairs_ai_json_code_fences_and_trailing_commas(self) -> None:
        valid_json = _workout_response()
        response = "```json\n" + valid_json[:-1] + ",}\n```"

        workout = normalize_ai_workout(response, sport="running")

        self.assertEqual(workout["workoutId"], "tempo_run")

    def test_rejects_multiple_workouts_invalid_sport_and_bad_steps(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly one"):
            normalize_ai_workout(
                json.dumps({"workouts": {"one": {"steps": []}, "two": {"steps": []}}}),
                sport="running",
            )
        with self.assertRaisesRegex(ValueError, "sportType must be cycling"):
            normalize_ai_workout(
                json.dumps({"workouts": {"wo_1": {"name": "Wrong", "sportType": "running", "steps": [{"intensity": "active", "duration": "open"}]}}}),
                sport="cycling",
            )
        bad = json.loads(_workout_response())
        bad["workouts"]["wo_1"]["steps"][0]["duration"] = "soon"
        with self.assertRaisesRegex(ValueError, "Unsupported workout duration"):
            normalize_ai_workout(json.dumps(bad), sport="running")

    def test_writes_readable_fit_and_sidecar_and_lists_generated_workout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workout = normalize_ai_workout(_workout_response(), sport="running")
            output = write_ai_workout_fit(workout, root / "tempo_run.fit")
            decoded = FitFile.from_file(str(output))
            messages = [record.message for record in decoded.records if not record.is_definition]
            fit_steps = [message for message in messages if message.name == "workout_step"]
            fit_workout = next(message for message in messages if message.name == "workout")

            self.assertEqual(fit_workout.workout_name, "Tempo Run")
            self.assertEqual(fit_workout.num_valid_steps, 5)
            self.assertEqual(len(fit_steps), 5)
            self.assertEqual(fit_steps[0].target_hr_zone, 1)
            self.assertAlmostEqual(fit_steps[1].custom_target_speed_low, 1000 / 270, places=3)
            self.assertEqual(fit_steps[2].target_type, 2)
            self.assertIn("Target: easy jog", fit_steps[2].notes)
            self.assertEqual(fit_steps[3].duration_type, 6)
            self.assertEqual(fit_steps[3].duration_step, 1)
            self.assertEqual(fit_steps[3].target_repeat_steps, 2)

            sidecar = json.loads(Path(f"{output}.meta").read_text(encoding="utf-8"))
            self.assertEqual(sidecar["type"], "workout")
            self.assertTrue(sidecar["metadata"]["conversionLosses"])
            listed = list_workout_templates(generated_dir=root)
            self.assertTrue(any(item.fit_path == output for item in listed))

    def test_writes_fit_targets_using_logical_values_and_one_based_zones(self) -> None:
        response = json.dumps(
            {
                "workouts": {
                    "wo_1": {
                        "workoutId": "cycling_targets",
                        "name": "Cycling targets",
                        "sportType": "cycling",
                        "steps": [
                            {"intensity": "active", "duration": "10min", "target": "150 bpm"},
                            {"intensity": "interval", "duration": "5min", "target": "250 W"},
                            {"intensity": "active", "duration": "5min", "target": "90% FTP"},
                            {"intensity": "recovery", "duration": "2min", "target": "power zone 4"},
                        ],
                    }
                }
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            workout = normalize_ai_workout(response, sport="cycling")
            output = write_ai_workout_fit(workout, Path(directory) / "targets.fit")
            decoded = FitFile.from_file(str(output))
            steps = [
                record.message
                for record in decoded.records
                if not record.is_definition and record.message.name == "workout_step"
            ]

        self.assertEqual(steps[0].custom_target_heart_rate_low, 150)
        self.assertEqual(steps[0].custom_target_heart_rate_high, 150)
        self.assertEqual(steps[1].custom_target_power_low, 250)
        self.assertEqual(steps[1].custom_target_power_high, 250)
        self.assertEqual(steps[2].custom_target_power_low, 90)
        self.assertEqual(steps[2].custom_target_power_high, 90)
        self.assertEqual(steps[3].target_power_zone, 4)

    def test_does_not_overwrite_existing_output_and_rejects_target_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "existing.fit"
            output.write_bytes(b"keep")
            workout = normalize_ai_workout(_workout_response(), sport="running")
            with self.assertRaises(FileExistsError):
                write_ai_workout_fit(workout, output)
            self.assertEqual(output.read_bytes(), b"keep")

            ranged = json.loads(_workout_response())
            ranged["workouts"]["wo_1"]["steps"][0]["target"] = "85-95% FTP"
            invalid = normalize_ai_workout(json.dumps(ranged), sport="running")
            with self.assertRaisesRegex(ValueError, "target ranges"):
                write_ai_workout_fit(invalid, root / "range.fit")
            self.assertFalse((root / "range.fit").exists())

    def test_cli_prompt_only_skips_ai_and_cli_generation_creates_browsable_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = SimpleNamespace(
                data_dir=root / ".data",
                ai_api_base_url="https://ai.example/v1",
                ai_model="test-model",
                ai_api_key=None,
                log_level="INFO",
                log_path=root / "sync.log",
            )
            prompt_output = io.StringIO()
            with (
                patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
                patch("sport_sync_bridge.cli.configure_logging"),
                patch("sport_sync_bridge.activity_analysis.request_ai_analysis") as request,
                contextlib.redirect_stdout(prompt_output),
            ):
                self.assertEqual(
                    main(["workouts", "generate", "--sport", "running", "--task", "Tempo intervals", "--prompt-only"]),
                    0,
                )
            request.assert_not_called()
            self.assertIn("USER PROMPT", prompt_output.getvalue())
            self.assertFalse((config.data_dir / "generated_workouts").exists())

            response_with_repairs = "```json\n" + _workout_response()[:-1] + ",}\n```"
            with (
                patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
                patch("sport_sync_bridge.cli.configure_logging"),
                patch(
                    "sport_sync_bridge.activity_analysis.request_ai_analysis",
                    return_value=response_with_repairs,
                ) as request,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    main(["workouts", "generate", "--sport", "running", "--task", "Tempo intervals"]),
                    0,
                )
            request.assert_called_once()
            generated = list_workout_templates(generated_dir=config.data_dir / "generated_workouts")
            generated_output = config.data_dir / "generated_workouts"
            generated_item = next(item for item in generated if item.fit_path.parent == generated_output)
            self.assertEqual(generated_item.name, "Tempo Run")
            self.assertTrue(generated_item.fit_path.is_file())


if __name__ == "__main__":
    unittest.main()
