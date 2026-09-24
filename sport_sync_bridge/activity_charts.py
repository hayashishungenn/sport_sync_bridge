from __future__ import annotations

import html
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .formats import ActivityFile, TrackPoint


@dataclass(frozen=True, slots=True)
class ActivityChartResult:
    output_path: Path
    point_count: int
    chart_count: int
    x_axis: str


_SERIES = (
    ("heart_rate_bpm", "心率", "bpm", "#cf4d63", 1.0),
    ("speed_mps", "速度", "km/h", "#16846e", 3.6),
    ("elevation_m", "海拔", "m", "#7158a5", 1.0),
    ("power_w", "功率", "W", "#d27c28", 1.0),
    ("cadence_rpm", "踏频", "rpm", "#2779b8", 1.0),
)
_MAX_CHART_POINTS = 1_200


def write_activity_charts(activity: ActivityFile, output_path: Path) -> ActivityChartResult:
    output_path = output_path.expanduser().resolve()
    if output_path.suffix.lower() not in {".html", ".htm"}:
        raise ValueError("Activity chart output path must use .html or .htm")

    points = activity.track_points
    if not points:
        raise ValueError("Activity has no track samples to chart")

    x_axis, x_values, axis_label = _x_values(points)
    x_range = (min(x_values.values()), max(x_values.values()))
    charts: list[str] = []
    for field_name, label, unit, color, multiplier in _SERIES:
        samples = [
            (x_values[index], value * multiplier)
            for index, point in enumerate(points)
            if index in x_values
            and (value := _finite_value(getattr(point, field_name))) is not None
        ]
        if samples:
            charts.append(_render_chart(label, unit, color, samples, x_range, axis_label))
    if not charts:
        raise ValueError("Activity has no chartable time-series measurements")

    title = html.escape(activity.name or "运动记录", quote=True)
    sport = activity.sport_type or ""
    start_time = activity.start_time.isoformat() if activity.start_time else ""
    subtitle = " · ".join(value for value in (sport, start_time, f"{len(points)} 个采样点") if value)
    page = _html_page(
        title=title,
        subtitle=html.escape(subtitle, quote=True),
        chart_count=len(charts),
        axis_label=html.escape(axis_label, quote=True),
        charts="\n".join(charts),
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(page)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, output_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise

    return ActivityChartResult(
        output_path=output_path,
        point_count=len(points),
        chart_count=len(charts),
        x_axis=x_axis,
    )


def _html_page(*, title: str, subtitle: str, chart_count: int, axis_label: str, charts: str) -> str:
    return "\n".join(
        (
            "<!doctype html>",
            '<html lang="zh-CN">',
            "<head>",
            '  <meta charset="utf-8">',
            '  <meta name="viewport" content="width=device-width, initial-scale=1">',
            f"  <title>{title} · 活动图表</title>",
            "  <style>",
            '    :root { color-scheme: light; font-family: system-ui, -apple-system, "Segoe UI", sans-serif; color: #17252d; background: #f3f6f7; }',
            "    * { box-sizing: border-box; }",
            "    body { max-width: 1020px; margin: 0 auto; padding: 22px; }",
            "    header { padding: 4px 0 16px; }",
            "    .brand { color: #16846e; font-size: 12px; font-weight: 750; letter-spacing: .08em; }",
            "    h1 { margin: 8px 0 4px; font-size: clamp(21px, 4vw, 30px); overflow-wrap: anywhere; }",
            "    header p { margin: 0; color: #62727a; font-size: 13px; }",
            "    .chart { margin: 14px 0; padding: 14px 16px 8px; border: 1px solid #dde5e8; border-radius: 12px; background: #fff; box-shadow: 0 3px 14px #152f3b0b; }",
            "    h2 { margin: 0 0 4px; font-size: 16px; }",
            "    h2 span { color: #697980; font-size: 13px; font-weight: 500; }",
            "    svg { display: block; width: 100%; height: auto; overflow: visible; }",
            "    .grid { stroke: #e8eef0; stroke-width: 1; }",
            "    .tick { fill: #697980; font-size: 11px; }",
            "    @media (max-width: 600px) { body { padding: 12px; } .chart { padding: 12px 8px 6px; } }",
            "  </style>",
            "</head>",
            "<body>",
            "  <header>",
            '    <div class="brand">SPORT SYNC BRIDGE</div>',
            f"    <h1>{title}</h1>",
            f"    <p>{subtitle}</p>",
            f"    <p>{chart_count} 项图表 · 横轴：{axis_label}</p>",
            "  </header>",
            charts,
            "</body>",
            "</html>",
            "",
        )
    )


def _x_values(points: list[TrackPoint]) -> tuple[str, dict[int, float], str]:
    timestamped = [
        (index, _datetime_seconds(point.timestamp))
        for index, point in enumerate(points)
        if point.timestamp
    ]
    distinct_times = {timestamp for _, timestamp in timestamped}
    if len(distinct_times) > 1:
        origin = min(timestamp for _, timestamp in timestamped)
        return (
            "time",
            {index: timestamp - origin for index, timestamp in timestamped},
            "运动经过时间",
        )

    distances = [
        (index, distance)
        for index, point in enumerate(points)
        if (distance := _finite_value(point.distance_m)) is not None
    ]
    if len({distance for _, distance in distances}) > 1:
        origin_distance = min(distance for _, distance in distances)
        return (
            "distance",
            {index: distance - origin_distance for index, distance in distances},
            "累计距离",
        )

    return "sample", {index: float(index) for index in range(len(points))}, "采样点序号"


def _datetime_seconds(value: datetime) -> float:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).timestamp()


