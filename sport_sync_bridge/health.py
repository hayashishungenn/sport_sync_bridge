from __future__ import annotations

import csv
import hashlib
import io
import math
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from .state import StateDB
from .utils import parse_datetime


_METRIC_ALIASES = {
    "weight": "weight_kg",
    "weight_kg": "weight_kg",
    "body_weight": "weight_kg",
    "height": "height_cm",
    "height_cm": "height_cm",
    "resting_hr": "resting_hr_bpm",
    "resting_heart_rate": "resting_hr_bpm",
    "resting_hr_bpm": "resting_hr_bpm",
    "hrv": "hrv_ms",
    "hrv_ms": "hrv_ms",
    "spo2": "spo2_percent",
    "oxygen_saturation": "spo2_percent",
    "spo2_percent": "spo2_percent",
    "sleep": "sleep_hours",
    "sleep_hours": "sleep_hours",
    "steps": "steps",
    "step_count": "steps",
    "stress": "stress_score",
    "stress_score": "stress_score",
    "body_battery": "body_battery",
    "vo2_max_run": "vo2_max_run",
    "vo2max_run": "vo2_max_run",
    "vo2_max_running": "vo2_max_run",
    "vo2_max_ride": "vo2_max_ride",
    "vo2max_ride": "vo2_max_ride",
    "vo2_max_cycling": "vo2_max_ride",
    "sleep_score": "sleep_score",
    "lt_hr": "lactate_threshold_hr_bpm",
    "lt_hr_bpm": "lactate_threshold_hr_bpm",
    "lactate_threshold_hr": "lactate_threshold_hr_bpm",
    "lactate_threshold_hr_bpm": "lactate_threshold_hr_bpm",
    "lt_speed": "lactate_threshold_speed_kmh",
    "lt_speed_kmh": "lactate_threshold_speed_kmh",
    "lactate_threshold_speed": "lactate_threshold_speed_kmh",
    "lactate_threshold_speed_kmh": "lactate_threshold_speed_kmh",
    "calories": "calories_kcal",
    "calories_kcal": "calories_kcal",
    "active_calories": "calories_kcal",
    "floors": "floors",
    "floor_count": "floors",
    "respiration": "respiration_bpm",
    "respiration_rate": "respiration_bpm",
    "respiration_bpm": "respiration_bpm",
    "hydration": "hydration_l",
    "hydration_l": "hydration_l",
    "hydration_liters": "hydration_l",
    "recovery": "recovery_hours",
    "recovery_hours": "recovery_hours",
    "recovery_time_hours": "recovery_hours",
    "hrv_status": "hrv_status",
    "ready_to_train": "ready_to_train_status",
    "readiness": "ready_to_train_status",
    "ready_to_train_status": "ready_to_train_status",
    "fully_recovered": "fully_recovered",
    "systolic": "systolic_bp_mmhg",
    "systolic_bp": "systolic_bp_mmhg",
    "systolic_bp_mmhg": "systolic_bp_mmhg",
    "diastolic": "diastolic_bp_mmhg",
    "diastolic_bp": "diastolic_bp_mmhg",
    "diastolic_bp_mmhg": "diastolic_bp_mmhg",
}
_DEFAULT_UNITS = {
    "weight_kg": "kg",
    "height_cm": "cm",
    "resting_hr_bpm": "bpm",
    "hrv_ms": "ms",
    "spo2_percent": "%",
    "sleep_hours": "h",
    "steps": "count",
    "stress_score": "score",
    "body_battery": "%",
    "vo2_max_run": "mL/kg/min",
    "vo2_max_ride": "mL/kg/min",
    "sleep_score": "score",
    "lactate_threshold_hr_bpm": "bpm",
    "lactate_threshold_speed_kmh": "km/h",
    "calories_kcal": "kcal",
    "floors": "count",
    "respiration_bpm": "brpm",
    "hydration_l": "L",
    "recovery_hours": "h",
    "hrv_status": "status",
    "ready_to_train_status": "status",
    "fully_recovered": "status",
    "systolic_bp_mmhg": "mmHg",
    "diastolic_bp_mmhg": "mmHg",
}
_STATUS_VALUES = {
    "hrv_status": {
        "invalid", "very good", "good", "moderate", "poor", "very poor", "none",
        "无效", "很好", "好", "中等", "差", "非常差", "无",
    },
    "ready_to_train_status": {
        "high", "moderate", "medium", "low", "ready", "not ready",
        "高", "中", "中等", "低", "准备就绪", "未准备",
    },
    "fully_recovered": {
        "true", "false", "yes", "no", "recovered", "not recovered",
        "fully recovered", "not fully recovered", "是", "否", "完全恢复", "未恢复",
    },
}
_HEALTH_DISPLAY_LABELS = {
    "weight_kg": "体重",
    "height_cm": "身高",
    "resting_hr_bpm": "静息心率",
    "hrv_ms": "HRV",
    "spo2_percent": "血氧饱和度",
    "sleep_hours": "睡眠时长",
    "steps": "步数",
    "stress_score": "压力",
    "body_battery": "身体电量",
    "vo2_max_run": "跑步 VO₂max",
    "vo2_max_ride": "骑行 VO₂max",
    "sleep_score": "睡眠分数",
    "lactate_threshold_hr_bpm": "乳酸阈值心率",
    "lactate_threshold_speed_kmh": "乳酸阈值速度",
    "calories_kcal": "卡路里",
    "floors": "楼层",
    "respiration_bpm": "呼吸频率",
    "hydration_l": "饮水量",
    "recovery_hours": "恢复时长",
    "hrv_status": "HRV 状态",
    "ready_to_train_status": "训练准备状态",
    "fully_recovered": "完全恢复状态",
    "systolic_bp_mmhg": "收缩压",
    "diastolic_bp_mmhg": "舒张压",
    "bmi": "BMI",
}
_HEALTH_DISPLAY_ORDER = {metric: index for index, metric in enumerate(_HEALTH_DISPLAY_LABELS)}


