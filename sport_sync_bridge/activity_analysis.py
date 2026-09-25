from __future__ import annotations

import csv
import html
import io
import json
import math
import os
import re
import statistics
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .ai_profile import validate_ai_athlete_profile
from .formats import ActivityFile, ActivityTimeInZone, ActivityZoneTime, TrackPoint
from .training_intensity import (
    classify_activity_training_intensity,
    classify_heart_rate_intensity,
    classify_power_intensity,
    classify_speed_intensity,
    derive_speed_zone_times,
)
from .utils import ensure_directory, safe_filename


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


def write_activity_report_pdf(rows: Iterable[object], output_path: Path) -> Path:
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except ImportError as exc:
        raise RuntimeError("PDF reports require ReportLab; install the project requirements") from exc

    rows = list(rows)
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
    labels = {
        "activity_id": "活动 ID",
        "name": "名称",
        "sport_type": "运动类型",
        "start_time": "开始时间",
        "distance_m": "距离",
        "elapsed_time_s": "总时长",
        "timer_time_s": "运动时长",
        "average_speed_mps": "平均速度",
        "average_heart_rate_bpm": "平均心率",
        "maximum_heart_rate_bpm": "最高心率",
        "average_cadence_rpm": "平均踏频",
        "average_power_w": "平均功率",
        "maximum_power_w": "最高功率",
        "normalized_power_w": "标准化功率",
        "intensity_factor": "强度因子",
        "aerobic_training_effect": "有氧训练效果",
        "anaerobic_training_effect": "无氧训练效果",
        "training_stress_score": "训练压力分",
        "total_ascent_m": "总爬升",
        "time_in_zone_messages": "区间时间",
        "lap_count": "圈数",
        "track_point_count": "轨迹点数",
    }
    summaries = [_row_summary(row) for row in rows]
    records = [
        {
            key: str(row["fingerprint"])[:12] if key == "activity_id" else summary.get(key)
            for key in fields
        }
        for row, summary in zip(rows, summaries)
    ]

    font_name = "STSong-Light"
    if font_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(font_name))

    sample_styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ActivityPdfTitle",
        parent=sample_styles["Title"],
        fontName=font_name,
        fontSize=18,
        leading=23,
        alignment=0,
        textColor=colors.HexColor("#172554"),
        spaceAfter=5,
    )
    activity_style = ParagraphStyle(
        "ActivityPdfHeading",
        parent=sample_styles["Heading2"],
        fontName=font_name,
        fontSize=12,
        leading=16,
        textColor=colors.HexColor("#1E3A8A"),
        spaceBefore=10,
        spaceAfter=5,
        keepWithNext=True,
    )
    label_style = ParagraphStyle(
        "ActivityPdfLabel",
        parent=sample_styles["BodyText"],
        fontName=font_name,
        fontSize=7.5,
        leading=10,
        textColor=colors.HexColor("#475569"),
        wordWrap="CJK",
    )
    value_style = ParagraphStyle(
        "ActivityPdfValue",
        parent=sample_styles["BodyText"],
        fontName=font_name,
        fontSize=7.5,
        leading=10,
        wordWrap="CJK",
    )
    metadata_style = ParagraphStyle(
        "ActivityPdfMetadata",
        parent=sample_styles["BodyText"],
        fontName=font_name,
        fontSize=8,
        leading=11,
        textColor=colors.HexColor("#475569"),
        spaceAfter=10,
    )
    zone_style = ParagraphStyle(
        "ActivityPdfZone",
        parent=sample_styles["BodyText"],
        fontName=font_name,
        fontSize=7,
        leading=9,
        wordWrap="CJK",
        leftIndent=4,
        spaceAfter=2,
    )

    def escape_paragraph(value: object) -> str:
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value))
        return html.escape(text, quote=False).replace("\n", "<br/>")

    def format_value(key: str, value: object) -> str:
        if value is None:
            return "未知"
        number = _number(value)
        if key == "distance_m" and number is not None:
            return f"{value} 米（{number / 1000:.2f} 公里）"
        if key in {"elapsed_time_s", "timer_time_s"} and number is not None:
            return f"{value} 秒（{_format_duration(number)}）"
        if key == "average_speed_mps" and number is not None:
            return f"{value} 米/秒（{number * 3.6:.2f} 公里/小时）"
        if key == "time_in_zone_messages":
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        units = {
            "average_heart_rate_bpm": "bpm",
            "maximum_heart_rate_bpm": "bpm",
            "average_cadence_rpm": "rpm",
            "average_power_w": "W",
            "maximum_power_w": "W",
            "normalized_power_w": "W",
            "training_stress_score": "TSS",
            "total_ascent_m": "m",
            "lap_count": "圈",
            "track_point_count": "点",
        }
        if key in units and number is not None:
            return f"{value} {units[key]}"
        if number is not None:
            return str(value)
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return str(value)

    output_path = Path(output_path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=output_path.parent,
            prefix=f".{output_path.name}-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)

        document = SimpleDocTemplate(
            str(temporary_path),
            pagesize=A4,
            leftMargin=36,
            rightMargin=36,
            topMargin=36,
            bottomMargin=40,
            title="运动活动报告",
            author="sport_sync_bridge",
        )
        available_width = A4[0] - document.leftMargin - document.rightMargin
        label_width = available_width * 0.15
        value_width = available_width * 0.35
        story = [
            Paragraph("运动活动报告", title_style),
            Paragraph(
                f"生成时间：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}　活动数：{len(records)}",
                metadata_style,
            ),
        ]
        if not records:
            story.append(Paragraph("当前活动库没有记录。", value_style))

        for index, record in enumerate(records, start=1):
            heading = f"{index}. {record.get('sport_type') or '未知运动'} | {record.get('name') or '未命名活动'}"
            story.append(Paragraph(escape_paragraph(heading), activity_style))
            table_rows = []
            metric_fields = [field for field in fields if field != "time_in_zone_messages"]
            for offset in range(0, len(metric_fields), 2):
                first = metric_fields[offset]
                second = metric_fields[offset + 1] if offset + 1 < len(metric_fields) else None
                table_rows.append(
                    [
                        Paragraph(escape_paragraph(labels[first]), label_style),
                        Paragraph(escape_paragraph(format_value(first, record.get(first))), value_style),
                        Paragraph(escape_paragraph(labels[second]), label_style) if second else "",
                        Paragraph(escape_paragraph(format_value(second, record.get(second))), value_style)
                        if second
                        else "",
                    ]
                )
            table = Table(
                table_rows,
                colWidths=[label_width, value_width, label_width, value_width],
                hAlign="LEFT",
                splitByRow=1,
            )
            table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F1F5F9")),
                        ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#F1F5F9")),
                        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#CBD5E1")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 4),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                        ("TOPPADDING", (0, 0), (-1, -1), 3),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ]
                )
            )
            story.append(table)
            zones = record.get("time_in_zone_messages") or []
            story.append(Paragraph(escape_paragraph(labels["time_in_zone_messages"]), label_style))
            if zones:
                zone_text = json.dumps(zones, ensure_ascii=False, separators=(",", ":"))
                for start in range(0, len(zone_text), 130):
                    story.append(Paragraph(escape_paragraph(zone_text[start : start + 130]), zone_style))
            else:
                story.append(Paragraph("无", zone_style))
            story.append(Spacer(1, 8))

        def draw_footer(canvas: object, doc: object) -> None:
            canvas.saveState()
            canvas.setFont(font_name, 7)
            canvas.setFillColor(colors.HexColor("#64748B"))
            canvas.drawString(doc.leftMargin, 22, "sport_sync_bridge")
            canvas.drawRightString(A4[0] - doc.rightMargin, 22, f"{doc.page}")
            canvas.restoreState()

        document.build(story, onFirstPage=draw_footer, onLaterPages=draw_footer)
        os.replace(temporary_path, output_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return output_path


def _format_sleep_stage_duration(seconds: object) -> str:
    total_minutes = float(seconds) / 60
    if total_minutes < 60:
        return f"{total_minutes:g} min"
    return f"{total_minutes / 60:.1f} h"


def build_ai_analysis_prompt(
    activity_summary: dict[str, object],
    recent_rows: Iterable[object],
    question: str | None = None,
    health_summary: dict[str, object] | None = None,
    *,
    language: str = "zh-CN",
    focus: str = "performance",
    detail: str = "normal",
    athlete_profile: dict[str, object] | None = None,
    speed_samples: Sequence[tuple[datetime | None, object]] | None = None,
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

    profile = validate_ai_athlete_profile(
        athlete_profile if athlete_profile is not None else {}
    )
    profile_labels = (
        ("gender", "gender", ""),
        ("age", "age", ""),
        ("weight_kg", "weight", " kg"),
        ("height_cm", "height", " cm"),
        ("resting_hr_bpm", "RHR", " bpm"),
        ("max_hr_bpm", "MaxHR", " bpm"),
        ("lactate_threshold_hr_bpm", "LTHR", " bpm"),
        ("vo2_max_run", "VO2max(run)", ""),
        ("vo2_max_bike", "VO2max(bike)", ""),
        ("ftp_w", "FTP", " W"),
        ("threshold_pace_s_per_km", "Threshold Pace", " s/km"),
    )
    profile_lines = []
    for field, label, unit in profile_labels:
        if field not in profile:
            continue
        value = profile[field]
        rendered = {"female": "Female", "male": "Male"}.get(value, value)
        profile_lines.append(f"{label}: {rendered}{unit}")

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
        "### Athlete Profile:",
        *(profile_lines or ["未设置运动员档案。"]),
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
        "速度分区时间："
        + _format_zone_groups(
            activity_summary.get("time_in_zone_messages"),
            "speed_zones",
            "",
            "m/s",
        ),
        "踏频分区时间："
        + _format_zone_groups(
            activity_summary.get("time_in_zone_messages"),
            "cadence_zones",
            "",
            "rpm",
        ),
        "功率分区时间："
        + _format_zone_groups(
            activity_summary.get("time_in_zone_messages"),
            "power_zones",
            "power_calculation",
            "W",
        ),
        "FIT 分区训练强度参考："
        + _format_training_intensity(activity_summary, health_summary, speed_samples),
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
    sleep_context = (
        health_summary.get("sleep_before_activity")
        if isinstance(health_summary, dict)
        else None
    )
    if isinstance(sleep_context, dict):
        lines.extend(("", "### Sleep & Recovery — Night Before Activity:"))
        calendar_date = sleep_context.get("calendar_date")
        if calendar_date is not None:
            lines.append(f"Garmin 睡眠日期：{calendar_date}")
        sleep_end = sleep_context.get("sleep_end_utc")
        if sleep_end is not None:
            lines.append(f"Sleep End (UTC): {sleep_end}")
        sleep_duration = sleep_context.get("sleep_hours")
        if sleep_duration is not None:
            lines.append(f"- Sleep Duration: {_display(sleep_duration)} hours")
        for metric, label in (
            ("deep_sleep_seconds", "Deep"),
            ("light_sleep_seconds", "Light"),
            ("rem_sleep_seconds", "REM"),
            ("awake_sleep_seconds", "Awake"),
        ):
            seconds = sleep_context.get(metric)
            if seconds is not None:
                lines.append(f"{label}: {_format_sleep_stage_duration(seconds)}")
        sleep_values = (
            ("sleep_score", "Sleep Score", " / 100"),
            ("sleep_quality", "Sleep Quality", ""),
            ("sleep_avg_hr_bpm", "Sleep Avg HR", " bpm"),
            ("sleep_avg_hrv_ms", "Sleep Avg HRV", " ms"),
            ("hrv_7d_baseline_ms", "HRV 7-Day Baseline", " ms"),
            ("sleep_avg_spo2_percent", "Sleep Avg SpO2", "%"),
            ("sleep_avg_respiration_bpm", "Sleep Avg Respiration", " brpm"),
        )
        for field, label, unit in sleep_values:
            value = sleep_context.get(field)
            if value is not None:
                lines.append(f"- {label}: {_display(value)}{unit}")
    health_before = (
        health_summary.get("before_activity")
        if isinstance(health_summary, dict)
        else None
    )
    readiness = (
        health_summary.get("training_readiness_before_activity")
        if isinstance(health_summary, dict)
        else None
    )
    if (isinstance(health_before, dict) and health_before) or isinstance(readiness, dict):
        lines.extend(("", "### Morning Baseline — Day of Activity:"))
    if isinstance(health_before, dict) and health_before:
        lines.extend(("", "活动开始前每项指标最近一次记录（时间均早于活动开始，时间戳为 UTC）："))
        for metric, value in sorted(health_before.items()):
            if isinstance(value, dict):
                lines.append(
                    f"{metric}: {value.get('value')} {value.get('unit', '')} "
                    f"({value.get('observed_at', 'unknown time')})"
                )
    if isinstance(readiness, dict):
        lines.extend(("", "活动开始前最近一次训练准备度记录（时间戳为 UTC）："))
        lines.append(f"记录时间：{readiness.get('observed_at', 'unknown time')}")
        readiness_values = [
            ("score", "准备度分数"),
            ("level", "等级"),
            ("recoveryTime", "恢复时间"),
            ("recoveryTimeChangePhrase", "恢复时间变化"),
            ("recoveryTimeFactorPercent", "恢复时间因子百分比"),
            ("recoveryTimeFactorFeedback", "恢复时间因子反馈"),
            ("acwrFactorPercent", "急慢性负荷比因子百分比"),
            ("acwrFactorFeedback", "急慢性负荷比因子反馈"),
            ("acuteLoad", "急性负荷"),
            ("stressHistoryFactorPercent", "压力历史因子百分比"),
            ("stressHistoryFactorFeedback", "压力历史因子反馈"),
            ("hrvFactorPercent", "HRV 因子百分比"),
            ("hrvFactorFeedback", "HRV 因子反馈"),
            ("hrvWeeklyAverage", "HRV 周均值"),
            ("sleepHistoryFactorPercent", "睡眠历史因子百分比"),
            ("sleepHistoryFactorFeedback", "睡眠历史因子反馈"),
            ("sleepScore", "睡眠评分"),
            ("validSleep", "睡眠数据有效"),
        ]
        readiness_data = readiness.get("data")
        if not isinstance(readiness_data, dict):
            readiness_data = {}
        for field, label in readiness_values:
            value = readiness.get(field) if field in {"score", "level"} else readiness_data.get(field)
            if value is not None:
                rendered = json.dumps(value, ensure_ascii=False, allow_nan=False)
                lines.append(f"{label}：{rendered}")
    health_after = health_summary.get("after_activity") if isinstance(health_summary, dict) else None
    if isinstance(health_after, dict) and health_after:
        lines.extend(
            (
                "",
                "### Post-Activity — Same Day:",
                "活动结束后至结束日 UTC 日末记录的健康指标（时间戳为 UTC）：",
            )
        )
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
    on_delta: Callable[[str], None] | None = None,
    system_prompt: str = "你是运动训练记录分析助手。",
    temperature: float = 0.3,
) -> str:
    import requests

    endpoint = base_url.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream, application/json",
        "Cache-Control": "no-cache",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    response = None
    try:
        response = requests.post(
            endpoint,
            headers=headers,
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                "temperature": temperature,
                "stream": True,
            },
            timeout=timeout_seconds,
            stream=True,
        )
        if not response.ok:
            raise RuntimeError(f"AI service returned HTTP {response.status_code}")
        return _read_chat_completion_response(response, on_delta)
    except requests.RequestException as exc:
        raise RuntimeError(f"AI service request failed: {exc.__class__.__name__}") from exc
    finally:
        if response is not None:
            response.close()


