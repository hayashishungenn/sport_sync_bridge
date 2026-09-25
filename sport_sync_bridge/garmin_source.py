from __future__ import annotations

import io
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from .config import AppConfig
from .models import Activity
from .sources import SourceAdapter
from .utils import fit_signature_ok, parse_datetime, safe_filename


class GarminClient(Protocol):
    def get_activities(self, start: int = 0, limit: int = 20) -> object: ...

    def download_activity(self, activity_id: str, *, dl_fmt: object) -> bytes: ...


class GarminTargetProvider(Protocol):
    client: GarminClient | None

    def is_configured(self) -> bool: ...

    def authenticate(self) -> None: ...


class GarminSource(SourceAdapter):
    name = "garmin"
    page_size = 50

    def __init__(self, config: AppConfig, target: GarminTargetProvider):
        super().__init__(config)
        self.target = target

    def is_configured(self) -> bool:
        return self.target.is_configured()

    def authenticate(self) -> None:
        self.target.authenticate()

    def list_activities(
        self,
        since: datetime | None,
        until: datetime | None,
        limit: int | None,
    ) -> list[Activity]:
        if limit is not None and limit <= 0:
            return []

        client = self._client()
        lower_bound = parse_datetime(since)
        upper_bound = parse_datetime(until)
        activities: list[Activity] = []
        offset = 0

        while True:
            page = client.get_activities(start=offset, limit=self.page_size)
            if not isinstance(page, list):
                raise RuntimeError("Garmin activity response must be a JSON list")
            if not page:
                break

            for index, item in enumerate(page):
                if not isinstance(item, dict):
                    raise RuntimeError(f"Garmin activity {index} must be a JSON object")
                raw_id = item.get("activityId") or item.get("id")
                if raw_id in (None, ""):
                    raise RuntimeError(f"Garmin activity {index} is missing its ID")
                source_id = str(raw_id)
                start_time = parse_datetime(
                    item.get("startTimeGMT") or item.get("startTimeLocal") or item.get("startTime")
                )
                if start_time is None:
                    raise RuntimeError(f"Garmin activity {source_id} is missing a valid start time")
                if lower_bound is not None and start_time < lower_bound:
                    continue
                if upper_bound is not None and start_time > upper_bound:
                    continue

                sport_type = _garmin_sport_type(item)
                name = item.get("activityName") or item.get("name")
                activities.append(
                    Activity(
                        source=self.name,
                        source_id=source_id,
                        name=str(name) if name else f"Garmin activity {source_id}",
                        sport_type=sport_type,
                        start_time=start_time,
                        raw=item,
                    )
                )
                if limit is not None and len(activities) >= limit:
                    break

            if limit is not None and len(activities) >= limit:
                break
            offset += len(page)
            if len(page) < self.page_size:
                break

        return sorted(activities, key=_activity_sort_key)

    def download_fit(self, activity: Activity, output_dir: Path) -> Path:
        activity_dir = output_dir / self.name
        activity_dir.mkdir(parents=True, exist_ok=True)
        path = activity_dir / f"{safe_filename(activity.source_id)}.fit"
        if path.is_file() and path.stat().st_size >= 100 and fit_signature_ok(path):
            return path

        try:
            import garminconnect
        except ImportError as exc:
            raise RuntimeError("garminconnect is required for Garmin downloads") from exc

        client = self._client()
        original_format = garminconnect.Garmin.ActivityDownloadFormat.ORIGINAL
        downloaded = client.download_activity(activity.source_id, dl_fmt=original_format)
        if not isinstance(downloaded, (bytes, bytearray)):
            raise RuntimeError(f"Garmin download for {activity.source_id} did not return bytes")
        fit_bytes = _extract_fit(bytes(downloaded), activity.source_id)

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=activity_dir, prefix=f".{path.name}.", suffix=".tmp", delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(fit_bytes)
            temporary_path.replace(path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

        if path.stat().st_size < 100 or not fit_signature_ok(path):
            path.unlink(missing_ok=True)
            raise RuntimeError(f"Garmin download for {activity.source_id} is not a valid FIT")
        return path

    def _client(self) -> GarminClient:
        self.target.authenticate()
        client = self.target.client
        if client is None:
            raise RuntimeError("Garmin authentication did not create a client")
        return client


def _extract_fit(downloaded: bytes, source_id: str) -> bytes:
    if _fit_bytes_ok(downloaded):
        return downloaded
    if not zipfile.is_zipfile(io.BytesIO(downloaded)):
        raise RuntimeError(f"Garmin download for {source_id} is neither FIT nor a valid ZIP archive")

    try:
        with zipfile.ZipFile(io.BytesIO(downloaded)) as archive:
            fit_members = [
                member
                for member in archive.infolist()
                if not member.is_dir() and Path(member.filename).suffix.lower() == ".fit"
            ]
            if not fit_members:
                raise RuntimeError(f"Garmin ZIP for {source_id} contains no FIT file")
            if len(fit_members) != 1:
                raise RuntimeError(f"Garmin ZIP for {source_id} contains multiple FIT files")
            fit_bytes = archive.read(fit_members[0])
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError(f"Garmin ZIP for {source_id} could not be read") from exc

    if not _fit_bytes_ok(fit_bytes):
        raise RuntimeError(f"Garmin ZIP for {source_id} contains an invalid FIT file")
    return fit_bytes


def _fit_bytes_ok(value: bytes) -> bool:
    return len(value) >= 100 and value[8:12] == b".FIT"


def _garmin_sport_type(item: dict[str, object]) -> str | None:
    activity_type = item.get("activityType")
    if isinstance(activity_type, dict):
        raw_type = activity_type.get("typeKey") or activity_type.get("typeName")
    else:
        raw_type = activity_type
    raw_type = raw_type or item.get("activityTypeKey") or item.get("sportType")
    if not isinstance(raw_type, str):
        return None

    normalized = raw_type.strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "run": "running",
        "trail_running": "running",
        "treadmill_running": "running",
        "road_biking": "cycling",
        "mountain_biking": "cycling",
        "gravel_cycling": "cycling",
        "indoor_cycling": "cycling",
        "lap_swimming": "swimming",
        "open_water_swimming": "swimming",
        "walking": "walking",
        "hiking": "hiking",
        "strength_training": "strength_training",
        "fitness_equipment": "strength_training",
    }
    return aliases.get(normalized, normalized or None)


def _activity_sort_key(activity: Activity) -> tuple[datetime, str]:
    return (activity.start_time or datetime(1970, 1, 1, tzinfo=timezone.utc), activity.source_id)
