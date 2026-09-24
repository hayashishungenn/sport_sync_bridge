from __future__ import annotations

import csv
import html
import io
import json
import re
import statistics
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .formats import ActivityFile, ActivityTimeInZone, ActivityZoneTime, TrackPoint


def summarize_activity(activity: ActivityFile) -> dict[str, object]:
    points = activity.track_points
    timed = [point.timestamp for point in points if point.timestamp is not None]
    start = activity.start_time or (min(timed) if timed else None)
    end = activity.end_time or (max(timed) if timed else None)
    distance = activity.distance_m
    if distance is None:
        distances = [point.distance_m for point in points if point.distance_m is not None]
        distance = max(distances) if distances else None
    elapsed = activity.elapsed_time_s
    if elapsed is None and start and end:
        elapsed = max(0.0, (end - start).total_seconds())
    timer = activity.timer_time_s
    duration = timer or elapsed
    speeds = [point.speed_mps for point in points if point.speed_mps is not None]
    heart_rates = [point.heart_rate_bpm for point in points if point.heart_rate_bpm is not None]
    cadences = [point.cadence_rpm for point in points if point.cadence_rpm is not None]
    powers = [point.power_w for point in points if point.power_w is not None]
    altitudes = [point.elevation_m for point in points if point.elevation_m is not None]
    ascent = sum(max(0.0, current - previous) for previous, current in zip(altitudes, altitudes[1:]))
    if speeds:
        average_speed = statistics.fmean(speeds)
    elif distance is not None and duration and duration > 0:
        average_speed = distance / duration
    else:
        average_speed = None
    pace = 1000.0 / average_speed if average_speed and average_speed > 0 else None
    average_hr = activity.average_heart_rate_bpm
    if average_hr is None:
        average_hr = statistics.fmean(heart_rates) if heart_rates else _lap_hr(activity, "average_heart_rate")
    maximum_hr = activity.maximum_heart_rate_bpm
    if maximum_hr is None:
        maximum_hr = max(heart_rates) if heart_rates else _lap_hr(activity, "maximum_heart_rate", maximum=True)
    average_power = activity.average_power_w
    if average_power is None and powers:
        average_power = statistics.fmean(powers)
    maximum_power = activity.maximum_power_w
    if maximum_power is None and powers:
        maximum_power = max(powers)
    return {
        "name": activity.name,
        "sport_type": activity.sport_type,
        "start_time": start.isoformat() if start else None,
        "end_time": end.isoformat() if end else None,
        "distance_m": distance,
        "elapsed_time_s": elapsed,
        "timer_time_s": timer,
        "average_speed_mps": average_speed,
        "pace_seconds_per_km": pace,
        "average_heart_rate_bpm": average_hr,
        "maximum_heart_rate_bpm": maximum_hr,
        "training_stress_score": activity.training_stress_score,
        "average_cadence_rpm": statistics.fmean(cadences) if cadences else None,
        "average_power_w": average_power,
        "maximum_power_w": maximum_power,
        "normalized_power_w": activity.normalized_power_w,
        "intensity_factor": activity.intensity_factor,
        "aerobic_training_effect": activity.aerobic_training_effect,
        "anaerobic_training_effect": activity.anaerobic_training_effect,
        "total_ascent_m": ascent if altitudes else None,
        "time_in_zone_messages": [
            _time_in_zone_summary(message) for message in activity.time_in_zone_messages
        ],
        "lap_count": len(activity.laps),
        "track_point_count": len(points),
        "has_gps_track": any(point.latitude is not None and point.longitude is not None for point in points),
        "conversion_losses": list(activity.losses),
    }


def _lap_hr(activity: ActivityFile, attribute: str, *, maximum: bool = False) -> float | None:
    values = [getattr(lap, attribute) for lap in activity.laps if getattr(lap, attribute) is not None]
    if not values:
        return None
    return max(values) if maximum else statistics.fmean(values)


def validate_ai_language_code(language: str) -> str:
    if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", language):
        raise ValueError("AI analysis language must be a language code such as zh-CN or en")
    return language


