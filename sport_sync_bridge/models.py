from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class Activity:
    source: str
    source_id: str
    name: str
    sport_type: str | None = None
    start_time: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class UploadResult:
    status: str
    remote_id: str | None = None
    message: str | None = None


@dataclass(slots=True)
class FileBundle:
    original_path: Path
    upload_path: Path
    sha1: str
