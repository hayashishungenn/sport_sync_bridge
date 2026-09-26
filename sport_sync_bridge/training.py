from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from .state import StateDB


TRAINING_PLAN_DIR = Path(__file__).parent / "data" / "training_plans"
WORKOUT_DIR = Path(__file__).parent / "data" / "workouts"
SUPPORTED_LOCALES = {"en", "es", "fr", "it", "pt", "zh"}
_DAY_OFFSETS = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}
_DISTANCE_TARGET_RE = re.compile(r"([\d.]+)\s*(km|m)\b", re.IGNORECASE)
_DURATION_HOURS_RE = re.compile(r"(\d+)\s*h(?![a-z])", re.IGNORECASE)
_DURATION_MINUTES_RE = re.compile(r"(\d+)\s*min\b", re.IGNORECASE)
_RUN_PACE_TARGET_RE = re.compile(r"(\d+):(\d{2})/km\b", re.IGNORECASE)
_SWIM_PACE_TARGET_RE = re.compile(r"(\d+):(\d{2})/100m\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class WorkoutTemplate:
    template_id: str
    name: str
    sport_type: str
    estimated_duration_s: float | None
    estimated_distance_m: float | None
    steps: tuple[dict[str, object], ...]
    fit_path: Path
    description: str = ""


def list_training_templates(
    *,
    locale: str = "zh",
    sport_type: str | None = None,
) -> list[dict[str, object]]:
    locale = _validate_locale(locale)
    directory = TRAINING_PLAN_DIR / locale
    if not directory.is_dir():
        raise ValueError(f"Training plan templates are missing for locale {locale}")
    templates = [_load_plan(path) for path in sorted(directory.glob("*.json"))]
    if sport_type:
        templates = [item for item in templates if str(item.get("trainingPlan", {}).get("sportType", "")).lower() == sport_type.lower()]
    return templates


def get_training_template(identifier: str, *, locale: str = "zh") -> dict[str, object]:
    for template in list_training_templates(locale=locale):
        if identifier in {str(template.get("id", "")), str(template.get("templateId", ""))}:
            return template
        if identifier == str(template.get("_file_id", "")):
            return template
    raise ValueError(f"Training plan template was not found: {identifier} ({locale})")


def materialize_schedule(template: dict[str, object], start_date: date) -> list[dict[str, object]]:
    if start_date.weekday() != 0:
        raise ValueError("Training plan start date must be a Monday")
    week_templates = template.get("weekTemplates")
    if not isinstance(week_templates, list) or not week_templates:
        raise ValueError("Training plan has no weekly schedule")
    overrides = template.get("overrides") if isinstance(template.get("overrides"), list) else []
    items: list[dict[str, object]] = []
    for week in week_templates:
        if not isinstance(week, dict):
            continue
        weeks = week.get("applyToWeeks")
        days = week.get("days")
        if not isinstance(weeks, list) or not isinstance(days, dict):
            continue
        for week_number in weeks:
            if not isinstance(week_number, int) or isinstance(week_number, bool) or week_number < 1:
                continue
            for day_key, raw_definition in days.items():
                if day_key not in _DAY_OFFSETS or not isinstance(raw_definition, dict):
                    continue
                definition = dict(raw_definition)
                for override in overrides:
                    if (
                        isinstance(override, dict)
                        and override.get("weekId") == week_number
                        and override.get("day") == day_key
                        and isinstance(override.get("field"), str)
                    ):
                        definition[override["field"]] = override.get("value")
                scheduled_date = start_date + timedelta(weeks=week_number - 1, days=_DAY_OFFSETS[day_key])
                rest_day = bool(definition.get("isRestDay"))
                base_definition = {key: value for key, value in definition.items() if key != "workouts"}
                nested_workouts = definition.get("workouts")
                if rest_day:
                    if nested_workouts:
                        raise ValueError(f"Rest day {day_key} cannot contain workouts")
                    sessions = [base_definition]
                elif nested_workouts is None:
                    sessions = [base_definition]
                else:
                    if not isinstance(nested_workouts, list) or not nested_workouts:
                        raise ValueError(f"Workout day {day_key} must contain at least one session")
                    if any(not isinstance(session, dict) for session in nested_workouts):
                        raise ValueError(f"Workout day {day_key} contains an invalid session")
                    sessions = [{**base_definition, **session} for session in nested_workouts]

                for session_index, session in enumerate(sessions):
                    session_is_rest = bool(session.get("isRestDay"))
                    display_name = str(
                        session.get("name") or ("Rest day" if session_is_rest else "Workout")
                    )
                    item_key = f"{template.get('id')}:{scheduled_date.isoformat()}:{day_key}"
                    if len(sessions) > 1:
                        item_key += f":session:{session_index + 1}"
                    item_id = str(uuid5(NAMESPACE_URL, item_key))
                    payload = {
                        **session,
                        "week_number": week_number,
                        "weekday": day_key,
                    }
                    if len(sessions) > 1:
                        payload["session_number"] = session_index + 1
                    items.append(
                        {
                            "item_id": item_id,
                            "scheduled_date": scheduled_date.isoformat(),
                            "item_type": "rest" if session_is_rest else "workout",
                            "name": display_name,
                            "sport_type": session.get("sportType"),
                            "payload": payload,
                        }
                    )
    if not items:
        raise ValueError("Training plan has no usable scheduled days")
    return sorted(items, key=lambda item: (str(item["scheduled_date"]), str(item["item_id"])))


def install_training_plan(
    state_db: StateDB,
    template: dict[str, object],
    *,
    locale: str,
    start_date: date,
) -> tuple[str, list[dict[str, object]]]:
    schedule = materialize_schedule(template, start_date)
    template_id = str(template.get("id") or template.get("_file_id"))
    plan_id = hashlib.sha256(f"{locale}:{template_id}:{start_date.isoformat()}".encode()).hexdigest()[:20]
    if state_db.get_training_plan(plan_id) is not None:
        raise ValueError(f"This training plan is already installed: {plan_id}")
    training_plan = template.get("trainingPlan")
    if not isinstance(training_plan, dict):
        training_plan = {}
    state_db.save_training_plan(
        plan_id=plan_id,
        template_id=template_id,
        name=str(training_plan.get("name") or template.get("name") or template_id),
        sport_type=str(training_plan.get("sportType")) if training_plan.get("sportType") else None,
        locale=locale,
        start_date=start_date.isoformat(),
        template=template,
        schedule_items=schedule,
    )
    return plan_id, schedule


def export_training_plan_ics(state_db: StateDB, plan_id: str, output_path: Path) -> Path:
    plan = state_db.get_training_plan(plan_id)
    if plan is None:
        raise ValueError(f"Installed training plan was not found: {plan_id}")
    items = state_db.get_schedule_items(plan["plan_id"])
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//sport_sync_bridge//Training plan//EN",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{_ics_escape(plan['name'])}",
    ]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for item in items:
        if item["item_type"] == "rest":
            continue
        day = date.fromisoformat(item["scheduled_date"])
        payload = json.loads(item["payload_json"])
        description = payload.get("coachNote") or payload.get("description") or ""
        lines.extend(
            (
                "BEGIN:VEVENT",
                f"UID:{_ics_escape(item['item_id'])}@sport-sync-bridge",
                f"DTSTAMP:{stamp}",
                f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}",
                f"DTEND;VALUE=DATE:{(day + timedelta(days=1)).strftime('%Y%m%d')}",
                f"SUMMARY:{_ics_escape(item['name'])}",
                f"DESCRIPTION:{_ics_escape(str(description))}",
                f"CATEGORIES:{_ics_escape(item['sport_type'] or 'workout')}",
                "END:VEVENT",
            )
        )
    lines.append("END:VCALENDAR")
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8", newline="")
    return output_path


