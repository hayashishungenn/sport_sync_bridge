from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable, Iterator

from .activity_analysis import summarize_activity
from .formats import ActivityFile, ActivityLap, TrackPoint, _write_gpx, read_activity_file
from .state import StateDB
from .utils import parse_datetime


ACTIVITY_FORMATS = {"fit", "gpx", "tcx", "json", "csv"}
CONVERTIBLE_FORMATS = {"fit", "gpx", "tcx"}
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 1000


@dataclass(frozen=True, slots=True)
class ImportResult:
    fingerprint: str
    name: str
    sport_type: str | None
    start_time: str | None
    file_format: str
    source_label: str
    duplicate: bool


@dataclass(frozen=True, slots=True)
class ImportPreview:
    fingerprint: str
    name: str
    sport_type: str | None
    start_time: str | None
    file_format: str
    source_label: str
    duplicate: bool
    summary: dict[str, object]


class LocalActivityLibrary:
    def __init__(self, state_db: StateDB, data_dir: Path):
        self.state_db = state_db
        self.data_dir = data_dir
        self.file_dir = data_dir / "local_imports"

    def import_paths(
        self,
        paths: Iterable[Path],
        *,
        recursive: bool = False,
        zip_password: bytes | None = None,
    ) -> list[ImportResult]:
        candidates = _collect_candidate_paths(paths, recursive=recursive, operation="import")
        results: list[ImportResult] = []
        for path in candidates:
            if not path.is_file():
                raise ValueError(f"Input file does not exist: {path}")
            if path.stat().st_size > MAX_ARCHIVE_BYTES:
                raise ValueError(f"Input file is too large: {path}")
            if path.suffix.lower() == ".zip":
                results.extend(self._import_archive(path, zip_password))
            else:
                if path.stat().st_size > MAX_FILE_BYTES:
                    raise ValueError(f"Activity file is too large: {path}")
                results.append(self.import_payload(path.name, path.read_bytes(), source_label=str(path)))
        return results

    def preview_paths(
        self,
        paths: Iterable[Path],
        *,
        recursive: bool = False,
        zip_password: bytes | None = None,
    ) -> list[ImportPreview]:
        candidates = _collect_candidate_paths(paths, recursive=recursive, operation="preview")
        previews: list[ImportPreview] = []
        for path in candidates:
            if not path.is_file():
                raise ValueError(f"Input file does not exist: {path}")
            if path.stat().st_size > MAX_ARCHIVE_BYTES:
                raise ValueError(f"Input file is too large: {path}")
            if path.suffix.lower() == ".zip":
                previews.extend(
                    self.preview_archive_payload(
                        path.name,
                        path.read_bytes(),
                        source_label=str(path),
                        password=zip_password,
                    )
                )
            else:
                if path.stat().st_size > MAX_FILE_BYTES:
                    raise ValueError(f"Activity file is too large: {path}")
                previews.append(
                    self.preview_payload(path.name, path.read_bytes(), source_label=str(path))
                )
        return previews

    def preview_payload(
        self,
        filename: str,
        payload: bytes,
        *,
        source_label: str | None = None,
    ) -> ImportPreview:
        if len(payload) > MAX_FILE_BYTES:
            raise ValueError(f"Activity file is too large: {filename}")
        suffix = Path(filename).suffix.lower().lstrip(".")
        if suffix not in ACTIVITY_FORMATS:
            raise ValueError(f"Unsupported activity file type: .{suffix or '(none)'}")

        file_format = suffix
        if suffix in CONVERTIBLE_FORMATS:
            with tempfile.TemporaryDirectory(prefix="sport-sync-preview-") as temporary_dir:
                activity_path = Path(temporary_dir) / f"preview.{suffix}"
                activity_path.write_bytes(payload)
                activity = read_activity_file(activity_path)
        elif suffix == "json":
            activity, has_track = _read_json_activity(payload, Path(filename).stem)
            if has_track:
                file_format = "gpx"
        else:
            activity = _read_csv_activity(payload, Path(filename).stem)
            file_format = "gpx"

        _validate_coordinates(activity, filename)
        if not activity.name:
            activity.name = Path(filename).stem
        summary = summarize_activity(activity)
        start_time = summary.get("start_time")
        if isinstance(start_time, str):
            parsed_start = parse_datetime(start_time)
            if parsed_start is not None:
                start_time = parsed_start.isoformat()
                summary["start_time"] = start_time
        fingerprint = hashlib.sha256(payload).hexdigest()
        return ImportPreview(
            fingerprint=fingerprint,
            name=activity.name,
            sport_type=activity.sport_type,
            start_time=str(start_time) if start_time else None,
            file_format=file_format,
            source_label=source_label or filename,
            duplicate=self.state_db.get_local_activity(fingerprint) is not None,
            summary=summary,
        )

    def preview_archive_payload(
        self,
        filename: str,
        payload: bytes,
        *,
        source_label: str | None = None,
        password: bytes | None = None,
    ) -> list[ImportPreview]:
        return [
            self.preview_payload(
                member_name,
                member_payload,
                source_label=f"{source_label or filename}!/{member_path}",
            )
            for member_name, member_path, member_payload in _iter_archive_activity_payloads(
                filename, payload, password, action="preview"
            )
        ]

    def import_payload(
        self,
        filename: str,
        payload: bytes,
        *,
        source_label: str | None = None,
    ) -> ImportResult:
        if len(payload) > MAX_FILE_BYTES:
            raise ValueError(f"Activity file is too large: {filename}")
        suffix = Path(filename).suffix.lower().lstrip(".")
        if suffix not in ACTIVITY_FORMATS:
            raise ValueError(f"Unsupported activity file type: .{suffix or '(none)'}")
        fingerprint = hashlib.sha256(payload).hexdigest()
        existing = self.state_db.get_local_activity(fingerprint)
        duplicate = existing is not None
        if existing is not None and Path(existing["file_path"]).is_file():
            return ImportResult(
                fingerprint=fingerprint,
                name=existing["name"],
                sport_type=existing["sport_type"],
                start_time=existing["start_time"],
                file_format=existing["file_format"],
                source_label=existing["source_label"],
                duplicate=True,
            )

        self.file_dir.mkdir(parents=True, exist_ok=True)
        original_path = self.file_dir / f"{fingerprint}.{suffix}"
        original_existed = original_path.is_file()
        _write_bytes_once(original_path, payload)
        activity: ActivityFile
        normalized_path = original_path
        normalized_created = False
        file_format = suffix
        try:
            if suffix in CONVERTIBLE_FORMATS:
                activity = read_activity_file(original_path)
            elif suffix == "json":
                activity, has_track = _read_json_activity(payload, Path(filename).stem)
                if has_track:
                    normalized_path = self.file_dir / f"{fingerprint}.gpx"
                    normalized_created = not normalized_path.exists()
                    _write_bytes_once(normalized_path, _write_gpx(activity))
                    file_format = "gpx"
            else:
                activity = _read_csv_activity(payload, Path(filename).stem)
                normalized_path = self.file_dir / f"{fingerprint}.gpx"
                normalized_created = not normalized_path.exists()
                _write_bytes_once(normalized_path, _write_gpx(activity))
                file_format = "gpx"
            _validate_coordinates(activity, filename)
        except Exception:
            if normalized_created:
                normalized_path.unlink(missing_ok=True)
            if not original_existed:
                original_path.unlink(missing_ok=True)
            raise

        if not activity.name:
            activity.name = Path(filename).stem
        summary = summarize_activity(activity)
        start_time = summary.get("start_time")
        if isinstance(start_time, str):
            parsed_start = parse_datetime(start_time)
            if parsed_start is not None:
                start_time = parsed_start.isoformat()
                summary["start_time"] = start_time
        label = source_label or filename
        self.state_db.upsert_local_activity(
            fingerprint=fingerprint,
            name=activity.name,
            sport_type=activity.sport_type,
            start_time=str(start_time) if start_time else None,
            file_path=str(normalized_path),
            source_label=label,
            file_format=file_format,
            summary=summary,
        )
        return ImportResult(
            fingerprint=fingerprint,
            name=activity.name,
            sport_type=activity.sport_type,
            start_time=str(start_time) if start_time else None,
            file_format=file_format,
            source_label=label,
            duplicate=duplicate,
        )

    def import_archive_payload(
        self,
        filename: str,
        payload: bytes,
        *,
        source_label: str | None = None,
        password: bytes | None = None,
    ) -> list[ImportResult]:
        return [
            self.import_payload(
                member_name,
                member_payload,
                source_label=f"{filename}!/{member_path}",
            )
            for member_name, member_path, member_payload in _iter_archive_activity_payloads(
                filename, payload, password
            )
        ]

    def get_activity(self, identifier: str):
        row = self.state_db.get_local_activity(identifier)
        if row is None:
            raise ValueError(f"Local activity was not found: {identifier}")
        return row

    def _import_archive(self, archive_path: Path, password: bytes | None) -> list[ImportResult]:
        if archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
            raise ValueError(f"ZIP file is too large: {archive_path}")
        return self.import_archive_payload(
            archive_path.name,
            archive_path.read_bytes(),
            source_label=str(archive_path),
            password=password,
        )


