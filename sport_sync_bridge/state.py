from __future__ import annotations

import sqlite3
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