def list_training_schedule(state_db: StateDB, plan_id: str) -> dict[str, object]:
    plan = _require_training_plan(state_db, plan_id)
    items = []
    for row in state_db.get_schedule_items(plan["plan_id"]):
        payload = _read_schedule_payload(row["payload_json"], str(row["item_id"]))
        targets, unsupported_targets = _training_targets(payload, row["sport_type"])
        item: dict[str, object] = {
            "item_id": row["item_id"],
            "scheduled_date": row["scheduled_date"],
            "item_type": row["item_type"],
            "name": row["name"],
            "sport_type": row["sport_type"],
            "targets": targets,
        }
        if unsupported_targets:
            item["unparsed_targets"] = unsupported_targets
        activity_id = payload.get("_manual_activity_id")
        if activity_id is not None:
            item["activity_id"] = activity_id
        items.append(item)
    return {"plan_id": plan["plan_id"], "name": plan["name"], "items": items}


def link_training_activity(
    state_db: StateDB,
    plan_id: str,
    item_id: str,
    activity_id: str,
) -> dict[str, str]:
    plan = _require_training_plan(state_db, plan_id)
    resolved_activity_id = state_db.set_schedule_item_activity(
        plan["plan_id"], item_id, activity_id
    )
    if resolved_activity_id is None:
        raise ValueError("A local activity ID is required")
    return {
        "plan_id": plan["plan_id"],
        "item_id": item_id,
        "activity_id": resolved_activity_id,
    }