def summarize_rows(rows: Iterable[object]) -> dict[str, object]:
    summaries = [_row_summary(row) for row in rows]
    sports: dict[str, dict[str, float | int]] = {}
    weeks: dict[str, dict[str, float | int]] = {}
    for summary in summaries:
        sport = str(summary.get("sport_type") or "unknown")
        sport_total = sports.setdefault(sport, {"activities": 0, "distance_m": 0.0, "duration_s": 0.0})
        sport_total["activities"] += 1
        sport_total["distance_m"] += _number(summary.get("distance_m")) or 0.0
        sport_total["duration_s"] += _number(summary.get("timer_time_s")) or _number(summary.get("elapsed_time_s")) or 0.0
        start = _parse_iso(summary.get("start_time"))
        if start is not None:
            monday = start.date() - timedelta(days=start.weekday())
            key = monday.isoformat()
            week_total = weeks.setdefault(key, {"activities": 0, "distance_m": 0.0, "duration_s": 0.0})
            week_total["activities"] += 1
            week_total["distance_m"] += _number(summary.get("distance_m")) or 0.0
            week_total["duration_s"] += _number(summary.get("timer_time_s")) or _number(summary.get("elapsed_time_s")) or 0.0
    return {
        "activity_count": len(summaries),
        "distance_m": sum(_number(item.get("distance_m")) or 0.0 for item in summaries),
        "duration_s": sum(
            _number(item.get("timer_time_s")) or _number(item.get("elapsed_time_s")) or 0.0
            for item in summaries
        ),
        "sports": sports,
        "weeks": dict(sorted(weeks.items())),
    }


def format_activity_report(rows: Iterable[object], output_format: str) -> str:
    rows = list(rows)
    summaries = [_row_summary(row) for row in rows]
    fields = (
        "activity_id",
        "name",
        "sport_type",
        "start_time",
        "distance_m",
        "elapsed_time_s",
        "timer_time_s",
        "average_speed_mps",
        "average_heart_rate_bpm",
        "maximum_heart_rate_bpm",
        "average_cadence_rpm",
        "average_power_w",
        "maximum_power_w",
        "normalized_power_w",
        "intensity_factor",
        "aerobic_training_effect",
        "anaerobic_training_effect",
        "training_stress_score",
        "total_ascent_m",
        "time_in_zone_messages",
        "lap_count",
        "track_point_count",
    )
    records = [
        {key: (str(row["fingerprint"])[:12] if key == "activity_id" else summary.get(key)) for key in fields}
        for row, summary in zip(rows, summaries)
    ]
    if output_format == "json":
        return json.dumps(records, ensure_ascii=False, indent=2) + "\n"
    if output_format == "csv":
        _encode_nested_report_fields(records, "time_in_zone_messages")
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
        return stream.getvalue()
    if output_format == "html":
        _encode_nested_report_fields(records, "time_in_zone_messages")
        headers = "".join(f"<th>{html.escape(field.replace('_', ' '))}</th>" for field in fields)
        body = "".join(
            "<tr>" + "".join(f"<td>{html.escape(str(record.get(field) or ''))}</td>" for field in fields) + "</tr>"
            for record in records
        )
        return (
            "<!doctype html><meta charset=\"utf-8\"><title>Activity report</title>"
            "<style>body{font:14px system-ui;margin:2rem}table{border-collapse:collapse}"
            "th,td{border:1px solid #ccc;padding:.4rem;text-align:left}</style>"
            f"<table><thead><tr>{headers}</tr></thead><tbody>{body}</tbody></table>\n"
        )
    if output_format == "txt":
        if not records:
            return "No activities.\n"
        lines = []
        for record in records:
            distance = _number(record.get("distance_m"))
            duration = _number(record.get("timer_time_s")) or _number(record.get("elapsed_time_s"))
            lines.append(
                f"{record['activity_id']}  {record.get('start_time') or 'unknown time'}  "
                f"{record.get('sport_type') or 'unknown'}  {record.get('name') or 'activity'}  "
                f"{distance / 1000:.2f} km  {_format_duration(duration)}"
            )
        return "\n".join(lines) + "\n"
    raise ValueError(f"Unsupported activity report format: {output_format}")


