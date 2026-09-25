from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from .state import StateDB


TRAINING_PLAN_DIR = Path(__file__).parent / "data" / "training_plans"
WORKOUT_DIR = Path(__file__).parent / "data" / "workouts"
SUPPORTED_LOCALES = {"en", "es", "fr", "it", "pt", "zh"}
_DAY_OFFSETS = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}


@dataclass(frozen=True, slots=True)
class WorkoutTemplate:
    template_id: str
    name: str
    sport_type: str
    estimated_duration_s: float | None
    estimated_distance_m: float | None
    steps: tuple[dict[str, object], ...]
    fit_path: Path


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


def list_workout_templates(sport_type: str | None = None) -> list[WorkoutTemplate]:
    templates: list[WorkoutTemplate] = []
    if not WORKOUT_DIR.is_dir():
        return templates
    for metadata_path in sorted(WORKOUT_DIR.glob("*.fit.meta")):
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
            )
        )
    return templates


def get_workout_template(identifier: str) -> WorkoutTemplate:
    for template in list_workout_templates():
        if identifier in {template.template_id, template.fit_path.stem}:
            return template
    raise ValueError(f"Workout template was not found: {identifier}")


def export_workout_template(identifier: str, output_path: Path) -> Path:
    template = get_workout_template(identifier)
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
