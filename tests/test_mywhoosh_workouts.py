from __future__ import annotations

import base64
import contextlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests

from sport_sync_bridge.mywhoosh_source import MyWhooshSource
from sport_sync_bridge.mywhoosh_workouts import (
    build_mywhoosh_workout_data,
    stable_mywhoosh_workout_id,
    upload_mywhoosh_workout,
)
from sport_sync_bridge.training import WorkoutTemplate, list_workout_templates
from sport_sync_bridge.cli import main


class _Response:
    def __init__(self, payload: object | None = None, *, status: int = 200):
        self.payload = payload
        self.status_code = status
        self.content = b"" if payload is None else json.dumps(payload).encode("utf-8")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self) -> object:
        if self.payload is None:
            raise ValueError("missing JSON")
        return self.payload


class _Session:
    def __init__(self, *, get=None, post=None, delete=None):
        self.headers: dict[str, str] = {}
        self.responses = {
            "get": list(get or []),
            "post": list(post or []),
            "delete": list(delete or []),
        }
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def _send(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses[method].pop(0)

    def get(self, url: str, **kwargs):
        return self._send("get", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self._send("post", url, **kwargs)

    def delete(self, url: str, **kwargs):
        return self._send("delete", url, **kwargs)


class _State:
    def get_value(self, key: str) -> str | None:
        return None

    def set_value(self, key: str, value: str) -> None:
        pass


def _token(*, user_id: object = "account-1") -> str:
    payload = {"exp": time.time() + 3600, "userId": user_id}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


def _template(
    *,
    steps: tuple[dict[str, object], ...] | None = None,
    sport: str = "cycling",
    identifier: str = "interval-workout",
) -> WorkoutTemplate:
    return WorkoutTemplate(
        template_id=identifier,
        name="Tempo intervals",
        sport_type=sport,
        estimated_duration_s=1800,
        estimated_distance_m=None,
        steps=steps or (
            {"intensity": "warmup", "duration": "5min", "target": "60% FTP", "notes": "Easy"},
            {"intensity": "interval", "duration": "20min", "target": "85% FTP"},
            {"intensity": "cooldown", "duration": "5min"},
        ),
        fit_path=Path("workout.fit"),
        description="A threshold session",
    )


def _source(session: _Session, *, token: str | None = None) -> MyWhooshSource:
    source = MyWhooshSource(
        SimpleNamespace(mywhoosh_username=None, mywhoosh_password=None),
        _State(),
    )
    source.session = session
    access_token = token or _token()
    source._access_token_cache = access_token
    source._access_token_expiry = time.time() + 3600
    return source


class MyWhooshWorkoutSerializationTests(unittest.TestCase):
    def test_maps_cycling_steps_and_preserves_supported_workout_fields(self) -> None:
        workout, losses = build_mywhoosh_workout_data(_template(), 123456)

        self.assertEqual(workout["Id"], 123456)
        self.assertEqual(workout["Name"], "Tempo intervals")
        self.assertEqual(workout["Description"], "A threshold session")
        self.assertEqual(workout["Time"], 1800)
        self.assertEqual(workout["StepCount"], 3)
        self.assertEqual(workout["WorkoutStepsArray"][0]["StepType"], "E_WarmUp")
        self.assertEqual(workout["WorkoutStepsArray"][1]["Power"], 0.85)
        self.assertEqual(workout["WorkoutStepsArray"][2]["StepType"], "E_CoolDown")
        self.assertEqual(workout["WorkoutStepsArray"][0]["WorkoutMessage"], [])
        self.assertIn("notes were omitted", losses[0])
        self.assertTrue(any("IF, TSS, and KJ" in loss for loss in losses))

    def test_expands_repeats_and_records_the_structural_loss(self) -> None:
        template = _template(
            steps=(
                {
                    "repeat": 3,
                    "steps": [
                        {"intensity": "interval", "duration": "30sec", "target": "100% FTP"},
                        {"intensity": "recovery", "duration": "30sec", "target": "50% FTP"},
                    ],
                },
            )
        )

        workout, losses = build_mywhoosh_workout_data(template)

        rows = workout["WorkoutStepsArray"]
        self.assertEqual(len(rows), 6)
        self.assertEqual([row["IntervalId"] for row in rows], [1, 1, 2, 2, 3, 3])
        self.assertTrue(workout["IsIntervals"])
        self.assertEqual(workout["Time"], 180)
        self.assertTrue(any("Repeat groups were expanded" in loss for loss in losses))

    def test_unmapped_target_is_omitted_and_reported(self) -> None:
        template = _template(
            steps=({"intensity": "active", "duration": "10min", "target": "250W"},)
        )
        workout, losses = build_mywhoosh_workout_data(template)

        self.assertIsNone(workout["WorkoutStepsArray"][0]["Power"])
        self.assertTrue(any("250W" in loss and "omitted" in loss for loss in losses))

    def test_rejects_noncycling_open_and_distance_workouts(self) -> None:
        with self.assertRaisesRegex(ValueError, "only accepts cycling"):
            build_mywhoosh_workout_data(_template(sport="running"))
        with self.assertRaisesRegex(ValueError, "finite time duration"):
            build_mywhoosh_workout_data(
                _template(steps=({"intensity": "active", "duration": "open"},))
            )
        with self.assertRaisesRegex(ValueError, "finite time duration"):
            build_mywhoosh_workout_data(
                _template(steps=({"intensity": "active", "duration": "1000m"},))
            )

    def test_id_is_stable_for_a_generated_template(self) -> None:
        self.assertEqual(
            stable_mywhoosh_workout_id("same-template"),
            stable_mywhoosh_workout_id("same-template"),
        )
        self.assertNotEqual(
            stable_mywhoosh_workout_id("same-template"),
            stable_mywhoosh_workout_id("other-template"),
        )

    def test_reads_description_from_generated_workout_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "local-ride.fit").write_bytes(b"FIT")
            (root / "local-ride.fit.meta").write_text(
                json.dumps(
                    {
                        "id": "local-ride",
                        "name": "Local ride",
                        "sportType": "cycling",
                        "description": "Keep this description",
                        "steps": [{"intensity": "active", "duration": "10min"}],
                    }
                ),
                encoding="utf-8",
            )

            template = next(
                item
                for item in list_workout_templates(generated_dir=root)
                if item.template_id == "local-ride"
            )

        self.assertEqual(template.description, "Keep this description")

    def test_upload_checks_for_existing_remote_id_before_posting(self) -> None:
        template = _template()
        remote_id = stable_mywhoosh_workout_id(template.template_id)
        source = _source(
            _Session(get=[_Response({"data": [{"WorkoutId": remote_id, "Name": template.name}]})])
        )

        result = upload_mywhoosh_workout(source, template)

        self.assertEqual(result["status"], "already_exists")
        self.assertEqual(len(source.session.calls), 1)


class MyWhooshWorkoutApiTests(unittest.TestCase):
    def test_lists_workouts_from_the_reversed_coaching_endpoint(self) -> None:
        session = _Session(get=[_Response({"data": [{"WorkoutId": 123, "Name": "Ride"}]})])
        source = _source(session)

        workouts = source.list_workouts()

        self.assertEqual(workouts, [{"WorkoutId": 123, "Name": "Ride"}])
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "get")
        self.assertEqual(
            url,
            "https://coaching.mywhoosh.com/api/v2/workout-builder/my-workouts",
        )
        self.assertEqual(kwargs["headers"]["Source"], "connect")

    def test_upload_wraps_workout_with_user_and_sports_mode(self) -> None:
        session = _Session(post=[_Response({"success": True}, status=201)])
        source = _source(session)

        source.upload_workout({"Id": 123, "Name": "Ride"})

        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "post")
        self.assertEqual(
            url,
            "https://coaching.mywhoosh.com/api/v2/client/custom-workout-upload",
        )
        self.assertEqual(
            kwargs["json"],
            {
                "UserId": "account-1",
                "SportsModeType": 0,
                "WorkoutsData": [{"Id": 123, "Name": "Ride"}],
            },
        )

    def test_delete_uses_token_user_id_and_remote_workout_id(self) -> None:
        session = _Session(delete=[_Response(status=200)])
        token = _token(user_id="account/one")
        source = _source(session, token=token)

        status = source.delete_workout("123456")

        self.assertEqual(status, 200)
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "delete")
        self.assertEqual(
            url,
            "https://coaching.mywhoosh.com/api/v2/client/custom-workout-upload/account%2Fone/123456",
        )
        self.assertEqual(kwargs["headers"]["Authorization"], f"Bearer {token}")

    def test_upload_requires_user_id_claim(self) -> None:
        session = _Session()
        source = _source(session, token=_token(user_id=None))

        with self.assertRaisesRegex(RuntimeError, "user ID"):
            source.upload_workout({"Id": 1})
        self.assertEqual(session.calls, [])

    def test_delete_rejects_non_ascii_ids_before_request(self) -> None:
        session = _Session()
        source = _source(session)

        with self.assertRaisesRegex(ValueError, "positive integer"):
            source.delete_workout("１２３")
        self.assertEqual(session.calls, [])

    def test_cli_parser_exposes_mywhoosh_workout_actions(self) -> None:
        from sport_sync_bridge.cli import build_parser

        args = build_parser().parse_args(["workouts", "mywhoosh", "upload", "tempo-ride"])

        self.assertEqual(args.workouts_action, "mywhoosh")
        self.assertEqual(args.mywhoosh_workout_action, "upload")
        self.assertEqual(args.template_id, "tempo-ride")

    def test_cli_upload_runs_through_local_template_and_mywhoosh_client(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = SimpleNamespace(
                data_dir=root / ".data",
                db_path=root / "state.sqlite",
                log_level="INFO",
                log_path=root / "sync.log",
            )
            state = MagicMock()
            source = MagicMock()
            source.list_workouts.return_value = []
            template = _template(identifier="local-ride")
            stdout = io.StringIO()
            with (
                patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
                patch("sport_sync_bridge.cli.configure_logging"),
                patch("sport_sync_bridge.cli.StateDB", return_value=state),
                patch("sport_sync_bridge.cli.MyWhooshSource", return_value=source),
                patch("sport_sync_bridge.cli.get_workout_template", return_value=template) as get_template,
                contextlib.redirect_stdout(stdout),
            ):
                result = main(["workouts", "mywhoosh", "upload", "local-ride"])

        self.assertEqual(result, 0)
        self.assertEqual(get_template.call_args.args[0], "local-ride")
        source.upload_workout.assert_called_once()
        output = json.loads(stdout.getvalue())
        self.assertEqual(output["status"], "uploaded")
        self.assertEqual(output["name"], "Tempo intervals")
        state.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
