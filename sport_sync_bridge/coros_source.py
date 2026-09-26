from __future__ import annotations

import asyncio
import base64
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, urlparse

import httpx2
from mcp import ClientSession
from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from pydantic import AnyUrl

from .config import AppConfig
from .models import Activity
from .sources import SourceAdapter
from .state import StateDB
from .utils import fit_signature_ok, parse_datetime, safe_filename


_TOKEN_KEY = "coros_mcp_oauth_tokens"
_CLIENT_INFO_KEY = "coros_mcp_oauth_client_info"
_DOWNLOAD_TIMES_KEY = "coros_mcp_fit_download_attempts"
_DOWNLOAD_LIMIT = 50
_DOWNLOAD_WINDOW = timedelta(hours=24)


class CorosMcpTokenStorage(TokenStorage):
    def __init__(self, state_db: StateDB):
        self.state_db = state_db

    async def get_tokens(self) -> OAuthToken | None:
        value = self.state_db.get_value(_TOKEN_KEY)
        return OAuthToken.model_validate_json(value) if value else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.state_db.set_value(_TOKEN_KEY, tokens.model_dump_json())

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        value = self.state_db.get_value(_CLIENT_INFO_KEY)
        return OAuthClientInformationFull.model_validate_json(value) if value else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.state_db.set_value(_CLIENT_INFO_KEY, client_info.model_dump_json())


class CorosMcpSource(SourceAdapter):
    name = "coros"
    _callback_uri = "http://localhost:8765/callback"

    def __init__(
        self,
        config: AppConfig,
        state_db: StateDB,
        *,
        input_fn: Callable[[str], str] = input,
        output_fn: Callable[[str], None] = print,
    ):
        super().__init__(config)
        self.state_db = state_db
        self.input_fn = input_fn
        self.output_fn = output_fn

    def is_configured(self) -> bool:
        return True

    def authenticate(self) -> None:
        async def check(session: ClientSession) -> None:
            await session.list_tools()

        _run_async(self._with_session(check))

    def list_activities(
        self,
        since: datetime | None,
        until: datetime | None,
        limit: int | None,
    ) -> list[Activity]:
        async def list_records(session: ClientSession) -> tuple[object, object]:
            tools = await session.list_tools()
            tool = _find_tool(tools, "querySportRecords")
            start = since or datetime.now(timezone.utc) - timedelta(days=90)
            end = until or datetime.now(timezone.utc)
            arguments = _build_tool_arguments(
                tool,
                since=start,
                until=end,
                limit=limit,
                timezone_name=self.config.coros_mcp_timezone,
            )
            result = await session.call_tool(tool.name, arguments)
            return result, tool

        result, _ = _run_async(self._with_session(list_records))
        rows = _collect_activity_rows(_result_payload(result))
        activities = [_activity_from_record(row) for row in rows]
        activities = [item for item in activities if item is not None]
        if since:
            activities = [item for item in activities if item.start_time is None or item.start_time >= since]
        if until:
            activities = [item for item in activities if item.start_time is None or item.start_time <= until]
        activities.sort(key=lambda item: (item.start_time or datetime.min.replace(tzinfo=timezone.utc), item.source_id))
        return activities[:limit] if limit is not None else activities

    def download_fit(self, activity: Activity, output_dir: Path) -> Path:
        activity_dir = output_dir / self.name
        activity_dir.mkdir(parents=True, exist_ok=True)
        path = activity_dir / f"{safe_filename(activity.source_id)}.fit"
        if path.is_file() and path.stat().st_size > 100 and fit_signature_ok(path):
            return path

        async def download(session: ClientSession) -> object:
            tools = await session.list_tools()
            tool = _find_tool(tools, "downloadActivityFitFiles")
            sport_type_code = _first_value(
                activity.raw,
                {"sporttypecode", "sporttype", "sportcode", "sport_type_code"},
            )
            arguments = _build_tool_arguments(
                tool,
                activity_id=activity.source_id,
                sport_type_code=sport_type_code,
                timezone_name=self.config.coros_mcp_timezone,
            )
            self._reserve_fit_download_attempt()
            return await session.call_tool(tool.name, arguments)

        result = _run_async(self._with_session(download))
        file_bytes = _extract_fit_bytes(result)
        if file_bytes is None or len(file_bytes) <= 100 or file_bytes[8:12] != b".FIT":
            raise RuntimeError(
                "COROS did not return a valid embedded FIT file for "
                f"{activity.source_id}; the COROS MCP FIT tool may have changed"
            )
        path.write_bytes(file_bytes)
        return path

    def _reserve_fit_download_attempt(self) -> None:
        now = datetime.now(timezone.utc)
        raw_value = self.state_db.get_value(_DOWNLOAD_TIMES_KEY)
        try:
            decoded = json.loads(raw_value) if raw_value else []
        except ValueError as exc:
            raise RuntimeError("Stored COROS FIT download quota state is invalid") from exc
        if not isinstance(decoded, list):
            raise RuntimeError("Stored COROS FIT download quota state is invalid")

        attempts: list[datetime] = []
        for value in decoded:
            timestamp = parse_datetime(value)
            if timestamp is None:
                raise RuntimeError("Stored COROS FIT download quota state is invalid")
            age = now - timestamp
            if timedelta(0) <= age < _DOWNLOAD_WINDOW:
                attempts.append(timestamp)

        if len(attempts) >= _DOWNLOAD_LIMIT:
            retry_at = min(attempts) + _DOWNLOAD_WINDOW
            raise RuntimeError(
                "COROS MCP FIT download allowance reached (50 files per 24 hours); "
                f"try again after {retry_at.isoformat()}"
            )

        attempts.append(now)
        self.state_db.set_value(
            _DOWNLOAD_TIMES_KEY,
            json.dumps([item.isoformat() for item in attempts], separators=(",", ":")),
        )

    async def _with_session(self, operation: Callable[[ClientSession], Any]) -> Any:
        oauth = OAuthClientProvider(
            server_url=self.config.coros_mcp_url,
            client_metadata=OAuthClientMetadata(
                client_name="sport_sync_bridge",
                redirect_uris=[AnyUrl(self._callback_uri)],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
            ),
            storage=CorosMcpTokenStorage(self.state_db),
            redirect_handler=self._show_authorization_url,
            callback_handler=self._read_callback,
        )
        async with httpx2.AsyncClient(auth=oauth) as http_client:
            async with streamable_http_client(
                self.config.coros_mcp_url,
                http_client=http_client,
            ) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    return await operation(session)

    async def _show_authorization_url(self, auth_url: str) -> None:
        self.output_fn(f"Open this COROS authorization URL in a browser:\n{auth_url}")

    async def _read_callback(self) -> AuthorizationCodeResult:
        callback_url = await asyncio.to_thread(
            self.input_fn,
            "After authorizing, paste the full localhost callback URL: ",
        )
        query = parse_qs(urlparse(callback_url.strip()).query)
        error = query.get("error", [None])[0]
        if error:
            description = query.get("error_description", [""])[0]
            raise RuntimeError(f"COROS OAuth authorization failed: {error} {description}".strip())
        code = query.get("code", [None])[0]
        if not code:
            raise RuntimeError("COROS callback URL does not contain an authorization code")
        return AuthorizationCodeResult(
            code=code,
            state=query.get("state", [None])[0],
            iss=query.get("iss", [None])[0],
        )


