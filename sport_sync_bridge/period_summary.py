from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping

from .formats import read_activity_file
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
POWER_CURVE_DURATIONS_S = (10, 60, 120, 360, 600, 2400, 3600, 7200, 14400, 21600)
POWER_FILE_FORMATS = {"fit", "gpx", "tcx"}
SAMPLED_ZONE_FIELDS = {
    "heart_rate": ("heart_rate_zones", "heart_rate_bpm"),
    "speed": ("speed_zones", "speed_mps"),
}
SAMPLE_ZONE_MAX_GAP_SECONDS = 30.0
CADENCE_SPORTS = RUNNING_SPORTS | {"cycling"}
CYCLING_SPORTS = {"cycling", "ride", "virtual_ride", "indoor_cycling", "mountain_biking"}
PR_DISTANCE_TARGETS = {
    "running": (("5K", 5_000.0), ("10K", 10_000.0), ("halfMarathon", 21_097.5), ("marathon", 42_195.0)),
    "cycling": (("40K", 40_000.0),),
    "swimming": (("100m", 100.0), ("400m", 400.0), ("1500m", 1_500.0)),
}
PR_DISTANCE_TOLERANCE = 0.03


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
    pr_history: list[dict[str, object]] = []
    for row in rows:
        summary = _load_summary(_row_value(row, "summary_json"))
        activity_sport = _normalize_sport(_row_value(row, "sport_type") or summary.get("sport_type"))
        if selected_sport is not None and activity_sport != selected_sport:
            continue
        start_time = _activity_start_time(_row_value(row, "start_time") or summary.get("start_time"))
        if start_time is None:
            continue

        distance = _optional_nonnegative(summary.get("distance_m"))
        duration = _optional_positive(summary.get("timer_time_s"))
        if duration is None:
            duration = _optional_positive(summary.get("elapsed_time_s"))
        if distance is not None and duration is not None:
            pr_history.append(
                {
                    "activity_id": str(_row_value(row, "fingerprint") or summary.get("activity_id") or ""),
                    "sport_type": activity_sport,
                    "start_time": start_time,
                    "distance_m": distance,
                    "duration_s": duration,
                }
            )
        if not start_day <= start_time.date() <= end_day:
            continue

        average_speed = _optional_positive(summary.get("average_speed_mps"))
        if average_speed is None and distance is not None and duration is not None:
            average_speed = distance / duration
        pace = _optional_positive(summary.get("pace_seconds_per_km"))
        if pace is None and average_speed is not None:
            pace = 1000.0 / average_speed
        average_hr = _optional_nonnegative(summary.get("average_heart_rate_bpm"))
        average_cadence = _optional_nonnegative(summary.get("average_cadence_rpm"))
        average_power = _optional_nonnegative(summary.get("average_power_w"))
        normalized_power = _optional_nonnegative(summary.get("normalized_power_w"))
        intensity_factor = _optional_nonnegative(summary.get("intensity_factor"))
        ftp_estimate_from_np_if = (
            normalized_power / intensity_factor
            if normalized_power is not None and intensity_factor is not None and intensity_factor > 0
            else None
        )
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
                "file_path": _row_value(row, "file_path"),
                "start_time": start_time,
                "date": start_time.date(),
                "distance_m": distance,
                "duration_s": duration,
                "average_speed_mps": average_speed,
                "pace_seconds_per_km": pace,
                "average_heart_rate_bpm": average_hr,
                "average_cadence": average_cadence,
                "average_power_w": average_power,
                "normalized_power_w": normalized_power,
                "intensity_factor": intensity_factor,
                "ftp_estimate_from_np_if_w": ftp_estimate_from_np_if,
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
    normalized_power_values = [
        float(value)
        for item in activities
        if (value := _optional_nonnegative(item.get("normalized_power_w"))) is not None and value > 0
    ]
    cadence_values = [
        float(value)
        for item in activities
        if item["sport_type"] in CADENCE_SPORTS
        and (value := _optional_nonnegative(item.get("average_cadence"))) is not None
    ]
    total_distance = sum(float(item["distance_m"]) for item in activities if item["distance_m"] is not None)
    total_duration = sum(float(item["duration_s"]) for item in activities if item["duration_s"] is not None)
    total_tss = sum(float(item["training_stress_score"]) for item in scored)
    fit_count = sum(item["file_format"] == "fit" for item in activities)
    parsed_activity_files = _read_period_activity_files(activities)
    power_curve, power_sample_activity_count, power_curve_unavailable_count = _aggregate_power_curves(
        activities, parsed_activity_files
    )
    sampled_zone_time, sampled_zone_activity_count = _aggregate_sampled_zone_times(
        activities, parsed_activity_files
    )
    pr_changes = _find_pr_changes(pr_history, start_day, end_day)
    ftp_trend = _build_ftp_trend(activities)

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
        "ftp_trend": ftp_trend,
        "avg_norm_power_w": (
            sum(normalized_power_values) / len(normalized_power_values)
            if normalized_power_values
            else None
        ),
        "avg_norm_power_activity_count": len(normalized_power_values),
        "avg_cadence": sum(cadence_values) / len(cadence_values) if cadence_values else None,
        "avg_cadence_activity_count": len(cadence_values),
        "pr_changes": pr_changes,
        "weekly_slices": weekly_slices,
        "recorded_zone_time_s": _aggregate_recorded_zone_time(activities),
        "sampled_zone_time_s": sampled_zone_time,
        "sampled_zone_activity_count": sampled_zone_activity_count,
        "power_curve_w": power_curve,
        "power_curve_sample_activity_count": power_sample_activity_count,
        "power_curve_unavailable_activity_count": power_curve_unavailable_count,
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
        f"平均踏频：{_format_optional(summary['avg_cadence'])}（{summary['avg_cadence_activity_count']} 次有效活动）",
        f"平均 NP：{_format_optional(summary['avg_norm_power_w'])} W（{summary['avg_norm_power_activity_count']} 次有效活动）",
        "个人纪录变化：",
    ]
    sampled_zones = summary.get("sampled_zone_time_s")
    sampled_zone_counts = summary.get("sampled_zone_activity_count")
    if isinstance(sampled_zones, Mapping):
        for metric, label in (("heart_rate", "心率"), ("speed", "速度")):
            zone_times = sampled_zones.get(metric)
            if not isinstance(zone_times, Mapping) or not zone_times:
                continue
            values = "，".join(
                f"Z{int(zone)} {float(seconds):g}秒"
                for zone, seconds in sorted(zone_times.items(), key=lambda item: int(item[0]))
            )
            activity_count = (
                sampled_zone_counts.get(metric, 0)
                if isinstance(sampled_zone_counts, Mapping)
                else 0
            )
            lines.append(f"轨迹重算{label}分区：{values}（{activity_count} 次活动）")
    ftp_trend = summary.get("ftp_trend")
    if isinstance(ftp_trend, Mapping):
        if ftp_trend.get("single_value_w") is not None:
            lines.append(
                f"FTP估算：{_format_optional(ftp_trend['single_value_w'])} W"
                "（2 个有效样本，单值结果）"
            )
        else:
            lines.append(
                f"FTP趋势：{float(ftp_trend['first_window_best_w']):.0f} W → "
                f"{float(ftp_trend['last_window_best_w']):.0f} W"
                f"（{int(ftp_trend['window_days'])} 天窗口，区间最大 "
                f"{float(ftp_trend['period_best_w']):.0f} W，变化 "
                f"{float(ftp_trend['change_w']):+.0f} W）"
            )
    else:
        lines.append("FTP估算：暂无 NP / 正 IF 有效样本")
    pr_changes = summary.get("pr_changes")
    if isinstance(pr_changes, list) and pr_changes:
        for record in pr_changes:
            if not isinstance(record, Mapping) or record.get("newValue") is None:
                continue
            previous = record.get("oldValue")
            achievement = "首次本地记录" if previous is None else f"提升 {float(record['improvementPct']):.1f}%"
            lines.append(
                f"  {record['prType']}：{_format_duration(float(record['newValue']))}"
                f"（{achievement}，{record['achievedAt']}）"
            )
    else:
        lines.append("  无周期个人纪录变化")
    lines.append("周汇总：")
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
    power_curve = summary["power_curve_w"]
    if isinstance(power_curve, Mapping) and power_curve:
        values = ", ".join(
            f"{int(duration)} 秒 {int(watts)} W"
            for duration, watts in sorted(power_curve.items(), key=lambda item: int(item[0]))
        )
        lines.append(f"功率曲线：{values}")
    else:
        lines.append("功率曲线：暂无符合时长要求的功率样本")
    if int(summary["power_curve_unavailable_activity_count"]):
        lines.append(f"功率曲线缺少可读取的运动文件：{summary['power_curve_unavailable_activity_count']} 项")
    return "\n".join(lines) + "\n"


