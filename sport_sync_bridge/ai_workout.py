from __future__ import annotations

import json
import math
import os
import re
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .activity_analysis import validate_ai_language_code
from .formats import _fit_timestamp


WORKOUT_SPORTS = frozenset({"running", "cycling", "swimming"})
TARGET_MODES = frozenset({"pace", "heart_rate", "power", "mixed"})
_INTENSITIES = frozenset({"warmup", "cooldown", "interval", "recovery", "active", "rest", "strength"})
_MAX_RESPONSE_BYTES = 2_000_000
_MAX_NESTING = 5
_MAX_FIT_STEPS = 250
_DURATION_PATTERN = re.compile(
    r"^(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>seconds?|secs?|s|minutes?|mins?|min|hours?|hrs?|hr|h|"
    r"meters?|m|kilometers?|km|yards?|yd|miles?|mi)$",
    re.IGNORECASE,
)
_PACE_PATTERN = re.compile(r"^(?P<minutes>\d{1,2}):(?P<seconds>\d{2})\s*/\s*(?P<unit>km|100m|mi)$", re.I)


def build_ai_single_workout_prompts(
    *,
    sport: str,
    task: str,
    target_mode: str = "mixed",
    target_duration: str | None = None,
    target_distance: str | None = None,
    target_pace: str | None = None,
    target_heart_rate: str | None = None,
    target_tss: float | None = None,
    athlete_context: str | None = None,
    feedback: list[str] | None = None,
    language: str = "zh-CN",
    today: date | None = None,
) -> tuple[str, str]:
    sport = _validate_sport(sport)
    target_mode = target_mode.strip().lower()
    _validate_target_mode(sport, target_mode)
    language = validate_ai_language_code(language)
    task = _required_text(task, "task", 2000)
    optional = {
        "target duration": _optional_text(target_duration, "target duration", 120),
        "target distance": _optional_text(target_distance, "target distance", 120),
        "target pace": _optional_text(target_pace, "target pace", 120),
        "target heart rate": _optional_text(target_heart_rate, "target heart rate", 120),
        "athlete context": _optional_text(athlete_context, "athlete context", 4000),
    }
    if target_tss is not None and (
        isinstance(target_tss, bool)
        or not isinstance(target_tss, (int, float))
        or not math.isfinite(target_tss)
        or target_tss <= 0
    ):
        raise ValueError("Target TSS must be a positive finite number")
    feedback_items = [
        _required_text(value, "feedback", 1000)
        for value in (feedback or [])
    ]
    if len(feedback_items) > 20:
        raise ValueError("At most 20 feedback items can be supplied")

    system_prompt = _build_system_prompt(sport, target_mode)
    lines = [
        f"Today is {(today or date.today()).isoformat()}. Respond in language code {language}.",
        "Generate one structured workout for this training session:",
        f"Task: {task}",
        f"Sport: {sport}",
    ]
    for label, value in optional.items():
        if value:
            lines.append(f"{label.title()}: {value}")
    if target_tss is not None:
        lines.append(f"Target TSS: {target_tss:g}")
    if feedback_items:
        lines.extend(("User feedback to incorporate:", *(f"- {item}" for item in feedback_items)))
    if optional["athlete context"]:
        lines.extend(("Athlete profile and recent training context:", optional["athlete context"]))
    lines.extend(
        (
            "Return exactly one workout as raw JSON with this shape:",
            '{"workouts":{"wo_1":{"workoutId":"short_descriptive_id","name":"string",'
            f'"sportType":"{sport}","steps":[...]}}}}}}',
            "A plain step has intensity, duration, optional target, and optional notes.",
            'A repeat group has an integer repeat count and a non-empty steps array.',
            "Use only warmup, cooldown, interval, recovery, active, rest, or strength for intensity.",
            'Duration uses a positive number and unit such as "15min", "30sec", "1000m", or "open".',
            "Use one precise target value, never a numeric range. Include a warmup and cooldown when suitable.",
            "Do not add prose or markdown fences.",
        )
    )
    return system_prompt, "\n".join(lines)


