from __future__ import annotations

import json
import math
import re


_SWIM_TIME_PATTERN = re.compile(
    r"^\d+(?:\.\d{1,2})?(?::\d{1,2}(?:\.\d{1,2})?)?(?::\d{1,2}(?:\.\d{1,2})?)?$"
)


def parse_swim_time(value: str, label: str) -> float:
    if not isinstance(value, str) or not _SWIM_TIME_PATTERN.fullmatch(value.strip()):
        raise ValueError(f"{label} must use seconds, M:SS, or H:MM:SS")
    parts = value.strip().split(":")
    try:
        if len(parts) == 1:
            seconds = float(parts[0])
        elif len(parts) == 2:
            minutes, seconds_part = int(parts[0]), float(parts[1])
            if seconds_part >= 60:
                raise ValueError
            seconds = minutes * 60 + seconds_part
        else:
            hours, minutes, seconds_part = int(parts[0]), int(parts[1]), float(parts[2])
            if minutes >= 60 or seconds_part >= 60:
                raise ValueError
            seconds = hours * 3600 + minutes * 60 + seconds_part
    except ValueError as exc:
        raise ValueError(f"{label} has invalid time components") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{label} must be a positive finite time")
    return seconds


def calculate_swim_css(
    time_200m_s: float,
    time_400m_s: float,
    *,
    pool_length_m: int = 25,
) -> dict[str, object]:
    time_200 = _positive_finite(time_200m_s, "200 m time")
    time_400 = _positive_finite(time_400m_s, "400 m time")
    if time_400 <= time_200:
        raise ValueError("400 m time must be longer than 200 m time")
    if pool_length_m not in {25, 50}:
        raise ValueError("Pool length must be 25 or 50 meters")

    pace_per_100m = (time_400 - time_200) / 2
    split_distances = (25, 50, 100, 200, 400)
    return {
        "time_200m_s": time_200,
        "time_400m_s": time_400,
        "css_seconds_per_100m": pace_per_100m,
        "css_pace_per_100m": _format_pace(pace_per_100m),
        "css_speed_m_per_s": 100 / pace_per_100m,
        "pool_length_m": pool_length_m,
        "target_pool_length_s": pace_per_100m * pool_length_m / 100,
        "target_splits_s": {
            f"{distance}m": pace_per_100m * distance / 100
            for distance in split_distances
        },
    }


def calculate_swim_rest_seconds(distance_m: int) -> int:
    if isinstance(distance_m, bool) or not isinstance(distance_m, int) or distance_m <= 0:
        raise ValueError("Swim interval distance must be a positive whole number of meters")

    fixed_rests = {25: 12, 50: 20, 100: 25, 200: 35, 400: 50, 800: 60}
    if distance_m in fixed_rests:
        return fixed_rests[distance_m]
    return math.floor(distance_m * 0.075 + 0.5)


def format_swim_css(result: dict[str, object], output_format: str) -> str:
    if output_format == "json":
        return json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if output_format != "txt":
        raise ValueError(f"Unsupported swim CSS report format: {output_format}")

    lines = [
        f"200 米成绩：{_format_duration(float(result['time_200m_s']))}",
        f"400 米成绩：{_format_duration(float(result['time_400m_s']))}",
        f"CSS 配速：{result['css_pace_per_100m']} /100 米",
        f"CSS 速度：{float(result['css_speed_m_per_s']):.3f} 米/秒",
        f"{result['pool_length_m']} 米泳池目标：{_format_duration(float(result['target_pool_length_s']))}",
        "CSS 分段目标：",
    ]
    splits = result["target_splits_s"]
    if isinstance(splits, dict):
        lines.extend(
            f"  {distance}：{_format_duration(float(seconds))}"
            for distance, seconds in splits.items()
        )
    return "\n".join(lines) + "\n"


def format_swim_rest(distance_m: int, rest_seconds: int, output_format: str) -> str:
    result = {"distance_m": distance_m, "rest_seconds": rest_seconds}
    if output_format == "json":
        return json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if output_format != "txt":
        raise ValueError(f"Unsupported swim rest report format: {output_format}")
    return f"间歇距离：{distance_m} 米\n默认休息时间：{rest_seconds} 秒\n"


def _positive_finite(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a positive finite number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{label} must be a positive finite number")
    return result


def _format_pace(seconds: float) -> str:
    rounded = max(0, int(math.floor(seconds + 0.5)))
    minutes, remainder = divmod(rounded, 60)
    return f"{minutes}:{remainder:02}"


def _format_duration(seconds: float) -> str:
    rounded = max(0, int(math.floor(seconds + 0.5)))
    hours, remainder = divmod(rounded, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes}:{secs:02}"