def _find_pr_changes(
    history: list[dict[str, object]],
    start_day: date,
    end_day: date,
) -> list[dict[str, object]]:
    current_sports = {
        str(item["sport_type"])
        for item in history
        if start_day <= item["start_time"].date() <= end_day
    }
    changes: list[dict[str, object]] = []
    for sport in sorted(current_sports):
        if sport in RUNNING_SPORTS:
            targets = PR_DISTANCE_TARGETS["running"]
        elif sport in CYCLING_SPORTS:
            targets = PR_DISTANCE_TARGETS["cycling"]
        elif sport in SWIMMING_SPORTS:
            targets = PR_DISTANCE_TARGETS["swimming"]
        else:
            continue

        for pr_type, target_distance in targets:
            tolerance_m = target_distance * PR_DISTANCE_TOLERANCE
            candidates = [
                item
                for item in history
                if item["sport_type"] == sport
                and abs(float(item["distance_m"]) - target_distance) <= tolerance_m
            ]
            previous = [item for item in candidates if item["start_time"].date() < start_day]
            current = [item for item in candidates if start_day <= item["start_time"].date() <= end_day]
            if not current:
                continue

            best_current = min(
                current,
                key=lambda item: (float(item["duration_s"]), item["start_time"], str(item["activity_id"])),
            )
            best_previous = min(
                previous,
                key=lambda item: (float(item["duration_s"]), item["start_time"], str(item["activity_id"])),
                default=None,
            )
            old_value = float(best_previous["duration_s"]) if best_previous is not None else None
            new_value = float(best_current["duration_s"])
            if old_value is not None and new_value >= old_value:
                continue
            changes.append(
                {
                    "prType": pr_type,
                    "oldValue": old_value,
                    "newValue": new_value,
                    "achievedAt": best_current["start_time"].isoformat(),
                    "activityId": str(best_current["activity_id"]),
                    "improvementPct": (old_value - new_value) / old_value * 100.0 if old_value else None,
                }
            )
    return changes


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
        "ftp_estimate_from_np_if_w": item["ftp_estimate_from_np_if_w"],
        "training_stress_score": item["training_stress_score"],
        "vdot": item["vdot"],
        "functional_threshold_power_w": item["ftp"],
        "estimated_ftp_w": item["estimated_ftp"],
        "critical_swim_speed": item["css"],
        "total_ascent_m": item["total_ascent_m"],
        "sport_type": item["sport_type"],
        "name": item["name"],
    }