def build_ai_analysis_prompt(
    activity_summary: dict[str, object],
    recent_rows: Iterable[object],
    question: str | None = None,
    health_summary: dict[str, object] | None = None,
    *,
    language: str = "zh-CN",
    focus: str = "performance",
    detail: str = "normal",
) -> str:
    language = validate_ai_language_code(language)
    focus_instructions = {
        "performance": "重点分析速度、功率、心率效率，并给出提升运动表现的建议。",
        "health": "重点关注训练负荷的影响和长期健康价值，不作医学判断。",
        "recovery": "重点评估恢复状态和过度训练风险，并给出恢复建议，不作医学判断。",
    }
    detail_instructions = {
        "brief": "使用纯文本，不要 Markdown 标题。训练分析写 2-3 句，改进建议给出 1-2 条，控制在一屏左右。",
        "normal": "使用 Markdown 标题。训练分析写 2-3 段，改进建议写 1-2 段并包含 2-3 条建议，控制在两屏左右。",
        "detailed": "使用 Markdown 标题。训练分析覆盖 3-5 个维度，给出 3-5 条有数据依据的建议；每个维度写 1-3 段，控制在五屏以内。",
    }
    if focus not in focus_instructions:
        raise ValueError(f"Unsupported AI analysis focus: {focus}")
    if detail not in detail_instructions:
        raise ValueError(f"Unsupported AI analysis detail: {detail}")

    recent = summarize_rows(recent_rows)
    lines = [
        "你是一名注重数据的耐力运动教练。分析下面的运动和健康数据，并给出有依据、可执行的建议。",
        f"必须使用语言代码 {language} 作答。",
        "准确引用数据中的数值，不要取整、近似或编造。缺失的指标明确说明未知。",
        "不要提供医疗建议或作医学诊断。",
        "回答分为训练分析（Training Analysis）和改进建议（Improvement Advice）两部分。",
        f"分析侧重点：{focus_instructions[focus]}",
        f"回答详略：{detail_instructions[detail]}",
        "为保护隐私，内容不包含 GPS 坐标，也不请求身份信息。",
        "",
        "本次活动：",
        f"运动类型：{activity_summary.get('sport_type') or '未知'}",
        f"开始时间：{activity_summary.get('start_time') or '未知'}",
        f"距离（米）：{_display(activity_summary.get('distance_m'))}",
        f"计时（秒）：{_display(activity_summary.get('timer_time_s'))}",
        f"经过时间（秒）：{_display(activity_summary.get('elapsed_time_s'))}",
        f"平均速度（米/秒）：{_display(activity_summary.get('average_speed_mps'))}",
        f"平均心率：{_display(activity_summary.get('average_heart_rate_bpm'))}",
        f"最高心率：{_display(activity_summary.get('maximum_heart_rate_bpm'))}",
        f"平均踏频：{_display(activity_summary.get('average_cadence_rpm'))}",
        f"平均功率（瓦）：{_display(activity_summary.get('average_power_w'))}",
        f"最大功率（瓦）：{_display(activity_summary.get('maximum_power_w'))}",
        f"标准化功率（瓦）：{_display(activity_summary.get('normalized_power_w'))}",
        f"强度因子（IF）：{_display(activity_summary.get('intensity_factor'))}",
        f"有氧训练效果：{_display(activity_summary.get('aerobic_training_effect'))}",
        f"无氧训练效果：{_display(activity_summary.get('anaerobic_training_effect'))}",
        f"训练压力分（TSS）：{_display(activity_summary.get('training_stress_score'))}",
        f"爬升（米）：{_display(activity_summary.get('total_ascent_m'))}",
        f"轨迹点数：{_display(activity_summary.get('track_point_count'))}",
        "心率分区时间："
        + _format_zone_groups(
            activity_summary.get("time_in_zone_messages"),
            "heart_rate_zones",
            "heart_rate_calculation",
            "bpm",
        ),
        "功率分区时间："
        + _format_zone_groups(
            activity_summary.get("time_in_zone_messages"),
            "power_zones",
            "power_calculation",
            "W",
        ),
        "",
        f"已导入活动：{recent['activity_count']} 次",
        f"活动总距离（米）：{recent['distance_m']:.1f}",
        f"活动总时长（秒）：{recent['duration_s']:.1f}",
        "按周训练汇总：",
    ]
    weeks = recent["weeks"]
    for week_start, values in list(weeks.items())[-8:]:
        lines.append(
            f"{week_start}: {values['activities']} 次，{values['distance_m'] / 1000:.2f} 公里，"
            f"{values['duration_s'] / 3600:.2f} 小时"
        )
    health_before = health_summary.get("before_activity") if isinstance(health_summary, dict) else None
    if isinstance(health_before, dict) and health_before:
        lines.extend(("", "活动开始前每项指标最近一次记录（时间均早于活动开始，时间戳为 UTC）："))
        for metric, value in sorted(health_before.items()):
            if isinstance(value, dict):
                lines.append(
                    f"{metric}: {value.get('value')} {value.get('unit', '')} "
                    f"({value.get('observed_at', 'unknown time')})"
                )
    health_after = health_summary.get("after_activity") if isinstance(health_summary, dict) else None
    if isinstance(health_after, dict) and health_after:
        lines.extend(("", "活动结束后至结束日 UTC 日末记录的健康指标（时间戳为 UTC）："))
        for metric, value in sorted(health_after.items()):
            if isinstance(value, dict):
                lines.append(
                    f"{metric}: {value.get('value')} {value.get('unit', '')} "
                    f"({value.get('observed_at', 'unknown time')})"
                )
    if question:
        lines.extend(("", "用户关注的问题：", question))
    return "\n".join(lines)


