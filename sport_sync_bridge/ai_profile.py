from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from typing import Protocol


AI_ATHLETE_PROFILE_FIELDS = (
    "gender",
    "age",
    "weight_kg",
    "height_cm",
    "resting_hr_bpm",
    "max_hr_bpm",
    "lactate_threshold_hr_bpm",
    "vo2_max_run",
    "vo2_max_bike",
    "ftp_w",
    "threshold_pace_s_per_km",
)
AI_ATHLETE_PROFILE_INTEGER_FIELDS = (
    "age",
    "resting_hr_bpm",
    "max_hr_bpm",
    "lactate_threshold_hr_bpm",
)
AI_ATHLETE_PROFILE_GENDERS = ("female", "male")

_PROFILE_KEY = "ai_athlete_profile"
_PROFILE_RANGES = {
    "age": (1, 120),
    "weight_kg": (0.1, 500),
    "height_cm": (30, 280),
    "resting_hr_bpm": (20, 250),
    "max_hr_bpm": (30, 250),
    "lactate_threshold_hr_bpm": (30, 250),
    "vo2_max_run": (1, 150),
    "vo2_max_bike": (1, 150),
    "ftp_w": (1, 2500),
    "threshold_pace_s_per_km": (30, 3600),
}


class ProfileStore(Protocol):
    def get_value(self, key: str) -> str | None: ...

    def set_value(self, key: str, value: str) -> None: ...


def validate_ai_athlete_profile(profile: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(profile, Mapping):
        raise ValueError("Saved AI athlete profile must be a JSON object")
    values = dict(profile)
    unexpected = sorted(set(values) - set(AI_ATHLETE_PROFILE_FIELDS))
    if unexpected:
        raise ValueError(f"Unsupported AI athlete profile fields: {', '.join(unexpected)}")

    if "gender" in values:
        gender = values["gender"]
        if not isinstance(gender, str) or gender.strip().casefold() not in AI_ATHLETE_PROFILE_GENDERS:
            raise ValueError("Athlete profile gender must be female or male")
        values["gender"] = gender.strip().casefold()

    integer_fields = set(AI_ATHLETE_PROFILE_INTEGER_FIELDS)
    for field, value in values.items():
        if field == "gender":
            continue
        minimum, maximum = _PROFILE_RANGES[field]
        if field in integer_fields:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"Athlete profile {field} must be an integer")
            numeric_value = value
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"Athlete profile {field} must be a number")
            try:
                numeric_value = float(value)
            except (OverflowError, ValueError) as exc:
                raise ValueError(f"Athlete profile {field} must be a finite number") from exc
            if not math.isfinite(numeric_value):
                raise ValueError(f"Athlete profile {field} must be a finite number")
        if not minimum <= numeric_value <= maximum:
            raise ValueError(
                f"Athlete profile {field} must be between {minimum} and {maximum}"
            )
    return values


def load_ai_athlete_profile(store: ProfileStore) -> dict[str, object]:
    raw = store.get_value(_PROFILE_KEY)
    if raw is None:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Saved AI athlete profile contains invalid JSON") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("Saved AI athlete profile must be a JSON object")
    return validate_ai_athlete_profile(payload)


def save_ai_athlete_profile(
    store: ProfileStore,
    values: Mapping[str, object],
    *,
    clear_fields: Iterable[str] = (),
) -> dict[str, object]:
    updates = validate_ai_athlete_profile(values)
    fields_to_clear = tuple(clear_fields)
    unexpected = sorted(set(fields_to_clear) - set(AI_ATHLETE_PROFILE_FIELDS))
    if unexpected:
        raise ValueError(f"Unsupported AI athlete profile fields to clear: {', '.join(unexpected)}")
    overlap = sorted(set(updates) & set(fields_to_clear))
    if overlap:
        raise ValueError(f"Cannot set and clear the same athlete profile fields: {', '.join(overlap)}")
    if not updates and not fields_to_clear:
        raise ValueError("Set at least one athlete profile field or use --clear")

    profile = load_ai_athlete_profile(store)
    for field in fields_to_clear:
        profile.pop(field, None)
    profile.update(updates)
    profile = validate_ai_athlete_profile(profile)
    store.set_value(_PROFILE_KEY, json.dumps(profile, sort_keys=True, allow_nan=False))
    return profile


def reset_ai_athlete_profile(store: ProfileStore) -> dict[str, object]:
    store.set_value(_PROFILE_KEY, "{}")
    return {}
