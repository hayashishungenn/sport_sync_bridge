from __future__ import annotations

import csv
import html
import io
import json
import statistics
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .formats import ActivityFile, TrackPoint


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
        "average_heart_rate_bpm": statistics.fmean(heart_rates) if heart_rates else _lap_hr(activity, "average_heart_rate"),
        "maximum_heart_rate_bpm": max(heart_rates) if heart_rates else _lap_hr(activity, "maximum_heart_rate", maximum=True),
        "average_cadence_rpm": statistics.fmean(cadences) if cadences else None,
        "average_power_w": statistics.fmean(powers) if powers else None,
        "total_ascent_m": ascent if altitudes else None,
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
        "total_ascent_m",
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
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
        return stream.getvalue()
    if output_format == "html":
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
) -> str:
    recent = summarize_rows(recent_rows)
    lines = [
        "请用中文分析这次运动记录，先概括训练内容，再指出有数据支持的表现和可执行建议。",
        "只依据提供的汇总数据；没有数据的指标要明确说未知，不要推测伤病或作医学诊断。",
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
        f"爬升（米）：{_display(activity_summary.get('total_ascent_m'))}",
        f"轨迹点数：{_display(activity_summary.get('track_point_count'))}",
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
    latest_health = health_summary.get("latest") if isinstance(health_summary, dict) else None
    if isinstance(latest_health, dict) and latest_health:
        lines.extend(("", "近期健康指标："))
        for metric, value in sorted(latest_health.items()):
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