def normalize_ai_workout(response: str, *, sport: str) -> dict[str, Any]:
    if not isinstance(response, str):
        raise ValueError("AI workout response must be text")
    if len(response.encode("utf-8")) > _MAX_RESPONSE_BYTES:
        raise ValueError("AI workout response exceeds the 2 MB limit")
    sport = _validate_sport(sport)
    text = response.strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        repaired = _repair_ai_json_response(text)
        if repaired == text:
            raise ValueError("AI workout response is not valid JSON") from exc
        try:
            payload = json.loads(repaired)
        except json.JSONDecodeError as repair_exc:
            raise ValueError("AI workout response is not valid JSON") from repair_exc
    if not isinstance(payload, dict):
        raise ValueError("AI workout response must be a JSON object")

    workouts = payload.get("workouts")
    if workouts is None and isinstance(payload.get("steps"), list):
        workouts = {"wo_1": payload}
    elif workouts is None:
        workouts = payload
    if isinstance(workouts, list):
        items = [(str(index + 1), item) for index, item in enumerate(workouts)]
    elif isinstance(workouts, dict) and isinstance(workouts.get("steps"), list):
        items = [(str(workouts.get("workoutId") or "wo_1"), workouts)]
    elif isinstance(workouts, dict):
        items = list(workouts.items())
    else:
        raise ValueError("AI workout response is missing its workouts object")
    if len(items) != 1:
        raise ValueError("AI single-workout generation must return exactly one workout")

    workout_key, raw_workout = items[0]
    if not isinstance(raw_workout, dict):
        raise ValueError("Generated workout must be an object")
    name = _required_text(raw_workout.get("name"), "workout name", 160)
    returned_sport = raw_workout.get("sportType", sport)
    if not isinstance(returned_sport, str) or returned_sport.strip().lower() != sport:
        raise ValueError(f"Generated workout sportType must be {sport}")
    raw_steps = raw_workout.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("Generated workout must contain at least one step")
    steps = [_normalize_step(item, f"steps[{index}]", 0) for index, item in enumerate(raw_steps)]
    fit_steps = _compile_fit_steps(steps)
    if not fit_steps:
        raise ValueError("Generated workout has no usable steps")
    if len(fit_steps) > _MAX_FIT_STEPS:
        raise ValueError(f"Generated workout contains more than {_MAX_FIT_STEPS} FIT steps")

    workout_id = raw_workout.get("workoutId") or workout_key
    workout_id = _slug(_required_text(workout_id, "workoutId", 100)) or "ai_workout"
    description = _optional_text(raw_workout.get("description"), "workout description", 2000)
    duration, distance = _estimate_workout(steps)
    return {
        "workoutId": workout_id,
        "name": name,
        "sportType": sport,
        "description": description,
        "estimatedDuration": duration,
        "estimatedDistance": distance,
        "steps": steps,
    }


def _repair_ai_json_response(text: str) -> str:
    repaired = text.strip()
    if repaired.startswith("```"):
        repaired = re.sub(r"^```\w*\s*", "", repaired, count=1)
        if repaired.endswith("```"):
            repaired = repaired[:-3].rstrip()
    return re.sub(r",(\s*[}\]])", r"\1", repaired)


