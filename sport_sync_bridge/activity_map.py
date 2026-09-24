from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .formats import ActivityFile


@dataclass(frozen=True, slots=True)
class RouteMapResult:
    output_path: Path
    point_count: int


def write_route_map(activity: ActivityFile, output_path: Path) -> RouteMapResult:
    output_path = output_path.expanduser().resolve()
    if output_path.suffix.lower() not in {".html", ".htm"}:
        raise ValueError("Route map output path must use .html or .htm")

    points: list[list[float]] = []
    for point in activity.track_points:
        if point.latitude is None and point.longitude is None:
            continue
        if point.latitude is None or point.longitude is None:
            raise ValueError("Activity contains incomplete GPS coordinates")
        latitude = float(point.latitude)
        longitude = float(point.longitude)
        if (
            not math.isfinite(latitude)
            or not math.isfinite(longitude)
            or not -90 <= latitude <= 90
            or not -180 <= longitude <= 180
        ):
            raise ValueError("Activity contains invalid GPS coordinates")
        points.append([latitude, longitude])
    if not points:
        raise ValueError("Activity has no GPS track to map")

    title = activity.name or "运动记录"
    sport = activity.sport_type or ""
    payload = _json_for_script({"title": title, "sport": sport, "points": points})
    page = _ROUTE_MAP_HTML.replace("__ACTIVITY__", payload)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(page)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, output_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise

    return RouteMapResult(output_path=output_path, point_count=len(points))


def _json_for_script(value: object) -> str:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


_ROUTE_MAP_HTML = "\n".join(
    (
        '<!doctype html>',
        '<html lang="zh-CN">',
        '<head>',
        '  <meta charset="utf-8">',
        '  <meta name="viewport" content="width=device-width, initial-scale=1">',
        '  <title>运动路线</title>',
        '  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" crossorigin="">',
        '  <style>',
        '    :root { color-scheme: light; font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }',
        '    * { box-sizing: border-box; }',
        '    html, body { width: 100%; height: 100%; margin: 0; }',
        '    body { display: flex; flex-direction: column; background: #f4f7f8; color: #17252d; }',
        '    header { min-height: 68px; display: flex; align-items: center; gap: 18px; padding: 12px 20px; }',
        '    .brand { color: #16846e; font-size: 12px; font-weight: 750; letter-spacing: .08em; white-space: nowrap; }',
        '    h1 { margin: 0; font-size: 18px; font-weight: 680; overflow-wrap: anywhere; }',
        '    #meta { margin-left: auto; color: #5b6d75; font-size: 13px; text-align: right; }',
        '    #map { flex: 1; min-height: 260px; background: #e9eef0; }',
        '    .leaflet-container { font: inherit; }',
        '    @media (max-width: 560px) {',
        '      header { align-items: flex-start; flex-wrap: wrap; gap: 5px 12px; padding: 12px 14px; }',
        '      h1 { flex: 1; min-width: 55%; font-size: 16px; }',
        '      #meta { width: 100%; margin-left: 0; text-align: left; }',
        '    }',
        '  </style>',
        '</head>',
        '<body>',
        '  <header>',
        '    <div class="brand">SPORT SYNC BRIDGE</div>',
        '    <h1 id="activity-title">运动路线</h1>',
        '    <div id="meta"></div>',
        '  </header>',
        '  <main id="map" aria-label="运动轨迹地图"></main>',
        '  <noscript>请启用 JavaScript 以查看路线地图。</noscript>',
        '  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" crossorigin=""></script>',
        '  <script>',
        '    const activity = __ACTIVITY__;',
        '    document.title = activity.title + " · 运动路线";',
        '    document.getElementById("activity-title").textContent = activity.title;',
        '    document.getElementById("meta").textContent = [activity.sport, activity.points.length + " 个轨迹点"]',
        '      .filter(Boolean).join(" · ");',
        '    const map = L.map("map", { scrollWheelZoom: true });',
        '    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19 }).addTo(map);',
        '    const attribution = L.control.attribution({ prefix: false }).addTo(map);',
        '    attribution.addAttribution(`&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>`);',
        '    L.control.scale({ imperial: false }).addTo(map);',
        '    if (activity.points.length > 1) {',
        '      const route = L.polyline(activity.points, { color: "#14a986", weight: 5, opacity: 0.9 }).addTo(map);',
        '      L.circleMarker(activity.points[0], { radius: 7, color: "#ffffff", weight: 2, fillColor: "#14845e", fillOpacity: 1 })',
        '        .bindTooltip("起点").addTo(map);',
        '      L.circleMarker(activity.points[activity.points.length - 1], { radius: 7, color: "#ffffff", weight: 2, fillColor: "#e05b55", fillOpacity: 1 })',
        '        .bindTooltip("终点").addTo(map);',
        '      map.fitBounds(route.getBounds(), { padding: [36, 36], maxZoom: 17 });',
        '    } else {',
        '      map.setView(activity.points[0], 14);',
        '      L.circleMarker(activity.points[0], { radius: 7, color: "#ffffff", weight: 2, fillColor: "#14845e", fillOpacity: 1 })',
        '        .bindTooltip("轨迹点").addTo(map);',
        '    }',
        '  </script>',
        '</body>',
        '</html>',
        '',
    )
)
