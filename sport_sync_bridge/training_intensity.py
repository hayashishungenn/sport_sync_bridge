from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone


_TRAINING_INTENSITY_ORDER = {
    "Recovery": 0,
    "Base": 1,
    "Tempo": 2,
    "Threshold": 3,
    "VO2max": 4,
    "Anaerobic": 5,
}
_SPEED_ZONE_FACTORS = (0.70, 0.80, 0.90, 1.05, 1.15, 3.00)
_SPEED_ZONE_MAX_SAMPLE_GAP_SECONDS = 30.0


def classify_activity_training_intensity(
    heart_rate_zones: object,
    power_zones: object,
    duration_seconds: object,
    sport_type: object,
    *,
    intensity_factor: object = None,
    speed_zones: object = None,
) -> str | None:
    heart_seconds = _zone_seconds(heart_rate_zones)
    heart_rate = classify_heart_rate_intensity(heart_rate_zones, duration_seconds, sport_type)

    if not _is_cycling_sport(sport_type):
        if _is_heart_rate_only_sport(sport_type):
            return heart_rate
        speed = classify_speed_intensity(speed_zones)
        if speed is not None:
            return speed
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


def derive_speed_zone_times(
    samples: Sequence[tuple[datetime | None, object]],
    threshold_speed_kmh: object,
) -> list[dict[str, float | int]]:
    threshold = _finite_nonnegative(threshold_speed_kmh)
    if threshold is None or threshold == 0:
        return []

    points: list[tuple[datetime, float]] = []
    for timestamp, raw_speed in samples:
        if not isinstance(timestamp, datetime):
            continue
        speed = _finite_nonnegative(raw_speed)
        if speed is None:
            continue
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        else:
            timestamp = timestamp.astimezone(timezone.utc)
        points.append((timestamp, speed))
    if len(points) < 2:
        return []
    points.sort(key=lambda point: point[0])

    smoothed: list[tuple[datetime, float]] = []
    for index, (timestamp, _) in enumerate(points):
        start = max(0, index - 2)
        end = min(len(points), index + 3)
        speed = statistics.median(point[1] for point in points[start:end])
        smoothed.append((timestamp, speed))

    reference_speed_mps = threshold / 3.6
    boundaries = [reference_speed_mps * factor for factor in _SPEED_ZONE_FACTORS]
    seconds_by_zone = {zone: 0.0 for zone in range(1, len(boundaries) + 1)}
    for (start_time, start_speed), (end_time, end_speed) in zip(smoothed, smoothed[1:]):
        interval = (end_time - start_time).total_seconds()
        if interval <= 0:
            continue
        duration = min(interval, _SPEED_ZONE_MAX_SAMPLE_GAP_SECONDS)
        cuts = [0.0, 1.0]
        if end_speed != start_speed:
            for boundary in boundaries:
                fraction = (boundary - start_speed) / (end_speed - start_speed)
                if 0.0 < fraction < 1.0:
                    cuts.append(fraction)
        cuts.sort()
        for left, right in zip(cuts, cuts[1:]):
            midpoint_speed = start_speed + (end_speed - start_speed) * (left + right) / 2
            zone = next(
                (index + 1 for index, boundary in enumerate(boundaries) if midpoint_speed <= boundary),
                len(boundaries),
            )
            seconds_by_zone[zone] += duration * (right - left)

    return [
        {"zone": zone, "seconds": seconds, "high_boundary": boundaries[zone - 1]}
        for zone, seconds in seconds_by_zone.items()
        if seconds > 0
    ]


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


def _is_heart_rate_only_sport(sport_type: object) -> bool:
    if not isinstance(sport_type, str):
        return False
    sport = sport_type.casefold().strip().replace(" ", "_")
    return sport in {"run", "running", "trail_running", "walking", "hiking"}


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
