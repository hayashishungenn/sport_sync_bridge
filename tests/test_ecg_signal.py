from __future__ import annotations

import unittest
from unittest.mock import patch

from sport_sync_bridge.ecg_signal import (
    EcgSignalNormalizer,
    _classify_bigrun_ecg_patterns,
    _find_ecg_wave_boundary,
    _has_pathological_q,
    _heart_rate_threshold_status,
    _is_pvc_pattern,
    _measure_st_segment,
    analyze_bigrun_ecg_signal,
    normalize_ecg_signal,
)


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

    def test_heart_rate_status_uses_recovered_strict_thresholds(self) -> None:
        self.assertEqual(_heart_rate_threshold_status(None), None)
        self.assertEqual(_heart_rate_threshold_status(59.99), "below_60_bpm")
        self.assertEqual(_heart_rate_threshold_status(60), "between_60_and_100_bpm")
        self.assertEqual(_heart_rate_threshold_status(100), "between_60_and_100_bpm")
        self.assertEqual(_heart_rate_threshold_status(100.01), "above_100_bpm")

    def test_analysis_includes_non_diagnostic_heart_rate_status(self) -> None:
        samples = (0.0,) * 1250
        with (
            patch(
                "sport_sync_bridge.ecg_signal._preprocess_bigrun_ecg_signal",
                return_value=samples,
            ),
            patch(
                "sport_sync_bridge.ecg_signal._detect_bigrun_ecg_r_peaks",
                return_value=(0, 250, 500, 750, 1000),
            ),
        ):
            metrics = analyze_bigrun_ecg_signal(samples, 250)

        self.assertEqual(metrics.heart_rate_bpm, 60)
        self.assertEqual(metrics.heart_rate_threshold_status, "between_60_and_100_bpm")
        self.assertEqual(metrics.pattern_labels, ("normal",))
        self.assertIn("不能替代专业的医疗诊断", metrics.disclaimer)


class EcgPatternClassificationTests(unittest.TestCase):
    def test_no_or_one_r_peak_returns_no_pattern(self) -> None:
        samples = (0.0,) * 1500

        self.assertEqual(_classify_bigrun_ecg_patterns(samples, (), 250), ())
        self.assertEqual(_classify_bigrun_ecg_patterns(samples, (250,), 250), ())

    def test_heart_rate_pattern_thresholds_are_strict(self) -> None:
        samples = (0.0,) * 1500

        self.assertEqual(
            _classify_bigrun_ecg_patterns(samples, (0, 250, 500), 250),
            ("normal",),
        )
        self.assertEqual(
            _classify_bigrun_ecg_patterns(samples, (0, 150, 300), 250),
            ("normal",),
        )
        self.assertIn(
            "tachycardia",
            _classify_bigrun_ecg_patterns(samples, (0, 149, 298), 250),
        )
        self.assertIn(
            "bradycardia",
            _classify_bigrun_ecg_patterns(samples, (0, 251, 502), 250),
        )

    def test_atrial_fibrillation_suppresses_premature_beat_label(self) -> None:
        samples = (0.0,) * 3000
        peaks = (0, 250, 750, 1000, 1500, 1750, 2250)

        labels = _classify_bigrun_ecg_patterns(samples, peaks, 250)

        self.assertIn("atrialFibrillation", labels)
        self.assertNotIn("pac", labels)

    def test_premature_beat_label_is_returned_without_atrial_fibrillation(self) -> None:
        samples = (0.0,) * 1200
        peaks = (0, 100, 500, 600, 700)

        labels = _classify_bigrun_ecg_patterns(samples, peaks, 250)

        self.assertIn("pac", labels)

    def test_consecutive_morphology_flags_and_labels_are_deduplicated(self) -> None:
        samples = (0.0,) * 1800
        peaks = (200, 450, 700, 950, 1200)

        with (
            patch(
                "sport_sync_bridge.ecg_signal._find_ecg_wave_boundary",
                side_effect=lambda _, peak, direction, __: peak + direction * 20,
            ),
            patch("sport_sync_bridge.ecg_signal._has_pathological_q", return_value=True),
            patch("sport_sync_bridge.ecg_signal._measure_st_segment", return_value=(True, False)),
            patch("sport_sync_bridge.ecg_signal._is_pvc_pattern", return_value=True),
        ):
            labels = _classify_bigrun_ecg_patterns(samples, peaks, 250)

        self.assertEqual(labels.count("pvc"), 1)
        self.assertIn("vt", labels)
        self.assertIn("myocardialInfarction", labels)
        self.assertNotIn("myocardialIschemia", labels)

    def test_consecutive_st_depression_is_classified(self) -> None:
        samples = (0.0,) * 1800
        peaks = (200, 450, 700, 950, 1200)

        with (
            patch(
                "sport_sync_bridge.ecg_signal._find_ecg_wave_boundary",
                side_effect=lambda _, peak, direction, __: peak + direction * 20,
            ),
            patch("sport_sync_bridge.ecg_signal._has_pathological_q", return_value=False),
            patch("sport_sync_bridge.ecg_signal._measure_st_segment", return_value=(False, True)),
            patch("sport_sync_bridge.ecg_signal._is_pvc_pattern", return_value=False),
        ):
            labels = _classify_bigrun_ecg_patterns(samples, peaks, 250)

        self.assertIn("myocardialIschemia", labels)

    def test_wave_boundary_and_pathological_q_thresholds(self) -> None:
        samples = [0.0] * 400
        samples[180:221] = [1.0] * 41

        self.assertEqual(_find_ecg_wave_boundary(tuple(samples), 200, -1, 250), 179)
        self.assertEqual(_find_ecg_wave_boundary(tuple(samples), 200, 1, 250), 221)

        qrs_samples = [0.0] * 300
        qrs_samples[150] = 2.0
        qrs_samples[110] = -0.5
        self.assertTrue(_has_pathological_q(tuple(qrs_samples), 150, 100, 1000))
        qrs_samples[110] = -0.49
        self.assertFalse(_has_pathological_q(tuple(qrs_samples), 150, 100, 1000))

    def test_st_segment_and_pvc_thresholds(self) -> None:
        st_samples = [0.0] * 400
        st_samples[200:260] = [0.21] * 60
        self.assertEqual(_measure_st_segment(tuple(st_samples), 150, 200, 1000), (True, False))

        st_samples[200:260] = [-0.11] * 60
        self.assertEqual(_measure_st_segment(tuple(st_samples), 150, 200, 1000), (False, True))

        pvc_samples = [0.0] * 300
        pvc_samples[200] = 1.0
        self.assertTrue(_is_pvc_pattern(tuple(pvc_samples), 200, 250))
        pvc_samples[180] = 0.16
        self.assertFalse(_is_pvc_pattern(tuple(pvc_samples), 200, 250))


if __name__ == "__main__":
    unittest.main()
