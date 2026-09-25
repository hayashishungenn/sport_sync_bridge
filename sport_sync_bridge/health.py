from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from .state import StateDB
from .utils import parse_datetime


_METRIC_ALIASES = {
    "weight": "weight_kg",
    "weight_kg": "weight_kg",
    "body_weight": "weight_kg",
    "body_fat": "body_fat_percent",
    "body_fat_percent": "body_fat_percent",
    "height": "height_cm",
    "height_cm": "height_cm",
    "resting_hr": "resting_hr_bpm",
    "resting_heart_rate": "resting_hr_bpm",
    "resting_hr_bpm": "resting_hr_bpm",
    "hrv": "hrv_ms",
    "hrv_ms": "hrv_ms",
    "hrv_sdnn": "hrv_sdnn_ms",
    "hrv_sdnn_ms": "hrv_sdnn_ms",
    "spo2": "spo2_percent",
    "oxygen_saturation": "spo2_percent",
    "spo2_percent": "spo2_percent",
    "spo2_7d_avg": "spo2_7d_average_percent",
    "spo2_7d_average_percent": "spo2_7d_average_percent",
    "spo2_weekly_average": "spo2_7d_average_percent",
    "avg_sleep_spo2": "avg_sleep_spo2_percent",
    "avg_sleep_spo2_percent": "avg_sleep_spo2_percent",
    "sleep": "sleep_hours",
    "sleep_hours": "sleep_hours",
    "deep_sleep_seconds": "deep_sleep_seconds",
    "light_sleep_seconds": "light_sleep_seconds",
    "rem_sleep_seconds": "rem_sleep_seconds",
    "awake_sleep_seconds": "awake_sleep_seconds",
    "restless_sleep_seconds": "restless_sleep_seconds",
    "time_in_bed_hours": "time_in_bed_hours",
    "sleep_latency_seconds": "sleep_latency_seconds",
    "sleep_after_wake_seconds": "sleep_after_wake_seconds",
    "steps": "steps",
    "step_count": "steps",
    "step_goal": "step_goal",
    "floors_goal": "floors_goal",
    "stress": "stress_score",
    "stress_score": "stress_score",
    "max_stress": "max_stress_score",
    "max_stress_score": "max_stress_score",
    "body_battery": "body_battery",
    "body_battery_high": "body_battery_high",
    "body_battery_low": "body_battery_low",
    "body_battery_charged": "body_battery_charged_points",
    "body_battery_charged_points": "body_battery_charged_points",
    "body_battery_drained": "body_battery_drained_points",
    "body_battery_drained_points": "body_battery_drained_points",
    "distance": "distance_km",
    "distance_km": "distance_km",
    "floors_descended": "floors_descended",
    "min_heart_rate": "min_heart_rate_bpm",
    "min_heart_rate_bpm": "min_heart_rate_bpm",
    "max_heart_rate": "max_heart_rate_bpm",
    "max_heart_rate_bpm": "max_heart_rate_bpm",
    "fitness_age": "fitness_age_years",
    "fitness_age_years": "fitness_age_years",
    "achievable_fitness_age": "achievable_fitness_age_years",
    "achievable_fitness_age_years": "achievable_fitness_age_years",
    "resting_hr_7d": "resting_hr_7d_bpm",
    "resting_hr_7d_bpm": "resting_hr_7d_bpm",
    "spo2_low": "spo2_low_percent",
    "spo2_low_percent": "spo2_low_percent",
    "hrv_weekly_average": "hrv_weekly_average_ms",
    "hrv_weekly_average_ms": "hrv_weekly_average_ms",
    "avg_waking_respiration": "avg_waking_respiration_bpm",
    "avg_waking_respiration_bpm": "avg_waking_respiration_bpm",
    "avg_sleep_respiration": "avg_sleep_respiration_bpm",
    "avg_sleep_respiration_bpm": "avg_sleep_respiration_bpm",
    "latest_respiration": "latest_respiration_bpm",
    "latest_respiration_bpm": "latest_respiration_bpm",
    "max_respiration": "max_respiration_bpm",
    "max_respiration_bpm": "max_respiration_bpm",
    "min_respiration": "min_respiration_bpm",
    "min_respiration_bpm": "min_respiration_bpm",
    "active_calories": "active_calories_kcal",
    "active_calories_kcal": "active_calories_kcal",
    "bmr_calories": "bmr_calories_kcal",
    "bmr_calories_kcal": "bmr_calories_kcal",
    "wellness_calories": "wellness_calories_kcal",
    "wellness_calories_kcal": "wellness_calories_kcal",
    "vo2_max_run": "vo2_max_run",
    "vo2max_run": "vo2_max_run",
    "vo2_max_running": "vo2_max_run",
    "vo2_max_ride": "vo2_max_ride",
    "vo2max_ride": "vo2_max_ride",
    "vo2_max_cycling": "vo2_max_ride",
    "sleep_score": "sleep_score",
    "sleep_quality": "sleep_quality_score",
    "sleep_quality_score": "sleep_quality_score",
    "comments": "wellness_comment",
    "wellness_comment": "wellness_comment",
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
    "hydration_goal_l": "hydration_goal_l",
    "hydration_base_goal_l": "hydration_base_goal_l",
    "hydration_activity_l": "hydration_activity_l",
    "hydration_sweat_loss_l": "hydration_sweat_loss_l",
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
    "pulse": "pulse_bpm",
    "pulse_bpm": "pulse_bpm",
}
_DEFAULT_UNITS = {
    "weight_kg": "kg",
    "body_fat_percent": "%",
    "height_cm": "cm",
    "resting_hr_bpm": "bpm",
    "hrv_ms": "ms",
    "hrv_sdnn_ms": "ms",
    "spo2_percent": "%",
    "spo2_7d_average_percent": "%",
    "avg_sleep_spo2_percent": "%",
    "sleep_hours": "h",
    "deep_sleep_seconds": "s",
    "light_sleep_seconds": "s",
    "rem_sleep_seconds": "s",
    "awake_sleep_seconds": "s",
    "restless_sleep_seconds": "s",
    "time_in_bed_hours": "h",
    "sleep_latency_seconds": "s",
    "sleep_after_wake_seconds": "s",
    "steps": "count",
    "step_goal": "count",
    "floors_goal": "count",
    "stress_score": "score",
    "max_stress_score": "score",
    "body_battery": "%",
    "body_battery_high": "%",
    "body_battery_low": "%",
    "body_battery_charged_points": "points",
    "body_battery_drained_points": "points",
    "distance_km": "km",
    "floors_descended": "count",
    "min_heart_rate_bpm": "bpm",
    "max_heart_rate_bpm": "bpm",
    "fitness_age_years": "years",
    "achievable_fitness_age_years": "years",
    "resting_hr_7d_bpm": "bpm",
    "spo2_low_percent": "%",
    "hrv_weekly_average_ms": "ms",
    "avg_waking_respiration_bpm": "brpm",
    "avg_sleep_respiration_bpm": "brpm",
    "latest_respiration_bpm": "brpm",
    "max_respiration_bpm": "brpm",
    "min_respiration_bpm": "brpm",
    "active_calories_kcal": "kcal",
    "bmr_calories_kcal": "kcal",
    "wellness_calories_kcal": "kcal",
    "vo2_max_run": "mL/kg/min",
    "vo2_max_ride": "mL/kg/min",
    "sleep_score": "score",
    "sleep_quality_score": "级",
    "wellness_comment": "text",
    "lactate_threshold_hr_bpm": "bpm",
    "lactate_threshold_speed_kmh": "km/h",
    "calories_kcal": "kcal",
    "floors": "count",
    "respiration_bpm": "brpm",
    "hydration_l": "L",
    "hydration_goal_l": "L",
    "hydration_base_goal_l": "L",
    "hydration_activity_l": "L",
    "hydration_sweat_loss_l": "L",
    "recovery_hours": "h",
    "hrv_status": "status",
    "ready_to_train_status": "status",
    "fully_recovered": "status",
    "systolic_bp_mmhg": "mmHg",
    "diastolic_bp_mmhg": "mmHg",
    "pulse_bpm": "bpm",
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
    "body_fat_percent": "体脂率",
    "height_cm": "身高",
    "resting_hr_bpm": "静息心率",
    "hrv_ms": "HRV",
    "hrv_sdnn_ms": "HRV SDNN",
    "spo2_percent": "血氧饱和度",
    "spo2_7d_average_percent": "血氧七日平均",
    "avg_sleep_spo2_percent": "睡眠平均血氧",
    "sleep_hours": "睡眠时长",
    "deep_sleep_seconds": "深睡时长",
    "light_sleep_seconds": "浅睡时长",
    "rem_sleep_seconds": "快速眼动睡眠时长",
    "awake_sleep_seconds": "清醒时长",
    "restless_sleep_seconds": "睡眠躁动时长",
    "time_in_bed_hours": "卧床时长",
    "sleep_latency_seconds": "入睡潜伏期",
    "sleep_after_wake_seconds": "醒后时长",
    "steps": "步数",
    "step_goal": "每日步数目标",
    "floors_goal": "每日爬楼目标",
    "stress_score": "压力",
    "max_stress_score": "最高压力",
    "body_battery": "身体电量",
    "body_battery_high": "身体电量最高值",
    "body_battery_low": "身体电量最低值",
    "body_battery_charged_points": "身体电量充入",
    "body_battery_drained_points": "身体电量消耗",
    "distance_km": "每日距离",
    "floors_descended": "下行楼层",
    "min_heart_rate_bpm": "最低心率",
    "max_heart_rate_bpm": "最高心率",
    "fitness_age_years": "健身年龄",
    "achievable_fitness_age_years": "可达健身年龄",
    "resting_hr_7d_bpm": "七日平均静息心率",
    "spo2_low_percent": "最低血氧饱和度",
    "hrv_weekly_average_ms": "HRV 七日平均",
    "avg_waking_respiration_bpm": "清醒平均呼吸频率",
    "avg_sleep_respiration_bpm": "睡眠平均呼吸频率",
    "latest_respiration_bpm": "最新呼吸频率",
    "max_respiration_bpm": "最高呼吸频率",
    "min_respiration_bpm": "最低呼吸频率",
    "active_calories_kcal": "活动卡路里",
    "bmr_calories_kcal": "基础代谢卡路里",
    "wellness_calories_kcal": "健康卡路里",
    "vo2_max_run": "跑步 VO₂max",
    "vo2_max_ride": "骑行 VO₂max",
    "sleep_score": "睡眠分数",
    "sleep_quality_score": "睡眠质量等级",
    "wellness_comment": "健康备注",
    "lactate_threshold_hr_bpm": "乳酸阈值心率",
    "lactate_threshold_speed_kmh": "乳酸阈值速度",
    "calories_kcal": "卡路里",
    "floors": "楼层",
    "respiration_bpm": "呼吸频率",
    "hydration_l": "饮水量",
    "hydration_goal_l": "饮水目标",
    "hydration_base_goal_l": "基础饮水目标",
    "hydration_activity_l": "运动饮水量",
    "hydration_sweat_loss_l": "估算汗液损失",
    "recovery_hours": "恢复时长",
    "hrv_status": "HRV 状态",
    "ready_to_train_status": "训练准备状态",
    "fully_recovered": "完全恢复状态",
    "systolic_bp_mmhg": "收缩压",
    "diastolic_bp_mmhg": "舒张压",
    "pulse_bpm": "血压测量脉搏",
    "bmi": "BMI",
}
_HEALTH_DISPLAY_ORDER = {metric: index for index, metric in enumerate(_HEALTH_DISPLAY_LABELS)}
_TEXT_METRICS = {"wellness_comment"}
_INTERVALS_WELLNESS_FIELDS = {
    "weight": ("weight", "kg"),
    "bodyFat": ("body_fat", "%"),
    "restingHR": ("resting_hr", "bpm"),
    "hrv": ("hrv", "ms"),
    "hrvSDNN": ("hrv_sdnn", "ms"),
    "sleepSecs": ("sleep", "s"),
    "sleepScore": ("sleep_score", "score"),
    "sleepQuality": ("sleep_quality", "score"),
    "spO2": ("spo2", "%"),
    "systolic": ("systolic", "mmHg"),
    "diastolic": ("diastolic", "mmHg"),
    "steps": ("steps", "count"),
    "respiration": ("respiration", "brpm"),
    "hydrationVolume": ("hydration", "L"),
    "comments": ("comments", "text"),
}
_GARMIN_USER_SUMMARY_FIELDS = {
    "totalSteps": ("steps", "count"),
    "dailyStepGoal": ("step_goal", "count"),
    "floorsAscended": ("floors", "count"),
    "floorsDescended": ("floors_descended", "count"),
    "userFloorsAscendedGoal": ("floors_goal", "count"),
    "restingHeartRate": ("resting_heart_rate", "bpm"),
    "minHeartRate": ("min_heart_rate_bpm", "bpm"),
    "maxHeartRate": ("max_heart_rate_bpm", "bpm"),
    "lastSevenDaysAvgRestingHeartRate": ("resting_hr_7d_bpm", "bpm"),
    "averageStressLevel": ("stress_score", "score"),
    "maxStressLevel": ("max_stress_score", "score"),
    "bodyBatteryMostRecentValue": ("body_battery", "%"),
    "bodyBatteryHighestValue": ("body_battery_high", "%"),
    "bodyBatteryLowestValue": ("body_battery_low", "%"),
    "bodyBatteryChargedValue": ("body_battery_charged_points", "points"),
    "bodyBatteryDrainedValue": ("body_battery_drained_points", "points"),
    "averageSpo2": ("spo2_percent", "%"),
    "lowestSpo2": ("spo2_low_percent", "%"),
    "sleepingSeconds": ("sleep", "s"),
    "avgWakingRespirationValue": ("avg_waking_respiration_bpm", "brpm"),
    "latestRespirationValue": ("latest_respiration_bpm", "brpm"),
    "highestRespirationValue": ("max_respiration_bpm", "brpm"),
    "hrvWeeklyAverage": ("hrv_weekly_average_ms", "ms"),
    "totalDistanceMeters": ("distance_km", "m"),
    "totalKilocalories": ("calories", "kcal"),
    "activeKilocalories": ("active_calories_kcal", "kcal"),
    "bmrKilocalories": ("bmr_calories_kcal", "kcal"),
    "wellnessKilocalories": ("wellness_calories_kcal", "kcal"),
    "hrvStatus": ("hrv_status", "status"),
}
_GARMIN_HEALTH_DETAIL_FIELDS = {
    "sleep": {
        "sleepTimeSeconds": ("sleep", "s"),
        "deepSleepSeconds": ("deep_sleep_seconds", "s"),
        "lightSleepSeconds": ("light_sleep_seconds", "s"),
        "remSleepSeconds": ("rem_sleep_seconds", "s"),
        "awakeSleepSeconds": ("awake_sleep_seconds", "s"),
    },
    "hrv": {
        "lastNightAvg": ("hrv", "ms"),
        "weeklyAverage": ("hrv_weekly_average", "ms"),
    },
    "stress": {
        "avgStressLevel": ("stress", "score"),
        "maxStressLevel": ("max_stress", "score"),
    },
    "body-battery": {},
    "respiration": {
        "avgWakingRespirationValue": ("avg_waking_respiration", "brpm"),
        "avgSleepRespirationValue": ("avg_sleep_respiration", "brpm"),
        "highestRespirationValue": ("max_respiration", "brpm"),
        "lowestRespirationValue": ("min_respiration", "brpm"),
    },
    "hydration": {
        "valueInML": ("hydration", "ml"),
        "goalInML": ("hydration_goal_l", "ml"),
        "baseGoalInML": ("hydration_base_goal_l", "ml"),
        "activityIntakeInML": ("hydration_activity_l", "ml"),
        "sweatLossInML": ("hydration_sweat_loss_l", "ml"),
    },
    "blood-pressure": {},
    "heart-rate": {
        "restingHR": ("resting_heart_rate", "bpm"),
        "wellnessMaxAvgHR": ("max_heart_rate", "bpm"),
        "wellnessMinAvgHR": ("min_heart_rate", "bpm"),
    },
    "fitness-age": {
        "fitnessAge": ("fitness_age", "years"),
        "achievableFitnessAge": ("achievable_fitness_age", "years"),
    },
    "spo2-acclimation": {
        "averageSpO2": ("spo2", "%"),
        "lowestSpO2": ("spo2_low", "%"),
        "lastSevenDaysAvgSpo2": ("spo2_7d_avg", "%"),
        "avgSleepSpo2": ("avg_sleep_spo2", "%"),
    },
    "floors-chart": {
        "floorsAscended": ("floors", "count"),
        "floorsDescended": ("floors_descended", "count"),
        "floorsGoal": ("floors_goal", "count"),
    },
    "steps": {
        "totalSteps": ("steps", "count"),
        "dailyStepGoal": ("step_goal", "count"),
    },
}
_GARMIN_BLOOD_PRESSURE_FIELDS = {
    "systolic": ("systolic", "mmHg"),
    "diastolic": ("diastolic", "mmHg"),
    "pulse": ("pulse", "bpm"),
}


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


