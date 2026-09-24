from __future__ import annotations

import json
import re
import sqlite3
import uuid
from pathlib import Path

from .models import Activity
from .utils import ensure_directory, utcnow


class StateDB:
    def __init__(self, path: Path):
        ensure_directory(path.parent)
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        cursor = self.connection.cursor()
        cursor.executescript(
            """
            CREATE TABLE IF NOT EXISTS activities (
                source TEXT NOT NULL,
                source_id TEXT NOT NULL,
                name TEXT NOT NULL,
                sport_type TEXT,
                start_time TEXT,
                original_path TEXT,
                upload_path TEXT,
                sha1 TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (source, source_id)
            );

            CREATE TABLE IF NOT EXISTS sync_status (
                source TEXT NOT NULL,
                source_id TEXT NOT NULL,
                target TEXT NOT NULL,
                status TEXT NOT NULL,
                remote_id TEXT,
                message TEXT,
                synced_at TEXT NOT NULL,
                PRIMARY KEY (source, source_id, target)
            );

            CREATE TABLE IF NOT EXISTS kv_store (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS local_activities (
                fingerprint TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                sport_type TEXT,
                start_time TEXT,
                file_path TEXT NOT NULL,
                source_label TEXT NOT NULL,
                file_format TEXT NOT NULL,
                summary_json TEXT NOT NULL,
                imported_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS health_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                observed_at TEXT NOT NULL,
                metric TEXT NOT NULL,
                value REAL NOT NULL,
                unit TEXT NOT NULL,
                source_label TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                UNIQUE(fingerprint, metric, observed_at)
            );
            CREATE INDEX IF NOT EXISTS idx_health_metric_time
                ON health_observations(metric, observed_at);

            CREATE TABLE IF NOT EXISTS ai_analysis (
                id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                activity_id TEXT NOT NULL,
                model_name TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ai_analysis_activity
                ON ai_analysis(activity_id);

            CREATE TABLE IF NOT EXISTS training_plans (
                plan_id TEXT PRIMARY KEY,
                template_id TEXT NOT NULL,
                name TEXT NOT NULL,
                sport_type TEXT,
                locale TEXT NOT NULL,
                start_date TEXT NOT NULL,
                template_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS schedule_items (
                item_id TEXT PRIMARY KEY,
                training_plan_id TEXT NOT NULL,
                scheduled_date TEXT NOT NULL,
                item_type TEXT NOT NULL,
                name TEXT NOT NULL,
                sport_type TEXT,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(training_plan_id) REFERENCES training_plans(plan_id)
                    ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_schedule_plan_date
                ON schedule_items(training_plan_id, scheduled_date);
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def upsert_activity(
        self,
        activity: Activity,
        original_path: str,
        upload_path: str,
        sha1: str,
    ) -> None:
        now = utcnow().isoformat()
        self.connection.execute(
            """
            INSERT INTO activities (
                source, source_id, name, sport_type, start_time,
                original_path, upload_path, sha1, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, source_id) DO UPDATE SET
                name = excluded.name,
                sport_type = excluded.sport_type,
                start_time = excluded.start_time,
                original_path = excluded.original_path,
                upload_path = excluded.upload_path,
                sha1 = excluded.sha1,
                updated_at = excluded.updated_at
            """,
            (
                activity.source,
                activity.source_id,
                activity.name,
                activity.sport_type,
                activity.start_time.isoformat() if activity.start_time else None,
                original_path,
                upload_path,
                sha1,
                now,
                now,
            ),
        )
        self.connection.commit()

    def get_activity_row(self, source: str, source_id: str) -> sqlite3.Row | None:
        cursor = self.connection.execute(
            "SELECT * FROM activities WHERE source = ? AND source_id = ?",
            (source, source_id),
        )
        return cursor.fetchone()

    def is_target_done(self, source: str, source_id: str, target: str) -> bool:
        cursor = self.connection.execute(
            """
            SELECT status
            FROM sync_status
            WHERE source = ? AND source_id = ? AND target = ?
            """,
            (source, source_id, target),
        )
        row = cursor.fetchone()
        if not row:
            return False
        return row["status"] in {"success", "duplicate"}

    def record_target_result(
        self,
        source: str,
        source_id: str,
        target: str,
        status: str,
        remote_id: str | None,
        message: str | None,
    ) -> None:
        now = utcnow().isoformat()
        self.connection.execute(
            """
            INSERT INTO sync_status (source, source_id, target, status, remote_id, message, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, source_id, target) DO UPDATE SET
                status = excluded.status,
                remote_id = excluded.remote_id,
                message = excluded.message,
                synced_at = excluded.synced_at
            """,
            (source, source_id, target, status, remote_id, message, now),
        )
        self.connection.commit()

    def get_value(self, key: str) -> str | None:
        cursor = self.connection.execute("SELECT value FROM kv_store WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row["value"] if row else None

    def set_value(self, key: str, value: str) -> None:
        now = utcnow().isoformat()
        self.connection.execute(
            """
            INSERT INTO kv_store (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value, now),
        )
        self.connection.commit()

    def stats(self) -> dict[str, int]:
        counts = {
            "activities": 0,
            "success": 0,
            "duplicate": 0,
            "failed": 0,
        }
        row = self.connection.execute("SELECT COUNT(*) AS cnt FROM activities").fetchone()
        if row:
            counts["activities"] = row["cnt"]

        for status in ("success", "duplicate", "failed"):
            row = self.connection.execute(
                "SELECT COUNT(*) AS cnt FROM sync_status WHERE status = ?",
                (status,),
            ).fetchone()
            if row:
                counts[status] = row["cnt"]
        return counts

    def upsert_local_activity(
        self,
        *,
        fingerprint: str,
        name: str,
        sport_type: str | None,
        start_time: str | None,
        file_path: str,
        source_label: str,
        file_format: str,
        summary: dict[str, object],
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO local_activities (
                fingerprint, name, sport_type, start_time, file_path,
                source_label, file_format, summary_json, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                name = excluded.name,
                sport_type = excluded.sport_type,
                start_time = excluded.start_time,
                file_path = excluded.file_path,
                source_label = excluded.source_label,
                file_format = excluded.file_format,
                summary_json = excluded.summary_json
            """,
            (
                fingerprint,
                name,
                sport_type,
                start_time,
                file_path,
                source_label,
                file_format,
                json.dumps(summary, ensure_ascii=False, sort_keys=True),
                utcnow().isoformat(),
            ),
        )
        self.connection.commit()

    def get_local_activity(self, identifier: str) -> sqlite3.Row | None:
        if not re.fullmatch(r"[0-9a-fA-F]{1,64}", identifier):
            raise ValueError("Activity ID must be a hexadecimal fingerprint or prefix")
        rows = self.connection.execute(
            "SELECT * FROM local_activities WHERE substr(fingerprint, 1, ?) = ? ORDER BY fingerprint LIMIT 2",
            (len(identifier), identifier.lower()),
        ).fetchall()
        if len(rows) > 1:
            raise ValueError(f"Activity ID prefix is ambiguous: {identifier}")
        return rows[0] if rows else None

    def list_local_activities(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
        sport_type: str | None = None,
        limit: int | None = None,
        syncable_only: bool = False,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        values: list[object] = []
        if since is not None:
            clauses.append("start_time >= ?")
            values.append(since)
        if until is not None:
            clauses.append("start_time <= ?")
            values.append(until)
        if sport_type is not None:
            clauses.append("lower(sport_type) = lower(?)")
            values.append(sport_type)
        if syncable_only:
            clauses.append("file_format IN ('fit', 'gpx', 'tcx')")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit_sql = " LIMIT ?" if limit is not None else ""
        if limit is not None:
            if limit < 0:
                raise ValueError("Activity list limit cannot be negative")
            values.append(limit)
        return self.connection.execute(
            f"SELECT * FROM local_activities {where} ORDER BY start_time DESC, imported_at DESC{limit_sql}",
            values,
        ).fetchall()

    def upsert_health_observation(
        self,
        *,
        observed_at: str,
        metric: str,
        value: float,
        unit: str,
        source_label: str,
        fingerprint: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO health_observations (
                observed_at, metric, value, unit, source_label, fingerprint
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (observed_at, metric, value, unit, source_label, fingerprint),
        )
        self.connection.commit()

    def list_health_observations(self, metric: str | None = None) -> list[sqlite3.Row]:
        if metric is None:
            return self.connection.execute(
                "SELECT * FROM health_observations ORDER BY observed_at DESC, metric"
            ).fetchall()
        return self.connection.execute(
            "SELECT * FROM health_observations WHERE metric = ? ORDER BY observed_at DESC",
            (metric,),
        ).fetchall()

    def save_ai_analysis_result(
        self,
        *,
        activity_id: str,
        model_name: str,
        content: str,
        metadata: dict[str, object] | None = None,
    ) -> str:
        if not activity_id or not model_name.strip() or not content.strip():
            raise ValueError("AI analysis activity, model, and content must be non-empty")
        result_id = uuid.uuid4().hex
        now = utcnow().isoformat()
        self.connection.execute(
            """
            INSERT INTO ai_analysis (
                id, source_id, activity_id, model_name, content,
                created_at, updated_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result_id,
                "local",
                activity_id,
                model_name,
                content,
                now,
                now,
                json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
            ),
        )
        self.connection.commit()
        return result_id

    def list_ai_analysis_results(self, activity_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM ai_analysis WHERE activity_id = ? ORDER BY created_at DESC, id DESC",
            (activity_id,),
        ).fetchall()

    def save_training_plan(
        self,
        *,
        plan_id: str,
        template_id: str,
        name: str,
        sport_type: str | None,
        locale: str,
        start_date: str,
        template: dict[str, object],
        schedule_items: list[dict[str, object]],
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO training_plans (
                    plan_id, template_id, name, sport_type, locale,
                    start_date, template_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    template_id,
                    name,
                    sport_type,
                    locale,
                    start_date,
                    json.dumps(template, ensure_ascii=False, sort_keys=True),
                    utcnow().isoformat(),
                ),
            )
            self.connection.executemany(
                """
                INSERT INTO schedule_items (
                    item_id, training_plan_id, scheduled_date, item_type,
                    name, sport_type, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        str(item["item_id"]),
                        plan_id,
                        str(item["scheduled_date"]),
                        str(item["item_type"]),
                        str(item["name"]),
                        item.get("sport_type"),
                        json.dumps(item.get("payload", {}), ensure_ascii=False, sort_keys=True),
                    )
                    for item in schedule_items
                ],
            )

    def list_training_plans(self) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM training_plans ORDER BY start_date DESC, created_at DESC"
        ).fetchall()

    def get_training_plan(self, plan_id: str) -> sqlite3.Row | None:
        if not re.fullmatch(r"[0-9a-fA-F]{1,64}", plan_id):
            raise ValueError("Training plan ID must be a hexadecimal fingerprint or prefix")
        rows = self.connection.execute(
            "SELECT * FROM training_plans WHERE substr(plan_id, 1, ?) = ? ORDER BY plan_id LIMIT 2",
            (len(plan_id), plan_id.lower()),
        ).fetchall()
        if len(rows) > 1:
            raise ValueError(f"Training plan ID prefix is ambiguous: {plan_id}")
        return rows[0] if rows else None

    def get_schedule_items(self, plan_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM schedule_items WHERE training_plan_id = ? ORDER BY scheduled_date, item_id",
            (plan_id,),
        ).fetchall()
