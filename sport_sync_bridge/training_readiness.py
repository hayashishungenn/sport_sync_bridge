from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timezone
from pathlib import Path

from .state import StateDB
from .utils import parse_datetime


_READINESS_FIELDS = (
    ("score", "训练准备度分数"),
    ("level", "等级"),
    ("feedbackShort", "简短反馈"),
    ("feedbackLong", "详细反馈"),
    ("recoveryTime", "恢复时间"),
    ("recoveryTimeChangePhrase", "恢复时间变化"),
    ("recoveryTimeFactorPercent", "恢复时间因子"),
    ("recoveryTimeFactorFeedback", "恢复时间因子反馈"),
    ("acwrFactorPercent", "ACWR 因子"),
    ("acwrFactorFeedback", "ACWR 因子反馈"),
    ("acuteLoad", "急性负荷"),
    ("stressHistoryFactorPercent", "压力历史因子"),
    ("stressHistoryFactorFeedback", "压力历史因子反馈"),
    ("hrvFactorPercent", "HRV 因子"),
    ("hrvFactorFeedback", "HRV 因子反馈"),
    ("hrvWeeklyAverage", "HRV 周平均"),
    ("sleepHistoryFactorPercent", "睡眠历史因子"),
    ("sleepHistoryFactorFeedback", "睡眠历史因子反馈"),
    ("sleepScore", "睡眠评分"),
    ("validSleep", "睡眠数据有效"),
)
_NUMERIC_FIELDS = {
    "score",
    "sleepScore",
    "recoveryTime",
    "recoveryTimeFactorPercent",
    "acwrFactorPercent",
    "acuteLoad",
    "stressHistoryFactorPercent",
    "hrvFactorPercent",
    "hrvWeeklyAverage",
    "sleepHistoryFactorPercent",
}
_STRING_FIELDS = {
    "id",
    "sourceId",
    "createdAt",
    "updatedAt",
    "calendarDate",
    "timestamp",
    "timestampLocal",
    "level",
    "feedbackShort",
    "feedbackLong",
    "recoveryTimeFactorFeedback",
    "acwrFactorFeedback",
    "stressHistoryFactorFeedback",
    "hrvFactorFeedback",
    "sleepHistoryFactorFeedback",
    "recoveryTimeChangePhrase",
    "deviceId",
}


def import_training_readiness_json(state_db: StateDB, input_path: Path) -> int:
    input_path = input_path.expanduser().resolve()
    try:
        payload = input_path.read_bytes()
    except OSError as exc:
        raise ValueError(f"Training readiness JSON does not exist or cannot be read: {input_path}") from exc
    try:
        decoded = json.loads(payload.decode("utf-8-sig"), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("Training readiness input must be valid UTF-8 JSON") from exc

    if isinstance(decoded, dict):
        source_records = [decoded]
    elif isinstance(decoded, list):
        source_records = decoded
    else:
        raise ValueError("Training readiness JSON must contain an object or a list of objects")
    if not source_records:
        raise ValueError("Training readiness JSON contains no records")

    records = [
        _normalize_record(record, input_path, index)
        for index, record in enumerate(source_records, 1)
    ]
    return state_db.save_training_readiness_records(records)


def summarize_training_readiness(state_db: StateDB, *, limit: int = 30) -> dict[str, object]:
    rows = state_db.list_training_readiness_records(limit=limit)
    records = [
        {
            "record_id": row["record_id"],
            "source_id": row["source_id"],
            "calendar_date": row["calendar_date"],
            "observed_at": row["observed_at"],
            "score": row["score"],
            "level": row["level"],
            "fingerprint": row["fingerprint"],
            "source_label": row["source_label"],
            "data": json.loads(row["payload_json"]),
        }
        for row in rows
    ]
    return {"record_count": len(records), "records": records}


def format_training_readiness_text(summary: dict[str, object]) -> str:
    records = summary.get("records")
    if not isinstance(records, list):
        records = []
    lines = [f"训练准备度记录：{len(records)}"]
    if not records:
        lines.append("暂无训练准备度记录")
        return "\n".join(lines)

    for record in records:
        if not isinstance(record, dict):
            continue
        lines.append("")
        lines.append(f"日期：{record.get('calendar_date', '未知')}")
        data = record.get("data")
        if not isinstance(data, dict):
            continue
        for field, label in _READINESS_FIELDS:
            if field not in data or data[field] is None:
                continue
            lines.append(f"{label}：{_format_value(data[field])}")
    return "\n".join(lines)


def _normalize_record(record: object, input_path: Path, index: int) -> dict[str, object]:
    if not isinstance(record, dict):
        raise ValueError(f"Training readiness record {index} must be a JSON object")
    _validate_scalar_fields(record, index)

    calendar_value = record.get("calendarDate")
    if calendar_value is None:
        calendar_date = None
    else:
        if not isinstance(calendar_value, str):
            raise ValueError(f"Training readiness record {index} has an invalid calendarDate")
        try:
            calendar_date = date.fromisoformat(calendar_value[:10]).isoformat()
        except ValueError as exc:
            raise ValueError(f"Training readiness record {index} has an invalid calendarDate") from exc

    timestamp_value = record.get("timestamp") or record.get("timestampLocal") or calendar_date
    observed = _parse_timestamp(timestamp_value, index)
    if calendar_date is None:
        calendar_date = observed.date().isoformat()

    source_id = str(record.get("sourceId") or "").strip() or "local-import"
    record_id = record.get("id")
    score = record.get("score")
    if isinstance(score, bool):
        score = None
    elif isinstance(score, (int, float)):
        score = float(score)
    else:
        score = None

    canonical = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {
        "fingerprint": fingerprint,
        "record_id": record_id,
        "source_id": source_id,
        "calendar_date": calendar_date,
        "observed_at": observed.isoformat(),
        "score": score,
        "level": record.get("level"),
        "payload_json": canonical,
        "source_label": str(input_path),
    }


def _validate_scalar_fields(record: dict[str, object], index: int) -> None:
    for field in _NUMERIC_FIELDS:
        value = record.get(field)
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise ValueError(f"Training readiness record {index} has an invalid {field}")
    for field in _STRING_FIELDS:
        value = record.get(field)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"Training readiness record {index} has an invalid {field}")
    valid_sleep = record.get("validSleep")
    if valid_sleep is not None and not isinstance(valid_sleep, bool):
        raise ValueError(f"Training readiness record {index} has an invalid validSleep")


def _parse_timestamp(value: object, index: int) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Training readiness record {index} requires calendarDate or timestamp")
    parsed = parse_datetime(value)
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"Training readiness record {index} has an invalid timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


def _format_value(value: object) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)
