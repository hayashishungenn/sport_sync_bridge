from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import requests

from .config import AppConfig
from .hammerhead_api import HammerheadClient
from .models import Activity, UploadResult
from .targets import TargetAdapter


class HammerheadTarget(TargetAdapter):
    name = "hammerhead"
    supported_route_formats = {".fit", ".gpx", ".tcx"}

    def __init__(self, config: AppConfig, client: HammerheadClient):
        self.config = config
        self.client = client

    def is_configured(self) -> bool:
        return self.client.is_configured()

    def authenticate(self) -> None:
        self.client.authenticate()

    def upload_file(self, file_path: Path, activity: Activity, external_id: str) -> UploadResult:
        suffix = file_path.suffix.lower()
        if suffix not in self.supported_route_formats:
            return UploadResult(
                status="failed",
                message="Hammerhead route upload accepts FIT, GPX, or TCX files",
            )
        try:
            with file_path.open("rb") as handle:
                response = self.client.api_request(
                    "post",
                    "routes/file",
                    files={"file": (file_path.name, handle, "application/octet-stream")},
                    timeout=120,
                )
            response.raise_for_status()
            payload = _response_object(response)
            route_id = payload.get("id")
            if route_id in (None, ""):
                return UploadResult(
                    status="success",
                    message="Hammerhead route created; response did not include its ID",
                )
            return UploadResult(status="success", remote_id=str(route_id))
        except (OSError, requests.RequestException) as exc:
            return UploadResult(status="failed", message=str(exc))

    def delete_route(self, route_id: str) -> None:
        if not route_id.strip():
            raise ValueError("Hammerhead route ID cannot be empty")
        response = self.client.api_request(
            "delete",
            f"routes/{quote(route_id.strip(), safe='')}",
            timeout=30,
        )
        response.raise_for_status()


def _response_object(response: requests.Response) -> dict:
    if not response.content:
        return {}
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Hammerhead route response must be a JSON object")
    return payload
