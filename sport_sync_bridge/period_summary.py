from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Mapping

from .utils import parse_datetime
from .vdot import RUNNING_SPORTS, calculate_vdot


DEFAULT_PERIOD_DAYS = 90
SWIMMING_SPORTS = {"swimming", "swim"}
SPORT_ALIASES = {
    "run": "running",
    "ride": "cycling",
    "walk": "walking",
    "trail_run": "trail_running",
}
RECORDED_ZONE_FIELDS = {
    "heart_rate": "heart_rate_zones",
    "speed": "speed_zones",
    "cadence": "cadence_zones",
    "power": "power_zones",
}


def calculate_period_summary(
    rows: Iterable[object],
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    days: int = DEFAULT_PERIOD_DAYS,
    sport: str | None = None,
    resting_hr: float = 60.0,
    threshold_hr: float | None = None,
) -> dict[str, object]:
    if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
        raise ValueError("Period days must be a positive integer")
    resting = _positive_finite(resting_hr, "resting heart rate")
    threshold = _positive_finite(threshold_hr, "lactate threshold heart rate") if threshold_hr is not None else None
    if threshold is not None and threshold <= resting:
        raise ValueError("Lactate threshold heart rate must exceed resting heart rate")

    end_day = date_to or datetime.now(timezone.utc).date()
    start_day = date_from or end_day - timedelta(days=days - 1)
    if start_day > end_day:
        raise ValueError("Start date must not be after end date")

    selected_sport = _normalize_sport(sport) if sport else None
    activities: list[dict[str, object]] = []
    for row in rows:
        summary = _load_summary(_row_value(row, "summary_json"))
        activity_sport = _normalize_sport(_row_value(row, "sport_type") or summary.get("sport_type"))
        if selected_sport is not None and activity_sport != selected_sport:
            continue
        start_time = _activity_start_time(_row_value(row, "start_time") or summary.get("start_time"))
        if start_time is None or not start_day <= start_time.date() <= end_day:
            continue

        distance = _optional_nonnegative(summary.get("distance_m"))
        duration = _optional_positive(summary.get("timer_time_s"))
        if duration is None:
            duration = _optional_positive(summary.get("elapsed_time_s"))
        average_speed = _optional_positive(summary.get("average_speed_mps"))
        if average_speed is None and distance is not None and duration is not None:
            average_speed = distance / duration
        pace = _optional_positive(summary.get("pace_seconds_per_km"))
        if pace is None and average_speed is not None:
            pace = 1000.0 / average_speed
        average_hr = _optional_nonnegative(summary.get("average_heart_rate_bpm"))
        average_power = _optional_nonnegative(summary.get("average_power_w"))
        normalized_power = _optional_nonnegative(summary.get("normalized_power_w"))
        ascent = _optional_nonnegative(summary.get("total_ascent_m"))
        tss = _optional_nonnegative(summary.get("training_stress_score"))
        if tss is None and threshold is not None and average_hr is not None and duration is not None:
            intensity = (average_hr - resting) / (threshold - resting)
            intensity = min(2.0, max(0.0, intensity))
            tss = duration / 3600.0 * intensity**2 * 100.0

        vdot = None
        if activity_sport in RUNNING_SPORTS and distance is not None and duration is not None:
            if distance > 200.0 and duration > 60.0:
                vdot = calculate_vdot(distance, duration)

        activities.append(
            {
                "activity_id": str(_row_value(row, "fingerprint") or summary.get("activity_id") or ""),
                "name": str(_row_value(row, "name") or summary.get("name") or "Activity"),
                "sport_type": activity_sport,
                "file_format": str(_row_value(row, "file_format") or ""),
                "start_time": start_time,
                "date": start_time.date(),
                "distance_m": distance,
                "duration_s": duration,
                "average_speed_mps": average_speed,
                "pace_seconds_per_km": pace,
                "average_heart_rate_bpm": average_hr,
                "average_power_w": average_power,
                "normalized_power_w": normalized_power,
                "intensity_factor": _optional_nonnegative(summary.get("intensity_factor")),
                "time_in_zone_messages": _zone_messages(summary.get("time_in_zone_messages")),
                "training_stress_score": tss,
                "vdot": vdot,
                "ftp": _optional_nonnegative(summary.get("functional_threshold_power_w")),
                "estimated_ftp": _optional_nonnegative(summary.get("estimated_ftp_w")),
                "css": _optional_nonnegative(summary.get("critical_swim_speed")),
                "total_ascent_m": ascent,
            }
        )

    activities.sort(key=lambda item: (item["start_time"], str(item["activity_id"])))
    scored = [item for item in activities if item["training_stress_score"] is not None]
    running = [item for item in activities if item["vdot"] is not None]
    vdot_values = [float(item["vdot"]) for item in running]
    total_distance = sum(float(item["distance_m"]) for item in activities if item["distance_m"] is not None)
    total_duration = sum(float(item["duration_s"]) for item in activities if item["duration_s"] is not None)
    total_tss = sum(float(item["training_stress_score"]) for item in scored)
    fit_count = sum(item["file_format"] == "fit" for item in activities)

    weekly_slices = _build_weekly_slices(activities)
    return {
        "start_date": start_day.isoformat(),
        "end_date": end_day.isoformat(),
        "fit_window_days": (end_day - start_day).days + 1,
        "sport_filter": selected_sport,
        "total_distance_m": total_distance,
        "total_duration_s": total_duration,
        "activity_count": len(activities),
        "total_tss": total_tss if scored else None,
        "scored_tss_activity_count": len(scored),
        "unscored_tss_activity_count": len(activities) - len(scored),
        "vdot_start": vdot_values[0] if vdot_values else None,
        "vdot_end": vdot_values[-1] if vdot_values else None,
        "vdot_max": max(vdot_values) if vdot_values else None,
        "weekly_slices": weekly_slices,
        "recorded_zone_time_s": _aggregate_recorded_zone_time(activities),
        "key_activities": _find_key_activities(activities),
        "activity_log": [_activity_log_entry(item) for item in reversed(activities)],
        "fit_file_count": fit_count,
        "total_activity_count": len(activities),
    }


