from __future__ import annotations

import csv
import hashlib
import io
import math
from datetime import datetime, timezone
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
    "body_battery": "score",
    "systolic_bp_mmhg": "mmHg",
    "diastolic_bp_mmhg": "mmHg",
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


def summarize_health(state_db: StateDB) -> dict[str, object]:
    grouped: dict[str, list[object]] = {}
    for row in state_db.list_health_observations():
        grouped.setdefault(str(row["metric"]), []).append(row)
    latest = {
        metric: {
            "value": float(rows[0]["value"]),
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


def _normalize_metric(name: object, raw_value: object, raw_unit: object) -> tuple[str, float, str] | None:
    key = str(name).strip().lower().replace(" ", "_").replace("-", "_")
    metric = _METRIC_ALIASES.get(key)
    if metric is None or raw_value is None or not str(raw_value).strip():
        return None
    try:
        value = float(str(raw_value).strip().replace(",", "."))
    except ValueError as exc:
        raise ValueError(f"Invalid value for health metric {name}: {raw_value}") from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"Invalid value for health metric {name}: {raw_value}")
    unit = str(raw_unit or _DEFAULT_UNITS[metric]).strip()
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
    elif metric == "steps":
        value = int(value)
        unit = "count"
    return metric, value, unit


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
