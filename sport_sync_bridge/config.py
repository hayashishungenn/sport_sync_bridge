from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .coordinate_rules import CoordinateRule, load_coordinate_rules
from .utils import env_or_none, parse_bool, parse_csv


@dataclass(slots=True)
class AppConfig:
    root_dir: Path
    data_dir: Path
    downloads_dir: Path
    repaired_dir: Path
    converted_dir: Path
    coordinate_rules_path: Path
    coordinate_rules: tuple[CoordinateRule, ...]
    db_path: Path
    log_path: Path
    sources: list[str]
    targets: list[str]
    lookback_days: int
    poll_interval_seconds: int
    log_level: str
    igpsport_username: str | None
    igpsport_password: str | None
    igpsport_access_token: str | None
    igpsport_coord_mode: str
    igpsport_coord_strict: bool
    onelap_username: str | None
    onelap_password: str | None
    onelap_cookie: str | None
    onelap_coord_mode: str
    onelap_coord_strict: bool
    intervals_icu_athlete_id: str | None
    intervals_icu_api_key: str | None
    garmin_email: str | None
    garmin_password: str | None
    garmin_session_b64: str | None
    strava_client_id: str | None
    strava_client_secret: str | None
    strava_redirect_uri: str
    strava_refresh_token: str | None
    strava_access_token: str | None
    strava_expires_at: str | None
    strava_scope: str | None
    ai_api_base_url: str | None
    ai_api_key: str | None
    ai_model: str | None

    @classmethod
    def load(cls, root_dir: Path) -> "AppConfig":
        load_dotenv(root_dir / ".env", override=False)

        data_dir = root_dir / os.getenv("SYNC_DATA_DIR", ".data")
        downloads_dir = data_dir / "downloads"
        repaired_dir = data_dir / "repaired"
        converted_dir = data_dir / "converted"
        coordinate_rules_path = Path(
            os.getenv("FIT_COORDINATE_RULES_FILE", "device_coordinate_rules.json")
        ).expanduser()
        if not coordinate_rules_path.is_absolute():
            coordinate_rules_path = root_dir / coordinate_rules_path

        return cls(
            root_dir=root_dir,
            data_dir=data_dir,
            downloads_dir=downloads_dir,
            repaired_dir=repaired_dir,
            converted_dir=converted_dir,
            coordinate_rules_path=coordinate_rules_path,
            coordinate_rules=load_coordinate_rules(coordinate_rules_path),
            db_path=data_dir / "sync_state.db",
            log_path=data_dir / "sync.log",
            sources=parse_csv(os.getenv("SYNC_SOURCES"), ["igpsport", "onelap"]),
            targets=parse_csv(os.getenv("SYNC_TARGETS"), ["garmin", "strava"]),
            lookback_days=int(os.getenv("SYNC_LOOKBACK_DAYS", "30")),
            poll_interval_seconds=int(os.getenv("SYNC_POLL_INTERVAL_SECONDS", "900")),
            log_level=os.getenv("SYNC_LOG_LEVEL", "INFO"),
            igpsport_username=env_or_none("IGPSPORT_USERNAME"),
            igpsport_password=env_or_none("IGPSPORT_PASSWORD"),
            igpsport_access_token=env_or_none("IGPSPORT_ACCESS_TOKEN"),
            igpsport_coord_mode=os.getenv("IGPSPORT_COORD_MODE", "gcj02_to_wgs84").strip().lower(),
            igpsport_coord_strict=parse_bool(os.getenv("IGPSPORT_COORD_STRICT"), False),
            onelap_username=env_or_none("ONELAP_USERNAME"),
            onelap_password=env_or_none("ONELAP_PASSWORD"),
            onelap_cookie=env_or_none("ONELAP_COOKIE"),
            onelap_coord_mode=os.getenv("ONELAP_COORD_MODE", "gcj02_to_wgs84").strip().lower(),
            onelap_coord_strict=parse_bool(os.getenv("ONELAP_COORD_STRICT"), False),
            intervals_icu_athlete_id=env_or_none("INTERVALS_ICU_ATHLETE_ID"),
            intervals_icu_api_key=env_or_none("INTERVALS_ICU_API_KEY"),
            garmin_email=env_or_none("GARMIN_EMAIL"),
            garmin_password=env_or_none("GARMIN_PASSWORD"),
            garmin_session_b64=env_or_none("GARMIN_SESSION_B64"),
            strava_client_id=env_or_none("STRAVA_CLIENT_ID"),
            strava_client_secret=env_or_none("STRAVA_CLIENT_SECRET"),
            strava_redirect_uri=os.getenv("STRAVA_REDIRECT_URI", "http://localhost/exchange_token").strip(),
            strava_refresh_token=env_or_none("STRAVA_REFRESH_TOKEN"),
            strava_access_token=env_or_none("STRAVA_ACCESS_TOKEN"),
            strava_expires_at=env_or_none("STRAVA_EXPIRES_AT"),
            strava_scope=env_or_none("STRAVA_SCOPE"),
            ai_api_base_url=env_or_none("AI_API_BASE_URL"),
            ai_api_key=env_or_none("AI_API_KEY"),
            ai_model=env_or_none("AI_MODEL"),
        )
