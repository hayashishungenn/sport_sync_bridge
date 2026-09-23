from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sport_sync_bridge.config import AppConfig
from sport_sync_bridge.coordinate_rules import CoordinateRule, load_coordinate_rules
from sport_sync_bridge.fit_tools import normalize_fit_coordinates
from sport_sync_bridge.formats import _read_fit
from tests.activity_fixtures import create_fit


class CoordinateRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_firmware_bounds_are_inclusive_and_unknown_requires_unbounded_rule(self) -> None:
        bounded = CoordinateRule(999, 123, 2.0, 4.0, "gcj02_to_wgs84")
        self.assertTrue(bounded.matches(999, 123, 2.0))
        self.assertTrue(bounded.matches(999, 123, 4.0))
        self.assertFalse(bounded.matches(999, 123, 1.99))
        self.assertFalse(bounded.matches(999, 123, None))

        unbounded = CoordinateRule(999, None, None, None, "none")
        self.assertTrue(unbounded.matches(999, 123, None))

    def test_rule_hit_fixes_coordinates_and_marker_prevents_repeat(self) -> None:
        source = create_fit(self.root / "source.fit", firmware=2.0)
        output = self.root / "fixed.fit"
        rules = (CoordinateRule(999, 123, 2.0, 4.0, "gcj02_to_wgs84"),)

        fixed_path, changed = normalize_fit_coordinates(source, output, "none", rules)
        self.assertEqual(fixed_path, output.resolve())
        self.assertEqual(changed, 2)
        before = _read_fit(fixed_path).track_points[0]
        original = _read_fit(source).track_points[0]
        self.assertNotAlmostEqual(before.latitude or 0, original.latitude or 0, places=4)
        self.assertTrue(Path(f"{fixed_path}.coord.json").is_file())

        repeated_path, repeated_changes = normalize_fit_coordinates(
            fixed_path,
            self.root / "twice-fixed.fit",
            "gcj02_to_wgs84",
            rules,
        )
        self.assertEqual(repeated_path, fixed_path)
        self.assertEqual(repeated_changes, 0)
        self.assertAlmostEqual(_read_fit(repeated_path).track_points[0].latitude or 0, before.latitude or 0, places=6)

    def test_skip_rule_overrides_source_fallback_and_unknown_firmware_misses_range(self) -> None:
        source = create_fit(self.root / "source.fit", firmware=3.0)
        skip_rule = CoordinateRule(999, 123, 3.0, 3.0, "none")
        skipped, changes = normalize_fit_coordinates(source, self.root / "skip.fit", "gcj02_to_wgs84", [skip_rule])
        self.assertEqual(skipped, source.resolve())
        self.assertEqual(changes, 0)

        unknown_firmware = create_fit(self.root / "unknown-firmware.fit", firmware=None)
        bounded_rule = CoordinateRule(999, 123, 1.0, 4.0, "gcj02_to_wgs84")
        untouched, changes = normalize_fit_coordinates(
            unknown_firmware,
            self.root / "unknown-fixed.fit",
            "none",
            [bounded_rule],
        )
        self.assertEqual(untouched, unknown_firmware.resolve())
        self.assertEqual(changes, 0)

    def test_unmatched_device_uses_source_fallback(self) -> None:
        source = create_fit(self.root / "fallback.fit", manufacturer=777)
        fixed, changes = normalize_fit_coordinates(source, self.root / "fallback-fixed.fit", "gcj02_to_wgs84")
        self.assertGreater(changes, 0)
        self.assertNotEqual(fixed, source.resolve())

    def test_missing_rules_file_is_empty_and_overlapping_rules_fail(self) -> None:
        self.assertEqual(load_coordinate_rules(self.root / "does-not-exist.json"), ())
        path = self.root / "rules.json"
        path.write_text(
            json.dumps(
                [
                    {"manufacturer_id": 999, "firmware_min": 1.0, "firmware_max": 2.0, "coordinate_mode": "none"},
                    {"manufacturer_id": 999, "product_id": 123, "firmware_min": 2.0, "coordinate_mode": "none"},
                ]
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "Overlapping coordinate rules"):
            load_coordinate_rules(path)

    def test_app_config_loads_rules_from_the_configured_json_path(self) -> None:
        rules_path = self.root / "custom-rules.json"
        rules_path.write_text(
            '[{"manufacturer_id": 999, "coordinate_mode": "none"}]',
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"FIT_COORDINATE_RULES_FILE": str(rules_path)}):
            config = AppConfig.load(self.root)
        self.assertEqual(config.coordinate_rules_path, rules_path)
        self.assertEqual(len(config.coordinate_rules), 1)

    def test_corrupt_marker_is_rejected(self) -> None:
        source = create_fit(self.root / "source.fit")
        output = self.root / "fixed.fit"
        normalize_fit_coordinates(source, output, "gcj02_to_wgs84")
        Path(f"{output}.coord.json").write_text(
            '{"format": 1, "output_sha256": "bad"}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(RuntimeError, "Coordinate marker does not match"):
            normalize_fit_coordinates(output, self.root / "next.fit", "gcj02_to_wgs84")


if __name__ == "__main__":
    unittest.main()
