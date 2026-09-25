from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sport_sync_bridge.cli import build_parser, main
from sport_sync_bridge.health import (
    import_garmin_health_details,
    list_garmin_health_details,
    summarize_health,
)
from sport_sync_bridge.health_sources import (
    GARMIN_HEALTH_DETAIL_ENDPOINTS,
    fetch_garmin_health_details,
    validate_garmin_health_detail_date_range,
)
from sport_sync_bridge.state import StateDB


class _FakeGarminClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def connectapi(self, path: str, **kwargs: object) -> object:
        self.calls.append((path, dict(kwargs)))
        params = kwargs.get("params")
        if isinstance(params, dict) and "date" in params:
            calendar_date = str(params["date"])
            dataset = next(
                name
                for name, endpoint in GARMIN_HEALTH_DETAIL_ENDPOINTS.items()
                if path == endpoint
            )
        else:
            calendar_date = path.rsplit("/", 1)[-1]
            dataset = next(
                name
                for name, endpoint in GARMIN_HEALTH_DETAIL_ENDPOINTS.items()
                if path.startswith(endpoint)
            )
        if dataset == "sleep":
            return {
                "dailySleepDTO": {
                    "calendarDate": calendar_date,
                    "sleepTimeSeconds": 25200,
                }
            }
        if dataset == "hrv":
            return None if calendar_date == "2026-08-03" else {"calendarDate": calendar_date}
        if dataset == "stress":
            return {"calendarDate": calendar_date, "avgStressLevel": 21}
        if dataset == "body-battery":
            return {"calendarDate": calendar_date, "bodyBatteryValuesArray": []}
        if dataset == "respiration":
            return {"calendarDate": calendar_date}
        if dataset == "hydration":
            return {"calendarDate": calendar_date, "valueInML": 1500}
        if dataset == "blood-pressure":
            return {"calendarDate": calendar_date, "bloodPressureMeasurements": []}
        if dataset == "heart-rate":
            return {
                "calendarDate": calendar_date,
                "restingHR": 57,
                "wellnessMaxAvgHR": 190,
                "wellnessMinAvgHR": 45,
            }
        if dataset == "fitness-age":
            return {
                "calendarDate": calendar_date,
                "fitnessAge": 31,
                "achievableFitnessAge": 29,
            }
        if dataset == "spo2-acclimation":
            return {
                "calendarDate": calendar_date,
                "averageSpO2": 97.5,
                "lowestSpO2": 90,
                "lastSevenDaysAvgSpo2": 96.7,
                "avgSleepSpo2": 95.2,
                "spO2HourlyAverages": [[1, 97], [2, 98]],
                "spo2DailyAverageArray": [[1, 97], [2, 98]],
            }
        if dataset == "floors-chart":
            return {
                "calendarDate": calendar_date,
                "floorsAscended": 17,
                "floorsDescended": 12,
                "floorsGoal": 10,
                "timeSeries": [],
            }
        if dataset == "steps":
            return {"calendarDate": calendar_date, "totalSteps": 8123, "dailyStepGoal": 9000}
        raise AssertionError(dataset)