def format_period_summary(summary: dict[str, object], output_format: str) -> str:
    if output_format == "json":
        return json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    if output_format != "txt":
        raise ValueError(f"Unsupported period summary format: {output_format}")

    lines = [
        f"周期：{summary['start_date']} 至 {summary['end_date']}（{summary['fit_window_days']} 天）",
        f"活动：{summary['activity_count']} 次，FIT：{summary['fit_file_count']} 份",
        f"距离：{float(summary['total_distance_m']) / 1000.0:.2f} km",
        f"时长：{_format_duration(float(summary['total_duration_s']))}",
        f"TSS：{_format_optional(summary['total_tss'])}（已评分 {summary['scored_tss_activity_count']}，未评分 {summary['unscored_tss_activity_count']}）",
        f"VDOT：起始 {_format_optional(summary['vdot_start'])}，结束 {_format_optional(summary['vdot_end'])}，最高 {_format_optional(summary['vdot_max'])}",
        "周汇总：",
    ]
    weekly = summary["weekly_slices"]
    if isinstance(weekly, list):
        lines.extend(
            f"  {item['week_start']}  {item['activity_count']} 次  "
            f"{float(item['total_distance_m']) / 1000.0:.2f} km  "
            f"{_format_duration(float(item['total_duration_s']))}  TSS {_format_optional(item['total_tss'])}"
            for item in weekly
        )
    lines.append("周期亮点：")
    highlights = summary["key_activities"]
    if isinstance(highlights, list) and highlights:
        lines.extend(f"  {item['title']}：{item['metric_label']}（{item['name']}，{item['date']}）" for item in highlights)
    else:
        lines.append("  无可用亮点")
    return "\n".join(lines) + "\n"


