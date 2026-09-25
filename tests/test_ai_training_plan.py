from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sport_sync_bridge.ai_training_plan import (
    WEEKDAYS,
    build_ai_training_plan_prompt,
    normalize_ai_training_plan,
)
from sport_sync_bridge.cli import build_parser, main
from sport_sync_bridge.state import StateDB
from sport_sync_bridge.training import materialize_schedule


def _plan_payload(sport: str, weeks: int, weekly_days: int) -> dict[str, object]:
    days: dict[str, object] = {}
    active_weekdays = set(WEEKDAYS[:weekly_days])
    for weekday in WEEKDAYS:
        if weekday not in active_weekdays:
            days[weekday] = {"name": "Rest", "isRestDay": True}
        elif sport == "duathlon":
            sessions = [
                {
                    "name": f"{sport} {weekday} run",
                    "sportType": "running",
                    "description": "Easy aerobic session",
                }
            ]
            if weekday == WEEKDAYS[0]:
                sessions.append(
                    {
                        "name": "Brick ride",
                        "sportType": "cycling",
                        "description": "Easy ride after the run",
                    }
                )
            days[weekday] = {"name": "Training day", "isRestDay": False, "workouts": sessions}
        else:
            days[weekday] = {
                "name": f"{sport} {weekday}",
                "sportType": sport,
                "isRestDay": False,
                "intensity": "easy",
                "targetDuration": "40min",
                "description": "Easy aerobic session",
            }
    return {
        "type": "structuredPlan",
        "trainingPlan": {
            "name": f"{sport} plan",
            "sportType": sport,
            "goal": "Build endurance",
            "description": "A progressive plan",
            "targetMetrics": {},
        },
        "weekTemplates": [
            {"name": "Progressive phase", "applyToWeeks": list(range(1, weeks + 1)), "days": days}
        ],
        "overrides": [],
    }