def _build_ftp_trend(activities: list[dict[str, object]]) -> dict[str, object] | None:
    samples = [
        (item["start_time"], float(estimate))
        for item in activities
        if (estimate := _optional_nonnegative(item.get("ftp_estimate_from_np_if_w"))) is not None
    ]
    if not samples:
        return None
    if len(samples) == 2:
        return {
            "sample_count": 2,
            "window_days": None,
            "single_value_w": samples[0][1],
        }

    span_days = max(0, (samples[-1][0] - samples[0][0]).days)
    requested_window_days = (
        math.floor(span_days * 0.2 + 0.5)
        if span_days >= 14
        else span_days
    )
    window_days = min(30, max(7, requested_window_days))
    first_window_end = samples[0][0] + timedelta(days=window_days)
    last_window_start = samples[-1][0] - timedelta(days=window_days)
    first_window_best = max(value for timestamp, value in samples if timestamp <= first_window_end)
    last_window_best = max(value for timestamp, value in samples if timestamp >= last_window_start)
    period_best = max(value for _, value in samples)
    return {
        "sample_count": len(samples),
        "window_days": window_days,
        "first_window_best_w": first_window_best,
        "last_window_best_w": last_window_best,
        "period_best_w": period_best,
        "change_w": last_window_best - first_window_best,
    }


