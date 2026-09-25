from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass


ECG_NORMALIZATION_MIN = -5.0
ECG_NORMALIZATION_MAX = 5.0
ECG_FLAT_SIGNAL_RANGE = 1e-9
ECG_MIN_SAMPLE_RATE_HZ = 100.0
ECG_MAX_SAMPLE_RATE_HZ = 2000.0
ECG_MIN_ANALYSIS_SECONDS = 5.0
ECG_MOVING_AVERAGE_WINDOW_MS = 100.0
ECG_R_PEAK_REFRACTORY_MS = 100.0
ECG_BRADYCARDIA_THRESHOLD_BPM = 60.0
ECG_TACHYCARDIA_THRESHOLD_BPM = 100.0
ECG_LOW_PASS_RC_SECONDS = 0.013262911924324612
ECG_HIGH_PASS_RC_SECONDS = 0.3183098861837907
ECG_WAVE_BOUNDARY_RELATIVE_AMPLITUDE = 0.3
ECG_WAVE_BOUNDARY_WINDOW_MS = 80.0
ECG_QRS_WIDE_THRESHOLD_MS = 120.0
ECG_ST_BASELINE_WINDOW_MS = 80.0
ECG_ST_WINDOW_AFTER_QRS_MS = 60.0
ECG_ST_ELEVATION_THRESHOLD = 0.2
ECG_ST_DEPRESSION_THRESHOLD = -0.1
ECG_PATHOLOGICAL_Q_MIN_SAMPLES = 30
ECG_PATHOLOGICAL_Q_RELATIVE_AMPLITUDE = 0.25
ECG_PREMATURE_BEAT_SHORT_RR_RATIO = 0.8
ECG_PREMATURE_BEAT_LONG_RR_RATIO = 1.2
ECG_AF_MIN_RR_INTERVALS = 5
ECG_AF_RR_STANDARD_DEVIATION_THRESHOLD_MS = 120.0
ECG_PVC_LOOKBACK_MS = 150.0
ECG_PVC_RELATIVE_AMPLITUDE = 0.15
ECG_VT_CONSECUTIVE_QRS_COUNT = 3
ECG_ISCHEMIA_CONSECUTIVE_COUNT = 5
ECG_ANALYSIS_DISCLAIMER = "免责声明：此分析仅供参考，不能替代专业的医疗诊断。"


@dataclass(frozen=True, slots=True)
class EcgAnalysisMetrics:
    sample_rate_hz: float
    sample_count: int
    duration_seconds: float
    r_peak_indices: tuple[int, ...]
    rr_intervals_ms: tuple[float, ...]
    average_rr_ms: float | None
    heart_rate_bpm: float | None
    heart_rate_threshold_status: str | None
    disclaimer: str = ECG_ANALYSIS_DISCLAIMER
    pattern_labels: tuple[str, ...] = ()


class EcgSignalNormalizer:
    def __init__(
        self,
        target_min: float = ECG_NORMALIZATION_MIN,
        target_max: float = ECG_NORMALIZATION_MAX,
    ) -> None:
        self.target_min = _finite_number(target_min, "target minimum")
        self.target_max = _finite_number(target_max, "target maximum")
        if self.target_max <= self.target_min:
            raise ValueError("Target maximum must be greater than target minimum")
        self.minimum: float | None = None
        self.maximum: float | None = None

    def observe(self, samples: Iterable[object]) -> int:
        count = 0
        for sample in samples:
            value = _finite_number(sample, "ECG sample")
            self.minimum = value if self.minimum is None else min(self.minimum, value)
            self.maximum = value if self.maximum is None else max(self.maximum, value)
            count += 1
        return count

    def normalize_value(self, sample: object) -> float:
        value = _finite_number(sample, "ECG sample")
        if self.minimum is None or self.maximum is None:
            raise ValueError("Cannot normalize a sample before observing signal values")
        signal_range = self.maximum - self.minimum
        if signal_range < ECG_FLAT_SIGNAL_RANGE:
            return 0.0
        scale = (self.target_max - self.target_min) / signal_range
        return (value - self.minimum) * scale + self.target_min


def normalize_ecg_signal(
    samples: Iterable[object],
    *,
    target_min: float = ECG_NORMALIZATION_MIN,
    target_max: float = ECG_NORMALIZATION_MAX,
) -> tuple[float, ...]:
    values = tuple(samples)
    normalizer = EcgSignalNormalizer(target_min, target_max)
    normalizer.observe(values)
    return tuple(normalizer.normalize_value(value) for value in values)


