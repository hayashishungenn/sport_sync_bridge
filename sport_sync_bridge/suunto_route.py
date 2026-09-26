from __future__ import annotations

from xml.etree import ElementTree as ET


def validate_route_gpx(payload: bytes) -> None:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise RuntimeError("Suunto route response is not valid GPX XML") from exc
    if root.tag.rsplit("}", 1)[-1].casefold() != "gpx":
        raise RuntimeError("Suunto route response must have a GPX root element")

    point_count = 0
    for point in root.iter():
        if point.tag.rsplit("}", 1)[-1].casefold() not in {"trkpt", "rtept"}:
            continue
        try:
            latitude = float(point.attrib["lat"])
            longitude = float(point.attrib["lon"])
        except (KeyError, ValueError) as exc:
            raise RuntimeError("Suunto route GPX contains an invalid coordinate") from exc
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise RuntimeError("Suunto route GPX contains an out-of-range coordinate")
        point_count += 1
    if point_count < 2:
        raise RuntimeError("Suunto route GPX must contain at least two route points")