def request_ai_analysis(
    *,
    base_url: str,
    model: str,
    api_key: str | None,
    prompt: str,
    timeout_seconds: int = 90,
) -> str:
    import requests

    endpoint = base_url.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        response = requests.post(
            endpoint,
            headers=headers,
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": "你是运动训练记录分析助手。"},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.3,
            },
            timeout=timeout_seconds,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"AI service request failed: {exc.__class__.__name__}") from exc
    if not response.ok:
        raise RuntimeError(f"AI service returned HTTP {response.status_code}")
    try:
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("AI service response did not contain a chat completion") from exc
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("AI service returned an empty analysis")
    return content.strip()


def _row_summary(row: object) -> dict[str, object]:
    summary_json = row["summary_json"]
    summary = json.loads(summary_json) if isinstance(summary_json, str) else {}
    summary["name"] = row["name"]
    summary["sport_type"] = row["sport_type"]
    summary["start_time"] = row["start_time"]
    summary["activity_id"] = str(row["fingerprint"])
    return summary


def _time_in_zone_summary(message: ActivityTimeInZone) -> dict[str, object]:
    hr_calculation_labels = {
        0: "custom",
        1: "percent_max_hr",
        2: "percent_heart_rate_reserve",
        3: "percent_lactate_threshold_hr",
    }
    power_calculation_labels = {0: "custom", 1: "percent_ftp"}
    return {
        "timestamp": message.timestamp.isoformat() if message.timestamp else None,
        "reference_message": message.reference_message,
        "reference_index": message.reference_index,
        "heart_rate_calculation_code": message.heart_rate_calculation,
        "heart_rate_calculation": hr_calculation_labels.get(
            message.heart_rate_calculation,
            f"unknown ({message.heart_rate_calculation})" if message.heart_rate_calculation is not None else None,
        ),
        "max_heart_rate_bpm": message.max_heart_rate_bpm,
        "resting_heart_rate_bpm": message.resting_heart_rate_bpm,
        "threshold_heart_rate_bpm": message.threshold_heart_rate_bpm,
        "power_calculation_code": message.power_calculation,
        "power_calculation": power_calculation_labels.get(
            message.power_calculation,
            f"unknown ({message.power_calculation})" if message.power_calculation is not None else None,
        ),
        "functional_threshold_power_w": message.functional_threshold_power_w,
        "heart_rate_zones": [_zone_summary(zone) for zone in message.heart_rate_zones],
        "speed_zones": [_zone_summary(zone) for zone in message.speed_zones],
        "cadence_zones": [_zone_summary(zone) for zone in message.cadence_zones],
        "power_zones": [_zone_summary(zone) for zone in message.power_zones],
    }


def _zone_summary(zone: ActivityZoneTime) -> dict[str, float | int | None]:
    return {"zone": zone.zone, "seconds": zone.seconds, "high_boundary": zone.high_boundary}


def _encode_nested_report_fields(records: list[dict[str, object]], field_name: str) -> None:
    for record in records:
        value = record.get(field_name)
        if isinstance(value, (dict, list)):
            record[field_name] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _format_zone_groups(
    groups: object,
    zone_field: str,
    calculation_field: str,
    boundary_unit: str,
) -> str:
    if not isinstance(groups, list):
        return "未知"
    summaries: list[str] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        zones = group.get(zone_field)
        if not isinstance(zones, list) or not zones:
            continue
        details: list[str] = []
        for zone in zones:
            if not isinstance(zone, dict):
                continue
            index = zone.get("zone")
            seconds = zone.get("seconds")
            duration = f"{_display(seconds)}秒" if seconds is not None else "未知秒数"
            boundary = zone.get("high_boundary")
            if boundary is not None:
                duration += f"（上界 {_display(boundary)} {boundary_unit}）"
            details.append(f"Z{index} {duration}")
        if not details:
            continue
        calculation = group.get(calculation_field)
        calculation_labels = {
            "custom": "自定义分区",
            "percent_max_hr": "最大心率百分比",
            "percent_heart_rate_reserve": "心率储备百分比",
            "percent_lactate_threshold_hr": "乳酸阈值心率百分比",
            "percent_ftp": "FTP 百分比",
        }
        calculation = calculation_labels.get(calculation, calculation)
        prefix = f"{calculation}：" if calculation else ""
        reference = group.get("reference_index")
        if reference is not None:
            prefix += f"索引 {reference}，"
        summaries.append(prefix + "、".join(details))
    return "；".join(summaries) if summaries else "未知"


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _display(value: object) -> str:
    return "未知" if value is None else str(value)


def _format_duration(value: float | None) -> str:
    if value is None:
        return "未知时长"
    seconds = max(0, int(value))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"