def import_intervals_icu_wellness(
    state_db: StateDB,
    records: list[dict[str, object]],
    *,
    source_label: str = "Intervals.icu wellness",
) -> int:
    pending: list[tuple[str, str, float | str, str, str]] = []
    for index, record in enumerate(records):
        record_id = record.get("id")
        observed = _parse_observed_at(record_id)
        if observed is None:
            raise ValueError(f"Intervals.icu wellness record {index} has no valid date ID")
        try:
            record_fingerprint = hashlib.sha256(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Intervals.icu wellness record {record_id} is not valid JSON data") from exc

        for api_field, (metric_name, unit) in _INTERVALS_WELLNESS_FIELDS.items():
            raw_value = record.get(api_field)
            parsed = _normalize_metric(metric_name, raw_value, unit)
            if parsed is None:
                continue
            metric, value, normalized_unit = parsed
            pending.append((observed, metric, value, normalized_unit, record_fingerprint))

    for observed, metric, value, unit, fingerprint in pending:
        state_db.upsert_health_observation(
            observed_at=observed,
            metric=metric,
            value=value,
            unit=unit,
            source_label=source_label,
            fingerprint=fingerprint,
        )
    return len(pending)


def import_google_health_data_points(
    state_db: StateDB,
    data_points_by_type: dict[str, list[dict[str, object]]],
) -> int:
    if not isinstance(data_points_by_type, dict):
        raise ValueError("Google Health data points must be grouped by data type")

    payload_fields = {
        "sleep": "sleep",
        "weight": "weight",
        "steps": "steps",
        "heart-rate": "heartRate",
    }
    pending: list[tuple[str, str, float | str, str, str, str]] = []
    for data_type, records in data_points_by_type.items():
        if data_type not in payload_fields:
            raise ValueError(f"Unsupported Google Health data type: {data_type}")
        if not isinstance(records, list):
            raise ValueError(f"Google Health {data_type} data points must be a list")
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                raise ValueError(f"Google Health {data_type} data point {index} must be an object")
            try:
                canonical_record = json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Google Health {data_type} data point {index} is not valid JSON data"
                ) from exc
            fingerprint = hashlib.sha256(
                f"{data_type}\n{canonical_record}".encode("utf-8")
            ).hexdigest()
            observed_at, metrics = _google_health_point_observations(
                data_type,
                record,
                payload_fields[data_type],
                index,
            )
            source_label = f"Google Health {data_type}"
            for metric_name, raw_value, raw_unit in metrics:
                parsed = _normalize_metric(metric_name, raw_value, raw_unit)
                if parsed is None:
                    continue
                metric, value, unit = parsed
                pending.append(
                    (observed_at, metric, value, unit, source_label, fingerprint)
                )

    state_db.upsert_health_observations(pending)
    return len(pending)


