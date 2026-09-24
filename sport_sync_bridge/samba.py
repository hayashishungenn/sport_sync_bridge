from __future__ import annotations

import math
from dataclasses import dataclass
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
    if port == 139:
        raise ValueError("The SMB2/3 backend uses direct TCP; NetBIOS port 139 is not supported")

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
) -> tuple[SambaDirectoryEntry, ...]:
    remote = parse_samba_url(url)
    _validate_timeout(timeout)
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
) -> list[ImportResult]:
    remote = parse_samba_url(url)
    _validate_timeout(timeout)
    suffix = PurePosixPath(remote.name).suffix.lower().lstrip(".")
    if suffix not in _SUPPORTED_SUFFIXES:
        raise ValueError(f"Unsupported Samba activity file type: .{suffix or '(none)'}")
    maximum_size = MAX_ARCHIVE_BYTES if suffix == "zip" else MAX_FILE_BYTES
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


def _access_error(remote: SambaPath, action: str, error: Exception, password: str | None) -> SambaAccessError:
    detail = str(error)
    if password:
        detail = detail.replace(password, "<redacted>")
    return SambaAccessError(f"Could not {action} Samba path {remote.url}: {type(error).__name__}: {detail}")
