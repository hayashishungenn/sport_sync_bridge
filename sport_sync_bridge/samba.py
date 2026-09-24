from __future__ import annotations

import math
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from .activity_library import ACTIVITY_FORMATS, MAX_ARCHIVE_BYTES, MAX_FILE_BYTES, ImportResult

if TYPE_CHECKING:
    from .activity_library import LocalActivityLibrary


_SUPPORTED_SUFFIXES = ACTIVITY_FORMATS | {"zip"}
_READ_CHUNK_BYTES = 1024 * 1024


class SambaAccessError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SambaPath:
    url: str
    host: str
    port: int
    unc_path: str
    segments: tuple[str, ...]

    @property
    def name(self) -> str:
        return self.segments[-1] if len(self.segments) > 1 else ""


@dataclass(frozen=True, slots=True)
class SambaDirectoryEntry:
    name: str
    url: str
    is_directory: bool
    size_bytes: int | None
    supported_activity: bool


def parse_samba_url(value: str) -> SambaPath:
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ValueError("Invalid Samba URL") from exc
    try:
        port = parsed.port if parsed.port is not None else 445
    except ValueError as exc:
        raise ValueError("Invalid Samba URL port") from exc
    if parsed.scheme.lower() != "smb" or not parsed.hostname:
        raise ValueError("Samba URL must use smb://host/share/path")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Put Samba credentials in arguments and environment variables, not in the URL")
    if parsed.query or parsed.fragment:
        raise ValueError("Samba URL cannot contain a query or fragment")
    if not 1 <= port <= 65535:
        raise ValueError("Samba URL port must be between 1 and 65535")
    segments = tuple(unquote(part) for part in parsed.path.split("/") if part)
    if not segments:
        raise ValueError("Samba URL must include a share name")
    if any(
        segment in {".", ".."}
        or "/" in segment
        or "\\" in segment
        or "\x00" in segment
        or any(ord(character) < 32 for character in segment)
        for segment in segments
    ):
        raise ValueError("Samba URL contains an invalid path segment")

    host = parsed.hostname
    unc_path = "\\\\" + "\\".join((host, *segments))
    canonical_path = "/" + "/".join(quote(segment, safe="") for segment in segments)
    authority = parsed.netloc
    canonical_url = urlunsplit(("smb", authority, canonical_path, "", ""))
    return SambaPath(canonical_url, host, port, unc_path, segments)


def list_samba_directory(
    url: str,
    *,
    username: str | None = None,
    password: str | None = None,
    timeout: float = 30.0,
    legacy_smb: bool = False,
    server_name: str | None = None,
) -> tuple[SambaDirectoryEntry, ...]:
    remote = parse_samba_url(url)
    _validate_timeout(timeout)
    _validate_samba_backend_options(remote, legacy_smb, server_name)
    if legacy_smb:
        return _list_legacy_samba_directory(
            remote,
            username=username,
            password=password,
            timeout=timeout,
            server_name=server_name,
        )

    client = _load_smbclient()
    try:
        with client.scandir(
            remote.unc_path,
            username=username,
            password=password,
            port=remote.port,
            connection_timeout=timeout,
        ) as entries:
            results = []
            for entry in entries:
                name = str(entry.name)
                if name in {".", ".."}:
                    continue
                is_directory = bool(entry.is_dir())
                is_file = bool(entry.is_file())
                size = int(entry.stat().st_size) if is_file else None
                suffix = PurePosixPath(name).suffix.lower().lstrip(".")
                child_url = _child_url(remote, name)
                results.append(
                    SambaDirectoryEntry(
                        name=name,
                        url=child_url,
                        is_directory=is_directory,
                        size_bytes=size,
                        supported_activity=is_file and suffix in _SUPPORTED_SUFFIXES,
                    )
                )
    except Exception as exc:
        raise _access_error(remote, "list", exc, password) from exc
    finally:
        client.reset_connection_cache()
    return tuple(sorted(results, key=lambda item: (not item.is_directory, item.name.casefold())))


def import_samba_activity(
    library: LocalActivityLibrary,
    url: str,
    *,
    username: str | None = None,
    password: str | None = None,
    archive_password: bytes | None = None,
    timeout: float = 30.0,
    legacy_smb: bool = False,
    server_name: str | None = None,
) -> list[ImportResult]:
    remote = parse_samba_url(url)
    _validate_timeout(timeout)
    suffix = PurePosixPath(remote.name).suffix.lower().lstrip(".")
    if suffix not in _SUPPORTED_SUFFIXES:
        raise ValueError(f"Unsupported Samba activity file type: .{suffix or '(none)'}")
    maximum_size = MAX_ARCHIVE_BYTES if suffix == "zip" else MAX_FILE_BYTES
    _validate_samba_backend_options(remote, legacy_smb, server_name)
    if legacy_smb:
        contents = _read_legacy_samba_file(
            remote,
            username=username,
            password=password,
            timeout=timeout,
            server_name=server_name,
            maximum_size=maximum_size,
        )
    else:
        client = _load_smbclient()
        oversized = False
        try:
            payload = bytearray()
            with client.open_file(
                remote.unc_path,
                mode="rb",
                username=username,
                password=password,
                port=remote.port,
                connection_timeout=timeout,
            ) as stream:
                while True:
                    chunk = stream.read(min(_READ_CHUNK_BYTES, maximum_size + 1 - len(payload)))
                    if not chunk:
                        break
                    payload.extend(chunk)
                    if len(payload) > maximum_size:
                        oversized = True
                        break
        except Exception as exc:
            raise _access_error(remote, "read", exc, password) from exc
        finally:
            client.reset_connection_cache()

        if oversized:
            raise ValueError(f"Remote activity file exceeds the {maximum_size}-byte limit")
        contents = bytes(payload)

    if suffix == "zip":
        return library.import_archive_payload(
            remote.name,
            contents,
            source_label=remote.url,
            password=archive_password,
        )
    return [library.import_payload(remote.name, contents, source_label=remote.url)]