def _google_health_point_observations(
    data_type: str,
    record: dict[str, object],
    payload_field: str,
    index: int,
) -> tuple[str, list[tuple[str, object, str]]]:
    payload = record.get(payload_field)
    if not isinstance(payload, dict):
        raise ValueError(
            f"Google Health {data_type} data point {index} must contain a {payload_field} object"
        )

    if data_type == "sleep":
        interval = _google_health_object(payload.get("interval"), "sleep interval")
        start = _google_health_timestamp(interval.get("startTime"), "sleep startTime")
        end = _google_health_timestamp(interval.get("endTime"), "sleep endTime")
        if end <= start:
            raise ValueError(f"Google Health sleep data point {index} has an invalid interval")
        metrics = _google_health_sleep_metrics(payload, start, end, index)
        return end.isoformat(), metrics

    if data_type == "weight":
        sample_time = _google_health_object(payload.get("sampleTime"), "weight sampleTime")
        observed_at = _google_health_timestamp(
            sample_time.get("physicalTime"), "weight physicalTime"
        )
        weight_grams = _google_health_number(
            payload.get("weightGrams"), "weightGrams", minimum=0, maximum=1_000_000
        )
        return observed_at.isoformat(), [("weight_kg", weight_grams / 1000, "kg")]

    if data_type == "steps":
        interval = _google_health_object(payload.get("interval"), "steps interval")
        start = _google_health_timestamp(interval.get("startTime"), "steps startTime")
        end = _google_health_timestamp(interval.get("endTime"), "steps endTime")
        if end <= start:
            raise ValueError(f"Google Health steps data point {index} has an invalid interval")
        count = _google_health_integer(payload.get("count"), "steps count", 0, 1_000_000)
        return end.isoformat(), [("steps", count, "count")]

    sample_time = _google_health_object(payload.get("sampleTime"), "heart-rate sampleTime")
    observed_at = _google_health_timestamp(
        sample_time.get("physicalTime"), "heart-rate physicalTime"
    )
    beats_per_minute = _google_health_integer(
        payload.get("beatsPerMinute"), "beatsPerMinute", 1, 300
    )
    return observed_at.isoformat(), [("pulse_bpm", beats_per_minute, "bpm")]