def _run_async(awaitable: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    raise RuntimeError("COROS MCP cannot run from an already active asyncio event loop")


def _find_tool(tools_result: object, expected_name: str) -> Any:
    tools = getattr(tools_result, "tools", None)
    if not isinstance(tools, Iterable):
        raise RuntimeError("COROS MCP returned an invalid tool list")
    for tool in tools:
        if str(getattr(tool, "name", "")).casefold() == expected_name.casefold():
            return tool
    available = ", ".join(sorted(str(getattr(tool, "name", "")) for tool in tools))
    raise RuntimeError(
        f"COROS MCP does not expose {expected_name}; available tools: {available or 'none'}"
    )


def _build_tool_arguments(
    tool: object,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int | None = None,
    activity_id: str | None = None,
    sport_type_code: object | None = None,
    timezone_name: str | None = None,
) -> dict[str, object]:
    schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None)
    if not isinstance(schema, dict):
        raise RuntimeError(f"COROS MCP tool {getattr(tool, 'name', '')} has no input schema")
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise RuntimeError(f"COROS MCP tool {getattr(tool, 'name', '')} has an invalid input schema")

    arguments: dict[str, object] = {}
    for property_name, property_schema in properties.items():
        normalized = _normalized_key(property_name)
        value: object | None = None
        if activity_id is not None and normalized in _ACTIVITY_ID_KEYS:
            if isinstance(property_schema, dict) and property_schema.get("type") == "array":
                item_schema = property_schema.get("items")
                value = [_coerce_schema_value(activity_id, item_schema)]
            else:
                value = _coerce_schema_value(activity_id, property_schema)
        elif since is not None and normalized in _START_DATE_KEYS:
            value = _date_argument(since, property_name, property_schema)
        elif until is not None and normalized in _END_DATE_KEYS:
            value = _date_argument(until, property_name, property_schema)
        elif limit is not None and normalized in _LIMIT_KEYS:
            value = limit
        elif normalized in _SPORT_TYPE_CODE_KEYS and sport_type_code is not None:
            if isinstance(property_schema, dict) and property_schema.get("type") == "array":
                item_schema = property_schema.get("items")
                value = [_coerce_schema_value(sport_type_code, item_schema)]
            else:
                value = _coerce_schema_value(sport_type_code, property_schema)
        elif normalized in _SPORT_TYPE_CODE_KEYS and activity_id is None:
            if isinstance(property_schema, dict) and property_schema.get("type") == "array":
                value = []
        elif normalized in _TIMEZONE_KEYS and timezone_name is not None:
            value = timezone_name
        if value is not None:
            arguments[str(property_name)] = value

    missing = [str(key) for key in required if key not in arguments]
    if missing:
        raise RuntimeError(
            f"COROS MCP tool {getattr(tool, 'name', '')} requires unsupported parameters: "
            + ", ".join(missing)
        )
    return arguments


