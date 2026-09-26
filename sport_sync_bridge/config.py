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
    wahoo_client_id: str | None
    wahoo_client_secret: str | None
    wahoo_redirect_uri: str
    wahoo_access_token: str | None
    wahoo_refresh_token: str | None
    wahoo_expires_at: str | None
    wahoo_scope: str | None
    concept2_client_id: str | None
    concept2_client_secret: str | None
    concept2_api_root: str
    concept2_allow_production_writes: bool
    concept2_redirect_uri: str
    concept2_access_token: str | None
    concept2_refresh_token: str | None
    concept2_expires_at: str | None
    concept2_scope: str | None
    hammerhead_client_id: str | None
    hammerhead_client_secret: str | None
    hammerhead_redirect_uri: str
    hammerhead_access_token: str | None
    hammerhead_refresh_token: str | None
    hammerhead_expires_at: str | None
    hammerhead_scope: str
    polar_client_id: str | None
    polar_client_secret: str | None
    polar_redirect_uri: str | None
    polar_access_token: str | None
    polar_member_id: str | None
    google_health_client_id: str | None
    google_health_client_secret: str | None
    google_health_redirect_uri: str
    withings_client_id: str | None
    withings_client_secret: str | None
    withings_redirect_uri: str
    coros_mcp_url: str
    coros_mcp_timezone: str | None
    smashrun_access_token: str | None
    ridewithgps_client_id: str | None
    ridewithgps_client_secret: str | None
    ridewithgps_redirect_uri: str
    ridewithgps_access_token: str | None
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
            wahoo_client_id=env_or_none("WAHOO_CLIENT_ID"),
            wahoo_client_secret=env_or_none("WAHOO_CLIENT_SECRET"),
            wahoo_redirect_uri=os.getenv("WAHOO_REDIRECT_URI", "http://localhost/").strip(),
            wahoo_access_token=env_or_none("WAHOO_ACCESS_TOKEN"),
            wahoo_refresh_token=env_or_none("WAHOO_REFRESH_TOKEN"),
            wahoo_expires_at=env_or_none("WAHOO_EXPIRES_AT"),
            wahoo_scope=env_or_none("WAHOO_SCOPE"),
            concept2_client_id=env_or_none("CONCEPT2_CLIENT_ID"),
            concept2_client_secret=env_or_none("CONCEPT2_CLIENT_SECRET"),
            concept2_api_root=os.getenv("CONCEPT2_API_ROOT", "https://log.concept2.com").rstrip("/"),
            concept2_allow_production_writes=parse_bool(
                os.getenv("CONCEPT2_ALLOW_PRODUCTION_WRITES"), False
            ),
            concept2_redirect_uri=os.getenv("CONCEPT2_REDIRECT_URI", "http://localhost/").strip(),
            concept2_access_token=env_or_none("CONCEPT2_ACCESS_TOKEN"),
            concept2_refresh_token=env_or_none("CONCEPT2_REFRESH_TOKEN"),
            concept2_expires_at=env_or_none("CONCEPT2_EXPIRES_AT"),
            concept2_scope=env_or_none("CONCEPT2_SCOPE"),
            hammerhead_client_id=env_or_none("HAMMERHEAD_CLIENT_ID"),
            hammerhead_client_secret=env_or_none("HAMMERHEAD_CLIENT_SECRET"),
            hammerhead_redirect_uri=os.getenv("HAMMERHEAD_REDIRECT_URI", "http://localhost/").strip(),
            hammerhead_access_token=env_or_none("HAMMERHEAD_ACCESS_TOKEN"),
            hammerhead_refresh_token=env_or_none("HAMMERHEAD_REFRESH_TOKEN"),
            hammerhead_expires_at=env_or_none("HAMMERHEAD_EXPIRES_AT"),
            hammerhead_scope=os.getenv(
                "HAMMERHEAD_SCOPE", "activity:read route:read route:write"
            ).strip(),
            polar_client_id=env_or_none("POLAR_CLIENT_ID"),
            polar_client_secret=env_or_none("POLAR_CLIENT_SECRET"),
            polar_redirect_uri=env_or_none("POLAR_REDIRECT_URI"),
            polar_access_token=env_or_none("POLAR_ACCESS_TOKEN"),
            polar_member_id=env_or_none("POLAR_MEMBER_ID"),
            google_health_client_id=env_or_none("GOOGLE_HEALTH_CLIENT_ID"),
            google_health_client_secret=env_or_none("GOOGLE_HEALTH_CLIENT_SECRET"),
            google_health_redirect_uri=os.getenv(
                "GOOGLE_HEALTH_REDIRECT_URI", "https://www.google.com"
            ).strip(),
            withings_client_id=env_or_none("WITHINGS_CLIENT_ID"),
            withings_client_secret=env_or_none("WITHINGS_CLIENT_SECRET"),
            withings_redirect_uri=os.getenv("WITHINGS_REDIRECT_URI", "http://localhost/").strip(),
            coros_mcp_url=os.getenv("COROS_MCP_URL", "https://mcp.coros.com/mcp").strip(),
            coros_mcp_timezone=env_or_none("COROS_TIMEZONE"),
            smashrun_access_token=env_or_none("SMASHRUN_ACCESS_TOKEN"),
            ridewithgps_client_id=env_or_none("RIDEWITHGPS_CLIENT_ID"),
            ridewithgps_client_secret=env_or_none("RIDEWITHGPS_CLIENT_SECRET"),
            ridewithgps_redirect_uri=os.getenv("RIDEWITHGPS_REDIRECT_URI", "http://localhost/").strip(),
            ridewithgps_access_token=env_or_none("RIDEWITHGPS_ACCESS_TOKEN"),
            ai_api_base_url=env_or_none("AI_API_BASE_URL"),
            ai_api_key=env_or_none("AI_API_KEY"),
            ai_model=env_or_none("AI_MODEL"),
        )
