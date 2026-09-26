from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from .ai_workout import _parse_duration
from .training import WorkoutTemplate

if TYPE_CHECKING:
    from .mywhoosh_source import MyWhooshSource


_MAX_MYWHOOSH_STEPS = 250
_MAX_WORKOUT_NESTING = 5
_FTP_TARGET = re.compile(r"^(?P<percent>\d+(?:\.\d+)?)\s*%\s*ftp$", re.IGNORECASE)
_STEP_TYPES = {
    "warmup": "E_WarmUp",
    "cooldown": "E_CoolDown",
    "rest": "E_Rest",
    "recovery": "E_Rest",
    "active": "E_Normal",
    "interval": "E_Normal",
    "strength": "E_Normal",
}


def stable_mywhoosh_workout_id(template_id: str) -> int:
    value = template_id.strip()
    if not value:
        raise ValueError("Workout template ID must not be empty")
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return 1_000_000_000_000 + int.from_bytes(digest[:8], "big") % 500_000_000_000


def build_mywhoosh_workout_data(
    template: WorkoutTemplate,
    workout_id: int | None = None,
) -> tuple[dict[str, Any], list[str]]:
    if template.sport_type.casefold() != "cycling":
        raise ValueError("MyWhoosh only accepts cycling workout templates")
    if not template.name.strip():
        raise ValueError("Workout name must not be empty")
    remote_id = workout_id if workout_id is not None else stable_mywhoosh_workout_id(template.template_id)
    if isinstance(remote_id, bool) or not isinstance(remote_id, int) or remote_id <= 0:
        raise ValueError("MyWhoosh workout ID must be a positive integer")

    rows: list[dict[str, Any]] = []
    losses: list[str] = []
    next_interval_id = 1
    has_repeat = False

    def append_steps(nodes: object, interval_id: int = 0, depth: int = 0) -> None:
        nonlocal next_interval_id, has_repeat
        if depth > _MAX_WORKOUT_NESTING:
            raise ValueError("Workout repeat nesting is too deep for MyWhoosh")
        if not isinstance(nodes, (list, tuple)):
            raise ValueError("Workout steps must be a list")
        for node in nodes:
            if not isinstance(node, Mapping):
                raise ValueError("Workout step must be an object")
            if "repeat" in node:
                repeat = node.get("repeat")
                children = node.get("steps")
                if isinstance(repeat, bool) or not isinstance(repeat, int) or not 1 <= repeat <= 100:
                    raise ValueError("Workout repeat count must be an integer from 1 to 100")
                if not isinstance(children, (list, tuple)) or not children:
                    raise ValueError("Repeated workout steps must not be empty")
                has_repeat = True
                for _ in range(repeat):
                    current_interval = next_interval_id
                    next_interval_id += 1
                    append_steps(children, current_interval, depth + 1)
                continue

            if len(rows) >= _MAX_MYWHOOSH_STEPS:
                raise ValueError(
                    f"MyWhoosh workout exceeds {_MAX_MYWHOOSH_STEPS} expanded steps"
                )
            intensity = node.get("intensity")
            if not isinstance(intensity, str) or intensity.casefold() not in _STEP_TYPES:
                raise ValueError("Workout step has an unsupported intensity")
            duration_kind, duration_value = _parse_duration(node.get("duration", "open"))
            if duration_kind != "time" or duration_value is None:
                raise ValueError("MyWhoosh workout steps must use a finite time duration")

            power = _ftp_power_ratio(node.get("target"), len(rows) + 1, losses)
            row = {
                "Id": len(rows),
                "IntervalId": interval_id,
                "StepType": _STEP_TYPES[intensity.casefold()],
                "WorkoutMessage": [],
                "Rpm": 0,
                "Power": power,
                "Pace": 2,
                "StartPower": power,
                "EndPower": power,
                "Time": round(duration_value, 3),
                "IsManualGrade": False,
                "ManualGradeValue": 0,
                "ShowAveragePower": False,
                "FlatRoad": 0,
            }
            rows.append(row)
            if node.get("notes"):
                losses.append(f"Step {len(rows)} notes were omitted from the MyWhoosh workout.")
            if intensity.casefold() == "strength" or any(
                node.get(field) is not None
                for field in ("exerciseName", "exerciseCategory", "targetReps", "targetRepsHigh", "targetWeight")
            ):
                losses.append(f"Strength details from step {len(rows)} were omitted.")

    append_steps(template.steps)
    if not rows:
        raise ValueError("Workout template has no usable steps")
    if has_repeat:
        losses.append("Repeat groups were expanded into individual MyWhoosh steps.")
    losses.append("IF, TSS, and KJ summaries were set to zero because the source has no matching calculations.")

    duration = round(sum(float(row["Time"]) for row in rows), 3)
    result = {
        "Id": remote_id,
        "Name": template.name.strip(),
        "Description": template.description,
        "CustomTagDescription": "",
        "CategoryId": 10,
        "SubcategoryId": 2,
        "Type": "E_Normal",
        "DisplayType": "E_byWatts",
        "Mode": "E_Ride",
        "ERGMode": "E_ON",
        "FTPMode": "E_FTP",
        "FTPMultiplier": 2,
        "IsRecovery": False,
        "IsIntervals": any(row["IntervalId"] != 0 for row in rows),
        "IsTT": False,
        "IsTSS": False,
        "IsIF": False,
        "StepCount": len(rows),
        "IsFavorite": False,
        "CompletedCount": 0,
        "WorkoutStepsArray": rows,
        "AuthorName": "",
        "WorkoutstepsTMap": [],
        "IF": 0,
        "TSS": 0,
        "KJ": 0,
        "StressPoint": 0,
        "Time": duration,
        "WorkoutSteps": {},
        "WokoutAssociationId": 0,
    }
    return result, list(dict.fromkeys(losses))