def write_ai_workout_fit(workout: dict[str, Any], output_path: Path) -> Path:
    output_path = output_path.expanduser().resolve()
    if output_path.suffix.lower() != ".fit":
        raise ValueError("Generated workout output path must end in .fit")
    metadata_path = Path(f"{output_path}.meta")
    if output_path.exists() or metadata_path.exists():
        raise FileExistsError(f"Generated workout output already exists: {output_path}")
    sport = _validate_sport(str(workout.get("sportType", "")))
    fit_steps = _compile_fit_steps(workout.get("steps", []))
    if not fit_steps or len(fit_steps) > _MAX_FIT_STEPS:
        raise ValueError(f"Workout must expand to between 1 and {_MAX_FIT_STEPS} FIT steps")

    try:
        from fit_tool.fit_file import FitFile
        from fit_tool.fit_file_builder import FitFileBuilder
        from fit_tool.profile.messages.file_id_message import FileIdMessage
        from fit_tool.profile.messages.workout_message import WorkoutMessage
        from fit_tool.profile.messages.workout_step_message import WorkoutStepMessage
        from fit_tool.profile.profile_type import (
            FileType,
            Intensity,
            Manufacturer,
            Sport,
            SubSport,
            WorkoutStepDuration,
            WorkoutStepTarget,
        )
    except ImportError as exc:
        raise RuntimeError("fit-tool is required for FIT workout generation") from exc

    sport_enum = {"running": Sport.RUNNING, "cycling": Sport.CYCLING, "swimming": Sport.SWIMMING}[sport]
    builder = FitFileBuilder(auto_define=True)
    file_id = FileIdMessage()
    file_id.type = FileType.WORKOUT
    file_id.manufacturer = Manufacturer.GARMIN
    file_id.product = 65534
    file_id.time_created = _fit_timestamp(datetime.now(timezone.utc))
    builder.add(file_id)

    workout_message = WorkoutMessage()
    workout_message.sport = sport_enum
    workout_message.sub_sport = SubSport.GENERIC
    workout_message.num_valid_steps = len(fit_steps)
    workout_message.workout_name = _fit_text(str(workout.get("name") or "AI workout"), 15)
    builder.add(workout_message)

    losses: list[str] = []
    for index, definition in enumerate(fit_steps):
        step = WorkoutStepMessage()
        step.message_index = index
        if "_repeat_count" in definition:
            step.workout_step_name = _fit_text(f"repeat {definition['_repeat_count']}", 15)
            step.intensity = Intensity.OTHER
            step.duration_type = WorkoutStepDuration.REPEAT_UNTIL_STEPS_CMPLT
            step.duration_step = definition["_repeat_start"]
            step.target_type = WorkoutStepTarget.OPEN
            step.target_repeat_steps = definition["_repeat_count"]
            step.notes = f"Repeat previous steps {definition['_repeat_count']} times"
        else:
            step.workout_step_name = _fit_text(f"{definition['intensity']} {index + 1}", 15)
            step.intensity = _fit_intensity(definition["intensity"], Intensity)
            step.notes = _fit_text(_step_notes(definition), 250)
            _write_duration(step, definition, WorkoutStepDuration)
            _write_target(step, definition, WorkoutStepTarget, losses)
        builder.add(step)

    fit_bytes = builder.build_bytes()
    decoded = FitFile.from_bytes(fit_bytes)
    _validate_generated_fit(decoded, len(fit_steps))
    if any(step.get("intensity") == "strength" for step in fit_steps if "_repeat_count" not in step):
        losses.append("Strength sets and repetitions remain in notes and sidecar metadata; FIT does not encode all of them.")
    losses = list(dict.fromkeys(losses))
    metadata = _workout_metadata(workout, sport, fit_steps, losses, output_path.stem)
    metadata_bytes = (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    created: list[Path] = []
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        _write_exclusive(output_path, fit_bytes)
        created.append(output_path)
        _write_exclusive(metadata_path, metadata_bytes)
        created.append(metadata_path)
    except Exception:
        for created_path in reversed(created):
            created_path.unlink(missing_ok=True)
        raise
    return output_path


def make_workout_output_path(workout: dict[str, Any], directory: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = _slug(str(workout.get("workoutId") or workout.get("name") or "ai_workout")) or "ai_workout"
    return directory.expanduser().resolve() / f"{slug}-{stamp}.fit"


def _build_system_prompt(sport: str, target_mode: str) -> str:
    sport_guidance = {
        "running": "Running steps may use time or distance. Pace is expressed as M:SS/km.",
        "cycling": "Cycling steps are mainly time based. Power can use a single % FTP or watt target.",
        "swimming": "Swimming uses distance-based sets and pace per 100m; heart rate is unreliable in water.",
    }[sport]
    mode_guidance = {
        "pace": "Prefer pace targets where supported.",
        "heart_rate": "Prefer heart-rate zones or a single bpm target.",
        "power": "Prefer power zones or a single % FTP or watt target.",
        "mixed": "Choose the supported target mode that best fits each step.",
    }[target_mode]
    return "\n".join(
        (
            "You are an endurance coach who turns a training request into a structured workout.",
            "Return raw JSON only. Do not use markdown fences or add prose.",
            "Return exactly one workout using the schema supplied by the user.",
            "A repeat group must have a positive integer repeat count and non-empty child steps.",
            "Use a single exact target value; target ranges are not supported.",
            "Include warmup and cooldown steps when appropriate, and do not invent athlete metrics.",
            sport_guidance,
            mode_guidance,
        )
    )


def _normalize_step(value: object, path: str, depth: int) -> dict[str, Any]:
    if depth > _MAX_NESTING:
        raise ValueError(f"{path} exceeds the maximum repeat nesting depth")
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    if "repeat" in value or ("steps" in value and "intensity" not in value):
        repeat = value.get("repeat")
        children = value.get("steps")
        if not isinstance(repeat, int) or isinstance(repeat, bool) or not 1 <= repeat <= 100:
            raise ValueError(f"{path}.repeat must be an integer from 1 to 100")
        if not isinstance(children, list) or not children:
            raise ValueError(f"{path}.steps must contain at least one repeated step")
        return {
            "repeat": repeat,
            "steps": [_normalize_step(child, f"{path}.steps[{index}]", depth + 1) for index, child in enumerate(children)],
        }

    intensity = value.get("intensity")
    if not isinstance(intensity, str) or intensity.strip().lower() not in _INTENSITIES:
        choices = ", ".join(sorted(_INTENSITIES))
        raise ValueError(f"{path}.intensity must be one of: {choices}")
    duration = value.get("duration", "open")
    if not isinstance(duration, (str, int, float)) or isinstance(duration, bool):
        raise ValueError(f"{path}.duration must be a duration string or number")
    _parse_duration(duration)
    target = _optional_text(value.get("target"), f"{path}.target", 120)
    notes = _optional_text(value.get("notes") or value.get("description"), f"{path}.notes", 500)
    normalized: dict[str, Any] = {
        "intensity": intensity.strip().lower(),
        "duration": _normalize_duration_text(duration),
        "target": target,
        "notes": notes,
    }
    for key, maximum in (("exerciseName", 100), ("exerciseCategory", 80), ("targetWeight", 40)):
        if key in value:
            item = _optional_text(value[key], f"{path}.{key}", maximum)
            if item:
                normalized[key] = item
    for key in ("targetReps", "targetRepsHigh", "numberOfSets", "restSeconds"):
        if key in value:
            item = value[key]
            if not isinstance(item, int) or isinstance(item, bool) or not 0 <= item <= 10000:
                raise ValueError(f"{path}.{key} must be a non-negative integer no greater than 10000")
            normalized[key] = item
    if (
        normalized.get("targetReps") is not None
        and normalized.get("targetRepsHigh") is not None
        and normalized["targetRepsHigh"] < normalized["targetReps"]
    ):
        raise ValueError(f"{path}.targetRepsHigh cannot be lower than targetReps")
    return normalized


def _normalize_duration_text(duration: str | int | float) -> str:
    if isinstance(duration, str):
        value = duration.strip()
        return "open" if value.lower() == "open" else value
    return f"{duration:g}sec"


def _parse_duration(duration: str | int | float) -> tuple[str, float | None]:
    if isinstance(duration, (int, float)) and not isinstance(duration, bool):
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("Workout duration must be positive and finite")
        return "time", float(duration)
    if not isinstance(duration, str):
        raise ValueError("Workout duration must be a string or number")
    text = duration.strip().lower()
    if text == "open":
        return "open", None
    clock = re.fullmatch(r"(?P<minutes>\d+):(?P<seconds>[0-5]\d)", text)
    if clock:
        return "time", int(clock.group("minutes")) * 60 + int(clock.group("seconds"))
    match = _DURATION_PATTERN.fullmatch(text)
    if match is None:
        raise ValueError(f"Unsupported workout duration: {duration}")
    amount = float(match.group("amount"))
    if amount <= 0 or not math.isfinite(amount):
        raise ValueError("Workout duration must be positive and finite")
    unit = match.group("unit").lower()
    if unit in {"s", "sec", "secs", "second", "seconds"}:
        return "time", amount
    if unit in {"min", "mins", "minute", "minutes"}:
        return "time", amount * 60
    if unit in {"h", "hr", "hrs", "hour", "hours"}:
        return "time", amount * 3600
    if unit in {"m", "meter", "meters"}:
        return "distance", amount
    if unit in {"km", "kilometer", "kilometers"}:
        return "distance", amount * 1000
    if unit in {"yd", "yard", "yards"}:
        return "distance", amount * 0.9144
    if unit in {"mi", "mile", "miles"}:
        return "distance", amount * 1609.344
    raise ValueError(f"Unsupported workout duration unit: {unit}")


def _compile_fit_steps(steps: object) -> list[dict[str, Any]]:
    if not isinstance(steps, list):
        return []
    compiled: list[dict[str, Any]] = []

    def append_nodes(nodes: list[object]) -> None:
        for step in nodes:
            if not isinstance(step, dict):
                raise ValueError("Workout contains an invalid step")
            if "repeat" in step:
                start_index = len(compiled)
                append_nodes(step["steps"])
                if step["repeat"] > 1:
                    compiled.append(
                        {"_repeat_start": start_index, "_repeat_count": step["repeat"]}
                    )
            else:
                compiled.append(step)
            if len(compiled) > _MAX_FIT_STEPS:
                raise ValueError(f"Generated workout contains more than {_MAX_FIT_STEPS} FIT steps")

    append_nodes(steps)
    return compiled


def _estimate_workout(steps: list[dict[str, Any]]) -> tuple[float | None, float | None]:
    duration = 0.0
    distance = 0.0
    has_duration = False
    has_distance = False
    duration_known = True
    for step in steps:
        if "repeat" in step:
            child_duration, child_distance = _estimate_workout(step["steps"])
            if child_duration is not None:
                duration += child_duration * step["repeat"]
                has_duration = True
            else:
                duration_known = False
            if child_distance is not None:
                distance += child_distance * step["repeat"]
                has_distance = True
            continue
        kind, amount = _parse_duration(step["duration"])
        if kind == "time" and amount is not None:
            duration += amount
            has_duration = True
        elif kind == "distance" and amount is not None:
            distance += amount
            has_distance = True
            speed = _pace_speed(str(step.get("target") or ""))
            if speed is None:
                duration_known = False
            else:
                duration += amount / speed
                has_duration = True
        else:
            duration_known = False
    return (duration if has_duration and duration_known else None, distance if has_distance else None)


def _write_duration(step: Any, definition: dict[str, Any], duration_enum: Any) -> None:
    kind, amount = _parse_duration(definition["duration"])
    if kind == "time":
        step.duration_type = duration_enum.TIME
        step.duration_value = amount
    elif kind == "distance":
        step.duration_type = duration_enum.DISTANCE
        step.duration_value = amount
    else:
        step.duration_type = duration_enum.OPEN


def _write_target(step: Any, definition: dict[str, Any], target_enum: Any, losses: list[str]) -> None:
    target = definition.get("target")
    if not target:
        step.target_type = target_enum.OPEN
        return
    if re.search(r"\d\s*[-–]\s*\d", target) and re.search(r"%|bpm|/km|/100m|rpe", target, re.I):
        raise ValueError(f"Workout target ranges are not supported: {target}")
    normalized = target.strip().lower()
    zone = re.fullmatch(r"zone\s*(\d+)\s*(?:hr|heart\s*rate)", normalized)
    if zone:
        zone_number = int(zone.group(1))
        if not 1 <= zone_number <= 10:
            raise ValueError("Heart-rate zone must be from 1 to 10")
        step.target_type = target_enum.HEART_RATE
        step.target_hr_zone = zone_number
        return
    zone = re.fullmatch(r"(?:power\s*)?zone\s*(\d+)(?:\s*power)?", normalized)
    if zone:
        zone_number = int(zone.group(1))
        if not 1 <= zone_number <= 10:
            raise ValueError("Power zone must be from 1 to 10")
        step.target_type = target_enum.POWER
        step.target_power_zone = zone_number
        return
    heart_rate = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:bpm|beats/min)", normalized)
    if heart_rate:
        bpm = int(round(float(heart_rate.group(1))))
        if not 1 <= bpm <= 255:
            raise ValueError("Heart-rate target must be from 1 to 255 bpm")
        step.target_type = target_enum.HEART_RATE
        step.target_value = 0
        step.custom_target_heart_rate_low = bpm
        step.custom_target_heart_rate_high = bpm
        return
    power = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(%\s*ftp|w|watts?)", normalized)
    if power:
        watts_or_pct = int(round(float(power.group(1))))
        step.target_type = target_enum.POWER
        is_percent = "%" in power.group(2)
        if is_percent:
            if not 0 <= watts_or_pct <= 1000:
                raise ValueError("Power target percentage must be from 0 to 1000% FTP")
            target_power = watts_or_pct
        else:
            if not 1 <= watts_or_pct <= 64535:
                raise ValueError("Power target must be from 1 to 64535 watts")
            target_power = watts_or_pct
        step.target_value = 0
        step.custom_target_power_low = target_power
        step.custom_target_power_high = target_power
        return
    cadence = re.fullmatch(r"(\d+(?:\.\d+)?)\s*rpm", normalized)
    if cadence:
        rpm = int(round(float(cadence.group(1))))
        if not 1 <= rpm <= 255:
            raise ValueError("Cadence target must be from 1 to 255 rpm")
        step.target_type = target_enum.CADENCE
        step.target_value = 0
        step.custom_target_cadence_low = rpm
        step.custom_target_cadence_high = rpm
        return
    pace = _PACE_PATTERN.fullmatch(normalized)
    if pace:
        speed_mps = _pace_speed(target)
        if speed_mps is None:
            raise ValueError(f"Invalid pace target: {target}")
        step.target_type = target_enum.SPEED
        step.target_value = 0
        step.custom_target_speed_low = speed_mps
        step.custom_target_speed_high = speed_mps
        return
    step.target_type = target_enum.OPEN
    losses.append(f"Target {target!r} is retained in the workout notes because FIT has no direct field for it.")


def _step_notes(definition: dict[str, Any]) -> str:
    parts = [str(definition.get("notes") or "").strip()]
    target = definition.get("target")
    if target and _target_is_unmapped(str(target)):
        parts.append(f"Target: {target}")
    if definition.get("intensity") == "strength":
        pieces = []
        if definition.get("exerciseName"):
            pieces.append(str(definition["exerciseName"]))
        if definition.get("exerciseCategory"):
            pieces.append(str(definition["exerciseCategory"]))
        sets = definition.get("numberOfSets")
        reps_low = definition.get("targetReps")
        reps_high = definition.get("targetRepsHigh", reps_low)
        if sets is not None or reps_low is not None:
            reps = str(reps_low) if reps_low == reps_high or reps_high is None else f"{reps_low}-{reps_high}"
            pieces.append(f"{sets or 1} sets x {reps or '?'} reps")
        if definition.get("targetWeight"):
            pieces.append(f"weight {definition['targetWeight']}")
        if definition.get("restSeconds") is not None:
            pieces.append(f"rest {definition['restSeconds']} s")
        if pieces:
            parts.append("; ".join(pieces))
    return " | ".join(part for part in parts if part)


def _target_is_unmapped(target: str) -> bool:
    normalized = target.strip().lower()
    return not any(
        pattern.fullmatch(normalized)
        for pattern in (
            re.compile(r"zone\s*\d+\s*(?:hr|heart\s*rate)"),
            re.compile(r"(?:power\s*)?zone\s*\d+(?:\s*power)?"),
            re.compile(r"\d+(?:\.\d+)?\s*(?:bpm|beats/min)"),
            re.compile(r"\d+(?:\.\d+)?\s*(?:%\s*ftp|w|watts?)"),
            re.compile(r"\d+(?:\.\d+)?\s*rpm"),
        )
    ) and _PACE_PATTERN.fullmatch(normalized) is None


def _pace_speed(target: str) -> float | None:
    match = _PACE_PATTERN.fullmatch(target.strip().lower())
    if match is None:
        return None
    seconds = int(match.group("minutes")) * 60 + int(match.group("seconds"))
    if seconds <= 0 or int(match.group("seconds")) >= 60:
        return None
    distance = {"km": 1000.0, "100m": 100.0, "mi": 1609.344}[match.group("unit").lower()]
    return distance / seconds


def _fit_intensity(value: str, intensity_enum: Any) -> Any:
    return {
        "active": intensity_enum.ACTIVE,
        "warmup": intensity_enum.WARMUP,
        "cooldown": intensity_enum.COOLDOWN,
        "recovery": intensity_enum.RECOVERY,
        "interval": intensity_enum.INTERVAL,
        "rest": intensity_enum.REST,
        "strength": intensity_enum.OTHER,
    }[value]


def _validate_generated_fit(fit_file: Any, expected_steps: int) -> None:
    messages = [record.message for record in fit_file.records if not record.is_definition]
    workouts = [message for message in messages if message.name == "workout"]
    steps = [message for message in messages if message.name == "workout_step"]
    if len(workouts) != 1 or len(steps) != expected_steps:
        raise ValueError("Generated FIT workout failed its structural validation")
    if workouts[0].num_valid_steps != expected_steps:
        raise ValueError("Generated FIT workout step count does not match its messages")
    indices = sorted(step.message_index for step in steps)
    if indices != list(range(expected_steps)):
        raise ValueError("Generated FIT workout has non-sequential step indexes")
    for step in steps:
        if step.duration_type == 6 and (
            step.duration_step is None
            or step.duration_step >= step.message_index
            or not step.target_repeat_steps
        ):
            raise ValueError("Generated FIT workout contains an invalid repeat control step")


def _workout_metadata(
    workout: dict[str, Any],
    sport: str,
    expanded_steps: list[dict[str, Any]],
    losses: list[str],
    identifier: str,
) -> dict[str, Any]:
    sport_id = {"running": 1, "cycling": 2, "swimming": 5}[sport]
    created_at = datetime.now(timezone.utc).isoformat()
    return {
        "type": "workout",
        "id": identifier,
        "workoutId": workout.get("workoutId"),
        "sourceId": "ai",
        "name": workout.get("name") or "AI workout",
        "createdAt": created_at,
        "updatedAt": created_at,
        "metadata": {"conversionLosses": losses},
        "sportType": sport.upper(),
        "sport": sport_id,
        "subSport": 0,
        "description": workout.get("description"),
        "estimatedDuration": workout.get("estimatedDuration"),
        "estimatedDistance": workout.get("estimatedDistance"),
        "steps": workout.get("steps") or expanded_steps,
    }


def _write_exclusive(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _validate_sport(sport: str) -> str:
    value = sport.strip().lower()
    if value not in WORKOUT_SPORTS:
        choices = ", ".join(sorted(WORKOUT_SPORTS))
        raise ValueError(f"Unsupported workout sport: {sport}; choose from {choices}")
    return value


def _validate_target_mode(sport: str, target_mode: str) -> None:
    if target_mode not in TARGET_MODES:
        raise ValueError(f"Unsupported workout target mode: {target_mode}")
    supported = {
        "running": {"pace", "heart_rate", "mixed"},
        "cycling": {"power", "heart_rate", "mixed"},
        "swimming": {"pace", "mixed"},
    }
    if target_mode not in supported[sport]:
        raise ValueError(f"Target mode {target_mode} is not supported for {sport}")


def _required_text(value: object, label: str, max_length: int) -> str:
    result = _optional_text(value, label, max_length)
    if not result:
        raise ValueError(f"{label} is required")
    return result


def _optional_text(value: object, label: str, max_length: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    result = value.strip()
    if len(result) > max_length:
        raise ValueError(f"{label} exceeds the {max_length} character limit")
    return result or None


def _fit_text(value: str, max_bytes: int) -> str:
    result = value
    while len(result.encode("utf-8")) > max_bytes:
        result = result[:-1]
    return result


def _slug(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", ascii_value).strip("_-").lower()[:80]
