from __future__ import annotations

import math
from collections.abc import Iterable


ECG_NORMALIZATION_MIN = -5.0
ECG_NORMALIZATION_MAX = 5.0
ECG_FLAT_SIGNAL_RANGE = 1e-9


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


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number