def _collect_candidate_paths(
    paths: Iterable[Path], *, recursive: bool, operation: str
) -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()
    for input_path in paths:
        resolved = input_path.expanduser().resolve()
        if resolved.is_dir():
            if not recursive:
                raise ValueError(f"Directory {operation} requires --recursive: {resolved}")
            entries = sorted(path for path in resolved.rglob("*") if path.is_file())
        else:
            entries = [resolved]
        for entry in entries:
            if entry not in seen:
                seen.add(entry)
                candidates.append(entry)
    if not candidates:
        raise ValueError(f"No files found to {operation}")
    return candidates


def _iter_archive_activity_payloads(
    filename: str,
    payload: bytes,
    password: bytes | None,
    *,
    action: str = "import",
) -> Iterator[tuple[str, str, bytes]]:
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise ValueError(f"ZIP file is too large: {filename}")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            entries = [info for info in archive.infolist() if not info.is_dir()]
            if len(entries) > MAX_ARCHIVE_ENTRIES:
                raise ValueError(f"ZIP contains too many entries: {filename}")
            if sum(info.file_size for info in entries) > MAX_ARCHIVE_BYTES:
                raise ValueError(f"ZIP expands beyond the allowed size: {filename}")
            supported_entries = [
                info
                for info in entries
                if Path(PurePosixPath(info.filename).name).suffix.lower().lstrip(".")
                in ACTIVITY_FORMATS
            ]
            if not supported_entries:
                raise ValueError(f"ZIP contains no supported activity files: {filename}")
            for info in supported_entries:
                member_name = PurePosixPath(info.filename).name
                if info.file_size > MAX_FILE_BYTES:
                    raise ValueError(f"ZIP member is too large: {member_name}")
                try:
                    member_payload = archive.read(info, pwd=password)
                except RuntimeError as exc:
                    if "password" in str(exc).lower() or "encrypted" in str(exc).lower():
                        raise ValueError(
                            f"ZIP member is encrypted; set ACTIVITY_ARCHIVE_PASSWORD to {action} {filename}"
                        ) from exc
                    raise ValueError(f"Could not read ZIP member {member_name}: {exc}") from exc
                yield member_name, info.filename, member_payload
    except zipfile.BadZipFile as exc:
        raise ValueError(f"Invalid ZIP archive: {filename}") from exc