class GarminHealthDetailTests(unittest.TestCase):
    def test_fetches_each_selected_dataset_for_inclusive_dates(self) -> None:
        client = _FakeGarminClient()

        records = fetch_garmin_health_details(
            client,
            ["sleep", "hrv", "body-battery", "blood-pressure"],
            "2026-08-03",
            "2026-08-04",
        )

        self.assertEqual(len(records), 8)
        self.assertEqual(
            client.calls,
            [
                (
                    "/sleep-service/sleep/dailySleepData",
                    {"params": {"date": "2026-08-03", "nonSleepBufferMinutes": 60}},
                ),
                ("/hrv-service/hrv/daily/2026-08-03/2026-08-03", {}),
                ("/wellness-service/wellness/bodyBattery/events/2026-08-03", {}),
                ("/bloodpressure-service/bloodpressure/dayview/2026-08-03", {}),
                (
                    "/sleep-service/sleep/dailySleepData",
                    {"params": {"date": "2026-08-04", "nonSleepBufferMinutes": 60}},
                ),
                ("/hrv-service/hrv/daily/2026-08-04/2026-08-04", {}),
                ("/wellness-service/wellness/bodyBattery/events/2026-08-04", {}),
                ("/bloodpressure-service/bloodpressure/dayview/2026-08-04", {}),
            ],
        )
        self.assertIsNone(records[1]["payload"])
        self.assertEqual(records[-1]["calendarDate"], "2026-08-04")

    def test_requests_remaining_confirmed_wellness_endpoints(self) -> None:
        client = _FakeGarminClient()

        fetch_garmin_health_details(
            client,
            ["stress", "respiration", "hydration"],
            "2026-08-03",
            "2026-08-03",
        )

        self.assertEqual(
            client.calls,
            [
                ("/wellness-service/wellness/dailyStress/2026-08-03", {}),
                ("/wellness-service/wellness/daily/respiration/2026-08-03", {}),
                ("/usersummary-service/usersummary/hydration/allData/2026-08-03", {}),
            ],
        )

    def test_fetches_additional_confirmed_garmin_health_endpoints(self) -> None:
        client = _FakeGarminClient()

        records = fetch_garmin_health_details(
            client,
            ["heart-rate", "fitness-age", "spo2-acclimation", "floors-chart", "steps"],
            "2026-08-03",
            "2026-08-03",
        )

        self.assertEqual(
            client.calls,
            [
                (
                    "/wellness-service/wellness/dailyHeartRate",
                    {"params": {"date": "2026-08-03"}},
                ),
                ("/fitnessage-service/fitnessage/2026-08-03", {}),
                (
                    "/wellness-service/wellness/daily/spo2acclimation/2026-08-03",
                    {},
                ),
                (
                    "/wellness-service/wellness/floorsChartData/daily/2026-08-03",
                    {},
                ),
                (
                    "/wellness-service/wellness/wellness-goals/consolidated/steps/2026-08-03",
                    {},
                ),
            ],
        )
        self.assertEqual(len(records), 5)
        self.assertEqual(records[0]["payload"]["restingHR"], 57)
        self.assertEqual(records[2]["payload"]["spo2DailyAverageArray"][0], [1, 97])

    def test_imports_fitness_age_heart_rate_and_floor_chart_metrics(self) -> None:
        records = [
            {
                "dataset": "heart-rate",
                "calendarDate": "2026-08-03",
                "payload": {
                    "restingHR": 57,
                    "wellnessMaxAvgHR": 190,
                    "wellnessMinAvgHR": 45,
                },
            },
            {
                "dataset": "fitness-age",
                "calendarDate": "2026-08-03",
                "payload": {"fitnessAge": 31, "achievableFitnessAge": 29},
            },
            {
                "dataset": "spo2-acclimation",
                "calendarDate": "2026-08-03",
                "payload": {
                    "averageSpO2": 97.5,
                    "lowestSpO2": 90,
                    "lastSevenDaysAvgSpo2": 96.7,
                    "avgSleepSpo2": 95.2,
                    "spO2HourlyAverages": [[1, 97], [2, 98]],
                    "spo2DailyAverageArray": [[1, 97], [2, 98]],
                },
            },
            {
                "dataset": "floors-chart",
                "calendarDate": "2026-08-03",
                "payload": {
                    "floorsAscended": 17,
                    "floorsDescended": 12,
                    "floorsGoal": 10,
                    "timeSeries": [],
                },
            },
            {
                "dataset": "steps",
                "calendarDate": "2026-08-03",
                "payload": {"totalSteps": 8123, "dailyStepGoal": 9000},
            },
        ]

        with tempfile.TemporaryDirectory() as directory:
            state = StateDB(Path(directory) / "health.db")
            try:
                imported = import_garmin_health_details(state, records)
                summary = summarize_health(state)
                stored = list_garmin_health_details(
                    state, "2026-08-03", "2026-08-03", "spo2-acclimation"
                )
            finally:
                state.close()

        latest = summary["latest"]
        self.assertEqual(imported["snapshots_stored"], 5)
        self.assertEqual(imported["observations_processed"], 14)
        self.assertEqual(latest["fitness_age_years"]["value"], 31)
        self.assertEqual(latest["achievable_fitness_age_years"]["value"], 29)
        self.assertEqual(latest["resting_hr_bpm"]["value"], 57)
        self.assertEqual(latest["max_heart_rate_bpm"]["value"], 190)
        self.assertEqual(latest["min_heart_rate_bpm"]["value"], 45)
        self.assertEqual(latest["floors"]["value"], 17)
        self.assertEqual(latest["floors_descended"]["value"], 12)
        self.assertEqual(latest["floors_goal"]["value"], 10)
        self.assertEqual(latest["steps"]["value"], 8123)
        self.assertEqual(latest["step_goal"]["value"], 9000)
        self.assertEqual(latest["spo2_percent"]["value"], 97.5)
        self.assertEqual(latest["spo2_low_percent"]["value"], 90)
        self.assertEqual(latest["spo2_7d_average_percent"]["value"], 96.7)
        self.assertEqual(latest["avg_sleep_spo2_percent"]["value"], 95.2)
        self.assertEqual(
            stored["records"][0]["payload"]["spo2DailyAverageArray"],
            [[1, 97], [2, 98]],
        )

    def test_rejects_bad_range_dataset_response_and_mismatched_date(self) -> None:
        client = _FakeGarminClient()
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            validate_garmin_health_detail_date_range("20260803", "2026-08-04")
        with self.assertRaisesRegex(ValueError, "on or after"):
            fetch_garmin_health_details(client, ["sleep"], "2026-08-04", "2026-08-03")
        with self.assertRaisesRegex(ValueError, "366 days"):
            validate_garmin_health_detail_date_range("2025-01-01", "2026-01-02")
        with self.assertRaisesRegex(ValueError, "must not be repeated"):
            fetch_garmin_health_details(
                client, ["sleep", "sleep"], "2026-08-03", "2026-08-03"
            )
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            fetch_garmin_health_details(
                client, ["weight"], "2026-08-03", "2026-08-03"
            )
        self.assertEqual(client.calls, [])

        bad_body_battery = SimpleNamespace(connectapi=lambda path, **kwargs: "invalid")
        with self.assertRaisesRegex(ValueError, "list of objects"):
            fetch_garmin_health_details(
                bad_body_battery, ["body-battery"], "2026-08-03", "2026-08-03"
            )
        empty_body_battery = SimpleNamespace(connectapi=lambda path, **kwargs: None)
        self.assertIsNone(
            fetch_garmin_health_details(
                empty_body_battery, ["body-battery"], "2026-08-03", "2026-08-03"
            )[0]["payload"]
        )

        mismatched = SimpleNamespace(
            connectapi=lambda path, **kwargs: {
                "dailySleepDTO": {"calendarDate": "2026-08-02"}
            }
        )
        with self.assertRaisesRegex(ValueError, "does not match requested date"):
            fetch_garmin_health_details(
                mismatched, ["sleep"], "2026-08-03", "2026-08-03"
            )

        with self.assertRaisesRegex(RuntimeError, "does not support"):
            fetch_garmin_health_details(
                object(), ["sleep"], "2026-08-03", "2026-08-03"
            )

    def test_imports_raw_details_normalizes_confirmed_fields_and_is_idempotent(self) -> None:
        records = [
            {
                "dataset": "sleep",
                "calendarDate": "2026-08-03",
                "payload": {
                    "dailySleepDTO": {
                        "calendarDate": "2026-08-03",
                        "sleepTimeSeconds": 25200,
                        "deepSleepSeconds": 6000,
                        "lightSleepSeconds": 12000,
                        "remSleepSeconds": 6000,
                        "awakeSleepSeconds": 1200,
                        "unmapped": {"kept": True},
                    }
                },
            },
            {
                "dataset": "hrv",
                "calendarDate": "2026-08-03",
                "payload": {"hrvSummary": {"lastNightAvg": 52, "weeklyAverage": 49}},
            },
            {
                "dataset": "stress",
                "calendarDate": "2026-08-03",
                "payload": {"avgStressLevel": 24, "maxStressLevel": 71},
            },
            {
                "dataset": "respiration",
                "calendarDate": "2026-08-03",
                "payload": {
                    "avgWakingRespirationValue": 14,
                    "avgSleepRespirationValue": 12,
                    "highestRespirationValue": 20,
                    "lowestRespirationValue": 9,
                },
            },
            {
                "dataset": "hydration",
                "calendarDate": "2026-08-03",
                "payload": {
                    "valueInML": 1500,
                    "goalInML": 2200,
                    "baseGoalInML": 2000,
                    "activityIntakeInML": 500,
                    "sweatLossInML": 300,
                },
            },
            {
                "dataset": "blood-pressure",
                "calendarDate": "2026-08-03",
                "payload": {
                    "bloodPressureMeasurements": [
                        {"systolic": 120, "diastolic": 80, "pulse": 57},
                        {"systolic": 122, "diastolic": 81, "pulse": 58},
                    ]
                },
            },
            {
                "dataset": "body-battery",
                "calendarDate": "2026-08-03",
                "payload": [{"bodyBatteryValuesArray": [[1, 44], [2, 75]]}],
            },
        ]

        with tempfile.TemporaryDirectory() as directory:
            state = StateDB(Path(directory) / "health.db")
            try:
                first = import_garmin_health_details(state, records)
                second = import_garmin_health_details(state, records)
                summary = summarize_health(state)
                stored = list_garmin_health_details(
                    state, "2026-08-03", "2026-08-03"
                )
                observations = state.list_health_observations()
            finally:
                state.close()

        self.assertEqual(first, {"snapshots_stored": 7, "observations_processed": 24})
        self.assertEqual(second, {"snapshots_stored": 0, "observations_processed": 24})
        self.assertEqual(len(observations), 24)
        latest = summary["latest"]
        self.assertEqual(latest["sleep_hours"]["value"], 7)
        self.assertEqual(latest["deep_sleep_seconds"]["value"], 6000)
        self.assertEqual(latest["hrv_ms"]["value"], 52)
        self.assertEqual(latest["hydration_l"]["value"], 1.5)
        self.assertEqual(latest["hydration_goal_l"]["value"], 2.2)
        self.assertEqual(latest["systolic_bp_mmhg"]["value"], 122)
        self.assertEqual(latest["pulse_bpm"]["value"], 58)
        self.assertEqual(stored["record_count"], 7)
        sleep_payload = next(
            item["payload"] for item in stored["records"] if item["dataset"] == "sleep"
        )
        self.assertTrue(sleep_payload["dailySleepDTO"]["unmapped"]["kept"])

    def test_changed_payload_replaces_old_observations_for_only_that_dataset_date(self) -> None:
        first = {
            "dataset": "sleep",
            "calendarDate": "2026-08-03",
            "payload": {"dailySleepDTO": {"sleepTimeSeconds": 3600}},
        }
        changed = {
            "dataset": "sleep",
            "calendarDate": "2026-08-03",
            "payload": {"dailySleepDTO": {"sleepTimeSeconds": 7200}},
        }
        with tempfile.TemporaryDirectory() as directory:
            state = StateDB(Path(directory) / "health.db")
            try:
                import_garmin_health_details(state, [first])
                import_garmin_health_details(state, [changed])
                rows = state.list_health_observations("sleep_hours")
                stored = list_garmin_health_details(
                    state, "2026-08-03", "2026-08-03", "sleep"
                )
            finally:
                state.close()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["value"], 2)
        self.assertEqual(stored["records"][0]["payload"]["dailySleepDTO"]["sleepTimeSeconds"], 7200)

    def test_validation_and_database_failures_do_not_partially_store_payloads(self) -> None:
        invalid_records = [
            {"dataset": "sleep", "calendarDate": "2026-08-03", "payload": {}},
            {"dataset": "sleep", "calendarDate": "2026-08-03", "payload": {}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            state = StateDB(Path(directory) / "health.db")
            try:
                with self.assertRaisesRegex(ValueError, "appears more than once"):
                    import_garmin_health_details(state, invalid_records)
                with self.assertRaisesRegex(ValueError, "does not match requested date"):
                    import_garmin_health_details(
                        state,
                        [
                            {
                                "dataset": "sleep",
                                "calendarDate": "2026-08-03",
                                "payload": {
                                    "dailySleepDTO": {"calendarDate": "2026-08-02"}
                                },
                            }
                        ],
                    )

                state.connection.executescript(
                    "CREATE TRIGGER reject_garmin_detail_sleep "
                    "BEFORE INSERT ON health_observations "
                    "WHEN NEW.metric = 'sleep_hours' "
                    "BEGIN SELECT RAISE(ABORT, 'test database failure'); END;"
                )
                with self.assertRaisesRegex(sqlite3.IntegrityError, "test database failure"):
                    import_garmin_health_details(
                        state,
                        [
                            {
                                "dataset": "sleep",
                                "calendarDate": "2026-08-03",
                                "payload": {
                                    "dailySleepDTO": {"sleepTimeSeconds": 25200}
                                },
                            }
                        ],
                    )
                stored = list_garmin_health_details(
                    state, "2026-08-03", "2026-08-03"
                )
                self.assertEqual(state.list_health_observations(), [])
                self.assertEqual(stored["record_count"], 0)
            finally:
                state.close()

    def test_cli_fetches_and_displays_garmin_detail_payloads(self) -> None:
        fetch_args = [
            "health",
            "fetch-garmin-details",
            "--dataset",
            "sleep",
            "--dataset",
            "hydration",
            "--start-date",
            "2026-08-03",
            "--end-date",
            "2026-08-03",
        ]
        self.assertEqual(
            build_parser().parse_args(fetch_args).health_action,
            "fetch-garmin-details",
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = SimpleNamespace(
                data_dir=root / ".data",
                db_path=root / "state.db",
                log_level="INFO",
                log_path=root / "sync.log",
            )
            client = _FakeGarminClient()
            authenticate = Mock()
            target = SimpleNamespace(client=client, authenticate=authenticate)
            output = io.StringIO()
            with (
                patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
                patch("sport_sync_bridge.cli.configure_logging"),
                patch("sport_sync_bridge.cli.GarminTarget", return_value=target),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(main(fetch_args), 0)
            authenticate.assert_called_once_with()
            self.assertIn("payloads_fetched=2", output.getvalue())
            self.assertIn("payloads_stored=2", output.getvalue())

            output = io.StringIO()
            with (
                patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
                patch("sport_sync_bridge.cli.configure_logging"),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(
                    main(
                        [
                            "health",
                            "details",
                            "--dataset",
                            "sleep",
                            "--start-date",
                            "2026-08-03",
                            "--end-date",
                            "2026-08-03",
                        ]
                    ),
                    0,
                )

        result = json.loads(output.getvalue())
        self.assertEqual(result["record_count"], 1)
        self.assertEqual(result["records"][0]["dataset"], "sleep")
        self.assertEqual(
            result["records"][0]["payload"]["dailySleepDTO"]["sleepTimeSeconds"],
            25200,
        )


if __name__ == "__main__":
    unittest.main()