def _render_chart(
    label: str,
    unit: str,
    color: str,
    samples: list[tuple[float, float]],
    x_range: tuple[float, float],
    axis_label: str,
) -> str:
    width, height = 920, 260
    left, right, top, bottom = 76, 22, 38, 46
    plot_width = width - left - right
    plot_height = height - top - bottom
    minimum = min(value for _, value in samples)
    maximum = max(value for _, value in samples)
    padding = (maximum - minimum) * 0.08 or max(abs(minimum) * 0.05, 1.0)
    y_min, y_max = minimum - padding, maximum + padding
    x_min, x_max = x_range
    if x_min == x_max:
        x_max = x_min + 1.0

    samples = _downsample(samples)
    coordinates = [
        (
            left + (x - x_min) / (x_max - x_min) * plot_width,
            top + (y_max - value) / (y_max - y_min) * plot_height,
        )
        for x, value in samples
    ]
    path_data = " ".join(
        f"{'M' if index == 0 else 'L'} {x:.2f} {y:.2f}"
        for index, (x, y) in enumerate(coordinates)
    )
    escaped_label = html.escape(label, quote=True)
    escaped_unit = html.escape(unit, quote=True)
    escaped_axis = html.escape(axis_label, quote=True)
    grid: list[str] = []
    for step in range(5):
        ratio = step / 4
        y = top + ratio * plot_height
        value = y_max - ratio * (y_max - y_min)
        grid.append(
            f'<line x1="{left}" y1="{y:.2f}" x2="{width - right}" y2="{y:.2f}" class="grid" />'
            f'<text x="{left - 10}" y="{y + 4:.2f}" text-anchor="end" class="tick">{value:.1f}</text>'
        )
    x_start = _format_x(x_min, axis_label)
    x_end = _format_x(x_range[1], axis_label)
    path = f'<path d="{path_data}" fill="none" stroke="{color}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" />'
    if len(coordinates) == 1:
        x, y = coordinates[0]
        path += f'<circle cx="{x:.2f}" cy="{y:.2f}" r="4" fill="{color}" />'
    return "\n".join(
        (
            f'<section class="chart" aria-label="{escaped_label}图表">',
            f'  <h2>{escaped_label} <span>({escaped_unit})</span></h2>',
            f'  <svg viewBox="0 0 {width} {height}" role="img" aria-label="{escaped_label}随{escaped_axis}变化">',
            f'    {"".join(grid)}',
            f'    {path}',
            f'    <text x="{left}" y="{height - 12}" class="tick">{html.escape(x_start)}</text>',
            f'    <text x="{width - right}" y="{height - 12}" text-anchor="end" class="tick">{html.escape(x_end)}</text>',
            "  </svg>",
            "</section>",
        )
    )


def _downsample(samples: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if len(samples) <= _MAX_CHART_POINTS:
        return samples
    bucket_size = math.ceil((len(samples) - 2) / ((_MAX_CHART_POINTS - 2) // 2))
    reduced = [samples[0]]
    for start in range(1, len(samples) - 1, bucket_size):
        bucket = samples[start : start + bucket_size]
        extrema = {bucket.index(min(bucket, key=lambda sample: sample[1])), bucket.index(max(bucket, key=lambda sample: sample[1]))}
        reduced.extend(bucket[index] for index in sorted(extrema))
    reduced.append(samples[-1])
    return reduced


def _finite_value(value: object) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _format_x(value: float, axis_label: str) -> str:
    if axis_label == "运动经过时间":
        seconds = max(0, int(value))
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"
    if axis_label == "累计距离":
        return f"{value / 1000:.2f} km"
    return f"样本 {int(value) + 1}"
