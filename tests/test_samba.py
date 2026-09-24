from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sport_sync_bridge.activity_library import LocalActivityLibrary
from sport_sync_bridge.cli import main
from sport_sync_bridge.samba import (
    SambaAccessError,
    import_samba_activity,
    list_samba_directory,
    parse_samba_url,
)
from sport_sync_bridge.state import StateDB
from tests.activity_fixtures import create_gpx


class SambaTests(unittest.TestCase):
    def test_parses_smb_urls_and_rejects_unsafe_or_unsupported_forms(self) -> None:
        parsed = parse_samba_url("smb://nas.local:1445/activities/Long%20Ride.gpx")
        self.assertEqual(parsed.port, 1445)
        self.assertEqual(parsed.unc_path, "\\\\nas.local\\activities\\Long Ride.gpx")
        self.assertEqual(parsed.url, "smb://nas.local:1445/activities/Long%20Ride.gpx")

        for value in (
            "https://nas.local/share/ride.fit",
            "smb://user:secret@nas.local/share/ride.fit",
            "smb://nas.local/share/%2e%2e/ride.fit",
            "smb://nas.local:0/share/ride.fit",
            "smb://nas.local:139/share/ride.fit",
            "smb://nas.local/share/ride.fit?download=1",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_samba_url(value)

    def test_lists_one_directory_and_marks_supported_activity_files(self) -> None:
        directory = SimpleNamespace(name="routes", is_dir=lambda: True, is_file=lambda: False)
        activity = SimpleNamespace(
            name="ride.fit",
            is_dir=lambda: False,
            is_file=lambda: True,
            stat=lambda: SimpleNamespace(st_size=512),
        )
        client = SimpleNamespace(
            scandir=Mock(return_value=contextlib.nullcontext([activity, directory])),
            reset_connection_cache=Mock(),
        )

        with patch("sport_sync_bridge.samba._load_smbclient", return_value=client):
            entries = list_samba_directory("smb://nas.local/sports", username="athlete", password="pw")

        self.assertEqual([entry.name for entry in entries], ["routes", "ride.fit"])
        self.assertTrue(entries[0].is_directory)
        self.assertTrue(entries[1].supported_activity)
        self.assertEqual(entries[1].size_bytes, 512)
        client.scandir.assert_called_once_with(
            "\\\\nas.local\\sports",
            username="athlete",
            password="pw",
            port=445,
            connection_timeout=30.0,
        )
        client.reset_connection_cache.assert_called_once_with()

    def test_imports_remote_gpx_into_local_library(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample = create_gpx(root / "sample.gpx").read_bytes()
            state = StateDB(root / ".data" / "state.db")
            try:
                library = LocalActivityLibrary(state, root / ".data")
                client = SimpleNamespace(
                    open_file=Mock(return_value=contextlib.closing(io.BytesIO(sample))),
                    reset_connection_cache=Mock(),
                )
                with patch("sport_sync_bridge.samba._load_smbclient", return_value=client):
                    results = import_samba_activity(
                        library,
                        "smb://nas.local/activities/ride.gpx",
                        username="athlete",
                        password="secret-value",
                    )

                self.assertEqual(len(results), 1)
                self.assertEqual(results[0].file_format, "gpx")
                self.assertEqual(results[0].source_label, "smb://nas.local/activities/ride.gpx")
                self.assertTrue(Path(state.get_local_activity(results[0].fingerprint)["file_path"]).is_file())
                client.reset_connection_cache.assert_called_once_with()
            finally:
                state.close()

    def test_import_redacts_password_from_backend_errors(self) -> None:
        client = SimpleNamespace(
            open_file=Mock(side_effect=RuntimeError("authentication failed for super-secret")),
            reset_connection_cache=Mock(),
        )
        with tempfile.TemporaryDirectory() as directory:
            state = StateDB(Path(directory) / "state.db")
            try:
                with patch("sport_sync_bridge.samba._load_smbclient", return_value=client):
                    with self.assertRaises(SambaAccessError) as error:
                        import_samba_activity(
                            LocalActivityLibrary(state, Path(directory)),
                            "smb://nas.local/share/ride.gpx",
                            password="super-secret",
                        )
                self.assertNotIn("super-secret", str(error.exception))
                self.assertIn("<redacted>", str(error.exception))
            finally:
                state.close()

    def test_cli_import_uses_environment_password_and_local_library(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample = create_gpx(root / "sample.gpx").read_bytes()
            config = SimpleNamespace(
                data_dir=root / ".data",
                db_path=root / ".data" / "state.db",
                log_level="INFO",
                log_path=root / "sync.log",
            )
            client = SimpleNamespace(
                open_file=Mock(return_value=contextlib.closing(io.BytesIO(sample))),
                reset_connection_cache=Mock(),
            )
            stdout = io.StringIO()
            with patch("sport_sync_bridge.cli.AppConfig.load", return_value=config), patch(
                "sport_sync_bridge.cli.configure_logging"
            ), patch("sport_sync_bridge.samba._load_smbclient", return_value=client), patch.dict(
                "os.environ", {"SAMBA_PASSWORD": "private-test-password"}
            ), contextlib.redirect_stdout(stdout):
                status = main(
                    ["library", "samba", "import", "smb://nas.local/share/ride.gpx", "--username", "athlete"]
                )

            self.assertEqual(status, 0)
            self.assertIn('"format": "gpx"', stdout.getvalue())
            self.assertIn("imported=1", stdout.getvalue())
            self.assertNotIn("private-test-password", stdout.getvalue())
            self.assertEqual(client.open_file.call_args.kwargs["password"], "private-test-password")

    def test_imports_remote_zip_through_existing_archive_importer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gpx_payload = create_gpx(root / "sample.gpx").read_bytes()
            archive_buffer = io.BytesIO()
            with zipfile.ZipFile(archive_buffer, "w") as archive:
                archive.writestr("ride.gpx", gpx_payload)
            state = StateDB(root / ".data" / "state.db")
            try:
                client = SimpleNamespace(
                    open_file=Mock(return_value=contextlib.closing(io.BytesIO(archive_buffer.getvalue()))),
                    reset_connection_cache=Mock(),
                )
                with patch("sport_sync_bridge.samba._load_smbclient", return_value=client):
                    results = import_samba_activity(
                        LocalActivityLibrary(state, root / ".data"),
                        "smb://nas.local/activities/export.zip",
                    )

                self.assertEqual(len(results), 1)
                self.assertEqual(results[0].file_format, "gpx")
                self.assertTrue(results[0].source_label.endswith("export.zip!/ride.gpx"))
            finally:
                state.close()


if __name__ == "__main__":
    unittest.main()