def _google_health_sleep_metrics(
    payload: dict[str, object],
    session_start: datetime,
    session_end: datetime,
    index: int,
) -> list[tuple[str, object, str]]:
    metrics: list[tuple[str, object, str]] = []
    summary_value = payload.get("summary")
    summary: dict[str, object] = {}
    if summary_value is not None:
        summary = _google_health_object(summary_value, "sleep summary")

    minutes_in_period = _google_health_optional_integer(
        summary, "minutesInSleepPeriod", "sleep minutesInSleepPeriod"
    )
    minutes_asleep = _google_health_optional_integer(
        summary, "minutesAsleep", "sleep minutesAsleep"
    )
    minutes_awake = _google_health_optional_integer(
        summary, "minutesAwake", "sleep minutesAwake"
    )
    minutes_to_fall_asleep = _google_health_optional_integer(
        summary, "minutesToFallAsleep", "sleep minutesToFallAsleep"
    )
    minutes_after_wake = _google_health_optional_integer(
        summary, "minutesAfterWakeUp", "sleep minutesAfterWakeUp"
    )
    if minutes_in_period is not None:
        metrics.append(("time_in_bed_hours", minutes_in_period, "min"))
    if minutes_to_fall_asleep is not None:
        metrics.append(("sleep_latency_seconds", minutes_to_fall_asleep * 60, "s"))
    if minutes_after_wake is not None:
        metrics.append(("sleep_after_wake_seconds", minutes_after_wake * 60, "s"))

    summary_stages: dict[str, float] = {}
    stage_summaries = summary.get("stagesSummary")
    if stage_summaries is not None:
        if not isinstance(stage_summaries, list):
            raise ValueError(f"Google Health sleep data point {index} stagesSummary must be a list")
        for stage_index, raw_stage in enumerate(stage_summaries):
            stage = _google_health_object(
                raw_stage, f"sleep stage summary {stage_index}"
            )
            stage_type = stage.get("type")
            if not isinstance(stage_type, str) or not stage_type:
                raise ValueError(
                    f"Google Health sleep data point {index} stage summary {stage_index} has no type"
                )
            minutes = _google_health_integer(
                stage.get("minutes"), f"sleep stage {stage_type} minutes", 0
            )
            summary_stages[stage_type] = summary_stages.get(stage_type, 0) + minutes * 60

    interval_stages: dict[str, float] = {}
    raw_stages = payload.get("stages")
    if raw_stages is not None:
        if not isinstance(raw_stages, list):
            raise ValueError(f"Google Health sleep data point {index} stages must be a list")
        for stage_index, raw_stage in enumerate(raw_stages):
            stage = _google_health_object(raw_stage, f"sleep stage {stage_index}")
            stage_type = stage.get("type")
            if not isinstance(stage_type, str) or not stage_type:
                raise ValueError(
                    f"Google Health sleep data point {index} stage {stage_index} has no type"
                )
            start = _google_health_timestamp(
                stage.get("startTime"), f"sleep stage {stage_index} startTime"
            )
            end = _google_health_timestamp(
                stage.get("endTime"), f"sleep stage {stage_index} endTime"
            )
            if start < session_start or end > session_end or end <= start:
                raise ValueError(
                    f"Google Health sleep data point {index} stage {stage_index} is outside its session"
                )
            interval_stages[stage_type] = interval_stages.get(stage_type, 0) + (
                end - start
            ).total_seconds()

    stage_metrics = {
        "DEEP": "deep_sleep_seconds",
        "LIGHT": "light_sleep_seconds",
        "REM": "rem_sleep_seconds",
        "AWAKE": "awake_sleep_seconds",
        "RESTLESS": "restless_sleep_seconds",
    }
    for stage_type, metric_name in stage_metrics.items():
        stage_seconds = summary_stages.get(stage_type)
        if stage_seconds is None:
            stage_seconds = interval_stages.get(stage_type)
        if stage_type == "AWAKE" and minutes_awake is not None:
            stage_seconds = minutes_awake * 60
        if stage_seconds is not None:
            metrics.append((metric_name, stage_seconds, "s"))

    total_sleep_seconds: float | None = None
    if minutes_asleep is not None:
        total_sleep_seconds = minutes_asleep * 60
    else:
        stage_values = {
            **interval_stages,
            **summary_stages,
        }
        sleep_stage_values = [
            stage_values[stage_type]
            for stage_type in ("DEEP", "LIGHT", "REM", "ASLEEP")
            if stage_type in stage_values
        ]
        if sleep_stage_values:
            total_sleep_seconds = sum(sleep_stage_values)
    if total_sleep_seconds is not None:
        metrics.append(("sleep_hours", total_sleep_seconds, "s"))
    return metrics