def _read_json_activity(payload: bytes, default_name: str) -> tuple[ActivityFile, bool]:
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid activity JSON") from exc
    if isinstance(decoded, dict):
        root = decoded.get("activity", decoded)
    else:
        raise ValueError("Activity JSON must contain an object")
    if not isinstance(root, dict):
        raise ValueError("Activity JSON activity field must be an object")
    raw_laps = root.get("laps") if isinstance(root.get("laps"), list) else []
    laps = [_lap_from_json(item) for item in raw_laps if isinstance(item, dict)]
    top_points = root.get("track_points") or root.get("trackPoints") or root.get("points") or []
    if top_points and not laps:
        laps = [ActivityLap(track_points=[_point_from_json(item) for item in top_points if isinstance(item, dict)])]
    activity = ActivityFile(
        name=_first_text(root, "name", "title") or default_name,
        sport_type=_normalize_sport(_first_text(root, "sport_type", "sportType", "sport")),
        start_time=_coerce_datetime(_first(root, "start_time", "startTime")),
        end_time=_coerce_datetime(_first(root, "end_time", "endTime")),
        elapsed_time_s=_first_number(root, "elapsed_time_s", "elapsedTime", "duration"),
        timer_time_s=_first_number(root, "timer_time_s", "timerTime"),
        distance_m=_first_number(root, "distance_m", "distanceMeters", "distance"),
        laps=laps,
    )
    has_track = any(point.latitude is not None and point.longitude is not None for point in activity.track_points)
    return activity, has_track


def _lap_from_json(value: dict[str, object]) -> ActivityLap:
    raw_points = value.get("track_points") or value.get("trackPoints") or value.get("points") or []
    return ActivityLap(
        start_time=_coerce_datetime(_first(value, "start_time", "startTime")),
        end_time=_coerce_datetime(_first(value, "end_time", "endTime")),
        elapsed_time_s=_first_number(value, "elapsed_time_s", "elapsedTime"),
        timer_time_s=_first_number(value, "timer_time_s", "timerTime"),
        distance_m=_first_number(value, "distance_m", "distanceMeters", "distance"),
        calories=_first_integer(value, "calories"),
        average_heart_rate=_first_number(value, "average_heart_rate", "avgHeartRate"),
        maximum_heart_rate=_first_number(value, "maximum_heart_rate", "maxHeartRate"),
        track_points=[_point_from_json(item) for item in raw_points if isinstance(item, dict)],
    )


