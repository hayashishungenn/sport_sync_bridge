from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sport_sync_bridge.activity_library import MAX_FILE_BYTES, LocalActivityLibrary
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
            "smb://nas.local/share/ride.fit?download=1",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_samba_url(value)

        netbios = parse_samba_url("smb://nas.local:139/activities/ride.fit")
        self.assertEqual(netbios.port, 139)
        self.assertEqual(netbios.unc_path, "\\\\nas.local\\activities\\ride.fit")

    def test_port_139_requires_explicit_legacy_backend(self) -> None:
        with self.assertRaisesRegex(ValueError, "--legacy-smb"):
            list_samba_directory("smb://nas.local:139/activities")

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

    def test_legacy_backend_lists_netbios_share_read_only(self) -> None:
        directory = SimpleNamespace(filename="routes", isDirectory=True, file_size=0)
        activity = SimpleNamespace(filename="ride.fit", isDirectory=False, file_size=512)
        dot_entry = SimpleNamespace(filename=".", isDirectory=True, file_size=0)
        client = SimpleNamespace(
            connect=Mock(return_value=True),
            listPath=Mock(return_value=[activity, dot_entry, directory]),
            close=Mock(),
        )
        connection_factory = Mock(return_value=client)

        with patch("sport_sync_bridge.samba._load_pysmb", return_value=connection_factory):
            entries = list_samba_directory(
                "smb://nas.local:139/activities/2026",
                username="athlete",
                password="pw",
                timeout=8,
                legacy_smb=True,
                server_name="NAS01",
            )

        self.assertEqual([entry.name for entry in entries], ["routes", "ride.fit"])
        self.assertTrue(entries[0].is_directory)
        self.assertTrue(entries[1].supported_activity)
        self.assertEqual(entries[1].size_bytes, 512)
        connection_factory.assert_called_once_with(
            "athlete",
            "pw",
            "SPORTSYNC",
            "NAS01",
            use_ntlm_v2=True,
            is_direct_tcp=False,
        )
        client.connect.assert_called_once_with("nas.local", 139, timeout=8)
        client.listPath.assert_called_once_with("activities", "/2026", timeout=8)
        client.close.assert_called_once_with()

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

    def test_legacy_backend_imports_with_a_bounded_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample = create_gpx(root / "sample.gpx").read_bytes()
            state = StateDB(root / ".data" / "state.db")
            try:
                def retrieve(_share, _path, output, *, max_length, timeout):
                    self.assertEqual(max_length, MAX_FILE_BYTES + 1)
                    self.assertEqual(timeout, 12)
                    output.write(sample[:max_length])

                client = SimpleNamespace(
                    connect=Mock(return_value=True),
                    retrieveFileFromOffset=Mock(side_effect=retrieve),
                    close=Mock(),
                )
                connection_factory = Mock(return_value=client)
                with patch("sport_sync_bridge.samba._load_pysmb", return_value=connection_factory):
                    results = import_samba_activity(
                        LocalActivityLibrary(state, root / ".data"),
                        "smb://nas.local:139/activities/2026/ride.gpx",
                        username="athlete",
                        password="secret-value",
                        timeout=12,
                        legacy_smb=True,
                        server_name="NAS01",
                    )

                self.assertEqual(len(results), 1)
                self.assertEqual(results[0].file_format, "gpx")
                self.assertEqual(results[0].source_label, "smb://nas.local:139/activities/2026/ride.gpx")
                client.retrieveFileFromOffset.assert_called_once()
                self.assertEqual(
                    client.retrieveFileFromOffset.call_args.args[:2],
                    ("activities", "/2026/ride.gpx"),
                )
                client.close.assert_called_once_with()
            finally:
                state.close()

    def test_legacy_backend_rejects_remote_file_over_size_limit(self) -> None:
        client = SimpleNamespace(
            connect=Mock(return_value=True),
            retrieveFileFromOffset=Mock(
                side_effect=lambda _share, _path, output, *, max_length, timeout: output.write(
                    b"x" * max_length
                )
            ),
            close=Mock(),
        )
        library = Mock()
        with (
            patch("sport_sync_bridge.samba.MAX_FILE_BYTES", 4),
            patch("sport_sync_bridge.samba._load_pysmb", return_value=Mock(return_value=client)),
            self.assertRaisesRegex(ValueError, "4-byte limit"),
        ):
            import_samba_activity(
                library,
                "smb://nas.local:139/activities/ride.gpx",
                legacy_smb=True,
                server_name="NAS01",
            )

        library.import_payload.assert_not_called()
        client.close.assert_called_once_with()

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

    def test_legacy_backend_redacts_password_from_connect_error(self) -> None:
        client = SimpleNamespace(
            connect=Mock(side_effect=RuntimeError("authentication failed: private-password")),
            close=Mock(),
        )
        with patch("sport_sync_bridge.samba._load_pysmb", return_value=Mock(return_value=client)):
            with self.assertRaises(SambaAccessError) as error:
                list_samba_directory(
                    "smb://nas.local:139/activities",
                    password="private-password",
                    legacy_smb=True,
                    server_name="NAS01",
                )

        self.assertNotIn("private-password", str(error.exception))
        self.assertIn("<redacted>", str(error.exception))
        client.close.assert_called_once_with()

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

    def test_cli_lists_port_139_with_explicit_legacy_options(self) -> None:
        config = SimpleNamespace(data_dir=Path("."), log_level="INFO", log_path=Path("sync.log"))
        file_entry = SimpleNamespace(filename="ride.fit", isDirectory=False, file_size=512)
        client = SimpleNamespace(
            connect=Mock(return_value=True),
            listPath=Mock(return_value=[file_entry]),
            close=Mock(),
        )
        stdout = io.StringIO()
        with (
            patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
            patch("sport_sync_bridge.cli.configure_logging"),
            patch("sport_sync_bridge.samba._load_pysmb", return_value=Mock(return_value=client)),
            contextlib.redirect_stdout(stdout),
        ):
            status = main(
                [
                    "library",
                    "samba",
                    "list",
                    "smb://nas.local:139/activities",
                    "--legacy-smb",
                    "--server-name",
                    "NAS01",
                ]
            )

        self.assertEqual(status, 0)
        self.assertIn('"name": "ride.fit"', stdout.getvalue())
        self.assertIn("entries=1", stdout.getvalue())
        client.close.assert_called_once_with()

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