def import_health_csv(state_db: StateDB, input_path: Path) -> int:
    input_path = input_path.expanduser().resolve()
    if not input_path.is_file():
        raise ValueError(f"Health CSV does not exist: {input_path}")
    payload = input_path.read_bytes()
    try:
        reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")))
    except UnicodeDecodeError as exc:
        raise ValueError("Health CSV must use UTF-8 encoding") from exc
    if not reader.fieldnames:
        raise ValueError("Health CSV has no header")
    fingerprint = hashlib.sha256(payload).hexdigest()
    imported = 0
    for row_number, raw_row in enumerate(reader, 2):
        row = {(key or "").strip().lower(): value for key, value in raw_row.items()}
        observed = _parse_observed_at(_first(row, "observed_at", "timestamp", "datetime", "date", "time"))
        if observed is None:
            raise ValueError(f"Health CSV row {row_number} has no valid date/time")
        long_metric = _first(row, "metric", "type", "indicator", "name")
        long_value = _first(row, "value", "measurement")
        if long_metric is not None and long_value is not None:
            parsed = _normalize_metric(long_metric, long_value, _first(row, "unit", "units"))
            if parsed is None:
                continue
            observations = [parsed]
        else:
            observations = []
            for column, raw_value in row.items():
                parsed = _normalize_metric(column, raw_value, None)
                if parsed is not None:
                    observations.append(parsed)
        for metric, value, unit in observations:
            state_db.upsert_health_observation(
                observed_at=observed,
                metric=metric,
                value=value,
                unit=unit,
                source_label=str(input_path),
                fingerprint=fingerprint,
            )
            imported += 1
    if imported == 0:
        raise ValueError("Health CSV contains no recognized health measurements")
    return imported


def summarize_health(state_db: StateDB) -> dict[str, object]:
    grouped: dict[str, list[object]] = {}
    for row in state_db.list_health_observations():
        grouped.setdefault(str(row["metric"]), []).append(row)
    latest = {
        metric: {
            "value": _health_value(rows[0]["value"]),
            "unit": str(rows[0]["unit"]),
            "observed_at": str(rows[0]["observed_at"]),
            "count": len(rows),
        }
        for metric, rows in grouped.items()
    }
    weight = latest.get("weight_kg", {}).get("value")
    height = latest.get("height_cm", {}).get("value")
    if isinstance(weight, (int, float)) and isinstance(height, (int, float)) and height > 0:
        latest["bmi"] = {
            "value": round(weight / ((height / 100) ** 2), 1),
            "unit": "kg/m²",
            "observed_at": max(
                str(latest["weight_kg"]["observed_at"]),
                str(latest["height_cm"]["observed_at"]),
            ),
        }
    return {"measurement_count": sum(len(rows) for rows in grouped.values()), "latest": latest}


def format_health_summary_text(summary: dict[str, object]) -> str:
    latest = summary.get("latest")
    if not isinstance(latest, dict):
        latest = {}
    lines = [f"测量记录：{summary.get('measurement_count', 0)}"]
    if not latest:
        lines.append("暂无健康指标")
        return "\n".join(lines)

    for metric in sorted(latest, key=_health_display_order):
        observation = latest[metric]
        if not isinstance(observation, dict) or "value" not in observation:
            continue
        value = observation["value"]
        unit = str(observation.get("unit", ""))
        display_value = _format_health_value(value)
        if metric == "lactate_threshold_speed_kmh":
            pace = _format_kmh_to_pace(value)
            if pace:
                display_value = f"{pace} /km"
                suffix = f"（{_format_health_value(value)} km/h）"
            else:
                suffix = " km/h"
        else:
            display_unit = {"steps": "步", "floors": "层"}.get(metric, unit)
            suffix = f" {display_unit}" if display_unit and display_unit != "status" else ""
        observed_at = observation.get("observed_at")
        timestamp = f"（记录时间：{observed_at}）" if observed_at else ""
        label = _HEALTH_DISPLAY_LABELS.get(metric, metric)
        lines.append(f"{label}：{display_value}{suffix}{timestamp}")
    return "\n".join(lines)