def _google_health_object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"Google Health {field} must be an object")
    return value


def _google_health_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or "T" not in value:
        raise ValueError(f"Google Health {field} must be an RFC 3339 timestamp")
    parsed = parse_datetime(value)
    if parsed is None:
        raise ValueError(f"Google Health {field} must be an RFC 3339 timestamp")
    return parsed.astimezone(timezone.utc)


def _google_health_optional_integer(
    payload: dict[str, object], key: str, field: str
) -> int | None:
    if key not in payload or payload[key] is None:
        return None
    return _google_health_integer(payload[key], field, 0)


def _google_health_integer(
    value: object,
    field: str,
    minimum: int,
    maximum: int = (1 << 63) - 1,
) -> int:
    if isinstance(value, bool):
        raise ValueError(f"Google Health {field} must be an integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = int(value.strip())
        except ValueError as exc:
            raise ValueError(f"Google Health {field} must be an integer") from exc
    else:
        raise ValueError(f"Google Health {field} must be an integer")
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"Google Health {field} is outside the supported range")
    return parsed


def _google_health_number(
    value: object, field: str, *, minimum: float, maximum: float
) -> float:
    if isinstance(value, bool):
        raise ValueError(f"Google Health {field} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Google Health {field} must be a number") from exc
    if not math.isfinite(parsed) or parsed < minimum or parsed > maximum:
        raise ValueError(f"Google Health {field} is outside the supported range")
    return parsed


def import_garmin_user_summaries(
    state_db: StateDB,
    records: list[dict[str, object]],
    *,
    source_label: str = "Garmin Connect daily summary",
) -> dict[str, int]:
    stored_records: list[dict[str, str]] = []
    pending_observations: list[tuple[str, str, float | str, str, str, str]] = []
    seen_dates: set[str] = set()

    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(f"Garmin user summary record {index} must be an object")
        calendar_date = _parse_garmin_summary_date(
            record.get("calendarDate"), f"record {index} date"
        )
        if calendar_date in seen_dates:
            raise ValueError(f"Garmin user summary date {calendar_date} appears more than once")
        seen_dates.add(calendar_date)
        try:
            summary_json = json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Garmin user summary for {calendar_date} is not valid JSON data"
            ) from exc
        fingerprint = hashlib.sha256(summary_json.encode("utf-8")).hexdigest()
        stored_records.append(
            {
                "calendar_date": calendar_date,
                "summary_json": summary_json,
                "fingerprint": fingerprint,
                "source_label": source_label,
            }
        )

        observed_at = datetime.combine(
            date.fromisoformat(calendar_date), time.max, tzinfo=timezone.utc
        ).isoformat()
        for api_field, (metric_name, unit) in _GARMIN_USER_SUMMARY_FIELDS.items():
            parsed = _normalize_garmin_user_summary_metric(
                metric_name, record.get(api_field), unit
            )
            if parsed is None:
                continue
            metric, value, normalized_unit = parsed
            pending_observations.append(
                (observed_at, metric, value, normalized_unit, source_label, fingerprint)
            )

    summaries_stored = state_db.save_garmin_user_summaries(
        stored_records, pending_observations
    )
    return {
        "summaries_stored": summaries_stored,
        "observations_processed": len(pending_observations),
    }


