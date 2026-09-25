from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .coordinate_rules import CoordinateRule, COORDINATE_MODES
from .formats import _copy_coordinate_marker, _fit_datetime


@dataclass(frozen=True, slots=True)
class FitDeviceMetadata:
    manufacturer_id: int | None
    product_id: int | None
    firmware_version: float | None


def normalize_fit_coordinates(
    input_path: Path,
    output_path: Path,
    coordinate_mode: str,
    coordinate_rules: Sequence[CoordinateRule] = (),
) -> tuple[Path, int]:
    if coordinate_mode not in COORDINATE_MODES:
        raise RuntimeError(f"Unsupported coordinate mode: {coordinate_mode}")
    input_path = input_path.resolve()
    output_path = output_path.resolve()
    if not input_path.is_file():
        raise RuntimeError(f"FIT input file does not exist: {input_path}")

    try:
        from fit_tool.fit_file import FitFile
        from fit_tool.fit_file_builder import FitFileBuilder
    except ImportError as exc:
        raise RuntimeError("fit-tool is required for FIT coordinate repair") from exc

    try:
        fit_file = FitFile.from_file(str(input_path))
    except Exception as exc:
        raise RuntimeError(f"Could not decode FIT file {input_path}: {exc}") from exc

    if _has_valid_coordinate_marker(input_path):
        return input_path, 0

    effective_mode = _resolve_coordinate_mode(
        _fit_device_metadata(fit_file.records),
        coordinate_mode,
        coordinate_rules,
    )
    if effective_mode == "none":
        return input_path, 0
    if output_path == input_path:
        raise RuntimeError("Coordinate repair requires a different output path")

    builder = FitFileBuilder(auto_define=False)
    changed_pairs = 0

    for record in fit_file.records:
        message = record.message
        changed_pairs += _rewrite_message_positions(message)
        builder.add(message)

    if changed_pairs == 0:
        return input_path, 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = _temporary_sibling(output_path)
    temporary_marker = _marker_path(temporary_path)
    output_marker = _marker_path(output_path)
    try:
        builder.build().to_file(str(temporary_path))
        FitFile.from_file(str(temporary_path))
        marker = {
            "format": 1,
            "input_sha256": _sha256(input_path),
            "output_sha256": _sha256(temporary_path),
            "coordinate_mode": effective_mode,
        }
        _write_json_atomically(temporary_marker, marker)
        os.replace(temporary_marker, output_marker)
        os.replace(temporary_path, output_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        temporary_marker.unlink(missing_ok=True)
        raise
    return output_path, changed_pairs


def repair_fit_track_continuity(input_path: Path, output_path: Path) -> tuple[Path, int]:
    input_path = input_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    if input_path.suffix.lower() != ".fit":
        raise ValueError("FIT continuity repair requires a .fit input file")
    if output_path.suffix.lower() != ".fit":
        raise ValueError("FIT continuity repair output must use the .fit extension")
    if input_path == output_path:
        raise ValueError("FIT continuity repair requires a different output path")
    if not input_path.is_file():
        raise RuntimeError(f"FIT input file does not exist: {input_path}")
    if _has_valid_fit_repair_marker(input_path):
        return input_path, 0

    try:
        from fit_tool.fit_file import FitFile
        from fit_tool.fit_file_builder import FitFileBuilder
    except ImportError as exc:
        raise RuntimeError("fit-tool is required for FIT continuity repair") from exc

    try:
        fit_file = FitFile.from_file(str(input_path))
    except Exception as exc:
        raise RuntimeError(f"Could not decode FIT file {input_path}: {exc}") from exc

    builder = FitFileBuilder(auto_define=False)
    previous_timestamp = None
    removed_records = 0
    for record in fit_file.records:
        message = getattr(record, "message", None)
        if getattr(message, "name", None) != "record":
            builder.add(message)
            continue

        timestamp_field = message.get_field_by_name("timestamp")
        timestamp = (
            _fit_datetime(timestamp_field.get_value())
            if timestamp_field is not None and timestamp_field.is_valid()
            else None
        )
        if timestamp is None:
            builder.add(message)
            continue

        if previous_timestamp is not None:
            elapsed_seconds = (timestamp - previous_timestamp).total_seconds()
            if elapsed_seconds < 0 or elapsed_seconds > 172_800:
                removed_records += 1
                continue
        builder.add(message)
        previous_timestamp = timestamp

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = _temporary_sibling(output_path)
    temporary_coordinate_marker = _marker_path(temporary_path)
    temporary_repair_marker = _fit_repair_marker_path(temporary_path)
    output_coordinate_marker = _marker_path(output_path)
    output_repair_marker = _fit_repair_marker_path(output_path)
    try:
        if removed_records:
            builder.build().to_file(str(temporary_path))
        else:
            shutil.copyfile(input_path, temporary_path)
        FitFile.from_file(str(temporary_path))

        has_coordinate_marker = _copy_coordinate_marker(
            input_path,
            temporary_path,
            temporary_path.read_bytes(),
        )
        _write_json_atomically(
            temporary_repair_marker,
            {
                "format": 1,
                "operation": "track_continuity",
                "input_sha256": _sha256(input_path),
                "output_sha256": _sha256(temporary_path),
                "removed_records": removed_records,
            },
        )
        os.replace(temporary_path, output_path)
        if has_coordinate_marker:
            os.replace(temporary_coordinate_marker, output_coordinate_marker)
        else:
            output_coordinate_marker.unlink(missing_ok=True)
        os.replace(temporary_repair_marker, output_repair_marker)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        temporary_coordinate_marker.unlink(missing_ok=True)
        temporary_repair_marker.unlink(missing_ok=True)
        raise

    return output_path, removed_records


def _resolve_coordinate_mode(
    metadata: FitDeviceMetadata,
    fallback_mode: str,
    rules: Sequence[CoordinateRule],
) -> str:
    matching = [
        rule
        for rule in rules
        if rule.matches(
            metadata.manufacturer_id,
            metadata.product_id,
            metadata.firmware_version,
        )
    ]
    if len(matching) > 1:
        raise RuntimeError("More than one FIT coordinate rule matched this device")
    return matching[0].coordinate_mode if matching else fallback_mode


def _fit_device_metadata(records: Sequence[object]) -> FitDeviceMetadata:
    file_id: tuple[int | None, int | None] = (None, None)
    device_infos: list[tuple[int | None, int | None, float | None]] = []

    for record in records:
        message = getattr(record, "message", None)
        name = getattr(message, "name", None)
        if name not in {"file_id", "device_info"}:
            continue
        values = {
            field.name: field.get_value()
            for field in getattr(message, "fields", [])
            if field.is_valid()
        }
        manufacturer = _to_int(values.get("manufacturer"))
        product = _to_int(values.get("product"))
        if name == "file_id" and (manufacturer is not None or product is not None):
            file_id = (manufacturer, product)
        elif name == "device_info":
            version_value = values.get("software_version")
            firmware = float(version_value) if isinstance(version_value, (int, float)) else None
            device_infos.append((manufacturer, product, firmware))

    manufacturer, product = file_id
    if manufacturer is None and product is None and device_infos:
        manufacturer, product, firmware = device_infos[0]
        return FitDeviceMetadata(manufacturer, product, firmware)

    matching_devices = [
        item
        for item in device_infos
        if (manufacturer is None or item[0] == manufacturer)
        and (product is None or item[1] == product)
    ]
    firmware_versions = {item[2] for item in matching_devices if item[2] is not None}
    firmware = next(iter(firmware_versions)) if len(firmware_versions) == 1 else None
    if product is None and matching_devices:
        products = {item[1] for item in matching_devices if item[1] is not None}
        product = next(iter(products)) if len(products) == 1 else None
    if manufacturer is None and matching_devices:
        manufacturers = {item[0] for item in matching_devices if item[0] is not None}
        manufacturer = next(iter(manufacturers)) if len(manufacturers) == 1 else None
    return FitDeviceMetadata(manufacturer, product, firmware)


def _to_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _marker_path(path: Path) -> Path:
    return Path(f"{path}.coord.json")


def _fit_repair_marker_path(path: Path) -> Path:
    return Path(f"{path}.fit-repair.json")


def _has_valid_fit_repair_marker(path: Path) -> bool:
    marker_path = _fit_repair_marker_path(path)
    if not marker_path.exists():
        return False
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read FIT repair marker {marker_path}: {exc}") from exc
    if (
        not isinstance(marker, dict)
        or marker.get("format") != 1
        or marker.get("operation") != "track_continuity"
        or not isinstance(marker.get("output_sha256"), str)
        or marker["output_sha256"] != _sha256(path)
    ):
        raise RuntimeError(
            f"FIT repair marker does not match {path}; use the original FIT file or remove the marker"
        )
    return True


def _has_valid_coordinate_marker(path: Path) -> bool:
    marker_path = _marker_path(path)
    if not marker_path.exists():
        return False
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read coordinate marker {marker_path}: {exc}") from exc
    expected_hash = marker.get("output_sha256") if isinstance(marker, dict) else None
    if not isinstance(expected_hash, str) or _sha256(path) != expected_hash:
        raise RuntimeError(
            f"Coordinate marker does not match {path}; use the original FIT file or remove the marker"
        )
    return True


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _temporary_sibling(path: Path) -> Path:
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(handle)
    return Path(temporary)


def _write_json_atomically(path: Path, payload: dict[str, object]) -> None:
    temporary = _temporary_sibling(path)
    try:
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _rewrite_message_positions(message: object) -> int:
    fields = getattr(message, "fields", None)
    if not fields:
        return 0

    changed = 0
    lat_fields = [
        field.name
        for field in fields
        if field.is_valid() and field.name.endswith("_lat")
    ]
    for lat_name in lat_fields:
        lon_name = f"{lat_name[:-4]}_long"
        lat_field = message.get_field_by_name(lat_name)
        lon_field = message.get_field_by_name(lon_name)
        if lat_field is None or lon_field is None:
            continue
        if not lat_field.is_valid() or not lon_field.is_valid():
            continue

        lat_value = lat_field.get_value()
        lon_value = lon_field.get_value()
        if lat_value is None or lon_value is None:
            continue

        if not (-90.0 <= lat_value <= 90.0 and -180.0 <= lon_value <= 180.0):
            continue
        if _out_of_china(lat_value, lon_value):
            continue

        new_lat, new_lon = _gcj02_to_wgs84(lat_value, lon_value)
        if math.isclose(new_lat, lat_value, abs_tol=1e-8) and math.isclose(new_lon, lon_value, abs_tol=1e-8):
            continue

        lat_field.set_value(0, new_lat)
        lon_field.set_value(0, new_lon)
        changed += 1

    return changed


def _out_of_china(lat: float, lon: float) -> bool:
    return lon < 72.004 or lon > 137.8347 or lat < 0.8293 or lat > 55.8271


def _transform_lat(x: float, y: float) -> float:
    result = (
        -100.0
        + 2.0 * x
        + 3.0 * y
        + 0.2 * y * y
        + 0.1 * x * y
        + 0.2 * math.sqrt(abs(x))
    )
    result += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    result += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    result += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return result


def _transform_lon(x: float, y: float) -> float:
    result = (
        300.0
        + x
        + 2.0 * y
        + 0.1 * x * x
        + 0.1 * x * y
        + 0.1 * math.sqrt(abs(x))
    )
    result += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    result += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    result += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return result


def _gcj02_to_wgs84(lat: float, lon: float) -> tuple[float, float]:
    if _out_of_china(lat, lon):
        return lat, lon

    a = 6378245.0
    ee = 0.00669342162296594323
    d_lat = _transform_lat(lon - 105.0, lat - 35.0)
    d_lon = _transform_lon(lon - 105.0, lat - 35.0)
    rad_lat = lat / 180.0 * math.pi
    magic = math.sin(rad_lat)
    magic = 1 - ee * magic * magic
    sqrt_magic = math.sqrt(magic)
    d_lat = (d_lat * 180.0) / ((a * (1 - ee)) / (magic * sqrt_magic) * math.pi)
    d_lon = (d_lon * 180.0) / (a / sqrt_magic * math.cos(rad_lat) * math.pi)
    mg_lat = lat + d_lat
    mg_lon = lon + d_lon
    return lat * 2 - mg_lat, lon * 2 - mg_lon
