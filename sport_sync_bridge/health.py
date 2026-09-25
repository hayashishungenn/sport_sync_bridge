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
    "sleep": "sleep_hours",
    "sleep_hours": "sleep_hours",
    "deep_sleep_seconds": "deep_sleep_seconds",
    "light_sleep_seconds": "light_sleep_seconds",
    "rem_sleep_seconds": "rem_sleep_seconds",
    "awake_sleep_seconds": "awake_sleep_seconds",
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
    "sleep_hours": "h",
    "deep_sleep_seconds": "s",
    "light_sleep_seconds": "s",
    "rem_sleep_seconds": "s",
    "awake_sleep_seconds": "s",
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
    "sleep_hours": "睡眠时长",
    "deep_sleep_seconds": "深睡时长",
    "light_sleep_seconds": "浅睡时长",
    "rem_sleep_seconds": "快速眼动睡眠时长",
    "awake_sleep_seconds": "清醒时长",
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
    elif metric == "sleep_hours" and lowered_unit in {"min", "minute", "minutes"}:
        value /= 60
        unit = "h"
    elif metric == "sleep_hours" and lowered_unit in {"s", "sec", "secs", "second", "seconds"}:
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