_START_DATE_KEYS = {
    "start", "startdate", "startdatetime", "starttime", "datefrom", "fromdate", "from",
    "since", "begindate", "begintime", "starttimestamp", "fromtimestamp",
}
_END_DATE_KEYS = {
    "end", "enddate", "enddatetime", "endtime", "dateto", "todate", "to", "until",
    "finishdate", "finishtime", "endtimestamp", "totimestamp",
}
_LIMIT_KEYS = {"limit", "count", "size", "pagesize", "page_size", "maxresults", "maxcount"}
_ACTIVITY_ID_KEYS = {
    "activityid", "activityids", "labelid", "recordid", "recordids", "sportrecordid", "sportrecordids",
    "workoutid", "workoutids", "id", "ids",
}
_SPORT_TYPE_CODE_KEYS = {"sporttype", "sporttypecode", "sporttypecodes", "sportcode", "sportcodes"}
_TIMEZONE_KEYS = {"timezone", "timeZone"}


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _coerce_schema_value(value: object, schema: object) -> object:
    if not isinstance(schema, dict):
        return value
    value_type = schema.get("type")
    if value_type == "integer" and isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value)
    if value_type == "number" and isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return value
    if value_type == "string" and not isinstance(value, str):
        return str(value)
    return value


def _date_argument(value: datetime, name: str, schema: object) -> object:
    normalized = _normalized_key(name)
    schema_format = schema.get("format") if isinstance(schema, dict) else None
    schema_type = schema.get("type") if isinstance(schema, dict) else None
    if schema_type in {"integer", "number"} or "timestamp" in normalized:
        timestamp = value.timestamp()
        if normalized.endswith("ms") or normalized.endswith("millis"):
            timestamp *= 1000
        return int(timestamp)
    if schema_format == "date-time" or "time" in normalized:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return value.date().isoformat()


def _result_payload(result: object) -> object:
    if _result_flag(result, "isError", "is_error"):
        message = " ".join(
            str(getattr(block, "text", ""))
            for block in (getattr(result, "content", None) or [])
            if getattr(block, "text", None)
        )
        raise RuntimeError(f"COROS MCP tool failed: {message or 'unknown error'}")

    payload = _result_field(result, "structuredContent", "structured_content")
    if payload is not None:
        return payload
    content = getattr(result, "content", None) or []
    parsed: list[object] = []
    for block in content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            value = _parse_json_text(text)
            if value is not None:
                parsed.append(value)
        else:
            resource = getattr(block, "resource", None)
            resource_text = getattr(resource, "text", None)
            if isinstance(resource_text, str):
                value = _parse_json_text(resource_text)
                if value is not None:
                    parsed.append(value)
                elif resource_text.strip():
                    parsed.append(resource_text)
        if isinstance(text, str) and text.strip() and _parse_json_text(text) is None:
            parsed.append(text)
    if not parsed:
        return None
    return parsed[0] if len(parsed) == 1 else parsed


def _parse_json_text(text: str) -> object | None:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE)
    try:
        return json.loads(candidate)
    except ValueError:
        for opening, closing in (("{", "}"), ("[", "]")):
            start = candidate.find(opening)
            end = candidate.rfind(closing)
            if start >= 0 and end > start:
                try:
                    return json.loads(candidate[start : end + 1])
                except ValueError:
                    continue
    return None