class AITrainingPlanTests(unittest.TestCase):
    def test_prompt_expresses_requested_goal_and_structured_schedule(self) -> None:
        prompt = build_ai_training_plan_prompt(
            sport="running",
            weeks=12,
            weekly_days=4,
            goal="Finish a half marathon",
            start_date=date(2026, 10, 5),
            language="en-US",
            event_name="Autumn Half",
            target_distance_km=21.1,
        )

        self.assertIn("Plan length: 12 weeks", prompt)
        self.assertIn("Training days per week: exactly 4", prompt)
        self.assertIn("Autumn Half", prompt)
        self.assertIn('"weekTemplates"', prompt)
        self.assertIn("do not add markdown fences", prompt)
        self.assertNotIn("endurance rides", prompt)

    def test_prompt_includes_only_aggregated_history_when_explicitly_provided(self) -> None:
        prompt = build_ai_training_plan_prompt(
            sport="cycling",
            weeks=8,
            weekly_days=3,
            goal="Improve endurance",
            start_date=date(2026, 10, 5),
            recent_training={
                "activity_count": 2,
                "distance_m": 12000,
                "weeks": {"2026-09-07": {"activities": 2, "distance_m": 12000}},
            },
        )

        self.assertIn("activity_count", prompt)
        self.assertIn("no names, routes, or coordinates", prompt)
        self.assertNotIn("gps", prompt.lower())

    def test_normalizes_fenced_plan_and_validates_complete_weeks(self) -> None:
        payload = _plan_payload("running", 2, 3)
        normalized = normalize_ai_training_plan(
            f"```json\n{json.dumps(payload)}\n```",
            sport="running",
            weeks=2,
            weekly_days=3,
            goal="Build endurance",
        )

        self.assertEqual(normalized["type"], "structuredPlan")
        self.assertTrue(str(normalized["id"]).startswith("ai_"))
        schedule = materialize_schedule(normalized, date(2026, 10, 5))
        self.assertEqual(len(schedule), 14)
        self.assertEqual(sum(item["item_type"] == "workout" for item in schedule), 6)

    def test_rejects_missing_week_or_wrong_active_day_count(self) -> None:
        payload = _plan_payload("running", 2, 3)
        payload["weekTemplates"] = [
            {"name": "Only week one", "applyToWeeks": [1], "days": payload["weekTemplates"][0]["days"]}
        ]
        with self.assertRaisesRegex(ValueError, "does not cover week 2"):
            normalize_ai_training_plan(
                json.dumps(payload),
                sport="running",
                weeks=2,
                weekly_days=3,
                goal="Build endurance",
            )

        payload = _plan_payload("running", 2, 2)
        with self.assertRaisesRegex(ValueError, "exactly 3 were requested"):
            normalize_ai_training_plan(
                json.dumps(payload),
                sport="running",
                weeks=2,
                weekly_days=3,
                goal="Build endurance",
            )

        payload = _plan_payload("running", 2, 3)
        payload["weekTemplates"][0]["days"]["Mon"]["isRestDay"] = True
        payload["weekTemplates"][0]["days"]["Mon"]["workouts"] = [
            {"name": "Invalid session", "sportType": "running", "description": "Invalid"}
        ]
        with self.assertRaisesRegex(ValueError, "rest day Mon cannot contain workouts"):
            normalize_ai_training_plan(
                json.dumps(payload),
                sport="running",
                weeks=2,
                weekly_days=3,
                goal="Build endurance",
            )

    def test_multisport_plan_expands_brick_day_to_distinct_schedule_items(self) -> None:
        payload = _plan_payload("duathlon", 2, 3)
        normalized = normalize_ai_training_plan(
            json.dumps(payload),
            sport="duathlon",
            weeks=2,
            weekly_days=3,
            goal="Complete a duathlon",
        )

        schedule = materialize_schedule(normalized, date(2026, 10, 5))
        first_day = [item for item in schedule if item["scheduled_date"] == "2026-10-05"]
        self.assertEqual(len(first_day), 2)
        self.assertEqual(len({item["item_id"] for item in first_day}), 2)
        self.assertEqual({item["sport_type"] for item in first_day}, {"running", "cycling"})
        self.assertEqual(sum(item["item_type"] == "workout" for item in schedule), 8)

    def test_cli_prompt_only_does_not_call_ai_or_install_a_plan(self) -> None:
        args = [
            "plans",
            "generate",
            "--sport",
            "running",
            "--weeks",
            "2",
            "--weekly-days",
            "3",
            "--goal",
            "Build endurance",
            "--start-date",
            "2026-10-05",
            "--prompt-only",
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = SimpleNamespace(
                data_dir=root / ".data",
                db_path=root / ".data" / "state.db",
                log_level="INFO",
                log_path=root / "sync.log",
            )
            output = io.StringIO()
            with (
                patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
                patch("sport_sync_bridge.cli.configure_logging"),
                patch("sport_sync_bridge.activity_analysis.request_ai_analysis") as request,
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(main(args), 0)
            request.assert_not_called()
            self.assertIn("trainingPlan", output.getvalue())
            self.assertFalse(config.db_path.exists())

    def test_cli_generates_json_and_installs_local_schedule(self) -> None:
        payload = _plan_payload("running", 2, 3)
        args = [
            "plans",
            "generate",
            "--sport",
            "running",
            "--weeks",
            "2",
            "--weekly-days",
            "3",
            "--goal",
            "Build endurance",
            "--start-date",
            "2026-10-05",
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = SimpleNamespace(
                data_dir=root / ".data",
                db_path=root / ".data" / "state.db",
                log_level="INFO",
                log_path=root / "sync.log",
                ai_api_base_url="https://ai.example/v1",
                ai_api_key="local-test-key",
                ai_model="local-test-model",
            )
            output = io.StringIO()
            with (
                patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
                patch("sport_sync_bridge.cli.configure_logging"),
                patch(
                    "sport_sync_bridge.activity_analysis.request_ai_analysis",
                    return_value=json.dumps(payload),
                ) as request,
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(main(args), 0)

            request.assert_called_once()
            self.assertIn("只返回 JSON", request.call_args.kwargs["system_prompt"])
            self.assertEqual(request.call_args.kwargs["temperature"], 0.4)
            self.assertNotIn("GPS coordinates", request.call_args.kwargs["prompt"])
            result = json.loads(output.getvalue())
            plan_path = Path(result["plan_json"])
            self.assertTrue(plan_path.is_file())
            saved_plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertTrue(saved_plan["id"].startswith("ai_"))

            state = StateDB(config.db_path)
            try:
                plans = state.list_training_plans()
                self.assertEqual(len(plans), 1)
                schedule = state.get_schedule_items(plans[0]["plan_id"])
                self.assertEqual(len(schedule), 14)
                self.assertEqual(sum(item["item_type"] == "workout" for item in schedule), 6)
            finally:
                state.close()

    def test_parser_exposes_generate_command(self) -> None:
        parsed = build_parser().parse_args(
            [
                "plans",
                "generate",
                "--sport",
                "triathlon",
                "--weeks",
                "12",
                "--weekly-days",
                "5",
                "--goal",
                "Finish a triathlon",
                "--start-date",
                "2026-10-05",
            ]
        )
        self.assertEqual(parsed.plans_action, "generate")
        self.assertEqual(parsed.sport, "triathlon")


if __name__ == "__main__":
    unittest.main()