def _read_period_activity_files(activities: list[dict[str, object]]) -> dict[int, object]:
    parsed_files: dict[int, object] = {}
    for index, activity in enumerate(activities):
        file_format = str(activity.get("file_format") or "").lower()
        raw_path = activity.get("file_path")
        if (
            file_format not in POWER_FILE_FORMATS
            or not isinstance(raw_path, (str, Path))
            or not raw_path
        ):
            continue
        path = Path(raw_path)
        if path.is_file():
            parsed_files[index] = read_activity_file(path)
    return parsed_files


def _aggregate_power_curves(
    activities: list[dict[str, object]],
    parsed_files: Mapping[int, object] | None = None,
) -> tuple[dict[int, int], int, int]:
    if parsed_files is None:
        parsed_files = _read_period_activity_files(activities)
    curve: dict[int, int] = {}
    sample_activity_count = 0
    unavailable_activity_count = 0
    for index, activity in enumerate(activities):
        file_format = str(activity.get("file_format") or "").lower()
        raw_path = activity.get("file_path")
        if file_format not in POWER_FILE_FORMATS or not isinstance(raw_path, (str, Path)) or not raw_path:
            unavailable_activity_count += 1
            continue
        parsed = parsed_files.get(index)
        if parsed is None:
            unavailable_activity_count += 1
            continue
        if any(
            isinstance(getattr(point, "timestamp", None), datetime)
            and _optional_nonnegative(getattr(point, "power_w", None)) is not None
            for point in parsed.track_points
        ):
            sample_activity_count += 1
        samples = calculate_activity_power_curve(parsed.track_points)
        for duration, watts in samples.items():
            curve[duration] = max(curve.get(duration, watts), watts)
    return dict(sorted(curve.items())), sample_activity_count, unavailable_activity_count


def _aggregate_sampled_zone_times(
    activities: list[dict[str, object]],
    parsed_files: Mapping[int, object],
) -> tuple[dict[str, dict[int, float]], dict[str, int]]:
    totals: dict[str, dict[int, float]] = {metric: defaultdict(float) for metric in SAMPLED_ZONE_FIELDS}
    activity_counts = {metric: 0 for metric in SAMPLED_ZONE_FIELDS}
    for index, activity in enumerate(activities):
        parsed = parsed_files.get(index)
        if parsed is None:
            continue
        track_points = getattr(parsed, "track_points", [])
        messages = activity["time_in_zone_messages"]
        for metric, (zone_field, sample_field) in SAMPLED_ZONE_FIELDS.items():
            zone_config = _consistent_zone_thresholds(messages, zone_field)
            if zone_config is None:
                continue
            boundaries, overflow_zone = zone_config
            activity_zones = calculate_activity_sampled_zone_time(
                track_points,
                sample_field,
                boundaries,
                overflow_zone,
                reject_zero=metric == "heart_rate",
            )
            if not activity_zones:
                continue
            activity_counts[metric] += 1
            for zone, seconds in activity_zones.items():
                totals[metric][zone] += seconds
    return (
        {
            metric: dict(sorted(zone_times.items()))
            for metric, zone_times in totals.items()
        },
        activity_counts,
    )


def _consistent_zone_thresholds(
    messages: list[Mapping[str, object]],
    zone_field: str,
) -> tuple[tuple[tuple[int, float], ...], int] | None:
    candidates: set[tuple[tuple[tuple[int, float], ...], int]] = set()
    for message in messages:
        raw_zones = message.get(zone_field)
        if raw_zones is None:
            continue
        if not isinstance(raw_zones, list):
            raise ValueError(f"Activity {zone_field} must be a list")

        zone_ids: list[int] = []
        boundaries: dict[int, float] = {}
        for zone in raw_zones:
            if not isinstance(zone, Mapping):
                raise ValueError(f"Activity {zone_field} entry must be an object")
            zone_id = _optional_finite(zone.get("zone"))
            if zone_id is None or zone_id < 0 or not zone_id.is_integer():
                raise ValueError(f"Activity {zone_field} entry has an invalid zone")
            zone_number = int(zone_id)
            zone_ids.append(zone_number)
            boundary = _optional_nonnegative(zone.get("high_boundary"))
            if boundary is not None and boundary > 0:
                boundaries[zone_number] = boundary

        if len(zone_ids) != len(set(zone_ids)):
            continue
        ordered_boundaries = tuple(sorted(boundaries.items()))
        if not ordered_boundaries:
            continue
        boundary_zone_ids = [zone for zone, _ in ordered_boundaries]
        if boundary_zone_ids != list(range(1, len(boundary_zone_ids) + 1)):
            continue
        if any(
            current[1] <= previous[1]
            for previous, current in zip(ordered_boundaries, ordered_boundaries[1:])
        ):
            continue
        highest_zone = max(zone_ids, default=0)
        last_boundary_zone = boundary_zone_ids[-1]
        if highest_zone not in {last_boundary_zone, last_boundary_zone + 1}:
            continue
        candidates.add((ordered_boundaries, highest_zone))

    if len(candidates) != 1:
        return None
    return next(iter(candidates))


