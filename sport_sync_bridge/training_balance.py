from __future__ import annotations

import csv
import io
import json
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Mapping


CTL_DAYS = 42
ATL_DAYS = 7
RESTING_HEART_RATE_DEFAULT = 60.0

_ZONE_ADVICE = {
    "high_risk": "疲劳积累已经达到危险水平。建议暂停高强度训练，安排休息或低强度恢复。",
    "optimal": "当前处于体能提升区间。保持计划中的训练节奏，并继续观察恢复情况。",
    "grey": "当前状态接近中性。可按计划训练，结合主观感受调整强度。",
    "fresh": "近期训练负荷较低，恢复较充分。适合安排质量训练或比赛，同时留意近期负荷变化。",
    "transitional": "近期训练负荷偏低。若不是计划中的休整期，可逐步恢复规律训练。",
}


def calculate_training_balance(
    rows: Iterable[object],
    *,
    resting_hr: float = RESTING_HEART_RATE_DEFAULT,
    threshold_hr: float | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict[str, object]:
    resting_hr = _positive_finite(resting_hr, "resting heart rate")
    if threshold_hr is not None:
        threshold_hr = _positive_finite(threshold_hr, "lactate threshold heart rate")
        if threshold_hr <= resting_hr:
            raise ValueError("Lactate threshold heart rate must exceed resting heart rate")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise ValueError("Start date must not be after end date")

    today = datetime.now(timezone.utc).date()
    output_end = date_to or today
    activities: list[tuple[date, float | None, str | None]] = []
    for row in rows:
        start_time = _row_value(row, "start_time")
        summary_value = _row_value(row, "summary_json")
        summary = _decode_summary(summary_value)
        if not start_time:
            start_time = summary.get("start_time")
        activity_day = _activity_date(start_time)
        if activity_day is None or activity_day > output_end:
            continue

        tss = _optional_nonnegative(summary.get("training_stress_score"))
        source: str | None = None
        if tss is not None:
            source = "fit_tss"
        else:
            average_hr = _optional_nonnegative(
                summary.get("average_heart_rate_bpm", summary.get("average_heart_rate"))
            )
            duration_s = _optional_nonnegative(summary.get("timer_time_s"))
            if duration_s is None:
                duration_s = _optional_nonnegative(summary.get("elapsed_time_s"))
            if (
                threshold_hr is not None
                and average_hr is not None
                and duration_s is not None
                and duration_s > 0
            ):
                hr_if = (average_hr - resting_hr) / (threshold_hr - resting_hr)
                hr_if = min(2.0, max(0.0, hr_if))
                tss = duration_s / 3600.0 * hr_if * hr_if * 100.0
                source = "hr_tss"
            else:
                tss = None
        activities.append((activity_day, tss, source))

    if not activities:
        raise ValueError("No dated local activities found on or before the requested end date")

    first_activity_day = min(item[0] for item in activities)
    output_start = date_from or first_activity_day
    if output_start > output_end:
        raise ValueError("Start date must not be after end date")
    calculation_start = min(output_start, first_activity_day)

    daily_tss: dict[date, float] = defaultdict(float)
    daily_activity_counts: Counter[date] = Counter()
    source_counts: Counter[str] = Counter()
    unscored_activity_count = 0
    for activity_day, tss, source in activities:
        if source is not None and tss is not None:
            daily_tss[activity_day] += tss
        if activity_day >= output_start:
            daily_activity_counts[activity_day] += 1
            if source is None or tss is None:
                unscored_activity_count += 1
            else:
                source_counts[source] += 1

    ctl = 0.0
    atl = 0.0
    history: list[dict[str, object]] = []
    current = calculation_start
    while current <= output_end:
        load = daily_tss[current]
        ctl = ctl * (1.0 - 1.0 / CTL_DAYS) + load / CTL_DAYS
        atl = atl * (1.0 - 1.0 / ATL_DAYS) + load / ATL_DAYS
        tsb = ctl - atl
        if current >= output_start:
            zone = _zone_for_tsb(tsb)
            history.append(
                {
                    "date": current.isoformat(),
                    "activity_count": daily_activity_counts[current],
                    "training_stress_score": round(load, 3),
                    "ctl": round(ctl, 3),
                    "atl": round(atl, 3),
                    "tsb": round(tsb, 3),
                    "zone": zone,
                }
            )
        current += timedelta(days=1)

    if not history:
        raise ValueError("The requested date range contains no training balance days")
    latest = history[-1]
    return {
        "start_date": history[0]["date"],
        "end_date": history[-1]["date"],
        "as_of_date": latest["date"],
        "resting_heart_rate_bpm": resting_hr,
        "lactate_threshold_heart_rate_bpm": threshold_hr,
        "fitness_ctl": latest["ctl"],
        "fatigue_atl": latest["atl"],
        "form_tsb": latest["tsb"],
        "zone": latest["zone"],
        "advice": _ZONE_ADVICE[str(latest["zone"])],
        "scored_activity_count": sum(source_counts.values()),
        "unscored_activity_count": unscored_activity_count,
        "load_source_counts": dict(sorted(source_counts.items())),
        "history": history,
    }


def format_training_balance(result: dict[str, object], output_format: str) -> str:
    if output_format == "json":
        return json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    history = result["history"]
    if not isinstance(history, list):
        raise ValueError("Training balance history is invalid")
    fields = ("date", "activity_count", "training_stress_score", "ctl", "atl", "tsb", "zone")
    if output_format == "csv":
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(history)
        return stream.getvalue()
    if output_format == "txt":
        lines = [
            f"range={result['start_date']}..{result['end_date']}",
            f"CTL={result['fitness_ctl']}  ATL={result['fatigue_atl']}  TSB={result['form_tsb']}",
            f"zone={result['zone']}",
            f"advice={result['advice']}",
            f"scored={result['scored_activity_count']}  unscored={result['unscored_activity_count']}",
            "date  activities  TSS  CTL  ATL  TSB  zone",
        ]
        lines.extend(
            f"{item['date']}  {item['activity_count']}  {item['training_stress_score']}  "
            f"{item['ctl']}  {item['atl']}  {item['tsb']}  {item['zone']}"
            for item in history
        )
        return "\n".join(lines) + "\n"
    raise ValueError(f"Unsupported training balance format: {output_format}")


def _zone_for_tsb(tsb: float) -> str:
    if tsb < -30.0:
        return "high_risk"
    if tsb <= -10.0:
        return "optimal"
    if tsb <= 5.0:
        return "grey"
    if tsb <= 25.0:
        return "fresh"
    return "transitional"


def _decode_summary(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("Local activity summary is invalid JSON") from exc
        if isinstance(decoded, dict):
            return decoded
    return {}


def _row_value(row: object, key: str) -> object | None:
    if isinstance(row, Mapping):
        return row.get(key)
    if isinstance(row, sqlite3.Row):
        return row[key]
    return getattr(row, key, None)


def _activity_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).date() if value.tzinfo is not None else value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            return parsed.astimezone(timezone.utc).date() if parsed.tzinfo is not None else parsed.date()
        except ValueError:
            return None
    return None


def _positive_finite(value: float, label: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{label} must be a positive number")
    return parsed


def _optional_nonnegative(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return parsed
