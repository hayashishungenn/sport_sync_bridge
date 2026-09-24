from __future__ import annotations

import unittest

from sport_sync_bridge.ecg_signal import EcgSignalNormalizer, normalize_ecg_signal


class EcgSignalNormalizerTests(unittest.TestCase):
    def test_normalizes_samples_to_the_apk_display_range(self) -> None:
        self.assertEqual(normalize_ecg_signal((0, 2, 4)), (-5.0, 0.0, 5.0))

    def test_flat_or_near_flat_signal_maps_to_zero(self) -> None:
        self.assertEqual(normalize_ecg_signal((3, 3, 3)), (0.0, 0.0, 0.0))
        self.assertEqual(normalize_ecg_signal((0.0, 1e-10)), (0.0, 0.0))

    def test_range_at_apk_threshold_is_normalized(self) -> None:
        self.assertEqual(normalize_ecg_signal((0.0, 1e-9)), (-5.0, 5.0))

    def test_empty_signal_stays_empty(self) -> None:
        self.assertEqual(normalize_ecg_signal(()), ())

    def test_streaming_normalizer_observes_across_chunks(self) -> None:
        normalizer = EcgSignalNormalizer()
        normalizer.observe((-2, 0))
        normalizer.observe((2,))

        self.assertEqual(
            tuple(normalizer.normalize_value(value) for value in (-2, 0, 2)),
            (-5.0, 0.0, 5.0),
        )

    def test_rejects_invalid_targets_and_non_finite_samples(self) -> None:
        with self.assertRaisesRegex(ValueError, "greater than"):
            EcgSignalNormalizer(5, -5)
        with self.assertRaisesRegex(ValueError, "finite number"):
            normalize_ecg_signal((0, float("nan")))


if __name__ == "__main__":
    unittest.main()
