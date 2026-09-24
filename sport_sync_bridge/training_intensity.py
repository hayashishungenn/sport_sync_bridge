from __future__ import annotations

import math
from collections.abc import Mapping, Sequence


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
) -> str | None:
    seconds_by_zone = _zone_seconds(zones)
    if not seconds_by_zone:
        return None

    total_seconds = sum(seconds_by_zone.values())
    if total_seconds > 0:
        high_zone_share = (seconds_by_zone.get(6, 0.0) + seconds_by_zone.get(7, 0.0)) / total_seconds
        if high_zone_share >= 0.08:
            top_three_share = sum(seconds_by_zone.get(zone, 0.0) for zone in (5, 6, 7)) / total_seconds
            return "Anaerobic" if top_three_share < 0.15 else "VO2max"

        for numerator, denominator, threshold, label in (
            (5, 4, 1.5, "VO2max"),
            (4, 3, 1.0, "Threshold"),
            (3, 2, 0.8, "Tempo"),
        ):
            ratio = _zone_ratio(seconds_by_zone, numerator, denominator)
            if ratio is not None and ratio >= threshold:
                return label

    return _duration_intensity(duration_seconds, sport_type)


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


def _duration_intensity(duration_seconds: object, sport_type: object) -> str | None:
    duration = _finite_nonnegative(duration_seconds)
    if duration is None:
        return None
    sport = sport_type.casefold().strip() if isinstance(sport_type, str) else ""
    threshold = 3600.0 if sport == "cycling" else 1800.0
    return "Base" if duration >= threshold else "Recovery"


def _finite_nonnegative(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return None
    return number
