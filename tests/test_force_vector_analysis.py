from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sport_sync_bridge.force_vector_analysis import (
    ForceVectorSnapshot,
    build_force_vector_analysis_prompt,
    load_force_vector_snapshot,
)


class ForceVectorAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def load_payload(self, payload: object, *, name: str = "snapshot.json") -> ForceVectorSnapshot:
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return load_force_vector_snapshot(path)

    def test_loads_force_vector_nodes_and_metrics(self) -> None:
        snapshot = self.load_payload(
            {
                "left_foot_nodes": [12, 15.5, 20],
                "right_foot_nodes": [11, 14, 19.5],
                "left_torque_effectiveness_percent": 75.2,
                "right_torque_effectiveness_percent": 70.4,
                "left_pedal_smoothness_percent": 22.1,
                "right_pedal_smoothness_percent": 19.8,
            }
        )

        self.assertEqual(snapshot.left_foot_nodes, (12.0, 15.5, 20.0))
        self.assertEqual(snapshot.right_foot_nodes, (11.0, 14.0, 19.5))
        self.assertEqual(snapshot.left_torque_effectiveness_percent, 75.2)
        self.assertEqual(snapshot.right_pedal_smoothness_percent, 19.8)
        self.assertTrue(snapshot.has_data)

    def test_rejects_invalid_json_shapes_and_measurements(self) -> None:
        invalid_payloads = [
            [],
            {"left_foot_nodes": "12,15,20"},
            {"left_foot_nodes": [True]},
            {"left_foot_nodes": [float("nan")]},
            {"left_foot_nodes": [1] * 13},
            {"left_pedal_smoothness_percent": 101},
            {"unexpected": 1},
        ]
        for index, payload in enumerate(invalid_payloads):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.load_payload(payload, name=f"invalid-{index}.json")

        malformed_path = self.root / "malformed.json"
        malformed_path.write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Invalid force-vector JSON"):
            load_force_vector_snapshot(malformed_path)

    def test_prompt_includes_values_language_and_peak_power_focus(self) -> None:
        snapshot = self.load_payload(
            {
                "left_foot_nodes": [12, 15.5, 20],
                "left_torque_effectiveness_percent": 75.2,
            }
        )

        prompt = build_force_vector_analysis_prompt(
            snapshot,
            language="en-US",
            detail="brief",
            focus="peak_power",
            question="How can I improve the downstroke?",
        )

        self.assertIn('Respond in Language Code: "en-US"', prompt)
        self.assertIn("TE (Left): 75.2%", prompt)
        self.assertIn("Left Foot Nodes (30° intervals): [12.0, 15.5, 20.0]", prompt)
        self.assertIn("30°–120°", prompt)
        self.assertIn("How can I improve the downstroke?", prompt)
        self.assertNotIn("形状特征", prompt)

    def test_empty_snapshot_uses_generic_coaching_guidance(self) -> None:
        prompt = build_force_vector_analysis_prompt(ForceVectorSnapshot(), question="  ")

        self.assertIn("尚未获取到该运动员的具体实时数据", prompt)
        self.assertIn("No force-vector measurements were provided", prompt)
        self.assertNotIn("用户补充问题", prompt)

    def test_rejects_unsupported_prompt_options(self) -> None:
        snapshot = ForceVectorSnapshot()
        with self.assertRaises(ValueError):
            build_force_vector_analysis_prompt(snapshot, detail="verbose")
        with self.assertRaises(ValueError):
            build_force_vector_analysis_prompt(snapshot, focus="unknown")
        with self.assertRaises(ValueError):
            build_force_vector_analysis_prompt(snapshot, language="not a language")


if __name__ == "__main__":
    unittest.main()
