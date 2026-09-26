from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

from .config import AppConfig
from .concept2_source import Concept2Source
from .coros_source import CorosMcpSource
from .cycling_analytics import CyclingAnalyticsClient, CyclingAnalyticsSource, CyclingAnalyticsTarget
from .fit_tools import normalize_fit_coordinates
from .formats import SUPPORTED_FORMATS, convert_activity_file
from .garmin_source import GarminSource
from .google_health import GoogleHealthClient, GoogleHealthSource
from .hammerhead_api import HammerheadClient
from .hammerhead_source import HammerheadSource
from .intervals_icu import IntervalsIcuSource
from .intervals_icu_target import IntervalsIcuTarget
from .models import FileBundle, UploadResult
from .mywhoosh_source import MyWhooshSource
from .nolio_api import NolioClient
from .nolio_source import NolioSource
from .nolio_target import NolioTarget
from .suunto_api import SuuntoClient
from .suunto_source import SuuntoSource
from .suunto_target import SuuntoTarget
from .polar_api import PolarClient
from .polar_source import PolarSource
from .ridewithgps_api import RideWithGPSClient
from .ridewithgps_source import RideWithGPSSource
from .ridewithgps_target import RideWithGPSTarget
from .sources import IGPSportSource, LocalFileSource, OneLapSource, SourceAdapter
from .smashrun_source import SmashrunSource
from .strava_source import StravaSource
from .state import StateDB
from .hammerhead_target import HammerheadTarget
from .targets import GarminTarget, StravaTarget, TargetAdapter
from .wahoo_target import WahooTarget
from .wahoo_source import WahooSource
from .withings_source import WithingsClient, WithingsSource
from .utils import ensure_directory, safe_filename, sha1_file, utcnow


LOGGER = logging.getLogger(__name__)