def _point_from_json(value: dict[str, object]) -> TrackPoint:
    return TrackPoint(
        timestamp=_coerce_datetime(_first(value, "timestamp", "time", "date")),
        latitude=_first_number(value, "latitude", "lat"),
        longitude=_first_number(value, "longitude", "lon", "lng"),
        elevation_m=_first_number(value, "elevation_m", "elevation", "altitude", "altitudeMeters"),
        distance_m=_first_number(value, "distance_m", "distance", "distanceMeters"),
        speed_mps=_first_number(value, "speed_mps", "speed"),
        heart_rate_bpm=_first_number(value, "heart_rate_bpm", "heart_rate", "heartRate", "hr"),
        cadence_rpm=_first_number(value, "cadence_rpm", "cadence"),
        power_w=_first_number(value, "power_w", "power", "watts"),
    )


def _read_csv_activity(payload: bytes, default_name: str) -> ActivityFile:
    try:
        text = payload.decode("utf-8-sig")
        rows = list(csv.DictReader(io.StringIO(text)))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise ValueError("Invalid activity CSV") from exc
    if not rows:
        raise ValueError("Activity CSV contains no data rows")
    normalized = [{(key or "").strip().lower(): value for key, value in row.items()} for row in rows]
    aliases = {
        "timestamp": ("timestamp", "time", "datetime", "date"),
        "latitude": ("latitude", "lat"),
        "longitude": ("longitude", "lon", "lng"),
        "elevation_m": ("elevation_m", "elevation", "altitude", "altitude_m"),
        "distance_m": ("distance_m", "distance", "distance_meters"),
        "speed_mps": ("speed_mps", "speed"),
        "heart_rate_bpm": ("heart_rate_bpm", "heart_rate", "heartrate", "hr"),
        "cadence_rpm": ("cadence_rpm", "cadence"),
        "power_w": ("power_w", "power", "watts"),
    }
    points: list[TrackPoint] = []
    for row in normalized:
        values = {field: _first_number(row, *keys) for field, keys in aliases.items() if field != "timestamp"}
        points.append(
            TrackPoint(
                timestamp=_coerce_datetime(_first(row, *aliases["timestamp"])),
                **values,
            )
        )
    if not any(point.latitude is not None and point.longitude is not None for point in points):
        raise ValueError("Activity CSV needs latitude and longitude columns")
    if any(point.timestamp is None for point in points):
        raise ValueError("Activity CSV needs a timestamp on every row")
    return ActivityFile(
        name=default_name,
        sport_type=_first_text(normalized[0], "sport_type", "sport", "activity_type") or "generic",
        start_time=points[0].timestamp,
        end_time=points[-1].timestamp,
        elapsed_time_s=(points[-1].timestamp - points[0].timestamp).total_seconds(),
        laps=[ActivityLap(start_time=points[0].timestamp, end_time=points[-1].timestamp, track_points=points)],
    )


def _first(value: dict[str, object], *keys: str) -> object | None:
    for key in keys:
        if key in value and value[key] is not None and value[key] != "":
            return value[key]
    return None


def _first_text(value: dict[str, object], *keys: str) -> str | None:
    found = _first(value, *keys)
    return str(found).strip() if found is not None and str(found).strip() else None


def _first_number(value: dict[str, object], *keys: str) -> float | None:
    found = _first(value, *keys)
    if isinstance(found, bool) or found is None:
        return None
    try:
        number = float(found)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _first_integer(value: dict[str, object], *keys: str) -> int | None:
    number = _first_number(value, *keys)
    return int(number) if number is not None else None


def _coerce_datetime(value: object | None) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        timestamp = float(value)
        if abs(timestamp) > 10_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    if value is None:
        return None
    return parse_datetime(str(value))


def _validate_coordinates(activity: ActivityFile, label: str) -> None:
    for point in activity.track_points:
        if (point.latitude is None) != (point.longitude is None):
            raise ValueError(f"Incomplete GPS coordinates in {label}")
        if point.latitude is None or point.longitude is None:
            continue
        if not (-90 <= point.latitude <= 90 and -180 <= point.longitude <= 180):
            raise ValueError(f"Invalid GPS coordinates in {label}: {point.latitude}, {point.longitude}")


def _normalize_sport(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower().replace(" ", "_")
    return normalized or None


def _write_bytes_once(path: Path, payload: bytes) -> None:
    if path.is_file():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
