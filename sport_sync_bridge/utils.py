from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import base64
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


LOGGER = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_csv(value: str | None, default: Iterable[str] | None = None) -> list[str]:
    if value is None:
        return list(default or [])
    items = [item.strip() for item in value.split(",")]
    return [item for item in items if item]


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "_", value).strip(" ._")
    return cleaned or "activity"


def sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def parse_cookie_string(cookie_string: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for chunk in cookie_string.split(";"):
        if "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key:
            cookies[key] = value
    return cookies


def parse_datetime(value: object) -> datetime | None:
    if value in (None, "", 0):
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    if isinstance(value, (int, float)):
        return _from_epoch(float(value))

    text = str(value).strip()
    if not text:
        return None

    if text.isdigit():
        return _from_epoch(float(text))

    candidates = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y.%m.%d %H:%M:%S",
        "%Y.%m.%d %H:%M",
        "%Y.%m.%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d",
        "%Y%m%d%H%M%S",
        "%Y%m%d",
    )
    for fmt in candidates:
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        LOGGER.debug("Unable to parse datetime: %s", text)
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _from_epoch(value: float) -> datetime | None:
    try:
        if value > 1_000_000_000_000:
            value = value / 1000.0
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def fit_signature_ok(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            header = handle.read(14)
        return len(header) >= 12 and header[8:12] == b".FIT"
    except OSError:
        return False


def maybe_relative_url(base_url: str, value: str) -> str:
    if value.startswith("http://") or value.startswith("https://"):
        return value
    if value.startswith("/"):
        return f"{base_url.rstrip('/')}{value}"
    return f"{base_url.rstrip('/')}/{value}"


def configure_logging(level: str, log_path: Path | None = None) -> None:
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_path is not None:
        ensure_directory(log_path.parent)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))

    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )


def env_or_none(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def strava_sport_type(sport_type: str | None) -> str | None:
    if not sport_type:
        return None

    normalized = sport_type.strip().lower()
    mapping = {
        "cycling": "Ride",
        "ride": "Ride",
        "running": "Run",
        "run": "Run",
        "trail_run": "TrailRun",
        "walking": "Walk",
        "walk": "Walk",
        "hiking": "Hike",
        "hike": "Hike",
        "swimming": "Swim",
        "swim": "Swim",
        "indoor_cycling": "VirtualRide",
        "virtual_ride": "VirtualRide",
    }
    return mapping.get(normalized)


def pack_directory_to_base64_zip(directory: Path) -> str:
    directory = directory.resolve()
    if not directory.exists():
        raise RuntimeError(f"Directory does not exist: {directory}")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in directory.rglob("*"):
            if path.is_file():
                archive.write(path, arcname=str(path.relative_to(directory)))
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def restore_directory_from_base64_zip(encoded: str, directory: Path) -> None:
    raw = base64.b64decode(encoded)
    ensure_directory(directory)
    with zipfile.ZipFile(io.BytesIO(raw), "r") as archive:
        archive.extractall(directory)