class SyncEngine:
    def __init__(self, config: AppConfig):
        self.config = config
        ensure_directory(config.data_dir)
        ensure_directory(config.downloads_dir)
        ensure_directory(config.repaired_dir)
        ensure_directory(config.converted_dir)
        self.state_db = StateDB(config.db_path)
        self.hammerhead_client = HammerheadClient(config, self.state_db)
        self.google_health_client = GoogleHealthClient(config, self.state_db)
        self.cycling_analytics_client = CyclingAnalyticsClient(config)
        self.polar_client = PolarClient(config, self.state_db)
        self.withings_client = WithingsClient(config, self.state_db)
        self.smashrun_source = SmashrunSource(config, self.state_db)
        self.ridewithgps_client = RideWithGPSClient(config, self.state_db)
        self.nolio_client = NolioClient(config, self.state_db)
        self.suunto_client = SuuntoClient(config, self.state_db)
        self.targets = self._build_targets()
        self.sources = self._build_sources()

    def close(self) -> None:
        self.state_db.close()

    def sync_once(
        self,
        sources: list[str] | None = None,
        targets: list[str] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        dry_run: bool = False,
        target_formats: dict[str, str] | None = None,
    ) -> int:
        selected_sources = sources or self.config.sources
        selected_targets = targets or self.config.targets
        target_formats = target_formats or {}
        invalid_formats = {
            target: value
            for target, value in target_formats.items()
            if target not in {
                "garmin",
                "strava",
                "wahoo",
                "hammerhead",
                "intervals_icu",
                "cycling_analytics",
                "nolio",
                "suunto",
            }
            or not isinstance(value, str)
            or value.lower() not in SUPPORTED_FORMATS
            or (target == "wahoo" and value.lower() != "fit")
            or (target == "nolio" and value.lower() not in {"fit", "tcx"})
            or (target == "suunto" and value.lower() != "fit")
        }
        if invalid_formats:
            details = ", ".join(f"{target}={value}" for target, value in sorted(invalid_formats.items()))
            raise ValueError(f"Unsupported target format mapping: {details}")

        if since is None and self.config.lookback_days > 0:
            since = utcnow() - timedelta(days=self.config.lookback_days)
        if until is None:
            until = utcnow()

        synced_count = 0
        for source_name in selected_sources:
            source = self.sources[source_name]
            LOGGER.info("Scanning source: %s", source_name)
            activities = source.list_activities(since=since, until=until, limit=limit)
            LOGGER.info("Found %s activities from %s", len(activities), source_name)

            for activity in activities:
                pending_targets = [
                    target_name
                    for target_name in selected_targets
                    if target_name != activity.source
                    and not self.state_db.is_target_done(activity.source, activity.source_id, target_name)
                ]
                if not pending_targets:
                    continue

                LOGGER.info(
                    "Processing %s/%s %s -> %s",
                    activity.source,
                    activity.source_id,
                    activity.name,
                    ",".join(pending_targets),
                )

                if dry_run:
                    continue

                bundle = self._prepare_files(source, activity)
                for target_name in pending_targets:
                    target = self.targets[target_name]
                    target_format = target_formats.get(target_name, "fit").lower()
                    try:
                        upload_path, losses = self._prepare_target_file(
                            bundle.upload_path,
                            activity,
                            target_name,
                            target_format,
                        )
                    except Exception as exc:
                        result = UploadResult(status="failed", message=f"Conversion to {target_format} failed: {exc}")
                    else:
                        for loss in losses:
                            LOGGER.warning(
                                "Conversion loss %s/%s for %s: %s",
                                activity.source,
                                activity.source_id,
                                target_name,
                                loss,
                            )
                        result = target.upload_file(
                            upload_path,
                            activity,
                            external_id=f"{activity.source}:{activity.source_id}",
                        )
                    LOGGER.info(
                        "Target result %s/%s -> %s: %s %s",
                        activity.source,
                        activity.source_id,
                        target_name,
                        result.status,
                        result.message or "",
                    )
                    self.state_db.record_target_result(
                        source=activity.source,
                        source_id=activity.source_id,
                        target=target_name,
                        status=result.status,
                        remote_id=result.remote_id,
                        message=result.message,
                    )
                    if result.status in {"success", "duplicate"}:
                        synced_count += 1
        return synced_count

    def loop_forever(
        self,
        sources: list[str] | None = None,
        targets: list[str] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        dry_run: bool = False,
        interval_seconds: int | None = None,
        target_formats: dict[str, str] | None = None,
    ) -> None:
        interval = interval_seconds or self.config.poll_interval_seconds
        while True:
            try:
                self.sync_once(
                    sources=sources,
                    targets=targets,
                    since=since,
                    until=until,
                    limit=limit,
                    dry_run=dry_run,
                    target_formats=target_formats,
                )
            except Exception:
                LOGGER.exception("Sync loop iteration failed")
            LOGGER.info("Sleeping for %s seconds", interval)
            time.sleep(interval)

    def status(self) -> dict[str, int]:
        return self.state_db.stats()

    def check_connections(
        self,
        sources: list[str] | None = None,
        targets: list[str] | None = None,
    ) -> list[tuple[str, str, str]]:
        selected_sources = sources or self.config.sources
        selected_targets = targets or self.config.targets
        results: list[tuple[str, str, str]] = []

        for source_name in selected_sources:
            source = self.sources[source_name]
            try:
                source.authenticate()
                source.list_activities(since=None, until=None, limit=1)
                results.append(("source", source_name, "ok"))
            except Exception as exc:
                results.append(("source", source_name, f"failed: {exc}"))

        for target_name in selected_targets:
            target = self.targets[target_name]
            try:
                target.authenticate()
                results.append(("target", target_name, "ok"))
            except Exception as exc:
                results.append(("target", target_name, f"failed: {exc}"))

        return results

    def get_strava_target(self) -> StravaTarget:
        target = self.targets.get("strava")
        if target is None:
            raise RuntimeError("Strava target is not configured")
        return cast(StravaTarget, target)

    def get_wahoo_target(self) -> WahooTarget:
        target = self.targets.get("wahoo")
        if target is None:
            raise RuntimeError("Wahoo target is not configured")
        return cast(WahooTarget, target)

    def get_concept2_source(self) -> Concept2Source:
        source = self.sources.get("concept2")
        if source is None:
            raise RuntimeError("Concept2 source is not configured")
        return cast(Concept2Source, source)

    def get_coros_source(self) -> CorosMcpSource:
        source = self.sources.get("coros")
        if source is None:
            raise RuntimeError("COROS source is not configured")
        return cast(CorosMcpSource, source)

    def get_hammerhead_source(self) -> HammerheadSource:
        source = self.sources.get("hammerhead")
        if source is None:
            raise RuntimeError("Hammerhead source is not configured")
        return cast(HammerheadSource, source)

    def get_hammerhead_target(self) -> HammerheadTarget:
        target = self.targets.get("hammerhead")
        if target is None:
            raise RuntimeError("Hammerhead target is not configured")
        return cast(HammerheadTarget, target)

    def get_cycling_analytics_target(self) -> CyclingAnalyticsTarget:
        target = self.targets.get("cycling_analytics")
        if target is None:
            raise RuntimeError("Cycling Analytics target is not configured")
        return cast(CyclingAnalyticsTarget, target)

    def _prepare_files(self, source: SourceAdapter, activity) -> FileBundle:
        row = self.state_db.get_activity_row(activity.source, activity.source_id)
        original_path = None

        if row:
            if row["original_path"]:
                candidate = Path(row["original_path"])
                if candidate.exists():
                    original_path = candidate
        if original_path is None:
            original_path = source.download_fit(activity, self.config.downloads_dir)

        repaired_target = self.config.repaired_dir / activity.source / f"{safe_filename(original_path.stem)}-wgs84.fit"
        coordinate_mode, strict_mode = self._get_coordinate_config(activity.source)
        upload_path = original_path
        if original_path.suffix.lower() == ".fit":
            try:
                upload_path, changed_pairs = normalize_fit_coordinates(
                    input_path=original_path,
                    output_path=repaired_target,
                    coordinate_mode=coordinate_mode,
                    coordinate_rules=self.config.coordinate_rules,
                )
                if changed_pairs:
                    LOGGER.info(
                        "Repaired %s coordinate pairs for %s/%s",
                        changed_pairs,
                        activity.source,
                        activity.source_id,
                    )
            except Exception as exc:
                if strict_mode:
                    raise
                LOGGER.warning("FIT coordinate repair skipped for %s/%s: %s", activity.source, activity.source_id, exc)

        sha1 = sha1_file(upload_path)
        self.state_db.upsert_activity(
            activity=activity,
            original_path=str(original_path),
            upload_path=str(upload_path),
            sha1=sha1,
        )
        return FileBundle(original_path=original_path, upload_path=upload_path, sha1=sha1)

    def _prepare_target_file(
        self,
        corrected_fit_path: Path,
        activity,
        target_name: str,
        target_format: str,
    ) -> tuple[Path, tuple[str, ...]]:
        output_path = (
            self.config.converted_dir
            / activity.source
            / target_name
            / f"{safe_filename(activity.source_id)}.{target_format}"
        )
        result = convert_activity_file(
            corrected_fit_path,
            output_path,
            target_format,
            activity_name=activity.name,
            sport_type=activity.sport_type,
        )
        return result.output_path, result.losses

    def _build_sources(self) -> dict[str, SourceAdapter]:
        sources: dict[str, SourceAdapter] = {}
        adapters = (
            IGPSportSource(self.config),
            OneLapSource(self.config),
            IntervalsIcuSource(self.config),
            Concept2Source(self.config, self.state_db),
            CorosMcpSource(self.config, self.state_db),
            self.smashrun_source,
            HammerheadSource(self.config, self.hammerhead_client),
            PolarSource(self.config, self.polar_client),
            GoogleHealthSource(self.config, self.google_health_client),
            WithingsSource(self.config, self.withings_client),
            RideWithGPSSource(self.config, self.ridewithgps_client),
            NolioSource(self.config, self.nolio_client),
            SuuntoSource(self.config, self.suunto_client),
            CyclingAnalyticsSource(self.config, self.cycling_analytics_client),
            MyWhooshSource(self.config, self.state_db),
            LocalFileSource(self.config, self.state_db),
        )
        garmin_target = self.targets.get("garmin")
        if isinstance(garmin_target, GarminTarget):
            adapters += (GarminSource(self.config, garmin_target),)
        strava_target = self.targets.get("strava")
        if isinstance(strava_target, StravaTarget):
            adapters += (StravaSource(self.config, strava_target),)
        wahoo_target = self.targets.get("wahoo")
        if isinstance(wahoo_target, WahooTarget):
            adapters += (WahooSource(self.config, wahoo_target),)
        for adapter in adapters:
            if adapter.is_configured():
                sources[adapter.name] = adapter
        return sources

    def _build_targets(self) -> dict[str, TargetAdapter]:
        targets: dict[str, TargetAdapter] = {}
        garmin = GarminTarget(self.config)
        if garmin.is_configured():
            targets[garmin.name] = garmin

        strava = StravaTarget(self.config, self.state_db)
        if strava.is_configured():
            targets[strava.name] = strava

        intervals_icu = IntervalsIcuTarget(self.config)
        if intervals_icu.is_configured():
            targets[intervals_icu.name] = intervals_icu

        wahoo = WahooTarget(self.config, self.state_db)
        if wahoo.is_configured():
            targets[wahoo.name] = wahoo

        hammerhead = HammerheadTarget(self.config, self.hammerhead_client)
        if hammerhead.is_configured():
            targets[hammerhead.name] = hammerhead

        ridewithgps = RideWithGPSTarget(self.ridewithgps_client)
        if ridewithgps.is_configured():
            targets[ridewithgps.name] = ridewithgps

        nolio = NolioTarget(self.nolio_client)
        if nolio.is_configured():
            targets[nolio.name] = nolio

        suunto = SuuntoTarget(self.suunto_client)
        if suunto.is_configured():
            targets[suunto.name] = suunto

        cycling_analytics = CyclingAnalyticsTarget(self.cycling_analytics_client)
        if cycling_analytics.is_configured():
            targets[cycling_analytics.name] = cycling_analytics

        return targets

    def _get_coordinate_config(self, source_name: str) -> tuple[str, bool]:
        if source_name == "igpsport":
            return self.config.igpsport_coord_mode, self.config.igpsport_coord_strict
        if source_name == "onelap":
            return self.config.onelap_coord_mode, self.config.onelap_coord_strict
        return "none", False
