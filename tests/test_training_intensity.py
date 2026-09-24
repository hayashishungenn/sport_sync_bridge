from __future__ import annotations

import unittest

from sport_sync_bridge.activity_analysis import build_ai_analysis_prompt
from sport_sync_bridge.training_intensity import (
    classify_activity_training_intensity,
    classify_heart_rate_intensity,
    classify_power_intensity,
    classify_speed_intensity,
)


class TrainingIntensityTests(unittest.TestCase):
    def test_heart_rate_zone_ratio_thresholds(self) -> None:
        self.assertEqual(
            classify_heart_rate_intensity(
                [{"zone": 4, "seconds": 100}, {"zone": 5, "seconds": 150}],
                1200,
                "running",
            ),
            "VO2max",
        )
        self.assertEqual(
            classify_heart_rate_intensity(
                [
                    {"zone": 3, "seconds": 100},
                    {"zone": 4, "seconds": 100},
                    {"zone": 5, "seconds": 0},
                ],
                1200,
                "running",
            ),
            "Threshold",
        )
        self.assertEqual(
            classify_heart_rate_intensity(
                [{"zone": 2, "seconds": 100}, {"zone": 3, "seconds": 40}],
                1200,
                "running",
            ),
            "Tempo",
        )

    def test_heart_rate_fallback_uses_sport_duration_boundary(self) -> None:
        zones = [{"zone": 1, "seconds": 100}]
        self.assertEqual(classify_heart_rate_intensity(zones, 1799, "running"), "Recovery")
        self.assertEqual(classify_heart_rate_intensity(zones, 1800, "running"), "Base")
        self.assertEqual(classify_heart_rate_intensity(zones, 3599, "cycling"), "Recovery")
        self.assertEqual(classify_heart_rate_intensity(zones, 3600, "cycling"), "Base")

    def test_power_high_zone_share_thresholds(self) -> None:
        self.assertEqual(
            classify_power_intensity(
                [{"zone": 1, "seconds": 92}, {"zone": 6, "seconds": 8}],
                2400,
                "cycling",
            ),
            "Anaerobic",
        )
        self.assertEqual(
            classify_power_intensity(
                [
                    {"zone": 1, "seconds": 85},
                    {"zone": 5, "seconds": 7},
                    {"zone": 6, "seconds": 8},
                ],
                2400,
                "cycling",
            ),
            "VO2max",
        )

    def test_power_ratio_and_duration_fallbacks(self) -> None:
        self.assertEqual(
            classify_power_intensity(
                [
                    {"zone": 3, "seconds": 20},
                    {"zone": 4, "seconds": 10},
                    {"zone": 5, "seconds": 15},
                ],
                2400,
                "cycling",
            ),
            "VO2max",
        )
        zones = [{"zone": 1, "seconds": 100}]
        self.assertEqual(classify_power_intensity(zones, 3599, "cycling"), "Recovery")
        self.assertEqual(classify_power_intensity(zones, 3600, "cycling"), "Base")

    def test_cycling_intensity_factor_override_thresholds(self) -> None:
        zones = [{"zone": 1, "seconds": 92}, {"zone": 6, "seconds": 8}]
        self.assertEqual(
            classify_power_intensity(zones, 2400, "cycling", intensity_factor=0.80),
            "Anaerobic",
        )
        self.assertEqual(
            classify_power_intensity(zones, 2400, "cycling", intensity_factor=0.55),
            "Anaerobic",
        )
        self.assertEqual(
            classify_power_intensity(zones, 3899, "cycling", intensity_factor=0.54),
            "Base",
        )
        self.assertEqual(
            classify_power_intensity(zones, 3900, "cycling", intensity_factor=0.54),
            "Recovery",
        )
        self.assertEqual(
            classify_power_intensity(zones, 7200, "cycling", intensity_factor=0.54),
            "Base",
        )
        self.assertEqual(
            classify_power_intensity(zones, 9000, "running", intensity_factor=0.10),
            "Anaerobic",
        )
        self.assertEqual(
            classify_power_intensity(zones, 9000, "e_biking", intensity_factor=0.10),
            "Anaerobic",
        )

    def test_cycling_selector_combines_power_and_heart_rate_labels(self) -> None:
        selected = classify_activity_training_intensity(
            [{"zone": 4, "seconds": 100}, {"zone": 5, "seconds": 150}],
            [{"zone": 2, "seconds": 100}, {"zone": 3, "seconds": 80}],
            2400,
            "cycling",
        )

        self.assertEqual(selected, "VO2max")

    def test_cycling_selector_uses_base_and_zone_two_fractions(self) -> None:
        power_tempo = [{"zone": 2, "seconds": 100}, {"zone": 3, "seconds": 80}]
        base_hr = [{"zone": 1, "seconds": 90}, {"zone": 2, "seconds": 10}]
        self.assertEqual(
            classify_activity_training_intensity(base_hr, power_tempo, 3600, "cycling"),
            "Base",
        )

        power_threshold = [{"zone": 3, "seconds": 100}, {"zone": 4, "seconds": 100}]
        hr_zone_two_majority = [{"zone": 2, "seconds": 100}, {"zone": 3, "seconds": 40}]
        self.assertEqual(
            classify_activity_training_intensity(
                hr_zone_two_majority, power_threshold, 2400, "cycling"
            ),
            "Tempo",
        )

        hr_zone_two_minority = [{"zone": 2, "seconds": 40}, {"zone": 3, "seconds": 60}]
        self.assertEqual(
            classify_activity_training_intensity(
                hr_zone_two_minority, power_threshold, 2400, "cycling"
            ),
            "Threshold",
        )

    def test_cycling_selector_factor_and_anaerobic_short_circuits(self) -> None:
        hr_vo2max = [{"zone": 4, "seconds": 60}, {"zone": 5, "seconds": 100}]
        power_anaerobic = [{"zone": 1, "seconds": 92}, {"zone": 6, "seconds": 8}]
        self.assertEqual(
            classify_activity_training_intensity(
                hr_vo2max,
                power_anaerobic,
                3900,
                "cycling",
                intensity_factor=0.54,
            ),
            "Recovery",
        )

        power_tempo = [{"zone": 2, "seconds": 100}, {"zone": 3, "seconds": 80}]
        self.assertEqual(
            classify_activity_training_intensity(
                hr_vo2max,
                power_tempo,
                3900,
                "cycling",
                intensity_factor=0.55,
            ),
            "VO2max",
        )
        self.assertEqual(
            classify_activity_training_intensity(
                hr_vo2max, power_anaerobic, 3900, "cycling"
            ),
            "Anaerobic",
        )

    def test_cycling_selector_falls_back_to_heart_rate_without_power_zones(self) -> None:
        selected = classify_activity_training_intensity(
            [{"zone": 4, "seconds": 100}, {"zone": 5, "seconds": 150}],
            [],
            2400,
            "cycling",
        )

        self.assertEqual(selected, "VO2max")

    def test_speed_high_zone_share_classification(self) -> None:
        self.assertEqual(
            classify_speed_intensity(
                [
                    {"zone": 1, "seconds": 89},
                    {"zone": 5, "seconds": 3},
                    {"zone": 6, "seconds": 8},
                ]
            ),
            "Anaerobic",
        )
        self.assertEqual(
            classify_speed_intensity(
                [
                    {"zone": 1, "seconds": 85},
                    {"zone": 5, "seconds": 7},
                    {"zone": 6, "seconds": 8},
                ]
            ),
            "VO2max",
        )

    def test_speed_adjacent_zone_ratio_and_missing_data(self) -> None:
        self.assertEqual(
            classify_speed_intensity(
                [
                    {"zone": 1, "seconds": 30},
                    {"zone": 4, "seconds": 20},
                    {"zone": 5, "seconds": 30},
                ]
            ),
            "VO2max",
        )
        self.assertIsNone(classify_speed_intensity([{"zone": 1, "seconds": 100}]))
        self.assertIsNone(classify_speed_intensity([]))

    def test_missing_or_invalid_zone_times_do_not_create_a_label(self) -> None:
        self.assertIsNone(classify_heart_rate_intensity([], 7200, "cycling"))
        self.assertIsNone(
            classify_power_intensity(
                [{"zone": 2, "seconds": float("nan")}, {"zone": 3, "seconds": True}],
                7200,
                "cycling",
            )
        )

    def test_ai_prompt_includes_per_modality_training_intensity(self) -> None:
        prompt = build_ai_analysis_prompt(
            {
                "sport_type": "cycling",
                "timer_time_s": 2400,
                "time_in_zone_messages": [
                    {
                        "heart_rate_zones": [
                            {"zone": 2, "seconds": 100},
                            {"zone": 3, "seconds": 40},
                        ],
                        "power_zones": [
                            {"zone": 1, "seconds": 92},
                            {"zone": 6, "seconds": 8},
                        ],
                        "speed_zones": [
                            {"zone": 1, "seconds": 92},
                            {"zone": 6, "seconds": 8},
                        ],
                    }
                ],
            },
            [],
        )

        self.assertIn(
            "FIT 分区训练强度参考：心率=Tempo，功率=Anaerobic，速度=Anaerobic",
            prompt,
        )
        self.assertIn("GarSync分类=Anaerobic", prompt)

    def test_ai_prompt_applies_cycling_intensity_factor_override(self) -> None:
        prompt = build_ai_analysis_prompt(
            {
                "sport_type": "cycling",
                "timer_time_s": 3900,
                "intensity_factor": 0.54,
                "time_in_zone_messages": [
                    {
                        "power_zones": [
                            {"zone": 1, "seconds": 92},
                            {"zone": 6, "seconds": 8},
                        ]
                    }
                ],
            },
            [],
        )

        self.assertIn("FIT 分区训练强度参考：功率=Recovery", prompt)

    def test_ai_prompt_does_not_use_elapsed_time_for_cycling_override(self) -> None:
        prompt = build_ai_analysis_prompt(
            {
                "sport_type": "cycling",
                "elapsed_time_s": 3900,
                "intensity_factor": 0.54,
                "time_in_zone_messages": [
                    {
                        "power_zones": [
                            {"zone": 1, "seconds": 92},
                            {"zone": 6, "seconds": 8},
                        ]
                    }
                ],
            },
            [],
        )

        self.assertIn("FIT 分区训练强度参考：功率=Anaerobic", prompt)


if __name__ == "__main__":
    unittest.main()
