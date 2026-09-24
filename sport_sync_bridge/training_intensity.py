from __future__ import annotations

import math
from collections.abc import Mapping, Sequence


_TRAINING_INTENSITY_ORDER = {
    "Recovery": 0,
    "Base": 1,
    "Tempo": 2,
    "Threshold": 3,
    "VO2max": 4,
    "Anaerobic": 5,
}


def classify_activity_training_intensity(
    heart_rate_zones: object,
    power_zones: object,
    duration_seconds: object,
    sport_type: object,
    *,
    intensity_factor: object = None,
) -> str | None:
    heart_seconds = _zone_seconds(heart_rate_zones)
    heart_rate = classify_heart_rate_intensity(heart_rate_zones, duration_seconds, sport_type)

    if not _is_cycling_sport(sport_type):
        # GarSync uses HR directly for running, walking, and hiking. For other
        # sports its optional speed path needs a user threshold not modeled here;
        # HR is the recovered fallback when that path is unavailable.
        return heart_rate

    power_seconds = _zone_seconds(power_zones)
    if not power_seconds:
        return heart_rate

    power = classify_power_intensity(
        power_zones,
        duration_seconds,
        sport_type,
        intensity_factor=intensity_factor,
    )
    if power is None:
        return heart_rate

    intensity = _finite_nonnegative(intensity_factor)
    duration = _finite_nonnegative(duration_seconds)
    if _cycling_override_short_circuits_merge(intensity, duration):
        return power
    if intensity is None and power == "Anaerobic":
        return power
    if not heart_seconds or heart_rate is None:
        return power

    heart_order = _TRAINING_INTENSITY_ORDER[heart_rate]
    power_order = _TRAINING_INTENSITY_ORDER[power]
    if heart_order > power_order:
        return heart_rate

    base_fraction = _zone_fraction(heart_seconds, 1, 2)
    if base_fraction is not None and base_fraction >= 0.9:
        return heart_rate
    if power_order < 3 or heart_order >= 3:
        return power

    zone_two_fraction = _zone_fraction(heart_seconds, 2)
    if zone_two_fraction is not None and zone_two_fraction >= 0.5:
        return heart_rate
    return power


def classify_heart_rate_intensity(
    zones: object,
    duration_seconds: object,
    sport_type: object,
) -> str | None:
    seconds_by_zone = _zone_seconds(zones)
    if not seconds_by_zone:
        return None

    for numerator, denominator, threshold, label in (
        (5, 4, 1.5, "VO2max"),
        (4, 3, 1.0, "Threshold"),
        (3, 2, 0.4, "Tempo"),
    ):
        ratio = _zone_ratio(seconds_by_zone, numerator, denominator)
        if ratio is not None and ratio >= threshold:
            return label

    return _duration_intensity(duration_seconds, sport_type)


def classify_power_intensity(
    zones: object,
    duration_seconds: object,
    sport_type: object,
    *,
    intensity_factor: object = None,
) -> str | None:
    seconds_by_zone = _zone_seconds(zones)
    if not seconds_by_zone:
        return None

    total_seconds = sum(seconds_by_zone.values())
    label: str | None = None
    if total_seconds > 0:
        high_zone_share = (seconds_by_zone.get(6, 0.0) + seconds_by_zone.get(7, 0.0)) / total_seconds
        if high_zone_share >= 0.08:
            top_three_share = sum(seconds_by_zone.get(zone, 0.0) for zone in (5, 6, 7)) / total_seconds
            label = "Anaerobic" if top_three_share < 0.15 else "VO2max"
        else:
            for numerator, denominator, threshold, candidate in (
                (5, 4, 1.5, "VO2max"),
                (4, 3, 1.0, "Threshold"),
                (3, 2, 0.8, "Tempo"),
            ):
                ratio = _zone_ratio(seconds_by_zone, numerator, denominator)
                if ratio is not None and ratio >= threshold:
                    label = candidate
                    break

    if label is None:
        label = _duration_intensity(duration_seconds, sport_type)
    if label is None:
        return None

    if _is_cycling_sport(sport_type):
        label = _apply_cycling_intensity_factor(label, duration_seconds, intensity_factor)
    return label


def classify_speed_intensity(zones: object) -> str | None:
    seconds_by_zone = _zone_seconds(zones)
    total_seconds = sum(seconds_by_zone.values())
    if total_seconds <= 0:
        return None

    top_zone_share = seconds_by_zone.get(6, 0.0) / total_seconds
    if top_zone_share >= 0.08:
        top_two_share = (seconds_by_zone.get(5, 0.0) + seconds_by_zone.get(6, 0.0)) / total_seconds
        return "Anaerobic" if top_two_share < 0.15 else "VO2max"

    zone_four_seconds = seconds_by_zone.get(4, 0.0)
    if zone_four_seconds <= 0:
        return None
    return "VO2max" if seconds_by_zone.get(5, 0.0) / zone_four_seconds >= 1.5 else None


def _zone_seconds(zones: object) -> dict[int, float]:
    if not isinstance(zones, Sequence) or isinstance(zones, (str, bytes)):
        return {}

    seconds_by_zone: dict[int, float] = {}
    for item in zones:
        if not isinstance(item, Mapping):
            continue
        zone = item.get("zone")
        seconds = _finite_nonnegative(item.get("seconds"))
        if isinstance(zone, bool) or not isinstance(zone, int) or zone < 1 or seconds is None:
            continue
        seconds_by_zone[zone] = seconds_by_zone.get(zone, 0.0) + seconds
    return seconds_by_zone


def _zone_ratio(seconds_by_zone: dict[int, float], numerator: int, denominator: int) -> float | None:
    denominator_seconds = seconds_by_zone.get(denominator, 0.0)
    if denominator_seconds <= 0:
        return None
    return seconds_by_zone.get(numerator, 0.0) / denominator_seconds


def _zone_fraction(seconds_by_zone: dict[int, float], *zones: int) -> float | None:
    total_seconds = sum(seconds_by_zone.values())
    if total_seconds <= 0:
        return None
    return sum(seconds_by_zone.get(zone, 0.0) for zone in zones) / total_seconds


def _duration_intensity(duration_seconds: object, sport_type: object) -> str | None:
    duration = _finite_nonnegative(duration_seconds)
    if duration is None:
        return None
    sport = sport_type.casefold().strip() if isinstance(sport_type, str) else ""
    threshold = 3600.0 if sport == "cycling" else 1800.0
    return "Base" if duration >= threshold else "Recovery"


def _is_cycling_sport(sport_type: object) -> bool:
    if not isinstance(sport_type, str):
        return False
    sport = sport_type.casefold().strip()
    return sport in {
        "cycling",
        "ride",
        "virtual_ride",
        "indoor_cycling",
        "mountain_biking",
    }


def _apply_cycling_intensity_factor(
    label: str,
    duration_seconds: object,
    intensity_factor: object,
) -> str:
    intensity = _finite_nonnegative(intensity_factor)
    duration = _finite_nonnegative(duration_seconds)
    if intensity is None or duration is None or intensity >= 0.75:
        return label
    if duration >= 7200:
        return "Base"
    if intensity >= 0.55:
        return label
    return "Base" if duration < 3900 else "Recovery"


def _cycling_override_short_circuits_merge(
    intensity_factor: float | None,
    duration_seconds: float | None,
) -> bool:
    if intensity_factor is None or duration_seconds is None or intensity_factor >= 0.75:
        return False
    return intensity_factor < 0.55 or duration_seconds >= 7200


def _finite_nonnegative(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return None
    return number