def list_garmin_user_summaries(
    state_db: StateDB,
    start_date: str,
    end_date: str,
) -> dict[str, object]:
    start = _parse_garmin_summary_date(start_date, "start-date")
    end = _parse_garmin_summary_date(end_date, "end-date")
    if end < start:
        raise ValueError("Garmin user summary end date must be on or after the start date")
    rows = state_db.list_garmin_user_summaries(start, end)
    records: list[dict[str, object]] = []
    for row in rows:
        try:
            summary = json.loads(str(row["summary_json"]))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Stored Garmin user summary for {row['calendar_date']} is invalid JSON"
            ) from exc
        if not isinstance(summary, dict):
            raise ValueError(
                f"Stored Garmin user summary for {row['calendar_date']} must be an object"
            )
        records.append(
            {
                "calendar_date": str(row["calendar_date"]),
                "source_label": str(row["source_label"]),
                "summary": summary,
            }
        )
    return {"record_count": len(records), "records": records}


def import_garmin_health_details(
    state_db: StateDB,
    records: list[dict[str, object]],
) -> dict[str, int]:
    stored_records: list[dict[str, str]] = []
    pending_observations: list[tuple[str, str, float | str, str, str, str]] = []
    seen_dates: set[tuple[str, str]] = set()

    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(f"Garmin health detail record {index} must be an object")
        dataset = record.get("dataset")
        if not isinstance(dataset, str) or dataset not in _GARMIN_HEALTH_DETAIL_FIELDS:
            raise ValueError(f"Garmin health detail record {index} has an unsupported dataset")
        calendar_date = _parse_garmin_detail_date(
            record.get("calendarDate"), f"detail record {index} date"
        )
        key = (dataset, calendar_date)
        if key in seen_dates:
            raise ValueError(
                f"Garmin health detail {dataset} date {calendar_date} appears more than once"
            )
        seen_dates.add(key)
        payload = record.get("payload")
        if payload is not None and not isinstance(payload, (dict, list)):
            raise ValueError(
                f"Garmin health detail {dataset} for {calendar_date} must contain an object, list, or null"
            )
        try:
            payload_json = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Garmin health detail {dataset} for {calendar_date} is not valid JSON data"
            ) from exc
        for item in _garmin_health_detail_objects(payload):
            response_date = item.get("calendarDate")
            if response_date not in (None, ""):
                parsed_response_date = _parse_garmin_detail_date(
                    response_date, f"{dataset} response date for {calendar_date}"
                )
                if parsed_response_date != calendar_date:
                    raise ValueError(
                        f"Garmin {dataset} response date {parsed_response_date} "
                        f"does not match requested date {calendar_date}"
                    )

        fingerprint = hashlib.sha256(
            f"{dataset}\n{calendar_date}\n{payload_json}".encode("utf-8")
        ).hexdigest()
        source_label = f"Garmin Connect {dataset} details"
        stored_records.append(
            {
                "dataset": dataset,
                "calendar_date": calendar_date,
                "payload_json": payload_json,
                "fingerprint": fingerprint,
                "source_label": source_label,
            }
        )

        observed_at = datetime.combine(
            date.fromisoformat(calendar_date), time.max, tzinfo=timezone.utc
        ).isoformat()
        for metric_name, raw_value, unit, observation_fingerprint in (
            _garmin_health_detail_observations(dataset, payload, fingerprint)
        ):
            parsed = _normalize_metric(metric_name, raw_value, unit)
            if parsed is None:
                continue
            metric, value, normalized_unit = parsed
            pending_observations.append(
                (
                    observed_at,
                    metric,
                    value,
                    normalized_unit,
                    source_label,
                    observation_fingerprint,
                )
            )

    snapshots_stored = state_db.save_garmin_health_details(
        stored_records, pending_observations
    )
    return {
        "snapshots_stored": snapshots_stored,
        "observations_processed": len(pending_observations),
    }


def list_garmin_health_details(
    state_db: StateDB,
    start_date: str,
    end_date: str,
    dataset: str | None = None,
) -> dict[str, object]:
    start = _parse_garmin_detail_date(start_date, "start-date")
    end = _parse_garmin_detail_date(end_date, "end-date")
    if end < start:
        raise ValueError("Garmin health detail end date must be on or after the start date")
    if dataset is not None and dataset not in _GARMIN_HEALTH_DETAIL_FIELDS:
        raise ValueError(f"Unsupported Garmin health detail dataset: {dataset}")

    rows = state_db.list_garmin_health_details(start, end, dataset)
    records: list[dict[str, object]] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Stored Garmin health detail for {row['dataset']} on {row['calendar_date']} is invalid JSON"
            ) from exc
        records.append(
            {
                "dataset": str(row["dataset"]),
                "calendar_date": str(row["calendar_date"]),
                "source_label": str(row["source_label"]),
                "payload": payload,
            }
        )
    return {"record_count": len(records), "records": records}


def _garmin_health_detail_observations(
    dataset: str,
    payload: object,
    fingerprint: str,
) -> list[tuple[str, object, str, str]]:
    if dataset == "blood-pressure":
        if not isinstance(payload, dict):
            return []
        measurements = payload.get("bloodPressureMeasurements")
        if measurements is None:
            return []
        if not isinstance(measurements, list):
            raise ValueError("Garmin blood-pressure measurements must be a list")
        pending: list[tuple[str, object, str, str]] = []
        for index, measurement in enumerate(measurements):
            if not isinstance(measurement, dict):
                raise ValueError(f"Garmin blood-pressure measurement {index} must be an object")
            for api_field, (metric_name, unit) in _GARMIN_BLOOD_PRESSURE_FIELDS.items():
                if measurement.get(api_field) is None:
                    continue
                measurement_fingerprint = hashlib.sha256(
                    f"{fingerprint}\n{index}\n{api_field}".encode("utf-8")
                ).hexdigest()
                pending.append(
                    (metric_name, measurement[api_field], unit, measurement_fingerprint)
                )
        return pending

    fields = _GARMIN_HEALTH_DETAIL_FIELDS[dataset]
    if not fields:
        return []
    if dataset == "sleep":
        if not isinstance(payload, dict):
            return []
        sleep_data = payload.get("dailySleepDTO")
        nodes = _garmin_health_detail_objects(sleep_data)
    else:
        nodes = _garmin_health_detail_objects(payload)

    pending = []
    for api_field, (metric_name, unit) in fields.items():
        for node in nodes:
            raw_value = node.get(api_field)
            if raw_value is not None:
                pending.append((metric_name, raw_value, unit, fingerprint))
                break
    return pending


