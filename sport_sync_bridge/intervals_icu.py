from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import quote
from zipfile import BadZipFile, ZipFile

from .config import AppConfig
from .models import Activity
from .sources import SourceAdapter
from .utils import fit_signature_ok, parse_datetime, safe_filename


class IntervalsIcuSource(SourceAdapter):
    name = "intervals_icu"
    api_root = "https://intervals.icu/api/v1"

    def __init__(self, config: AppConfig):
        super().__init__(config)
        self._authenticated = False

    def is_configured(self) -> bool:
        return bool(self.config.intervals_icu_athlete_id and self.config.intervals_icu_api_key)

    def _auth(self) -> tuple[str, str]:
        athlete_id = self.config.intervals_icu_athlete_id
        api_key = self.config.intervals_icu_api_key
        if not athlete_id or not api_key:
            raise RuntimeError("Intervals.icu athlete ID and API key are not configured")
        return "API_KEY", api_key

    def _athlete_url(self, path: str) -> str:
        athlete_id = self.config.intervals_icu_athlete_id
        if not athlete_id:
            raise RuntimeError("Intervals.icu athlete ID is not configured")
        return f"{self.api_root}/athlete/{quote(athlete_id, safe='')}{path}"

    def authenticate(self) -> None:
        if self._authenticated:
            return
        auth = self._auth()
        response = self.session.get(
            self._athlete_url("/profile"),
            auth=auth,
            timeout=30,
        )
        response.raise_for_status()
        try:
            profile = response.json()
        except ValueError as exc:
            raise RuntimeError("Intervals.icu profile response is not valid JSON") from exc
        if not isinstance(profile, dict):
            raise RuntimeError("Intervals.icu profile response must be a JSON object")
        self._authenticated = True

    def list_activities(
        self,
        since: datetime | None,
        until: datetime | None,
        limit: int | None,
    ) -> list[Activity]:
        if limit is not None and limit <= 0:
            return []

        self.authenticate()
        params: dict[str, str | int] = {}
        if since is not None:
            params["oldest"] = since.strftime("%Y-%m-%dT%H:%M:%S")
        if until is not None:
            params["newest"] = until.strftime("%Y-%m-%dT%H:%M:%S")
        if limit is not None:
            params["limit"] = limit

        response = self.session.get(
            self._athlete_url("/activities"),
            params=params,
            auth=self._auth(),
            timeout=30,
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("Intervals.icu activity response is not valid JSON") from exc
        if not isinstance(payload, list):
            raise RuntimeError("Intervals.icu activity response must be a JSON list")

        lower_bound = parse_datetime(since)
        upper_bound = parse_datetime(until)
        activities: list[Activity] = []
        for index, item in enumerate(payload):
            if not isinstance(item, dict):
                raise RuntimeError(f"Intervals.icu activity {index} must be a JSON object")
            if str(item.get("source") or "").upper() == "STRAVA":
                continue

            raw_id = item.get("id")
            if raw_id in (None, ""):
                raise RuntimeError(f"Intervals.icu activity {index} is missing its ID")
            source_id = str(raw_id)
            start_time = parse_datetime(item.get("start_date") or item.get("start_date_local"))
            if lower_bound is not None and (start_time is None or start_time < lower_bound):
                continue
            if upper_bound is not None and (start_time is None or start_time > upper_bound):
                continue

            sport_type = _intervals_sport_type(item.get("type"))
            name = item.get("name")
            activities.append(
                Activity(
                    source=self.name,
                    source_id=source_id,
                    name=str(name) if name else f"{sport_type or 'activity'} {source_id}",
                    sport_type=sport_type,
                    start_time=start_time,
                    raw=item,
                )
            )
            if limit is not None and len(activities) >= limit:
                break

        return sorted(activities, key=_activity_sort_key)

    def download_fit(self, activity: Activity, output_dir: Path) -> Path:
        self.authenticate()
        activity_dir = output_dir / self.name
        activity_dir.mkdir(parents=True, exist_ok=True)
        path = activity_dir / f"{safe_filename(activity.source_id)}.fit"
        if path.is_file() and path.stat().st_size >= 100 and fit_signature_ok(path):
            return path

        response = self.session.post(
            self._athlete_url("/download-fit-files"),
            data={"ids": activity.source_id},
            auth=self._auth(),
            timeout=120,
        )
        response.raise_for_status()
        try:
            with ZipFile(BytesIO(response.content)) as archive:
                entries = [entry for entry in archive.infolist() if not entry.is_dir()]
                if not entries:
                    raise RuntimeError(
                        f"Empty ZIP file received from Intervals.icu for activity {activity.source_id}"
                    )
                fit_bytes = archive.read(entries[0])
        except BadZipFile as exc:
            raise RuntimeError(
                f"Invalid ZIP file received from Intervals.icu for activity {activity.source_id}"
            ) from exc

        if len(fit_bytes) < 100 or not _fit_signature_ok(fit_bytes):
            raise RuntimeError(f"Downloaded file is not a valid FIT for activity {activity.source_id}")
        path.write_bytes(fit_bytes)
        return path


def _intervals_sport_type(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    sport = value.strip().lower()
    mapping = {
        "ride": "cycling",
        "virtualride": "cycling",
        "ebikeride": "cycling",
        "mountainbikeride": "cycling",
        "emountainbikeride": "cycling",
        "gravelride": "cycling",
        "trackride": "cycling",
        "velomobile": "cycling",
        "run": "running",
        "virtualrun": "running",
        "trailrun": "running",
        "swim": "swimming",
        "openwaterswim": "swimming",
        "walk": "walking",
        "hike": "hiking",
        "alpineski": "skiing",
        "backcountryski": "skiing",
        "nordicski": "skiing",
        "rollerski": "skiing",
        "virtualski": "skiing",
        "canoeing": "canoeing",
        "kayaking": "kayaking",
        "crossfit": "strength_training",
        "weighttraining": "strength_training",
    }
    return mapping.get(sport)


def _activity_sort_key(activity: Activity) -> tuple[datetime, str]:
    return (activity.start_time or datetime(1970, 1, 1, tzinfo=timezone.utc), activity.source_id)


def _fit_signature_ok(data: bytes) -> bool:
    return len(data) >= 12 and data[8:12] == b".FIT"