def analyze_bigrun_ecg_signal(
    samples: Iterable[object],
    sample_rate_hz: object,
) -> EcgAnalysisMetrics:
    sample_rate = _finite_number(sample_rate_hz, "sample rate")
    if not ECG_MIN_SAMPLE_RATE_HZ <= sample_rate <= ECG_MAX_SAMPLE_RATE_HZ:
        raise ValueError("Sampling rate must be between 100 and 2000 Hz")

    values = tuple(_finite_number(sample, "ECG sample") for sample in samples)
    if not values:
        raise ValueError("ECG analysis requires at least one sample")
    duration = len(values) / sample_rate
    if duration < ECG_MIN_ANALYSIS_SECONDS:
        raise ValueError("ECG analysis requires at least 5 seconds of samples")

    normalized = normalize_ecg_signal(values)
    processed = _preprocess_bigrun_ecg_signal(normalized, sample_rate)
    peaks = _detect_bigrun_ecg_r_peaks(processed, sample_rate)
    rr_intervals = tuple(
        (right - left) * 1000.0 / sample_rate
        for left, right in zip(peaks, peaks[1:])
    )
    average_rr = (
        (peaks[-1] - peaks[0]) * 1000.0 / (len(peaks) - 1) / sample_rate
        if len(peaks) >= 2
        else None
    )
    heart_rate = 60000.0 / average_rr if average_rr is not None and average_rr > 0 else None
    return EcgAnalysisMetrics(
        sample_rate_hz=sample_rate,
        sample_count=len(values),
        duration_seconds=duration,
        r_peak_indices=peaks,
        rr_intervals_ms=rr_intervals,
        average_rr_ms=average_rr,
        heart_rate_bpm=heart_rate,
        heart_rate_threshold_status=_heart_rate_threshold_status(heart_rate),
        pattern_labels=_classify_bigrun_ecg_patterns(processed, peaks, sample_rate),
    )


def _classify_bigrun_ecg_patterns(
    samples: tuple[float, ...],
    r_peak_indices: tuple[int, ...],
    sample_rate_hz: float,
) -> tuple[str, ...]:
    if len(r_peak_indices) < 2:
        return ()

    rr_intervals_ms = tuple(
        (right - left) * 1000.0 / sample_rate_hz
        for left, right in zip(r_peak_indices, r_peak_indices[1:])
    )
    average_rr_ms = math.fsum(rr_intervals_ms) / len(rr_intervals_ms)
    average_heart_rate = 60000.0 / average_rr_ms if average_rr_ms > 0 else None
    labels: list[str] = []

    if average_heart_rate is not None:
        if average_heart_rate > ECG_TACHYCARDIA_THRESHOLD_BPM:
            labels.append("tachycardia")
        if average_heart_rate < ECG_BRADYCARDIA_THRESHOLD_BPM:
            labels.append("bradycardia")

    has_atrial_fibrillation = False
    if len(rr_intervals_ms) >= ECG_AF_MIN_RR_INTERVALS:
        rr_mean = math.fsum(rr_intervals_ms) / len(rr_intervals_ms)
        rr_variance = (
            math.fsum((value - rr_mean) ** 2 for value in rr_intervals_ms)
            / len(rr_intervals_ms)
        )
        if math.sqrt(rr_variance) > ECG_AF_RR_STANDARD_DEVIATION_THRESHOLD_MS:
            labels.append("atrialFibrillation")
            has_atrial_fibrillation = True

    if not has_atrial_fibrillation and average_rr_ms > 0:
        short_rr_limit = average_rr_ms * ECG_PREMATURE_BEAT_SHORT_RR_RATIO
        long_rr_limit = average_rr_ms * ECG_PREMATURE_BEAT_LONG_RR_RATIO
        if any(
            rr_intervals_ms[index] < short_rr_limit
            and rr_intervals_ms[index - 1] > long_rr_limit
            for index in range(1, len(rr_intervals_ms))
        ):
            labels.append("pac")

    qrs_is_wide: list[bool] = []
    st_elevation: list[bool] = []
    st_depression: list[bool] = []
    pathological_q: list[bool] = []

    for peak_index in r_peak_indices:
        qrs_start = _find_ecg_wave_boundary(samples, peak_index, -1, sample_rate_hz)
        qrs_end = _find_ecg_wave_boundary(samples, peak_index, 1, sample_rate_hz)
        qrs_duration_ms = abs(qrs_end - qrs_start) * 1000.0 / sample_rate_hz
        is_wide = qrs_duration_ms > ECG_QRS_WIDE_THRESHOLD_MS
        qrs_is_wide.append(is_wide)

        if is_wide and _is_pvc_pattern(samples, peak_index, sample_rate_hz):
            labels.append("pvc")

        pathological_q.append(
            _has_pathological_q(samples, peak_index, qrs_start, sample_rate_hz)
        )
        st_flags = _measure_st_segment(samples, qrs_start, qrs_end, sample_rate_hz)
        if st_flags is None:
            st_elevation.append(False)
            st_depression.append(False)
        else:
            st_elevation.append(st_flags[0])
            st_depression.append(st_flags[1])

    if _has_consecutive_values(qrs_is_wide, ECG_VT_CONSECUTIVE_QRS_COUNT):
        labels.append("vt")
    if _has_consecutive_pairs(
        st_elevation,
        pathological_q,
        ECG_ISCHEMIA_CONSECUTIVE_COUNT,
    ):
        labels.append("myocardialInfarction")
    if _has_consecutive_values(st_depression, ECG_ISCHEMIA_CONSECUTIVE_COUNT):
        labels.append("myocardialIschemia")

    if not labels:
        labels.append("normal")
    return tuple(dict.fromkeys(labels))