def upload_mywhoosh_workout(
    source: MyWhooshSource,
    template: WorkoutTemplate,
) -> dict[str, Any]:
    workout, losses = build_mywhoosh_workout_data(template)
    workout_id = str(workout["Id"])
    for existing in source.list_workouts():
        existing_id = _remote_workout_id(existing)
        if existing_id != workout_id:
            continue
        remote_name = existing.get("Name", existing.get("name"))
        if remote_name is not None and str(remote_name).strip() != workout["Name"]:
            raise RuntimeError(
                f"MyWhoosh workout ID collision for local template {template.template_id}"
            )
        return {
            "status": "already_exists",
            "workout_id": workout_id,
            "name": workout["Name"],
            "losses": losses,
        }

    source.upload_workout(workout)
    return {
        "status": "uploaded",
        "workout_id": workout_id,
        "name": workout["Name"],
        "losses": losses,
    }


def remote_workout_summary(workout: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "workout_id": _remote_workout_id(workout),
        "name": workout.get("Name", workout.get("name")),
        "description": workout.get("Description", workout.get("description")),
        "duration_s": workout.get("Time", workout.get("time")),
        "tss": workout.get("TSS", workout.get("tss")),
    }


def _ftp_power_ratio(value: object, step_number: int, losses: list[str]) -> float | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError(f"Workout step {step_number} target must be text")
    match = _FTP_TARGET.fullmatch(value.strip())
    if match is None:
        losses.append(
            f"Step {step_number} target {value!r} was omitted; MyWhoosh accepts % FTP targets in this conversion."
        )
        return None
    percent = float(match.group("percent"))
    if not 0 <= percent <= 1000:
        raise ValueError(f"Workout step {step_number} % FTP target must be from 0 to 1000")
    return percent / 100


def _remote_workout_id(workout: Mapping[str, Any]) -> str | None:
    for key in ("WorkoutId", "workoutId", "Id", "id"):
        value = workout.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return None
