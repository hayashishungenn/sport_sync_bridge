from __future__ import annotations

import re
from urllib.parse import quote


_COMPACT_UUID = re.compile(r"^[0-9a-fA-F]{32}$")
_HYPHENATED_UUID = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
_COMPONENT_SAFE_CHARS = "-_.!~*'()"
_INVITE_BASE_URL = "https://garsync.com/i"


def _compact_uuid(value: str) -> str:
    if not isinstance(value, str) or not (_COMPACT_UUID.fullmatch(value) or _HYPHENATED_UUID.fullmatch(value)):
        raise ValueError("GarSync invite IDs must be UUIDs in 32-character or hyphenated form")
    return value.replace("-", "")


def _optional_component(name: str, value: str | None) -> str:
    if value is None or value == "":
        return ""
    return f"&{name}={quote(value, safe=_COMPONENT_SAFE_CHARS)}"


def build_friend_invite_url(user_id: str, *, name: str | None = None) -> str:
    return f"{_INVITE_BASE_URL}/f/?u={_compact_uuid(user_id)}{_optional_component('n', name)}"


def build_group_invite_url(
    group_id: str,
    *,
    group_name: str | None = None,
    inviter_name: str | None = None,
) -> str:
    return (
        f"{_INVITE_BASE_URL}/g/?g={_compact_uuid(group_id)}"
        f"{_optional_component('gn', group_name)}{_optional_component('n', inviter_name)}"
    )
