from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

from .config import AppConfig
from .fit_tools import normalize_fit_coordinates
from .models import FileBundle
from .sources import IGPSportSource, OneLapSource, SourceAdapter
from .state import StateDB
from .targets import GarminTarget, StravaTarget, TargetAdapter
from .utils import ensure_directory, sha1_file, utcnow


LOGGER = logging.getLogger(__name__)


class SyncEngine:
    def __init__(self, config: AppConfig):
        self.config = config
        ensure_directory(config.data_dir)
        ensure_directory(config.downloads_dir)
        ensure_directory(config.repaired_dir)
        self.state_db = StateDB(config.db_path)
        self.sources = self._build_sources()
        self.targets = self._build_targets()

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
    ) -> int:
        selected_sources = sources or self.config.sources
        selected_targets = targets or self.config.targets

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
                    if not self.state_db.is_target_done(activity.source, activity.source_id, target_name)
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
                    result = target.upload_file(
                        bundle.upload_path,
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

    def _prepare_files(self, source: SourceAdapter, activity) -> FileBundle:
        row = self.state_db.get_activity_row(activity.source, activity.source_id)
        original_path = None
        upload_path = None

        if row:
            if row["original_path"]:
                candidate = Path(row["original_path"])
                if candidate.exists():
                    original_path = candidate
            if row["upload_path"]:
                candidate = Path(row["upload_path"])
                if candidate.exists():
                    upload_path = candidate

        if original_path is None:
            original_path = source.download_fit(activity, self.config.downloads_dir)

        if upload_path is None:
            upload_path = original_path
            repaired_target = self.config.repaired_dir / activity.source / f"{original_path.stem}-wgs84.fit"
            coordinate_mode, strict_mode = self._get_coordinate_config(activity.source)
            try:
                upload_path, changed_pairs = normalize_fit_coordinates(
                    input_path=original_path,
                    output_path=repaired_target,
                    coordinate_mode=coordinate_mode,
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
                upload_path = original_path

        sha1 = sha1_file(upload_path)
        self.state_db.upsert_activity(
            activity=activity,
            original_path=str(original_path),
            upload_path=str(upload_path),
            sha1=sha1,
        )
        return FileBundle(original_path=original_path, upload_path=upload_path, sha1=sha1)

    def _build_sources(self) -> dict[str, SourceAdapter]:
        sources: dict[str, SourceAdapter] = {}
        for adapter in (IGPSportSource(self.config), OneLapSource(self.config)):
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

        return targets

    def _get_coordinate_config(self, source_name: str) -> tuple[str, bool]:
        if source_name == "igpsport":
            return self.config.igpsport_coord_mode, self.config.igpsport_coord_strict
        if source_name == "onelap":
            return self.config.onelap_coord_mode, self.config.onelap_coord_strict
        return "none", False