def _find_ecg_wave_boundary(
    samples: tuple[float, ...],
    peak_index: int,
    direction: int,
    sample_rate_hz: float,
) -> int:
    if direction not in {-1, 1} or not 0 <= peak_index < len(samples):
        raise ValueError("ECG wave boundary requires a valid peak and direction")

    threshold = samples[peak_index] * ECG_WAVE_BOUNDARY_RELATIVE_AMPLITUDE
    maximum_distance = _samples_for_ms(ECG_WAVE_BOUNDARY_WINDOW_MS, sample_rate_hz)
    index = peak_index
    while 0 <= index < len(samples):
        value = samples[index]
        if abs(value) < threshold:
            return index
        if direction < 0 and index + 1 < len(samples) and samples[index + 1] > value:
            return index
        if direction > 0 and index - 1 >= 0 and samples[index - 1] > value:
            return index
        if abs(index - peak_index) > maximum_distance:
            return peak_index
        index += direction
    return peak_index


def _has_pathological_q(
    samples: tuple[float, ...],
    peak_index: int,
    qrs_start: int,
    sample_rate_hz: float,
) -> bool:
    lookback = _samples_for_ms(20.0, sample_rate_hz)
    end = min(len(samples), peak_index - lookback)
    start = max(0, qrs_start)
    minimum = 0.0
    minimum_index = 0
    for index in range(start, end):
        if samples[index] < minimum:
            minimum = samples[index]
            minimum_index = index
    distance = peak_index - minimum_index
    return (
        distance >= ECG_PATHOLOGICAL_Q_MIN_SAMPLES
        and abs(minimum) >= samples[peak_index] * ECG_PATHOLOGICAL_Q_RELATIVE_AMPLITUDE
    )


def _measure_st_segment(
    samples: tuple[float, ...],
    qrs_start: int,
    qrs_end: int,
    sample_rate_hz: float,
) -> tuple[bool, bool] | None:
    st_end = qrs_end + _samples_for_ms(ECG_ST_WINDOW_AFTER_QRS_MS, sample_rate_hz)
    if st_end >= len(samples):
        return None

    baseline_start = max(
        0,
        qrs_start - _samples_for_ms(ECG_ST_BASELINE_WINDOW_MS, sample_rate_hz),
    )
    baseline = _mean(samples[baseline_start:qrs_start])
    st_level = _mean(samples[qrs_end:st_end])
    difference = st_level - baseline
    return (
        difference > ECG_ST_ELEVATION_THRESHOLD,
        difference < ECG_ST_DEPRESSION_THRESHOLD,
    )


def _is_pvc_pattern(samples: tuple[float, ...], peak_index: int, sample_rate_hz: float) -> bool:
    start = max(0, peak_index - _samples_for_ms(ECG_PVC_LOOKBACK_MS, sample_rate_hz))
    threshold = samples[peak_index] * ECG_PVC_RELATIVE_AMPLITUDE
    return all(value <= threshold for value in samples[start:peak_index])


