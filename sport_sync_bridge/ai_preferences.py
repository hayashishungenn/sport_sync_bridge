from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Protocol


AI_ANALYSIS_FOCI = ("performance", "health", "recovery")
AI_ANALYSIS_DETAILS = ("brief", "normal", "detailed")
DEFAULT_AI_ANALYSIS_PREFERENCES = {"focus": "performance", "detail": "normal"}
_PREFERENCE_KEY = "ai_analysis_preferences"


class PreferenceStore(Protocol):
    def get_value(self, key: str) -> str | None: ...

    def set_value(self, key: str, value: str) -> None: ...


def load_ai_analysis_preferences(store: PreferenceStore) -> dict[str, str]:
    raw = store.get_value(_PREFERENCE_KEY)
    if raw is None:
        return dict(DEFAULT_AI_ANALYSIS_PREFERENCES)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Saved AI analysis preferences contain invalid JSON") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("Saved AI analysis preferences must be a JSON object")
    unexpected = sorted(set(payload) - set(DEFAULT_AI_ANALYSIS_PREFERENCES))
    if unexpected:
        raise ValueError(f"Unsupported saved AI analysis preferences: {', '.join(unexpected)}")

    preferences = dict(DEFAULT_AI_ANALYSIS_PREFERENCES)
    preferences.update(payload)
    _validate_preferences(preferences)
    return preferences


def save_ai_analysis_preferences(
    store: PreferenceStore,
    *,
    focus: str | None = None,
    detail: str | None = None,
) -> dict[str, str]:
    if focus is None and detail is None:
        raise ValueError("Set at least one of --focus or --detail")
    preferences = load_ai_analysis_preferences(store)
    if focus is not None:
        preferences["focus"] = focus
    if detail is not None:
        preferences["detail"] = detail
    _validate_preferences(preferences)
    store.set_value(_PREFERENCE_KEY, json.dumps(preferences, sort_keys=True))
    return preferences


def reset_ai_analysis_preferences(store: PreferenceStore) -> dict[str, str]:
    preferences = dict(DEFAULT_AI_ANALYSIS_PREFERENCES)
    store.set_value(_PREFERENCE_KEY, json.dumps(preferences, sort_keys=True))
    return preferences


def _validate_preferences(preferences: Mapping[str, object]) -> None:
    if preferences.get("focus") not in AI_ANALYSIS_FOCI:
        raise ValueError("Saved AI analysis focus must be performance, health, or recovery")
    if preferences.get("detail") not in AI_ANALYSIS_DETAILS:
        raise ValueError("Saved AI analysis detail must be brief, normal, or detailed")
