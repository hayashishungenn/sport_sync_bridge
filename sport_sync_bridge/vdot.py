from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import date, timezone
from typing import Iterable

from .utils import parse_datetime


MIN_ACTIVITY_DISTANCE_M = 200.0
MIN_ACTIVITY_DURATION_S = 60.0
MINIMUM_CALCULATION_VALUE = 60.0

RACE_DISTANCES_M = {
    "1mi": 1609.344,
    "3k": 3000.0,
    "5k": 5000.0,
    "10k": 10000.0,
    "half_marathon": 21097.5,
    "full_marathon": 42195.0,
}

TRAINING_PACE_FACTORS = {
    "easy": (0.59, 0.74),
    "marathon": (0.75, 0.84),
    "threshold": (0.83, 0.88),
    "interval": (0.95, 1.0),
    "repetition": (1.05, 1.10),
}

RUNNING_SPORTS = {"running", "run", "trail_running", "track_running", "treadmill_running"}
PACE_LABELS = {
    "easy": "Easy Run",
    "marathon": "Marathon Pace",
    "threshold": "Lactate Threshold",
    "interval": "Interval Run",
    "repetition": "Repetition Run",
}


def calculate_vdot(
    distance_m: float,
    duration_s: float,
    *,
    minimum_value: float = MINIMUM_CALCULATION_VALUE,
) -> float | None:
    distance = _finite_number(distance_m, "distance")
    duration = _finite_number(duration_s, "duration")
    minimum = _finite_number(minimum_value, "minimum value")
    if distance <= 0 or duration <= 0 or minimum < 0:
        raise ValueError("distance and duration must be positive; minimum value cannot be negative")
    if distance <= minimum or duration <= minimum:
        return None

    time_minutes = duration / 60.0
    speed_m_per_min = distance / time_minutes
    oxygen_cost = -4.6 + 0.182258 * speed_m_per_min + 0.000104 * speed_m_per_min**2
    sustainable_fraction = (
        0.8
        + 0.1894393 * math.exp(-0.012778 * time_minutes)
        + 0.2989558 * math.exp(-0.1932605 * time_minutes)
    )
    if oxygen_cost <= 0 or sustainable_fraction <= 0:
        return None
    value = oxygen_cost / sustainable_fraction
    return value if math.isfinite(value) and value > 0 else None


def predict_race_times(vdot: float) -> dict[str, float | None]:
    target_vdot = _finite_number(vdot, "VDOT")
    if target_vdot <= 0:
        raise ValueError("VDOT must be positive")
    predictions: dict[str, float | None] = {}
    for name, distance_m in RACE_DISTANCES_M.items():
        slowest_vdot = calculate_vdot(distance_m, distance_m, minimum_value=0)
        fastest_vdot = calculate_vdot(distance_m, distance_m / 500.0 * 60.0, minimum_value=0)
        if slowest_vdot is None or fastest_vdot is None or not slowest_vdot <= target_vdot <= fastest_vdot:
            predictions[name] = None
            continue

        low_speed = 60.0
        high_speed = 500.0
        for _ in range(50):
            speed = (low_speed + high_speed) / 2.0
            duration_s = distance_m / speed * 60.0
            estimated_vdot = calculate_vdot(distance_m, duration_s, minimum_value=0)
            if estimated_vdot is None:
                break
            if estimated_vdot < target_vdot:
                low_speed = speed
            else:
                high_speed = speed
        predictions[name] = distance_m / ((low_speed + high_speed) / 2.0) * 60.0
    return predictions


def training_paces(vdot: float) -> dict[str, dict[str, float]]:
    value = _finite_number(vdot, "VDOT")
    if value <= 0:
        raise ValueError("VDOT must be positive")
    discriminant = 0.182258**2 + 4 * 0.000104 * (value + 4.6)
    if discriminant <= 0:
        raise ValueError("VDOT is outside the training-pace formula range")
    vdot_speed_m_per_min = (-0.182258 + math.sqrt(discriminant)) / (2 * 0.000104)
    if vdot_speed_m_per_min <= 0:
        raise ValueError("VDOT is outside the training-pace formula range")

    result: dict[str, dict[str, float]] = {}
    for name, (low_factor, high_factor) in TRAINING_PACE_FACTORS.items():
        faster_pace = 60000.0 / (vdot_speed_m_per_min * high_factor)
        slower_pace = 60000.0 / (vdot_speed_m_per_min * low_factor)
        result[name] = {
            "min_seconds_per_km": faster_pace,
            "max_seconds_per_km": slower_pace,
        }
    return result


