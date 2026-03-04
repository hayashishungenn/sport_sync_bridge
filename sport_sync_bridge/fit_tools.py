from __future__ import annotations

import math
from pathlib import Path


def normalize_fit_coordinates(
    input_path: Path,
    output_path: Path,
    coordinate_mode: str,
) -> tuple[Path, int]:
    if coordinate_mode == "none":
        return input_path, 0
    if coordinate_mode != "gcj02_to_wgs84":
        raise RuntimeError(f"Unsupported coordinate mode: {coordinate_mode}")

    try:
        from fit_tool.fit_file import FitFile
        from fit_tool.fit_file_builder import FitFileBuilder
    except ImportError as exc:
        raise RuntimeError("fit-tool is required for FIT coordinate repair") from exc

    fit_file = FitFile.from_file(str(input_path))
    builder = FitFileBuilder(auto_define=False)
    changed_pairs = 0

    for record in fit_file.records:
        message = record.message
        changed_pairs += _rewrite_message_positions(message)
        builder.add(message)

    if changed_pairs == 0:
        return input_path, 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    builder.build().to_file(str(output_path))
    return output_path, changed_pairs


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