def _has_consecutive_values(values: list[bool], minimum: int) -> bool:
    run = 0
    for value in values:
        run = run + 1 if value else 0
        if run >= minimum:
            return True
    return False


def _has_consecutive_pairs(
    first: list[bool],
    second: list[bool],
    minimum: int,
) -> bool:
    run = 0
    for first_value, second_value in zip(first, second):
        run = run + 1 if first_value and second_value else 0
        if run >= minimum:
            return True
    return False


def _samples_for_ms(milliseconds: float, sample_rate_hz: float) -> int:
    return _round_positive(milliseconds * sample_rate_hz / 1000.0)


def _mean(values: tuple[float, ...] | list[float]) -> float:
    return math.fsum(values) / len(values) if values else 0.0


def _heart_rate_threshold_status(heart_rate_bpm: float | None) -> str | None:
    if heart_rate_bpm is None:
        return None
    if heart_rate_bpm < ECG_BRADYCARDIA_THRESHOLD_BPM:
        return "below_60_bpm"
    if heart_rate_bpm > ECG_TACHYCARDIA_THRESHOLD_BPM:
        return "above_100_bpm"
    return "between_60_and_100_bpm"


def _preprocess_bigrun_ecg_signal(
    samples: tuple[float, ...],
    sample_rate_hz: float,
) -> tuple[float, ...]:
    window_size = max(1, _round_positive(ECG_MOVING_AVERAGE_WINDOW_MS * sample_rate_hz / 1000.0))
    smoothed = _moving_average(samples, window_size)
    high_passed = _butterworth_high_pass(smoothed, sample_rate_hz, ECG_HIGH_PASS_RC_SECONDS)
    return _butterworth_low_pass(high_passed, sample_rate_hz, ECG_LOW_PASS_RC_SECONDS)


def _moving_average(samples: tuple[float, ...], window_size: int) -> tuple[float, ...]:
    if not samples:
        return ()
    half_window = window_size // 2
    prefix_sums = [0.0]
    for sample in samples:
        prefix_sums.append(prefix_sums[-1] + sample)
    result = []
    for index in range(len(samples)):
        left = max(0, index - half_window)
        right = min(len(samples), index + half_window + 1)
        result.append((prefix_sums[right] - prefix_sums[left]) / (right - left))
    return tuple(result)


def _butterworth_high_pass(
    samples: tuple[float, ...],
    sample_rate_hz: float,
    rc_seconds: float,
) -> tuple[float, ...]:
    if not samples:
        return ()
    alpha = rc_seconds / (rc_seconds + 1.0 / sample_rate_hz)
    result = [0.0] * len(samples)
    for index in range(1, len(samples)):
        result[index] = alpha * (result[index - 1] + samples[index] - samples[index - 1])
    return tuple(result)


def _butterworth_low_pass(
    samples: tuple[float, ...],
    sample_rate_hz: float,
    rc_seconds: float,
) -> tuple[float, ...]:
    if not samples:
        return ()
    alpha = (1.0 / sample_rate_hz) / ((1.0 / sample_rate_hz) + rc_seconds)
    result = [0.0] * len(samples)
    for index in range(1, len(samples)):
        result[index] = alpha * samples[index] + (1.0 - alpha) * result[index - 1]
    return tuple(result)


def _detect_bigrun_ecg_r_peaks(
    samples: tuple[float, ...],
    sample_rate_hz: float,
) -> tuple[int, ...]:
    if len(samples) < 3:
        return ()
    threshold = max(samples) * 0.5
    refractory_samples = max(
        1,
        _round_positive(ECG_R_PEAK_REFRACTORY_MS * sample_rate_hz / 1000.0),
    )
    peaks: list[int] = []
    index = 1
    while index < len(samples) - 1:
        value = samples[index]
        if value > threshold and value > samples[index - 1] and value > samples[index + 1]:
            left = samples[index - 1]
            right = samples[index + 1]
            denominator = left - 2.0 * value + right
            offset = 0.5 * (left - right) / denominator if denominator else 0.0
            peaks.append(index + _round_signed(offset))
            threshold = threshold * 0.7 + value * 0.3
            index += refractory_samples
        else:
            index += 1
    return tuple(peaks)


def _round_positive(value: float) -> int:
    return math.floor(value + 0.5)


def _round_signed(value: float) -> int:
    return math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5)


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number