def _health_display_order(metric: str) -> tuple[int, str]:
    return _HEALTH_DISPLAY_ORDER.get(metric, len(_HEALTH_DISPLAY_ORDER)), metric


def _format_health_value(value: object) -> str:
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _format_kmh_to_pace(value: object) -> str | None:
    try:
        speed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(speed) or speed <= 0:
        return None
    seconds_per_km = math.floor(3600 / speed + 0.5)
    minutes, seconds = divmod(seconds_per_km, 60)
    return f"{minutes}:{seconds:02d}"


def summarize_health_for_activity(
    state_db: StateDB,
    activity_start: object,
    activity_end: object,
) -> dict[str, dict[str, dict[str, object]]]:
    if activity_start in (None, ""):
        return {"before_activity": {}, "after_activity": {}}
    start = parse_datetime(activity_start)
    if start is None:
        raise ValueError("Activity start time is invalid")
    start = start.astimezone(timezone.utc)

    before = _latest_health_by_metric(
        state_db.list_health_observations(observed_before=start.isoformat())
    )
    after: dict[str, dict[str, object]] = {}
    if activity_end not in (None, ""):
        end = parse_datetime(activity_end)
        if end is None:
            raise ValueError("Activity end time is invalid")
        end = end.astimezone(timezone.utc)
        if end < start:
            raise ValueError("Activity end time is before its start time")
        end_of_day = datetime.combine(end.date() + timedelta(days=1), time.min, tzinfo=timezone.utc)
        after = _latest_health_by_metric(
            state_db.list_health_observations(
                observed_after=end.isoformat(),
                observed_before=end_of_day.isoformat(),
            )
        )
    return {"before_activity": before, "after_activity": after}


def _latest_health_by_metric(rows: list[object]) -> dict[str, dict[str, object]]:
    latest: dict[str, dict[str, object]] = {}
    for row in rows:
        metric = str(row["metric"])
        if metric in latest:
            continue
        latest[metric] = {
            "value": _health_value(row["value"]),
            "unit": str(row["unit"]),
            "observed_at": str(row["observed_at"]),
        }
    return latest


def _normalize_metric(name: object, raw_value: object, raw_unit: object) -> tuple[str, float | str, str] | None:
    key = str(name).strip().lower().replace(" ", "_").replace("-", "_")
    metric = _METRIC_ALIASES.get(key)
    if metric is None or raw_value is None or not str(raw_value).strip():
        return None
    unit = str(raw_unit or _DEFAULT_UNITS[metric]).strip()
    if metric in _STATUS_VALUES:
        text_value = " ".join(str(raw_value).strip().split())
        normalized_status = " ".join(text_value.casefold().replace("_", " ").split())
        if normalized_status not in _STATUS_VALUES[metric]:
            raise ValueError(f"Invalid status for health metric {name}: {raw_value}")
        return metric, text_value, unit
    try:
        value = float(str(raw_value).strip().replace(",", "."))
    except ValueError as exc:
        raise ValueError(f"Invalid value for health metric {name}: {raw_value}") from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"Invalid value for health metric {name}: {raw_value}")
    lowered_unit = unit.lower()
    if metric == "weight_kg" and lowered_unit in {"lb", "lbs", "pound", "pounds"}:
        value *= 0.45359237
        unit = "kg"
    elif metric == "height_cm" and lowered_unit in {"m", "meter", "meters"}:
        value *= 100
        unit = "cm"
    elif metric == "sleep_hours" and lowered_unit in {"min", "minute", "minutes"}:
        value /= 60
        unit = "h"
    elif metric == "hydration_l" and lowered_unit in {"ml", "milliliter", "milliliters"}:
        value /= 1000
        unit = "L"
    elif metric == "lactate_threshold_speed_kmh" and lowered_unit in {"mph", "mi/h"}:
        value *= 1.609344
        unit = "km/h"
    elif metric == "steps":
        value = int(value)
        unit = "count"
    return metric, value, unit


def _health_value(value: object) -> float | str:
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return value
    return float(value)


def _parse_observed_at(value: object) -> str | None:
    if value is None:
        return None
    parsed = parse_datetime(str(value))
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _first(row: dict[str, object], *names: str) -> object | None:
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip():
            return value
    return None