def _garmin_health_detail_objects(value: object) -> list[dict[str, object]]:
    objects: list[dict[str, object]] = []
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            objects.append(current)
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return objects


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
            suffix = f" {display_unit}" if display_unit and display_unit not in {"status", "text"} else ""
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


def _garmin_health_detail_metric_values(
    dataset: str,
    payload: object,
    fingerprint: str,
) -> dict[str, float | str]:
    metrics: dict[str, float | str] = {}
    for metric_name, raw_value, unit, _ in _garmin_health_detail_observations(
        dataset, payload, fingerprint
    ):
        normalized = _normalize_metric(metric_name, raw_value, unit)
        if normalized is not None:
            metric, value, _ = normalized
            metrics[metric] = value
    return metrics


def _garmin_health_detail_metrics_for_date(
    state_db: StateDB,
    calendar_date: str,
    dataset: str,
) -> dict[str, float | str]:
    metrics: dict[str, float | str] = {}
    for row in state_db.list_garmin_health_details(calendar_date, calendar_date, dataset):
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Stored Garmin health detail for {dataset} on {calendar_date} is invalid JSON"
            ) from exc
        metrics.update(
            _garmin_health_detail_metric_values(
                dataset,
                payload,
                str(row["fingerprint"]),
            )
        )
    return metrics


