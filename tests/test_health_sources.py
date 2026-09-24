from __future__ import annotations

import unittest

from sport_sync_bridge.health_sources import (
    fetch_garmin_training_readiness,
    validate_training_readiness_date_range,
)


class _FakeGarminClient:
    def __init__(self, responses: dict[str, object]):
        self.responses = responses
        self.requested_dates: list[str] = []

    def get_training_readiness(self, cdate: str) -> object:
        self.requested_dates.append(cdate)
        return self.responses[cdate]


class GarminTrainingReadinessSourceTests(unittest.TestCase):
    def test_fetches_each_day_inclusive_and_fills_source_defaults(self) -> None:
        client = _FakeGarminClient(
            {
                "2026-08-03": [{"score": 72}],
                "2026-08-04": [
                    {"calendarDate": "2026-08-04", "sourceId": "garmin-device", "score": 65}
                ],
                "2026-08-05": [],
            }
        )

        records = fetch_garmin_training_readiness(client, "2026-08-03", "2026-08-05")

        self.assertEqual(
            client.requested_dates,
            ["2026-08-03", "2026-08-04", "2026-08-05"],
        )
        self.assertEqual(
            records,
            [
                {"score": 72, "calendarDate": "2026-08-03", "sourceId": "garmin"},
                {"calendarDate": "2026-08-04", "sourceId": "garmin-device", "score": 65},
            ],
        )

    def test_rejects_reversed_or_non_iso_date_ranges_before_requesting(self) -> None:
        client = _FakeGarminClient({})

        with self.assertRaisesRegex(ValueError, "on or after"):
            fetch_garmin_training_readiness(client, "2026-08-04", "2026-08-03")
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            validate_training_readiness_date_range("20260803", "2026-08-04")

        self.assertEqual(client.requested_dates, [])

    def test_rejects_unexpected_api_payloads(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be a list"):
            fetch_garmin_training_readiness(
                _FakeGarminClient({"2026-08-03": {"score": 72}}),
                "2026-08-03",
                "2026-08-03",
            )
        with self.assertRaisesRegex(ValueError, "must be an object"):
            fetch_garmin_training_readiness(
                _FakeGarminClient({"2026-08-03": [None]}),
                "2026-08-03",
                "2026-08-03",
            )

    def test_rejects_a_client_without_the_readiness_endpoint(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "does not support"):
            fetch_garmin_training_readiness(
                object(),
                "2026-08-03",
                "2026-08-03",
            )


if __name__ == "__main__":
    unittest.main()