def _list_legacy_samba_directory(
    remote: SambaPath,
    *,
    username: str | None,
    password: str | None,
    timeout: float,
    server_name: str | None,
) -> tuple[SambaDirectoryEntry, ...]:
    client = None
    try:
        client = _connect_legacy_samba(
            remote,
            username=username,
            password=password,
            timeout=timeout,
            server_name=server_name,
        )
        share, path = _samba_service_path(remote)
        results = []
        for entry in client.listPath(share, path, timeout=timeout):
            name = str(entry.filename)
            if name in {"", ".", ".."}:
                continue
            is_directory = bool(entry.isDirectory)
            size = int(entry.file_size) if not is_directory else None
            suffix = PurePosixPath(name).suffix.lower().lstrip(".")
            results.append(
                SambaDirectoryEntry(
                    name=name,
                    url=_child_url(remote, name),
                    is_directory=is_directory,
                    size_bytes=size,
                    supported_activity=not is_directory and suffix in _SUPPORTED_SUFFIXES,
                )
            )
    except Exception as exc:
        raise _access_error(remote, "list", exc, password) from exc
    finally:
        _close_legacy_samba(client)
    return tuple(sorted(results, key=lambda item: (not item.is_directory, item.name.casefold())))


def _read_legacy_samba_file(
    remote: SambaPath,
    *,
    username: str | None,
    password: str | None,
    timeout: float,
    server_name: str | None,
    maximum_size: int,
) -> bytes:
    client = None
    try:
        client = _connect_legacy_samba(
            remote,
            username=username,
            password=password,
            timeout=timeout,
            server_name=server_name,
        )
        share, path = _samba_service_path(remote)
        payload = BytesIO()
        client.retrieveFileFromOffset(
            share,
            path,
            payload,
            max_length=maximum_size + 1,
            timeout=timeout,
        )
        contents = payload.getvalue()
    except Exception as exc:
        raise _access_error(remote, "read", exc, password) from exc
    finally:
        _close_legacy_samba(client)

    if len(contents) > maximum_size:
        raise ValueError(f"Remote activity file exceeds the {maximum_size}-byte limit")
    return contents


def _connect_legacy_samba(
    remote: SambaPath,
    *,
    username: str | None,
    password: str | None,
    timeout: float,
    server_name: str | None,
):
    connection_type = _load_pysmb()
    remote_name = _netbios_server_name(server_name, remote.host)
    client = connection_type(
        username or "",
        password or "",
        "SPORTSYNC",
        remote_name,
        use_ntlm_v2=True,
        is_direct_tcp=remote.port != 139,
    )
    try:
        if not client.connect(remote.host, remote.port, timeout=timeout):
            raise RuntimeError("SMB authentication or protocol negotiation failed")
    except Exception:
        _close_legacy_samba(client)
        raise
    return client


def _samba_service_path(remote: SambaPath) -> tuple[str, str]:
    share = remote.segments[0]
    path = "/" + "/".join(remote.segments[1:]) if len(remote.segments) > 1 else "/"
    return share, path


def _validate_samba_backend_options(
    remote: SambaPath,
    legacy_smb: bool,
    server_name: str | None,
) -> None:
    if remote.port == 139 and not legacy_smb:
        raise ValueError("SMB port 139 requires the explicit --legacy-smb option")
    if server_name is not None and not legacy_smb:
        raise ValueError("--server-name requires --legacy-smb")
    if legacy_smb:
        _netbios_server_name(server_name, remote.host)


def _netbios_server_name(server_name: str | None, host: str) -> str:
    value = server_name.strip() if server_name else host.split(".", 1)[0]
    if (
        not value
        or len(value) > 15
        or not value.isascii()
        or any(not (character.isalnum() or character == "-") for character in value)
    ):
        raise ValueError("SMB server name must be 1-15 ASCII letters, digits, or hyphens")
    return value.upper()


def _close_legacy_samba(client: object | None) -> None:
    if client is None:
        return
    try:
        client.close()
    except Exception:
        return


def _child_url(remote: SambaPath, name: str) -> str:
    parsed = urlsplit(remote.url)
    child_path = f"{parsed.path.rstrip('/')}/{quote(name, safe='')}"
    return urlunsplit((parsed.scheme, parsed.netloc, child_path, "", ""))


def _validate_timeout(timeout: float) -> None:
    if not math.isfinite(timeout) or not 1 <= timeout <= 300:
        raise ValueError("Samba timeout must be between 1 and 300 seconds")


def _load_smbclient():
    try:
        import smbclient
    except ImportError as exc:
        raise SambaAccessError("Samba support requires smbprotocol; install the project requirements") from exc
    return smbclient


def _load_pysmb():
    try:
        from smb.SMBConnection import SMBConnection
    except ImportError as exc:
        raise SambaAccessError("Legacy SMB support requires pysmb; install the project requirements") from exc
    return SMBConnection


def _access_error(remote: SambaPath, action: str, error: Exception, password: str | None) -> SambaAccessError:
    detail = str(error)
    if password:
        detail = detail.replace(password, "<redacted>")
    return SambaAccessError(f"Could not {action} Samba path {remote.url}: {type(error).__name__}: {detail}")