def _garmin_sleep_context_before_activity(
    state_db: StateDB,
    activity_start: datetime,
) -> dict[str, object] | None:
    first_date = (activity_start.date() - timedelta(days=1)).isoformat()
    last_date = (activity_start.date() + timedelta(days=1)).isoformat()
    rows = state_db.list_garmin_health_details(first_date, last_date, "sleep")
    candidates: list[tuple[datetime, dict[str, object]]] = []

    for row in rows:
        calendar_date = str(row["calendar_date"])
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Stored Garmin health detail for sleep on {calendar_date} is invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            continue
        sleep_data = payload.get("dailySleepDTO")
        if not isinstance(sleep_data, dict):
            continue

        sleep_end = parse_datetime(sleep_data.get("sleepEndTimestampGMT"))
        if sleep_end is None or sleep_end > activity_start:
            continue
        sleep_start = parse_datetime(sleep_data.get("sleepStartTimestampGMT"))
        if sleep_start is not None and (sleep_start >= sleep_end or sleep_start > activity_start):
            continue

        context: dict[str, object] = {
            "calendar_date": calendar_date,
            "sleep_end_utc": sleep_end.isoformat(),
            **_garmin_health_detail_metric_values(
                "sleep",
                payload,
                str(row["fingerprint"]),
            ),
        }
        if sleep_start is not None:
            context["sleep_start_utc"] = sleep_start.isoformat()

        sleep_score = sleep_data.get("sleepScore")
        sleep_quality = sleep_data.get("sleepQuality")
        sleep_scores = sleep_data.get("sleepScores")
        overall_score = sleep_scores.get("overall") if isinstance(sleep_scores, dict) else None
        if isinstance(overall_score, dict):
            if sleep_score is None:
                sleep_score = overall_score.get("value")
            if sleep_quality is None:
                sleep_quality = overall_score.get("qualifierKey")
        if sleep_score is not None:
            normalized_score = _normalize_metric("sleep_score", sleep_score, "score")
            if normalized_score is not None:
                context["sleep_score"] = normalized_score[1]
        if isinstance(sleep_quality, str) and sleep_quality.strip():
            context["sleep_quality"] = " ".join(sleep_quality.split())

        average_heart_rate = sleep_data.get("averageHeartRate")
        if average_heart_rate is not None:
            normalized_heart_rate = _normalize_metric("pulse_bpm", average_heart_rate, "bpm")
            if normalized_heart_rate is not None:
                context["sleep_avg_hr_bpm"] = normalized_heart_rate[1]

        hrv = _garmin_health_detail_metrics_for_date(state_db, calendar_date, "hrv")
        if "hrv_ms" in hrv:
            context["sleep_avg_hrv_ms"] = hrv["hrv_ms"]
        if "hrv_weekly_average_ms" in hrv:
            context["hrv_7d_baseline_ms"] = hrv["hrv_weekly_average_ms"]

        spo2 = _garmin_health_detail_metrics_for_date(
            state_db, calendar_date, "spo2-acclimation"
        )
        if "avg_sleep_spo2_percent" in spo2:
            context["sleep_avg_spo2_percent"] = spo2["avg_sleep_spo2_percent"]

        respiration = _garmin_health_detail_metrics_for_date(
            state_db, calendar_date, "respiration"
        )
        if "avg_sleep_respiration_bpm" in respiration:
            context["sleep_avg_respiration_bpm"] = respiration["avg_sleep_respiration_bpm"]

        candidates.append((sleep_end, context))

    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _google_health_sleep_context_before_activity(
    observations: list[object],
) -> dict[str, object] | None:
    sleep_metrics = {
        "sleep_hours",
        "time_in_bed_hours",
        "deep_sleep_seconds",
        "light_sleep_seconds",
        "rem_sleep_seconds",
        "awake_sleep_seconds",
        "restless_sleep_seconds",
        "sleep_latency_seconds",
        "sleep_after_wake_seconds",
    }
    sessions: dict[str, dict[str, object]] = {}
    for row in observations:
        if str(row["source_label"]) != "Google Health sleep":
            continue
        fingerprint = str(row["fingerprint"])
        observed_at = str(row["observed_at"])
        context = sessions.setdefault(
            fingerprint,
            {
                "source": "Google Health",
                "sleep_end_utc": observed_at,
            },
        )
        metric = str(row["metric"])
        if metric in sleep_metrics:
            context[metric] = _health_value(row["value"])

    candidates: list[tuple[datetime, dict[str, object]]] = []
    for context in sessions.values():
        sleep_end = parse_datetime(context.get("sleep_end_utc"))
        if sleep_end is not None and any(metric in context for metric in sleep_metrics):
            candidates.append((sleep_end, context))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def summarize_health_for_activity(
    state_db: StateDB,
    activity_start: object,
    activity_end: object,
) -> dict[str, object]:
    if activity_start in (None, ""):
        return {
            "before_activity": {},
            "after_activity": {},
            "training_readiness_before_activity": None,
            "sleep_before_activity": None,
        }
    start = parse_datetime(activity_start)
    if start is None:
        raise ValueError("Activity start time is invalid")
    start = start.astimezone(timezone.utc)

    observations_before = state_db.list_health_observations(observed_before=start.isoformat())
    before = _latest_health_by_metric(observations_before)
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
    garmin_sleep_context = _garmin_sleep_context_before_activity(state_db, start)
    google_health_sleep_context = _google_health_sleep_context_before_activity(
        observations_before
    )
    sleep_context_candidates = [
        context
        for context in (garmin_sleep_context, google_health_sleep_context)
        if context is not None
    ]
    sleep_context = max(
        sleep_context_candidates,
        key=lambda context: parse_datetime(context.get("sleep_end_utc"))
        or datetime.min.replace(tzinfo=timezone.utc),
        default=None,
    )
    if sleep_context is not None:
        sleep_metric_pairs = (
            ("sleep_hours", "sleep_hours"),
            ("deep_sleep_seconds", "deep_sleep_seconds"),
            ("light_sleep_seconds", "light_sleep_seconds"),
            ("rem_sleep_seconds", "rem_sleep_seconds"),
            ("awake_sleep_seconds", "awake_sleep_seconds"),
            ("restless_sleep_seconds", "restless_sleep_seconds"),
            ("time_in_bed_hours", "time_in_bed_hours"),
            ("sleep_latency_seconds", "sleep_latency_seconds"),
            ("sleep_after_wake_seconds", "sleep_after_wake_seconds"),
            ("hrv_ms", "sleep_avg_hrv_ms"),
            ("hrv_weekly_average_ms", "hrv_7d_baseline_ms"),
            ("avg_sleep_spo2_percent", "sleep_avg_spo2_percent"),
            ("avg_sleep_respiration_bpm", "sleep_avg_respiration_bpm"),
        )
        for observation_metric, sleep_field in sleep_metric_pairs:
            observation = after.get(observation_metric)
            sleep_value = sleep_context.get(sleep_field)
            if (
                isinstance(observation, dict)
                and sleep_value is not None
                and observation.get("value") == sleep_value
            ):
                after.pop(observation_metric, None)
    readiness = state_db.get_latest_training_readiness_before(start.isoformat())
    readiness_context = None
    if readiness is not None:
        try:
            payload = json.loads(str(readiness["payload_json"]))
        except (TypeError, ValueError) as exc:
            raise ValueError("Stored training readiness record is invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("Stored training readiness record must be a JSON object")
        readiness_fields = (
            "recoveryTime",
            "recoveryTimeChangePhrase",
            "recoveryTimeFactorPercent",
            "recoveryTimeFactorFeedback",
            "acwrFactorPercent",
            "acwrFactorFeedback",
            "acuteLoad",
            "stressHistoryFactorPercent",
            "stressHistoryFactorFeedback",
            "hrvFactorPercent",
            "hrvFactorFeedback",
            "hrvWeeklyAverage",
            "sleepHistoryFactorPercent",
            "sleepHistoryFactorFeedback",
            "sleepScore",
            "validSleep",
        )
        readiness_context = {
            "calendar_date": str(readiness["calendar_date"]),
            "observed_at": str(readiness["observed_at"]),
            "score": readiness["score"],
            "level": readiness["level"],
            "data": {
                field: payload[field]
                for field in readiness_fields
                if field in payload and payload[field] is not None
            },
        }
    return {
        "before_activity": before,
        "after_activity": after,
        "training_readiness_before_activity": readiness_context,
        "sleep_before_activity": sleep_context,
    }


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
    if metric in _TEXT_METRICS:
        text_value = str(raw_value).strip()
        if not text_value:
            return None
        return metric, text_value, unit
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
    elif metric in {"sleep_hours", "time_in_bed_hours"} and lowered_unit in {
        "min",
        "minute",
        "minutes",
    }:
        value /= 60
        unit = "h"
    elif metric in {"sleep_hours", "time_in_bed_hours"} and lowered_unit in {
        "s",
        "sec",
        "secs",
        "second",
        "seconds",
    }:
        value /= 3600
        unit = "h"
    elif metric in {
        "hydration_l",
        "hydration_goal_l",
        "hydration_base_goal_l",
        "hydration_activity_l",
        "hydration_sweat_loss_l",
    } and lowered_unit in {"ml", "milliliter", "milliliters"}:
        value /= 1000
        unit = "L"
    elif metric == "distance_km" and lowered_unit in {"m", "meter", "meters"}:
        value /= 1000
        unit = "km"
    elif metric == "lactate_threshold_speed_kmh" and lowered_unit in {"mph", "mi/h"}:
        value *= 1.609344
        unit = "km/h"
    elif metric in {"steps", "step_goal", "floors_goal", "floors_descended"}:
        value = int(value)
        unit = "count"
    return metric, value, unit


def _normalize_garmin_user_summary_metric(
    name: str,
    raw_value: object,
    unit: str,
) -> tuple[str, float | str, str] | None:
    metric = _METRIC_ALIASES.get(name)
    if metric == "hrv_status":
        if raw_value is None:
            return None
        value = " ".join(str(raw_value).strip().split())
        return (metric, value, unit) if value else None
    return _normalize_metric(name, raw_value, unit)


def _parse_garmin_summary_date(value: object, field: object) -> str:
    try:
        parsed = date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Garmin user summary {field} must use YYYY-MM-DD"
        ) from exc
    if parsed.isoformat() != str(value):
        raise ValueError(f"Garmin user summary {field} must use YYYY-MM-DD")
    return parsed.isoformat()


def _parse_garmin_detail_date(value: object, field: object) -> str:
    try:
        parsed = date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Garmin health detail {field} must use YYYY-MM-DD") from exc
    if parsed.isoformat() != str(value):
        raise ValueError(f"Garmin health detail {field} must use YYYY-MM-DD")
    return parsed.isoformat()


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