def _build_weekly_slices(activities: list[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[date, list[dict[str, object]]] = defaultdict(list)
    for item in activities:
        activity_day = item["date"]
        week_start = activity_day - timedelta(days=activity_day.weekday())
        grouped[week_start].append(item)

    result: list[dict[str, object]] = []
    for week_start, items in sorted(grouped.items()):
        distances = [float(item["distance_m"]) for item in items if item["distance_m"] is not None]
        durations = [float(item["duration_s"]) for item in items if item["duration_s"] is not None]
        distance_duration_pairs = [
            (float(item["distance_m"]), float(item["duration_s"]))
            for item in items
            if item["distance_m"] is not None and item["duration_s"] is not None
        ]
        tss_values = [float(item["training_stress_score"]) for item in items if item["training_stress_score"] is not None]
        powers = [item for item in items if item["average_power_w"] is not None and item["duration_s"] is not None]
        power_duration = sum(float(item["duration_s"]) for item in powers)
        average_power = (
            sum(float(item["average_power_w"]) * float(item["duration_s"]) for item in powers) / power_duration
            if power_duration > 0
            else None
        )
        sport_types = {str(item["sport_type"]) for item in items if item["sport_type"]}
        paired_distance = sum(pair[0] for pair in distance_duration_pairs)
        paired_duration = sum(pair[1] for pair in distance_duration_pairs)
        average_pace = (
            paired_duration / paired_distance * 1000.0
            if len(sport_types) == 1 and paired_distance > 0
            else None
        )
        result.append(
            {
                "week_start": week_start.isoformat(),
                "total_distance_m": sum(distances),
                "total_duration_s": sum(durations),
                "activity_count": len(items),
                "total_tss": sum(tss_values) if tss_values else None,
                "average_pace_seconds_per_km": average_pace,
                "average_power_w": average_power,
            }
        )
    return result


def _find_key_activities(activities: list[dict[str, object]]) -> list[dict[str, object]]:
    candidates: list[tuple[str, str, str, str, bool]] = [
        ("最长距离", "distance_m", "distance", "km", True),
        ("最高 TSS", "training_stress_score", "tss", "TSS", True),
        ("最快配速", "pace_seconds_per_km", "pace", "/km", False),
        ("最高 NP", "normalized_power_w", "power", "W", True),
        ("最高爬升", "total_ascent_m", "elevation", "m", True),
        ("最长时长", "duration_s", "duration", "", True),
        ("最高均速", "average_speed_mps", "speed", "km/h", True),
    ]
    highlights: list[dict[str, object]] = []
    for title, field, highlight_type, unit, maximize in candidates:
        eligible = [item for item in activities if _optional_finite(item.get(field)) is not None]
        if field == "pace_seconds_per_km":
            eligible = [item for item in eligible if item["sport_type"] in RUNNING_SPORTS | SWIMMING_SPORTS]
        if not eligible:
            continue
        best = min(eligible, key=lambda item: float(item[field])) if not maximize else max(
            eligible, key=lambda item: float(item[field])
        )
        highlights.append(
            {
                "activity_id": best["activity_id"],
                "title": title,
                "metric_label": _format_highlight(float(best[field]), highlight_type, unit, str(best["sport_type"])),
                "date": best["date"].isoformat(),
                "highlight_type": highlight_type,
                "name": best["name"],
            }
        )
    return highlights


def _activity_log_entry(item: dict[str, object]) -> dict[str, object]:
    return {
        "activity_id": item["activity_id"],
        "date": item["start_time"].isoformat(),
        "distance_m": item["distance_m"],
        "duration_s": item["duration_s"],
        "average_pace_seconds_per_km": item["pace_seconds_per_km"],
        "average_speed_mps": item["average_speed_mps"],
        "average_heart_rate_bpm": item["average_heart_rate_bpm"],
        "average_power_w": item["average_power_w"],
        "normalized_power_w": item["normalized_power_w"],
        "intensity_factor": item["intensity_factor"],
        "training_stress_score": item["training_stress_score"],
        "vdot": item["vdot"],
        "functional_threshold_power_w": item["ftp"],
        "estimated_ftp_w": item["estimated_ftp"],
        "critical_swim_speed": item["css"],
        "total_ascent_m": item["total_ascent_m"],
        "sport_type": item["sport_type"],
        "name": item["name"],
    }


def _aggregate_recorded_zone_time(activities: list[dict[str, object]]) -> dict[str, dict[int, float]]:
    totals: dict[str, dict[int, float]] = {name: defaultdict(float) for name in RECORDED_ZONE_FIELDS}
    for activity in activities:
        messages = activity["time_in_zone_messages"]
        if not isinstance(messages, list):
            raise ValueError("Activity time_in_zone_messages must be a list")
        for message in messages:
            if not isinstance(message, Mapping):
                raise ValueError("Activity time-in-zone message must be an object")
            for zone_type, field_name in RECORDED_ZONE_FIELDS.items():
                zones = message.get(field_name)
                if zones is None:
                    continue
                if not isinstance(zones, list):
                    raise ValueError(f"Activity {field_name} must be a list")
                for zone in zones:
                    if not isinstance(zone, Mapping):
                        raise ValueError(f"Activity {field_name} entry must be an object")
                    zone_id = _optional_finite(zone.get("zone"))
                    if zone_id is None or zone_id < 0 or not zone_id.is_integer():
                        raise ValueError(f"Activity {field_name} entry has an invalid zone")
                    seconds = _optional_nonnegative(zone.get("seconds"))
                    if seconds is not None:
                        totals[zone_type][int(zone_id)] += seconds
    return {name: dict(sorted(values.items())) for name, values in totals.items()}


def _zone_messages(value: object) -> list[Mapping[str, object]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("Activity time_in_zone_messages must be a list")
    messages: list[Mapping[str, object]] = []
    for message in value:
        if not isinstance(message, Mapping):
            raise ValueError("Activity time-in-zone message must be an object")
        messages.append(message)
    return messages


def _format_highlight(value: float, kind: str, unit: str, sport: str) -> str:
    if kind == "distance":
        return f"{value / 1000.0:.1f} {unit}"
    if kind == "tss":
        return f"{value:.0f} {unit}"
    if kind == "pace":
        if sport in SWIMMING_SPORTS:
            return f"{_format_clock(value / 10.0)} /100m"
        return f"{_format_clock(value)} {unit}"
    if kind == "power":
        return f"{value:.0f} {unit}"
    if kind == "elevation":
        return f"{value:.0f} {unit}"
    if kind == "duration":
        return _format_duration(value)
    return f"{value * 3.6:.1f} {unit}"


def _format_duration(seconds: float) -> str:
    rounded = max(0, int(round(seconds)))
    hours, remainder = divmod(rounded, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes}:{secs:02}"


def _format_clock(seconds: float) -> str:
    rounded = max(0, int(round(seconds)))
    minutes, secs = divmod(rounded, 60)
    return f"{minutes}:{secs:02}"


def _format_optional(value: object) -> str:
    return "未知" if value is None else f"{float(value):.1f}"


def _activity_start_time(value: object) -> datetime | None:
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    return parsed.astimezone(timezone.utc)


def _load_summary(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("Local activity summary is invalid JSON") from exc
        if isinstance(decoded, dict):
            return decoded
    raise ValueError("Local activity summary must be a JSON object")


def _row_value(row: object, key: str) -> object | None:
    if isinstance(row, Mapping):
        return row.get(key)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return getattr(row, key, None)


def _normalize_sport(value: object) -> str:
    normalized = str(value or "").strip().lower().replace(" ", "_")
    return SPORT_ALIASES.get(normalized, normalized)


def _optional_finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _optional_nonnegative(value: object) -> float | None:
    parsed = _optional_finite(value)
    return parsed if parsed is not None and parsed >= 0 else None


def _optional_positive(value: object) -> float | None:
    parsed = _optional_finite(value)
    return parsed if parsed is not None and parsed > 0 else None


def _positive_finite(value: float, label: str) -> float:
    parsed = _optional_finite(value)
    if parsed is None or parsed <= 0:
        raise ValueError(f"{label} must be a positive finite number")
    return parsed
