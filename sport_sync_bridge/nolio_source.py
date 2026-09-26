from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

from .config import AppConfig
from .formats import convert_activity_file
from .models import Activity
from .nolio_api import NolioClient, normalize_athlete_id
from .sources import SourceAdapter
from .utils import fit_signature_ok, safe_filename


LOGGER = logging.getLogger(__name__)

_SPORT_NAMES = {
    "running": "running",
    "run": "running",
    "trail run": "running",
    "trail running": "running",
    "bike": "cycling",
    "cycling": "cycling",
    "road cycling": "cycling",
    "mountain cycling": "cycling",
    "mountain biking": "cycling",
    "track cycling": "cycling",
    "cx cycling": "cycling",
    "virtual ride": "cycling",
    "swim": "swimming",
    "swimming": "swimming",
    "walk": "walking",
    "walking": "walking",
    "hiking": "hiking",
}
_SPORT_IDS = {
    2: "running",
    14: "cycling",
    15: "cycling",
    16: "hiking",
    18: "cycling",
    19: "swimming",
    35: "cycling",
    36: "cycling",
    45: "walking",
}


class NolioSource(SourceAdapter):
    name = "nolio"
    default_limit = 30

    def __init__(self, config: AppConfig, client: NolioClient):
        super().__init__(config)
        self.client = client

    def is_configured(self) -> bool:
        return self.client.is_configured()

    def authenticate(self) -> None:
        self.client.authenticate()

    def list_activities(
        self,
        since: datetime | None,
        until: datetime | None,
        limit: int | None,
    ) -> list[Activity]:
        if limit is not None and limit <= 0:
            return []

        requested_limit = self.default_limit if limit is None else limit
        params: dict[str, object] = {"limit": requested_limit}
        if since is not None:
            params["from"] = since.date().isoformat()
        if until is not None:
            params["to"] = until.date().isoformat()
        athlete_id = normalize_athlete_id(getattr(self.config, "nolio_athlete_id", None))
        if athlete_id:
            params["athlete_id"] = athlete_id

        payload = self.client.get_json("/get/training/", params=params)
        if not isinstance(payload, list):
            raise RuntimeError("Nolio workout list response must be a JSON array")

        activities: list[Activity] = []
        for index, item in enumerate(payload):
            if not isinstance(item, Mapping):
                raise RuntimeError(f"Nolio workout {index} must be a JSON object")
            activity = _activity_from_training(item, index)
            if since is not None and activity.start_time and activity.start_time.date() < since.date():
                continue
            if until is not None and activity.start_time and activity.start_time.date() > until.date():
                continue
            activities.append(activity)

        activities.sort(key=lambda item: (item.start_time or datetime.min.replace(tzinfo=timezone.utc), item.source_id))
        return activities[:limit] if limit is not None else activities

    def download_fit(self, activity: Activity, output_dir: Path) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"nolio-{safe_filename(activity.source_id)}.fit"
        if output_path.is_file() and fit_signature_ok(output_path):
            return output_path

        info = self.client.get_json("/get/training/info/", params={"id": activity.source_id})
        if not isinstance(info, Mapping):
            raise RuntimeError("Nolio workout detail response must be a JSON object")
        if isinstance(info.get("training"), Mapping):
            info = info["training"]
        file_url = info.get("file_url")
        if not isinstance(file_url, str) or not file_url.strip():
            raise RuntimeError(f"Nolio workout {activity.source_id} has no downloadable activity file")

        parsed_url = urlparse(file_url.strip())
        if parsed_url.scheme.lower() != "https" or not parsed_url.hostname or parsed_url.username or parsed_url.password:
            raise RuntimeError("Nolio activity file URL must be a credential-free HTTPS URL")

        try:
            response = self.session.get(file_url.strip(), timeout=60)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(f"Nolio activity file download failed with HTTP {getattr(exc.response, 'status_code', 'error')}") from exc

        payload = response.content
        file_format = _download_format(parsed_url.path, payload)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=".nolio-download-",
                suffix=f".{file_format}",
                dir=output_dir,
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                handle.write(payload)

            if file_format == "fit":
                if not fit_signature_ok(temporary_path):
                    raise RuntimeError("Nolio returned a file with an invalid FIT signature")
                os.replace(temporary_path, output_path)
                temporary_path = None
            else:
                conversion = convert_activity_file(
                    temporary_path,
                    output_path,
                    "fit",
                    activity_name=activity.name,
                    sport_type=activity.sport_type,
                )
                for loss in conversion.losses:
                    LOGGER.warning("Nolio TCX-to-FIT conversion loss for %s: %s", activity.source_id, loss)
                if not fit_signature_ok(output_path):
                    raise RuntimeError("Nolio TCX conversion did not produce a valid FIT file")
            return output_path
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def _activity_from_training(item: Mapping[str, object], index: int) -> Activity:
    raw_id = item.get("nolio_id")
    if raw_id in (None, ""):
        raise RuntimeError(f"Nolio workout {index} is missing nolio_id")
    source_id = str(raw_id).strip()
    if not source_id:
        raise RuntimeError(f"Nolio workout {index} has an empty nolio_id")

    name_value = item.get("name")
    name = name_value.strip() if isinstance(name_value, str) else ""
    if not name:
        name = f"Nolio-{source_id}"

    start_time = _date_marker(item.get("date_start"))
    sport_type = _sport_type(item.get("sport"), item.get("sport_id"))
    return Activity(
        source=NolioSource.name,
        source_id=source_id,
        name=name,
        sport_type=sport_type,
        start_time=start_time,
        raw=dict(item),
    )


def _date_marker(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise RuntimeError(f"Nolio workout has an invalid date_start value: {value}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _sport_type(name: object, sport_id: object) -> str | None:
    normalized_name = " ".join(str(name or "").strip().lower().replace("_", " ").split())
    if normalized_name in _SPORT_NAMES:
        return _SPORT_NAMES[normalized_name]
    try:
        normalized_id = int(sport_id) if sport_id is not None else None
    except (TypeError, ValueError):
        normalized_id = None
    return _SPORT_IDS.get(normalized_id)


def _download_format(url_path: str, payload: bytes) -> str:
    if len(payload) >= 12 and payload[8:12] == b".FIT":
        return "fit"
    if b"TrainingCenterDatabase" in payload[:8192]:
        return "tcx"
    suffix = Path(url_path).suffix.lower()
    if suffix in {".fit", ".tcx"}:
        return suffix[1:]
    raise RuntimeError("Nolio returned an unsupported activity file; expected FIT or TCX")