def calculate_activity_sampled_zone_time(
    points: Iterable[object],
    sample_field: str,
    boundaries: tuple[tuple[int, float], ...],
    overflow_zone: int,
    *,
    reject_zero: bool = False,
) -> dict[int, float]:
    samples: list[tuple[datetime, float]] = []
    for point in points:
        timestamp = getattr(point, "timestamp", None)
        if not isinstance(timestamp, datetime):
            continue
        value = _optional_nonnegative(getattr(point, sample_field, None))
        if value is None or (reject_zero and value == 0):
            continue
        timestamp = (
            timestamp.replace(tzinfo=timezone.utc)
            if timestamp.tzinfo is None
            else timestamp.astimezone(timezone.utc)
        )
        samples.append((timestamp, value))
    samples.sort(key=lambda item: item[0])
    if len(samples) < 2:
        return {}

    seconds_by_zone: dict[int, float] = defaultdict(float)
    for (start_time, start_value), (end_time, end_value) in zip(samples, samples[1:]):
        interval = (end_time - start_time).total_seconds()
        if interval <= 0 or interval > SAMPLE_ZONE_MAX_GAP_SECONDS:
            continue
        cuts = [0.0, 1.0]
        if end_value != start_value:
            for _, boundary in boundaries:
                fraction = (boundary - start_value) / (end_value - start_value)
                if 0.0 < fraction < 1.0:
                    cuts.append(fraction)
        cuts.sort()
        for left, right in zip(cuts, cuts[1:]):
            midpoint = start_value + (end_value - start_value) * (left + right) / 2
            zone = next(
                (zone_id for zone_id, boundary in boundaries if midpoint <= boundary),
                overflow_zone,
            )
            seconds_by_zone[zone] += interval * (right - left)
    return dict(sorted(seconds_by_zone.items()))


def calculate_activity_power_curve(points: Iterable[object]) -> dict[int, int]:
    samples: list[tuple[datetime, float | None]] = []
    for point in points:
        timestamp = getattr(point, "timestamp", None)
        if not isinstance(timestamp, datetime):
            continue
        timestamp = timestamp.replace(tzinfo=timezone.utc) if timestamp.tzinfo is None else timestamp.astimezone(timezone.utc)
        power = _optional_nonnegative(getattr(point, "power_w", None))
        samples.append((timestamp, power))
    samples.sort(key=lambda item: item[0])
    if len(samples) < 2:
        return {}

    activity_span = (samples[-1][0] - samples[0][0]).total_seconds()
    curve: dict[int, int] = {}
    for duration in POWER_CURVE_DURATIONS_S:
        if activity_span < duration:
            continue
        left = 0
        power_sum = 0.0
        power_count = 0
        best_average: float | None = None
        for right, (end_time, power) in enumerate(samples):
            if power is not None:
                power_sum += power
                power_count += 1
            cutoff = end_time - timedelta(seconds=duration)
            while left <= right and samples[left][0] < cutoff:
                old_power = samples[left][1]
                if old_power is not None:
                    power_sum -= old_power
                    power_count -= 1
                left += 1
            if end_time - samples[left][0] < timedelta(seconds=duration) or power_count == 0:
                continue
            average = power_sum / power_count
            if best_average is None or average > best_average:
                best_average = average
        if best_average is not None:
            curve[duration] = int(best_average)
    return curve


def _activity_power_samples(points: Iterable[object]) -> dict[int, int]:
    return calculate_activity_power_curve(points)


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