def _read_chat_completion_response(
    response: object,
    on_delta: Callable[[str], None] | None,
) -> str:
    chunks: list[str] = []
    event_data: list[str] = []
    raw_lines: list[str] = []
    saw_sse_data = False
    finished = False

    def consume_event() -> bool:
        nonlocal saw_sse_data
        if not event_data:
            return False
        data = "\n".join(event_data)
        event_data.clear()
        saw_sse_data = True
        if data.strip() == "[DONE]":
            return True
        try:
            payload = json.loads(data)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("AI service returned an invalid streaming event") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("AI service returned an invalid streaming event")
        if payload.get("error") is not None:
            raise RuntimeError("AI service returned an error event")
        choices = payload.get("choices")
        if choices is None:
            return False
        if not isinstance(choices, list):
            raise RuntimeError("AI service returned an invalid streaming event")
        if not choices:
            return False
        choice = choices[0]
        if not isinstance(choice, dict):
            raise RuntimeError("AI service returned an invalid streaming event")
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            delta = choice.get("message")
        if not isinstance(delta, dict):
            return False
        content = _completion_content_text(delta.get("content"))
        if content:
            chunks.append(content)
            if on_delta is not None:
                on_delta(content)
        return False

    iter_lines = getattr(response, "iter_lines", None)
    if callable(iter_lines):
        lines = iter_lines(decode_unicode=True)
    else:
        lines = str(getattr(response, "text", "")).splitlines()

    for raw_line in lines:
        if isinstance(raw_line, bytes):
            line = raw_line.decode("utf-8")
        else:
            line = str(raw_line)
        line = line.removesuffix("\r")
        if not line:
            if consume_event():
                finished = True
                break
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data = line[5:]
            if data.startswith(" "):
                data = data[1:]
            event_data.append(data)
            continue
        if line.startswith(("event:", "id:", "retry:")):
            continue
        raw_lines.append(line)

    if not finished and consume_event():
        finished = True

    if saw_sse_data:
        result = "".join(chunks).strip()
    else:
        raw_response = "\n".join(raw_lines).strip()
        try:
            if raw_response:
                payload = json.loads(raw_response)
            else:
                payload = response.json()
            choices = payload["choices"]
            choice = choices[0]
            message = choice.get("message") or choice.get("delta")
            result = _completion_content_text(message.get("content")) if isinstance(message, dict) else None
        except (AttributeError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("AI service response did not contain a chat completion") from exc
        if result and on_delta is not None:
            on_delta(result)
        result = result.strip() if result else ""

    if not result:
        raise RuntimeError("AI service returned an empty analysis")
    return result


def _completion_content_text(content: object) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            item["text"]
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        return "".join(parts) if parts else None
    return None


def write_ai_analysis_markdown(
    directory: Path,
    *,
    activity_id: str,
    result_id: str,
    model_name: str,
    content: str,
    created_at: str | None = None,
    output_path: Path | None = None,
) -> Path:
    if not activity_id or not result_id or not model_name.strip() or not content.strip():
        raise ValueError("AI analysis activity, result, model, and content must be non-empty")

    if output_path is None:
        output_path = directory / safe_filename(activity_id) / f"{safe_filename(result_id)}.md"
    else:
        output_path = Path(output_path).expanduser().resolve()
    if output_path.suffix.lower() != ".md":
        raise ValueError("AI analysis Markdown output path must use .md")
    ensure_directory(output_path.parent)
    metadata = json.dumps(
        {
            "activity_id": activity_id,
            "created_at": created_at or datetime.now(timezone.utc).isoformat(),
            "model_name": model_name,
            "result_id": result_id,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    document = f"---\n{metadata}\n---\n\n{content.strip()}\n"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=output_path.parent,
            prefix=f".{safe_filename(result_id)}-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(document)
        os.replace(temporary_path, output_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return output_path


def write_ai_analysis_pdf(
    output_path: Path,
    *,
    activity_id: str,
    activity_name: str,
    result_id: str,
    model_name: str,
    created_at: str,
    content: str,
) -> Path:
    if not all((activity_id, activity_name.strip(), result_id, model_name.strip(), content.strip())):
        raise ValueError("AI analysis activity, result, model, and content must be non-empty")
    output_path = Path(output_path).expanduser().resolve()
    if output_path.suffix.lower() != ".pdf":
        raise ValueError("AI analysis PDF output path must use .pdf")

    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer
    except ImportError as exc:
        raise RuntimeError("PDF reports require ReportLab; install the project requirements") from exc

    font_name = "STSong-Light"
    if font_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(font_name))

    sample_styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "AiAnalysisPdfTitle",
        parent=sample_styles["Title"],
        fontName=font_name,
        fontSize=18,
        leading=24,
        textColor=colors.HexColor("#172554"),
        alignment=0,
        spaceAfter=8,
    )
    metadata_style = ParagraphStyle(
        "AiAnalysisPdfMetadata",
        parent=sample_styles["BodyText"],
        fontName=font_name,
        fontSize=8,
        leading=12,
        textColor=colors.HexColor("#475569"),
        spaceAfter=14,
        wordWrap="CJK",
    )
    body_style = ParagraphStyle(
        "AiAnalysisPdfBody",
        parent=sample_styles["BodyText"],
        fontName=font_name,
        fontSize=10,
        leading=15,
        spaceAfter=7,
        wordWrap="CJK",
    )
    heading_styles = {
        1: ParagraphStyle(
            "AiAnalysisPdfHeading1",
            parent=sample_styles["Heading1"],
            fontName=font_name,
            fontSize=15,
            leading=20,
            textColor=colors.HexColor("#172554"),
            spaceBefore=9,
            spaceAfter=6,
            keepWithNext=True,
        ),
        2: ParagraphStyle(
            "AiAnalysisPdfHeading2",
            parent=sample_styles["Heading2"],
            fontName=font_name,
            fontSize=12,
            leading=16,
            textColor=colors.HexColor("#1E3A8A"),
            spaceBefore=8,
            spaceAfter=5,
            keepWithNext=True,
        ),
        3: ParagraphStyle(
            "AiAnalysisPdfHeading3",
            parent=sample_styles["Heading3"],
            fontName=font_name,
            fontSize=11,
            leading=14,
            textColor=colors.HexColor("#334155"),
            spaceBefore=6,
            spaceAfter=4,
            keepWithNext=True,
        ),
    }
    quote_style = ParagraphStyle(
        "AiAnalysisPdfQuote",
        parent=body_style,
        leftIndent=14,
        borderColor=colors.HexColor("#CBD5E1"),
        borderWidth=1,
        borderPadding=5,
        backColor=colors.HexColor("#F8FAFC"),
    )
    code_style = ParagraphStyle(
        "AiAnalysisPdfCode",
        parent=body_style,
        fontSize=8,
        leading=11,
        leftIndent=10,
        rightIndent=10,
        borderPadding=4,
        backColor=colors.HexColor("#F1F5F9"),
    )

    def clean_text(value: str) -> str:
        return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value)

    def inline_markup(value: str) -> str:
        value = clean_text(value)
        value = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", value)
        code_fragments: dict[str, str] = {}

        def hold_code(match: re.Match[str]) -> str:
            marker = f"AIANALYSISCODE{len(code_fragments)}TOKEN"
            code_fragments[marker] = html.escape(match.group(1), quote=False).replace(" ", "&nbsp;")
            return marker

        value = re.sub(r"`([^`\n]+)`", hold_code, value)
        value = html.escape(value, quote=False)
        value = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", value)
        value = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", value)
        value = re.sub(r"~~(.+?)~~", r"<strike>\1</strike>", value)
        for marker, code in code_fragments.items():
            value = value.replace(
                marker,
                f'<font name="{font_name}" backColor="#F1F5F9">{code}</font>',
            )
        return value or "&nbsp;"

    story = [
        Paragraph("AI Coach 分析报告", title_style),
        Paragraph(
            "活动：{}<br/>活动 ID：{}<br/>模型：{}<br/>生成时间：{}".format(
                html.escape(clean_text(activity_name), quote=False),
                html.escape(clean_text(activity_id), quote=False),
                html.escape(clean_text(model_name), quote=False),
                html.escape(clean_text(created_at or "未知时间"), quote=False),
            ),
            metadata_style,
        ),
    ]
    paragraph_lines: list[str] = []
    code_lines: list[str] | None = None

    def flush_paragraph() -> None:
        if paragraph_lines:
            story.append(Paragraph(inline_markup(" ".join(line.strip() for line in paragraph_lines)), body_style))
            paragraph_lines.clear()

    def flush_code() -> None:
        if code_lines:
            for line in code_lines:
                escaped = html.escape(clean_text(line), quote=False).replace("\t", "&nbsp;&nbsp;&nbsp;&nbsp;")
                escaped = escaped.replace(" ", "&nbsp;") or "&nbsp;"
                story.append(Paragraph(escaped, code_style))
            code_lines.clear()

    for raw_line in content.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped.startswith("```"):
            flush_paragraph()
            if code_lines is None:
                code_lines = []
            else:
                flush_code()
                code_lines = None
            continue
        if code_lines is not None:
            code_lines.append(line)
            continue
        if not stripped:
            flush_paragraph()
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading:
            flush_paragraph()
            level = min(len(heading.group(1)), 3)
            story.append(Paragraph(inline_markup(heading.group(2)), heading_styles[level]))
            continue
        if re.fullmatch(r"(?:-{3,}|\*{3,}|_{3,})", stripped):
            flush_paragraph()
            story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#CBD5E1"), spaceAfter=8))
            continue
        quote = re.match(r"^>\s?(.*)$", stripped)
        if quote:
            flush_paragraph()
            story.append(Paragraph(inline_markup(quote.group(1)), quote_style))
            continue
        bullet = re.match(r"^\s*[-*+]\s+(.+)$", line)
        if bullet:
            flush_paragraph()
            story.append(Paragraph("• " + inline_markup(bullet.group(1)), body_style))
            continue
        numbered = re.match(r"^\s*(\d+)[.)]\s+(.+)$", line)
        if numbered:
            flush_paragraph()
            story.append(Paragraph(f"{numbered.group(1)}. " + inline_markup(numbered.group(2)), body_style))
            continue
        if stripped.startswith("|"):
            flush_paragraph()
            story.append(Paragraph(inline_markup(stripped), code_style))
            continue
        paragraph_lines.append(line)
    flush_paragraph()
    if code_lines is not None:
        flush_code()
    story.append(Spacer(1, 6))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=output_path.parent,
            prefix=f".{output_path.name}-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
        document = SimpleDocTemplate(
            str(temporary_path),
            pagesize=A4,
            leftMargin=48,
            rightMargin=48,
            topMargin=44,
            bottomMargin=44,
            title="AI Coach Analysis Report",
            author="sport_sync_bridge",
        )
        document.build(story)
        os.replace(temporary_path, output_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return output_path


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


def _format_training_intensity(
    activity_summary: dict[str, object],
    health_summary: dict[str, object] | None = None,
    speed_samples: Sequence[tuple[datetime | None, object]] | None = None,
) -> str:
    messages = activity_summary.get("time_in_zone_messages")
    if not isinstance(messages, list):
        messages = []

    duration = activity_summary.get("timer_time_s")
    if duration is None:
        duration = activity_summary.get("elapsed_time_s")
    power_intensity_factor = (
        activity_summary.get("intensity_factor")
        if activity_summary.get("timer_time_s") is not None
        else None
    )
    sport_type = activity_summary.get("sport_type")
    threshold_speed = None
    health_before = (
        health_summary.get("before_activity")
        if isinstance(health_summary, dict)
        else None
    )
    if isinstance(health_before, dict):
        threshold_observation = health_before.get("lactate_threshold_speed_kmh")
        if isinstance(threshold_observation, dict):
            unit = str(threshold_observation.get("unit") or "").strip().casefold()
            if unit in {"km/h", "kmh", "kph"}:
                threshold_speed = threshold_observation.get("value")

    derived_speed_zones: list[dict[str, float | int]] = []
    timer_duration = activity_summary.get("timer_time_s")
    if (
        speed_samples is not None
        and isinstance(threshold_speed, (int, float))
        and not isinstance(threshold_speed, bool)
        and math.isfinite(float(threshold_speed))
        and threshold_speed > 0
        and isinstance(timer_duration, (int, float))
        and not isinstance(timer_duration, bool)
        and math.isfinite(float(timer_duration))
        and threshold_speed > timer_duration
    ):
        derived_speed_zones = derive_speed_zone_times(speed_samples, threshold_speed)

    messages = [message for message in messages if isinstance(message, dict)]
    if not messages and not derived_speed_zones:
        return "未知（无分区记录）"
    if not messages:
        messages = [{}]

    labels: list[str] = []
    for index, message in enumerate(messages, start=1):
        heart_rate = classify_heart_rate_intensity(
            message.get("heart_rate_zones"), duration, sport_type
        )
        power = classify_power_intensity(
            message.get("power_zones"),
            duration,
            sport_type,
            intensity_factor=power_intensity_factor,
        )
        speed = classify_speed_intensity(message.get("speed_zones"))
        selected = classify_activity_training_intensity(
            message.get("heart_rate_zones"),
            message.get("power_zones"),
            duration,
            sport_type,
            intensity_factor=power_intensity_factor,
            speed_zones=derived_speed_zones if index == 1 else None,
        )
        group_labels = []
        if heart_rate is not None:
            group_labels.append(f"心率={heart_rate}")
        if power is not None:
            group_labels.append(f"功率={power}")
        if speed is not None:
            group_labels.append(f"速度={speed}")
        if selected is not None:
            group_labels.append(f"GarSync分类={selected}")
        if group_labels:
            prefix = f"分区记录{index}：" if len(messages) > 1 else ""
            labels.append(prefix + "，".join(group_labels))
    return "；".join(labels) if labels else "未知（无有效分区时间）"


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
