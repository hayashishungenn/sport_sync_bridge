from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from sport_sync_bridge.state import DATABASE_SCHEMA_VERSION, StateDB


class StateDatabaseTests(unittest.TestCase):
    def test_connection_uses_garsync_storage_pragmas_and_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = StateDB(Path(temporary) / "state.db")
            try:
                connection = state.connection
                self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
                self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                self.assertEqual(connection.execute("PRAGMA synchronous").fetchone()[0], 1)
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], DATABASE_SCHEMA_VERSION)
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            finally:
                state.close()

    def test_legacy_schema_is_upgraded_without_losing_existing_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.db"
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TABLE kv_store (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO kv_store (key, value, updated_at) VALUES ('legacy', 'kept', '2026-01-01T00:00:00+00:00')"
            )
            connection.commit()
            connection.close()

            state = StateDB(path)
            try:
                self.assertEqual(state.get_value("legacy"), "kept")
                self.assertEqual(state.connection.execute("PRAGMA user_version").fetchone()[0], DATABASE_SCHEMA_VERSION)
            finally:
                state.close()

    def test_newer_schema_version_is_rejected_without_downgrade(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.db"
            connection = sqlite3.connect(path)
            connection.execute(f"PRAGMA user_version = {DATABASE_SCHEMA_VERSION + 1}")
            connection.commit()
            connection.close()

            with self.assertRaisesRegex(RuntimeError, "newer than supported"):
                StateDB(path)

    def test_schedule_items_cascade_when_training_plan_is_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = StateDB(Path(temporary) / "state.db")
            try:
                state.connection.execute(
                    "INSERT INTO training_plans "
                    "(plan_id, template_id, name, sport_type, locale, start_date, template_json, created_at) "
                    "VALUES ('plan-1', 'template-1', 'Plan', 'running', 'en', '2026-01-05', '{}', 'now')"
                )
                state.connection.execute(
                    "INSERT INTO schedule_items "
                    "(item_id, training_plan_id, scheduled_date, item_type, name, sport_type, payload_json) "
                    "VALUES ('item-1', 'plan-1', '2026-01-05', 'workout', 'Easy run', 'running', '{}')"
                )
                state.connection.commit()
                state.connection.execute("DELETE FROM training_plans WHERE plan_id = 'plan-1'")
                state.connection.commit()

                count = state.connection.execute("SELECT COUNT(*) FROM schedule_items").fetchone()[0]
                self.assertEqual(count, 0)
            finally:
                state.close()


if __name__ == "__main__":
    unittest.main()