def analyze_running_activities(
    rows: Iterable[object],
    *,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict[str, object]:
    if date_from and date_to and date_from > date_to:
        raise ValueError("Start date must not be after end date")

    running_count = 0
    eligible: list[dict[str, object]] = []
    for row in rows:
        sport = str(row["sport_type"] or "").strip().lower().replace(" ", "_")
        if sport not in RUNNING_SPORTS:
            continue

        start_raw = row["start_time"]
        if not start_raw:
            summary_for_date = _load_summary(row["summary_json"])
            start_raw = summary_for_date.get("start_time")
        start_time = parse_datetime(str(start_raw)) if start_raw else None
        if start_time is not None:
            if start_time.tzinfo is None:
                start_time = start_time.replace(tzinfo=timezone.utc)
            else:
                start_time = start_time.astimezone(timezone.utc)
        if (date_from or date_to) and (
            start_time is None
            or (date_from is not None and start_time.date() < date_from)
            or (date_to is not None and start_time.date() > date_to)
        ):
            continue
        running_count += 1

        summary = _load_summary(row["summary_json"])
        distance = _summary_number(summary.get("distance_m"))
        duration = _summary_number(summary.get("timer_time_s"))
        if duration is None or duration <= 0:
            duration = _summary_number(summary.get("elapsed_time_s"))
        if distance is None or duration is None:
            continue
        if distance <= MIN_ACTIVITY_DISTANCE_M or duration <= MIN_ACTIVITY_DURATION_S:
            continue
        value = calculate_vdot(distance, duration)
        if value is None:
            continue
        eligible.append(
            {
                "activity_id": str(row["fingerprint"]),
                "name": str(row["name"] or summary.get("name") or "Running activity"),
                "start_time": start_time.isoformat() if start_time else None,
                "date": start_time.date().isoformat() if start_time else None,
                "distance_m": distance,
                "duration_s": duration,
                "vdot": value,
            }
        )

    by_day: dict[str, list[dict[str, object]]] = defaultdict(list)
    for item in eligible:
        if item["date"] is not None:
            by_day[str(item["date"])].append(item)
    daily_trend: list[dict[str, object]] = []
    for day, day_activities in sorted(by_day.items()):
        best_for_day = max(day_activities, key=lambda activity: float(activity["vdot"]))
        daily_trend.append({**best_for_day, "activity_count": len(day_activities)})

    latest = daily_trend[-1] if daily_trend else None
    best = max(eligible, key=lambda activity: float(activity["vdot"])) if eligible else None
    if latest is not None:
        latest = _enrich_vdot_point(latest)
    if best is not None:
        best = _enrich_vdot_point(best)
    return {
        "running_activity_count": running_count,
        "eligible_activity_count": len(eligible),
        "daily_trend": daily_trend,
        "latest": latest,
        "best": best,
    }


def format_vdot_report(report: dict[str, object], output_format: str) -> str:
    if output_format == "json":
        return json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if output_format != "txt":
        raise ValueError(f"Unsupported VDOT report format: {output_format}")

    lines = [
        f"跑步活动：{report['running_activity_count']} 次，有效活动：{report['eligible_activity_count']} 次"
    ]
    latest = report.get("latest")
    best = report.get("best")
    if not isinstance(latest, dict):
        lines.append("没有满足距离和时长门槛的跑步记录。")
        return "\n".join(lines) + "\n"

    lines.append("最新趋势：")
    lines.extend(_format_vdot_point(latest))
    if isinstance(best, dict) and best.get("activity_id") != latest.get("activity_id"):
        lines.append("历史最佳：")
        lines.extend(_format_vdot_point(best))
    lines.append("标准距离预测（基于最新趋势）：")
    for name, prediction in latest["race_predictions"].items():
        if prediction["finish_time_s"] is not None:
            lines.append(f"  {name}: {prediction['finish_time']}")
    lines.append("训练配速（基于最新趋势）：")
    for name, pace in latest["training_paces"].items():
        lines.append(
            f"  {PACE_LABELS[name]}: {_format_pace(pace['min_seconds_per_km'])}–"
            f"{_format_pace(pace['max_seconds_per_km'])} /km"
        )
    return "\n".join(lines) + "\n"


def _enrich_vdot_point(point: dict[str, object]) -> dict[str, object]:
    value = float(point["vdot"])
    predictions = predict_race_times(value)
    return {
        **point,
        "race_predictions": {
            name: {
                "distance_m": distance,
                "finish_time_s": predictions[name],
                "finish_time": _format_duration(predictions[name]) if predictions[name] is not None else None,
            }
            for name, distance in RACE_DISTANCES_M.items()
        },
        "training_paces": training_paces(value),
    }


def _format_vdot_point(point: dict[str, object]) -> list[str]:
    distance_km = float(point["distance_m"]) / 1000.0
    return [
        f"  {point.get('date') or '日期未知'}  {point['name']}  "
        f"{distance_km:.2f} km / {_format_duration(float(point['duration_s']))}  "
        f"VDOT {float(point['vdot']):.1f}"
    ]


def _format_duration(seconds: float) -> str:
    rounded = max(0, int(round(seconds)))
    hours, remainder = divmod(rounded, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes}:{secs:02}"


def _format_pace(seconds_per_km: float) -> str:
    rounded = max(0, int(round(seconds_per_km)))
    minutes, seconds = divmod(rounded, 60)
    return f"{minutes}:{seconds:02}"


def _finite_number(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    return result


def _summary_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _load_summary(raw_value: object) -> dict[str, object]:
    try:
        summary = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
    except json.JSONDecodeError as exc:
        raise ValueError("Local activity has an invalid summary JSON") from exc
    if not isinstance(summary, dict):
        raise ValueError("Local activity summary must be a JSON object")
    return summary