def _collect_activity_rows(value: object) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    if isinstance(value, str):
        lines = [line.strip() for line in value.splitlines() if line.strip()]
        if any("|" in line for line in lines):
            blocks = [line for line in lines if "|" in line]
        else:
            blocks = re.split(r"\n\s*\n", value.strip())
        for block in blocks:
            record: dict[str, object] = {}
            for field in re.split(r"\s*\|\s*|\r?\n", block):
                match = re.match(r"\s*([^:]+):\s*(.*?)\s*$", field)
                if match:
                    record[match.group(1).strip()] = match.group(2).strip()
            if _activity_from_record(record) is not None:
                rows.append(record)
        if rows:
            return rows
        parsed = _parse_json_text(value)
        return _collect_activity_rows(parsed) if parsed is not None else rows
    if isinstance(value, list):
        for item in value:
            rows.extend(_collect_activity_rows(item))
        return rows
    if not isinstance(value, dict):
        return rows

    keys = {_normalized_key(str(key)): key for key in value}
    has_id = any(key in keys for key in _ACTIVITY_ID_KEYS)
    has_start = any(key in keys for key in _START_DATE_KEYS | {"startat", "startdateutc", "starttimeutc"})
    if has_id and has_start:
        rows.append(value)
        return rows

    preferred = (
        "records", "sportRecords", "activities", "workouts", "items", "results", "data", "result",
        "list", "content",
    )
    visited: set[int] = set()
    for preferred_key in preferred:
        actual_key = keys.get(_normalized_key(preferred_key))
        if actual_key is not None:
            child = value[actual_key]
            visited.add(id(child))
            rows.extend(_collect_activity_rows(child))
    for key, child in value.items():
        if id(child) not in visited and isinstance(child, (dict, list)):
            rows.extend(_collect_activity_rows(child))
    return rows


def _activity_from_record(record: dict[str, object]) -> Activity | None:
    source_id = _first_value(record, _ACTIVITY_ID_KEYS)
    start_value = _first_value(
        record,
        _START_DATE_KEYS | {"startat", "startdateutc", "starttimeutc", "starttimestamp"},
    )
    start_time = parse_datetime(start_value)
    if source_id is None or start_time is None:
        return None
    name = _first_value(record, {"name", "title", "activityname", "workoutname"})
    sport = _first_value(record, {"sport", "sporttype", "sporttypename", "sportname", "activitytype"})
    return Activity(
        source=CorosMcpSource.name,
        source_id=str(source_id),
        name=str(name or f"COROS-{source_id}"),
        sport_type=_normalize_sport(sport),
        start_time=start_time,
        raw=record,
    )


def _first_value(record: dict[str, object], candidates: set[str]) -> object | None:
    normalized_candidates = {_normalized_key(candidate) for candidate in candidates}
    for key, value in record.items():
        if _normalized_key(str(key)) in normalized_candidates and value not in (None, ""):
            if isinstance(value, dict):
                nested = _first_value(value, {"name", "type", "value", "id"})
                return nested if nested is not None else value
            return value
    return None


def _normalize_sport(value: object | None) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = re.sub(r"[\s-]+", "_", value.strip().casefold())
    if normalized.isdigit():
        return None
    aliases = {
        "run": "running",
        "trailrun": "trail_running",
        "trail_run": "trail_running",
        "ride": "cycling",
        "bike": "cycling",
        "biking": "cycling",
        "indoorbike": "indoor_cycling",
        "indoor_bike": "indoor_cycling",
        "swim": "swimming",
    }
    return aliases.get(normalized, normalized or None)


def _extract_fit_bytes(result: object) -> bytes | None:
    if _result_flag(result, "isError", "is_error"):
        _result_payload(result)
    structured = _result_field(result, "structuredContent", "structured_content")
    candidate = _decode_fit_value(structured)
    if candidate is not None:
        return candidate
    for block in getattr(result, "content", None) or []:
        resource = getattr(block, "resource", None)
        candidate = _decode_fit_value(resource)
        if candidate is not None:
            return candidate
        text = getattr(block, "text", None)
        if isinstance(text, str):
            candidate = _decode_fit_value(_parse_json_text(text))
            if candidate is not None:
                return candidate
            candidate = _decode_fit_value(text)
            if candidate is not None:
                return candidate
    return None


def _decode_fit_value(value: object, field_name: str = "") -> bytes | None:
    if isinstance(value, bytes):
        return value if len(value) > 100 and value[8:12] == b".FIT" else None
    if isinstance(value, dict):
        for key in ("blob", "base64", "fitBase64", "fileBase64", "data", "content", "file"):
            if key in value:
                decoded = _decode_fit_value(value[key], str(key))
                if decoded is not None:
                    return decoded
        return None
    blob = getattr(value, "blob", None)
    if isinstance(blob, str):
        return _decode_fit_value(blob, "blob")
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("data:") and ";base64," in text:
            text = text.split(";base64,", 1)[1]
            field_name = "base64"
        if field_name.casefold() in {"blob", "base64", "fitbase64", "filebase64", "data", "content", "file"}:
            try:
                decoded = base64.b64decode(text, validate=True)
            except (ValueError, base64.binascii.Error):
                return None
            return decoded if len(decoded) > 100 and decoded[8:12] == b".FIT" else None
    return None


def _result_flag(result: object, *names: str) -> bool:
    value = _result_field(result, *names)
    return bool(value)


def _result_field(result: object, *names: str) -> Any:
    for name in names:
        if isinstance(result, dict) and name in result:
            return result[name]
        value = getattr(result, name, None)
        if value is not None:
            return value
    return None