def unlink_training_activity(state_db: StateDB, plan_id: str, item_id: str) -> dict[str, str]:
    plan = _require_training_plan(state_db, plan_id)
    state_db.set_schedule_item_activity(plan["plan_id"], item_id, None)
    return {"plan_id": plan["plan_id"], "item_id": item_id}


def summarize_training_plan_progress(state_db: StateDB, plan_id: str) -> dict[str, object]:
    schedule = list_training_schedule(state_db, plan_id)
    items = []
    linked_count = 0
    missing_activity_count = 0
    for scheduled_item in schedule["items"]:
        if not isinstance(scheduled_item, dict):
            continue
        if scheduled_item.get("item_type") != "workout":
            continue
        activity_id = scheduled_item.get("activity_id")
        item = dict(scheduled_item)
        if activity_id is None:
            item["status"] = "unlinked"
            items.append(item)
            continue

        activity = state_db.get_local_activity(str(activity_id))
        if activity is None:
            item["status"] = "missing_activity"
            missing_activity_count += 1
            items.append(item)
            continue

        try:
            summary = json.loads(activity["summary_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Local activity has invalid summary JSON: {activity_id}") from exc
        if not isinstance(summary, dict):
            raise ValueError(f"Local activity has invalid summary JSON: {activity_id}")

        actual = _activity_metrics(summary, activity["sport_type"])
        targets = scheduled_item.get("targets")
        if not isinstance(targets, dict):
            targets = {}
        comparison = _compare_training_metrics(targets, actual)
        item["status"] = "linked"
        item["activity"] = {
            "activity_id": activity["fingerprint"],
            "name": activity["name"],
            "sport_type": activity["sport_type"],
            "start_time": activity["start_time"],
        }
        item["actual"] = actual
        item["comparison"] = comparison
        linked_count += 1
        items.append(item)

    return {
        "plan_id": schedule["plan_id"],
        "name": schedule["name"],
        "scheduled_item_count": len(items),
        "linked_activity_count": linked_count,
        "missing_activity_count": missing_activity_count,
        "unlinked_item_count": len(items) - linked_count - missing_activity_count,
        "items": items,
    }


def format_training_plan_progress(progress: dict[str, object]) -> str:
    items = progress.get("items")
    if not isinstance(items, list):
        items = []
    lines = [
        f"训练计划：{progress.get('name', '未知')}",
        f"计划训练：{progress.get('scheduled_item_count', 0)}，已关联：{progress.get('linked_activity_count', 0)}，"
        f"未关联：{progress.get('unlinked_item_count', 0)}，关联活动缺失：{progress.get('missing_activity_count', 0)}",
    ]
    labels = {
        "distance_m": "距离（米）",
        "duration_s": "时长（秒）",
        "training_stress_score": "训练压力分",
        "pace_seconds_per_km": "配速（秒/公里）",
        "pace_seconds_per_100m": "游泳配速（秒/100米）",
    }
    statuses = {
        "unlinked": "未关联活动",
        "missing_activity": "关联活动不存在",
        "linked": "已关联",
    }
    for item in items:
        if not isinstance(item, dict):
            continue
        lines.extend(
            (
                "",
                f"{item.get('scheduled_date', '')} {item.get('name', '')} "
                f"[{statuses.get(str(item.get('status')), '未知状态')}]",
                f"计划项 ID：{item.get('item_id', '')}",
            )
        )
        activity = item.get("activity")
        if isinstance(activity, dict):
            lines.append(
                f"活动：{activity.get('name', '')} ({activity.get('activity_id', '')})"
            )
        comparison = item.get("comparison")
        if isinstance(comparison, dict):
            for metric, values in comparison.items():
                if not isinstance(values, dict):
                    continue
                label = labels.get(metric, metric)
                lines.append(
                    f"{label}：目标 {_format_metric(values.get('target'))}，"
                    f"实际 {_format_metric(values.get('actual'))}，"
                    f"差值 {_format_metric(values.get('actual_minus_target'))}"
                )
    return "\n".join(lines)


def _require_training_plan(state_db: StateDB, plan_id: str) -> sqlite3.Row:
    plan = state_db.get_training_plan(plan_id)
    if plan is None:
        raise ValueError(f"Installed training plan was not found: {plan_id}")
    return plan


def _read_schedule_payload(payload_json: object, item_id: str) -> dict[str, object]:
    try:
        payload = json.loads(payload_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Scheduled training item has invalid JSON: {item_id}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Scheduled training item has invalid JSON: {item_id}")
    return payload


def _training_targets(
    payload: dict[str, object], sport_type: object
) -> tuple[dict[str, float], dict[str, object]]:
    targets: dict[str, float] = {}
    unsupported: dict[str, object] = {}
    raw_distance = payload.get("targetDistance")
    if raw_distance is not None:
        distance = _parse_target_distance(raw_distance)
        if distance is None:
            unsupported["targetDistance"] = raw_distance
        else:
            targets["distance_m"] = distance

    raw_duration = payload.get("targetDuration")
    if raw_duration is not None:
        duration = _parse_target_duration(raw_duration)
        if duration is None:
            unsupported["targetDuration"] = raw_duration
        else:
            targets["duration_s"] = duration

    raw_tss = payload.get("targetTSS")
    if raw_tss is not None:
        stress = _finite_nonnegative(raw_tss)
        if stress is None and isinstance(raw_tss, str):
            try:
                stress = _finite_nonnegative(float(raw_tss.strip()))
            except ValueError:
                stress = None
        if stress is None:
            unsupported["targetTSS"] = raw_tss
        else:
            targets["training_stress_score"] = stress

    raw_pace = payload.get("targetPace")
    if raw_pace is not None:
        pace_key, pace = _parse_target_pace(raw_pace, sport_type)
        if pace is None or pace_key is None:
            unsupported["targetPace"] = raw_pace
        else:
            targets[pace_key] = pace
    return targets, unsupported


def _parse_target_distance(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    match = _DISTANCE_TARGET_RE.search(value)
    if match is None:
        return None
    try:
        distance = float(match.group(1))
    except ValueError:
        return None
    if not math.isfinite(distance) or distance <= 0:
        return None
    return distance * 1000 if match.group(2).casefold() == "km" else distance


def _parse_target_duration(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    hours = _DURATION_HOURS_RE.search(value)
    minutes = _DURATION_MINUTES_RE.search(value)
    if hours is None and minutes is None:
        return None
    total_seconds = (int(hours.group(1)) * 3600 if hours else 0) + (
        int(minutes.group(1)) * 60 if minutes else 0
    )
    return float(total_seconds) if total_seconds > 0 else None


def _parse_target_pace(value: object, sport_type: object) -> tuple[str | None, float | None]:
    if not isinstance(value, str):
        return None, None
    sport = str(sport_type or "").casefold()
    pattern = _SWIM_PACE_TARGET_RE if "swim" in sport else _RUN_PACE_TARGET_RE
    match = pattern.search(value)
    if match is None:
        return None, None
    minutes, seconds = int(match.group(1)), int(match.group(2))
    if seconds >= 60:
        return None, None
    key = "pace_seconds_per_100m" if "swim" in sport else "pace_seconds_per_km"
    return key, float(minutes * 60 + seconds)


def _activity_metrics(summary: dict[str, object], sport_type: object) -> dict[str, float]:
    actual: dict[str, float] = {}
    distance = _finite_nonnegative(summary.get("distance_m"))
    if distance is not None:
        actual["distance_m"] = distance

    duration = _finite_nonnegative(summary.get("timer_time_s"))
    if duration is None or duration == 0:
        duration = _finite_nonnegative(summary.get("elapsed_time_s"))
    if duration is not None:
        actual["duration_s"] = duration

    stress = _finite_nonnegative(summary.get("training_stress_score"))
    if stress is not None:
        actual["training_stress_score"] = stress

    pace = _finite_nonnegative(summary.get("pace_seconds_per_km"))
    if pace is None and distance and duration:
        pace = duration * 1000 / distance
    if pace is not None:
        actual["pace_seconds_per_km"] = pace

    if "swim" in str(sport_type or "").casefold() and distance and duration:
        actual["pace_seconds_per_100m"] = duration * 100 / distance
    return actual


def _compare_training_metrics(
    targets: dict[str, object], actual: dict[str, float]
) -> dict[str, dict[str, float | None]]:
    comparison: dict[str, dict[str, float | None]] = {}
    for metric, target_value in targets.items():
        target = _finite_nonnegative(target_value)
        value = actual.get(metric)
        if target is None or value is None:
            continue
        difference = value - target
        comparison[metric] = {
            "target": target,
            "actual": value,
            "actual_minus_target": difference,
            "difference_percent": difference / target * 100 if target > 0 else None,
        }
    return comparison


def _finite_nonnegative(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _format_metric(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "未知"
    number = float(value)
    if not math.isfinite(number):
        return "未知"
    return f"{number:g}"


def list_workout_templates(
    sport_type: str | None = None,
    *,
    generated_dir: Path | None = None,
) -> list[WorkoutTemplate]:
    templates: list[WorkoutTemplate] = []
    directories = [WORKOUT_DIR]
    if generated_dir is not None and generated_dir.resolve() != WORKOUT_DIR.resolve():
        directories.append(generated_dir)
    for directory in directories:
        if not directory.is_dir():
            continue
        for metadata_path in sorted(directory.glob("*.fit.meta")):
            fit_path = metadata_path.with_suffix("")
            if not fit_path.is_file():
                continue
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"Invalid workout template metadata: {metadata_path.name}") from exc
            if not isinstance(metadata, dict):
                raise ValueError(f"Invalid workout template metadata: {metadata_path.name}")
            sport = str(metadata.get("sportType") or "unknown")
            if sport_type and sport.lower() != sport_type.lower():
                continue
            steps = metadata.get("steps") if isinstance(metadata.get("steps"), list) else []
            templates.append(
                WorkoutTemplate(
                    template_id=str(metadata.get("id") or fit_path.stem),
                    name=str(metadata.get("name") or fit_path.stem),
                    sport_type=sport,
                    estimated_duration_s=_optional_number(metadata.get("estimatedDuration")),
                    estimated_distance_m=_optional_number(metadata.get("estimatedDistance")),
                    steps=tuple(step for step in steps if isinstance(step, dict)),
                    fit_path=fit_path,
                    description=str(metadata.get("description") or ""),
                )
            )
    return sorted(templates, key=lambda item: (item.name.casefold(), item.template_id))


def get_workout_template(identifier: str, *, generated_dir: Path | None = None) -> WorkoutTemplate:
    for template in list_workout_templates(generated_dir=generated_dir):
        if identifier in {template.template_id, template.fit_path.stem}:
            return template
    raise ValueError(f"Workout template was not found: {identifier}")


def export_workout_template(
    identifier: str,
    output_path: Path,
    *,
    generated_dir: Path | None = None,
) -> Path:
    template = get_workout_template(identifier, generated_dir=generated_dir)
    output_path = output_path.expanduser().resolve()
    if output_path.suffix.lower() != ".fit":
        raise ValueError("Workout export path must end in .fit")
    if output_path.resolve() == template.fit_path.resolve():
        raise ValueError("Output path must be different from the bundled template")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template.fit_path, output_path)
    return output_path


def _load_plan(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid training plan template: {path.name}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid training plan template: {path.name}")
    payload["_file_id"] = path.stem
    return payload


def _validate_locale(locale: str) -> str:
    value = locale.strip().lower()
    if value not in SUPPORTED_LOCALES:
        choices = ", ".join(sorted(SUPPORTED_LOCALES))
        raise ValueError(f"Unsupported training plan locale: {locale}; choose from {choices}")
    return value


def _optional_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _ics_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n")
