from __future__ import annotations

import math
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageFont, ImageOps

from .activity_analysis import summarize_activity
from .formats import ActivityFile, TrackPoint
from .period_summary import calculate_activity_power_curve


POSTER_RATIOS = {
    "portrait": (1080, 1440),
    "square": (1080, 1080),
}
POSTER_LAYOUTS = (
    "classic",
    "track_top",
    "side_by_side",
    "data_below",
    "bottom_corner",
    "data_above",
    "full_info",
    "classic_orange",
    "indoor",
)
POSTER_METRICS = ("ascent", "speed", "pace", "power")


@dataclass(frozen=True, slots=True)
class PosterResult:
    output_path: Path
    width: int
    height: int
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Metric:
    label: str
    value: str
    unit: str = ""


@dataclass(frozen=True, slots=True)
class _PosterPreset:
    show_track: bool = True
    show_title: bool = False
    show_power_curve: bool = False
    metric: str = "pace"


_POSTER_PRESETS = {
    "classic": _PosterPreset(metric="pace"),
    "track_top": _PosterPreset(metric="pace"),
    "side_by_side": _PosterPreset(metric="speed"),
    "data_below": _PosterPreset(metric="ascent"),
    "bottom_corner": _PosterPreset(metric="pace"),
    "data_above": _PosterPreset(metric="ascent"),
    "full_info": _PosterPreset(show_title=True, metric="ascent"),
    "classic_orange": _PosterPreset(metric="pace"),
    "indoor": _PosterPreset(show_track=False, show_power_curve=True, metric="ascent"),
}


def write_activity_poster(
    activity: ActivityFile,
    output_path: Path,
    *,
    layout: str = "classic",
    ratio: str = "portrait",
    photo_path: Path | None = None,
    title: str | None = None,
    user: str | None = None,
    watermark: str | None = "SPORT SYNC BRIDGE",
    show_track: bool | None = None,
    show_title: bool | None = None,
    show_power_curve: bool | None = None,
    metric: str | None = None,
    text_color: str = "#F5F7FA",
    track_color: str = "#51E2B7",
    accent_color: str | None = None,
    font_path: Path | None = None,
) -> PosterResult:
    output_path = output_path.expanduser().resolve()
    if output_path.suffix.lower() not in {".jpg", ".jpeg"}:
        raise ValueError("Poster output path must use a .jpg or .jpeg extension")
    if layout not in POSTER_LAYOUTS:
        raise ValueError(f"Unsupported poster layout: {layout}")
    if ratio not in POSTER_RATIOS:
        raise ValueError(f"Unsupported poster ratio: {ratio}")
    if metric is not None and metric not in POSTER_METRICS:
        raise ValueError(f"Unsupported poster metric: {metric}")

    preset = _POSTER_PRESETS[layout]
    show_track = preset.show_track if show_track is None else show_track
    show_title = (preset.show_title or title is not None) if show_title is None else show_title
    show_power_curve = preset.show_power_curve if show_power_curve is None else show_power_curve
    selected_metric = metric or preset.metric

    text_rgb = _parse_color(text_color, "text color")
    track_rgb = _parse_color(track_color, "track color")
    default_accent = "#FF8A3D" if layout == "classic_orange" else "#51E2B7"
    accent_rgb = _parse_color(accent_color or default_accent, "accent color")

    width, height = POSTER_RATIOS[ratio]
    if photo_path is not None and not photo_path.is_file():
        raise ValueError(f"Background photo does not exist: {photo_path}")
    image = _make_background(width, height, photo_path)
    draw = ImageDraw.Draw(image)
    scale = width / 1080
    margin = round(width * 0.065)
    fonts = _Fonts(font_path)
    warnings: list[str] = []

    gps_points = _gps_points(activity.track_points)
    if show_track and not gps_points:
        warnings.append("No GPS track is available; the poster shows an empty track panel.")
    power_points = _power_points(activity.track_points) if show_power_curve else []
    if show_power_curve and len(power_points) < 2:
        warnings.append("Power curve was requested, but the activity has fewer than two curve points.")
    summary = summarize_activity(activity)
    heading = (title if title is not None else activity.name) or "运动记录"
    _draw_header(
        draw,
        width,
        height,
        margin,
        heading if show_title else None,
        user,
        activity.sport_type,
        activity.start_time,
        text_rgb,
        accent_rgb,
        fonts,
        scale,
    )

    route_box, stats_box = _layout_boxes(layout, width, height, margin)
    if show_track:
        _draw_route(image, route_box, gps_points, track_rgb, text_rgb, fonts, scale)
    if show_power_curve:
        _draw_power_chart(
            image,
            _power_chart_box(route_box, scale) if show_track else route_box,
            power_points,
            accent_rgb,
            text_rgb,
            fonts,
            scale,
            empty_label="无可用功率曲线",
        )

    selected_metrics = _select_metrics(summary, selected_metric)
    _draw_stats(
        image,
        stats_box,
        selected_metrics,
        text_rgb,
        accent_rgb,
        fonts,
        scale,
        compact=layout == "side_by_side",
        full_info=layout == "full_info",
    )
    _draw_watermark(draw, width, height, margin, watermark, text_rgb, fonts, scale)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{output_path.stem}.",
            suffix=output_path.suffix.lower(),
            dir=output_path.parent,
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
        jpeg_image = image.convert("RGB")
        jpeg_image.save(temp_path, format="JPEG", quality=93, optimize=True, progressive=True)
        jpeg_image.close()
        os.replace(temp_path, output_path)
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise
    finally:
        image.close()

    return PosterResult(output_path, width, height, tuple(warnings))


class _Fonts:
    def __init__(self, font_path: Path | None):
        self.requested = font_path.expanduser() if font_path else None
        self._cache: dict[tuple[int, bool], ImageFont.ImageFont] = {}

    def get(self, size: int, *, bold: bool = False) -> ImageFont.ImageFont:
        key = (max(12, size), bold)
        if key not in self._cache:
            self._cache[key] = _load_font(key[0], bold=bold, requested=self.requested)
        return self._cache[key]


def _load_font(size: int, *, bold: bool, requested: Path | None) -> ImageFont.ImageFont:
    candidates: list[Path] = []
    explicit = requested
    if explicit is None and os.getenv("SPORT_SYNC_BRIDGE_FONT"):
        explicit = Path(os.environ["SPORT_SYNC_BRIDGE_FONT"]).expanduser()
    if explicit is not None:
        if not explicit.is_file():
            raise ValueError(f"Poster font file does not exist: {explicit}")
        try:
            return ImageFont.truetype(str(explicit), size=size)
        except OSError as exc:
            raise ValueError(f"Could not load poster font: {explicit}") from exc
    if os.name == "nt":
        candidates.extend(
            [
                Path("C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc"),
                Path("C:/Windows/Fonts/simhei.ttf"),
                Path("C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf"),
            ]
        )
    else:
        candidates.extend(
            [
                Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
                Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
                Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            ]
        )
    for candidate in candidates:
        if candidate.is_file():
            try:
                return ImageFont.truetype(str(candidate), size=size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


def _make_background(width: int, height: int, photo_path: Path | None) -> Image.Image:
    top = (20, 37, 53)
    bottom = (5, 12, 23)
    gradient = Image.new("RGB", (1, height))
    pixels = gradient.load()
    for y in range(height):
        fraction = y / max(1, height - 1)
        pixels[0, y] = tuple(round(a + (b - a) * fraction) for a, b in zip(top, bottom))
    background = gradient.resize((width, height), Image.Resampling.BICUBIC)
    gradient.close()

    if photo_path is not None:
        if not photo_path.is_file():
            background.close()
            raise ValueError(f"Background photo does not exist: {photo_path}")
        try:
            with Image.open(photo_path) as photo:
                photo = ImageOps.exif_transpose(photo).convert("RGB")
                crop = ImageOps.fit(photo, (width, height), method=Image.Resampling.LANCZOS)
                blended = Image.blend(crop, background, 0.58)
                background.close()
                background = blended
                crop.close()
                photo.close()
        except (OSError, Image.DecompressionBombError) as exc:
            background.close()
            raise ValueError(f"Could not read background photo: {photo_path}") from exc

    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse(
        (round(width * 0.55), round(-height * 0.12), round(width * 1.25), round(height * 0.48)),
        fill=(30, 181, 148, 38),
    )
    blurred_glow = glow.filter(ImageFilter.GaussianBlur(radius=max(1, round(width * 0.06))))
    glow.close()
    glow = blurred_glow
    background_rgba = Image.alpha_composite(background.convert("RGBA"), glow)
    background.close()
    background = background_rgba
    glow.close()
    return background


def _draw_header(
    draw: ImageDraw.ImageDraw,
    width: int,
    height: int,
    margin: int,
    title: str | None,
    user: str | None,
    sport_type: str | None,
    start_time: datetime | None,
    text_color: tuple[int, int, int],
    accent: tuple[int, int, int],
    fonts: _Fonts,
    scale: float,
) -> None:
    draw.rounded_rectangle(
        (margin, round(height * 0.056), margin + round(46 * scale), round(height * 0.056) + round(8 * scale)),
        radius=round(5 * scale),
        fill=accent,
    )
    title_y = round(height * 0.082)
    if title:
        _draw_fitted_text(
            draw,
            (margin, title_y),
            title,
            fonts,
            size=round(62 * scale),
            max_width=width - margin * 2,
            color=text_color,
            bold=True,
        )
    details: list[str] = []
    sport_label = _sport_label(sport_type)
    if sport_label:
        details.append(sport_label)
    if start_time is not None:
        details.append(start_time.strftime("%Y-%m-%d  %H:%M"))
    if details:
        draw.text(
            (margin, round(height * (0.162 if title else 0.105))),
            "  ·  ".join(details),
            font=fonts.get(round(24 * scale)),
            fill=_with_alpha(text_color, 210),
        )
    if user:
        user_text = user.strip()
        if user_text:
            _draw_fitted_text(
                draw,
                (margin, round(height * (0.195 if title else 0.145))),
                user_text,
                fonts,
                size=round(22 * scale),
                max_width=width - margin * 2,
                color=_with_alpha(text_color, 205),
            )


def _layout_boxes(
    layout: str,
    width: int,
    height: int,
    margin: int,
) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
    if layout in {"classic", "classic_orange"}:
        return (
            (margin, round(height * 0.235), width - margin, round(height * 0.63)),
            (margin, round(height * 0.665), width - margin, round(height * 0.94)),
        )
    if layout == "track_top":
        return (
            (margin, round(height * 0.225), width - margin, round(height * 0.66)),
            (margin, round(height * 0.695), width - margin, round(height * 0.94)),
        )
    if layout == "side_by_side":
        return (
            (margin, round(height * 0.235), round(width * 0.56), round(height * 0.92)),
            (round(width * 0.59), round(height * 0.235), width - margin, round(height * 0.92)),
        )
    if layout == "data_below":
        return (
            (margin, round(height * 0.225), width - margin, round(height * 0.59)),
            (margin, round(height * 0.625), width - margin, round(height * 0.94)),
        )
    if layout == "bottom_corner":
        return (
            (margin, round(height * 0.22), width - margin, round(height * 0.88)),
            (margin + round(width * 0.015), round(height * 0.61), round(width * 0.61), round(height * 0.88)),
        )
    if layout == "data_above":
        return (
            (margin, round(height * 0.455), width - margin, round(height * 0.92)),
            (margin, round(height * 0.225), width - margin, round(height * 0.43)),
        )
    if layout == "full_info":
        return (
            (margin, round(height * 0.225), width - margin, round(height * 0.53)),
            (margin, round(height * 0.565), width - margin, round(height * 0.94)),
        )
    if layout == "indoor":
        return (
            (margin, round(height * 0.225), width - margin, round(height * 0.64)),
            (margin, round(height * 0.675), width - margin, round(height * 0.94)),
        )
    raise ValueError(f"Unsupported poster layout: {layout}")


def _draw_route(
    image: Image.Image,
    box: tuple[int, int, int, int],
    points: list[TrackPoint],
    track_color: tuple[int, int, int],
    text_color: tuple[int, int, int],
    fonts: _Fonts,
    scale: float,
) -> None:
    _draw_panel(image, box, radius=round(28 * scale), fill=(4, 15, 27, 144))
    draw = ImageDraw.Draw(image)
    x0, y0, x1, y1 = box
    if not points:
        text = "无可用 GPS 轨迹"
        font = fonts.get(round(25 * scale))
        width = draw.textbbox((0, 0), text, font=font)[2]
        draw.text(((x0 + x1 - width) / 2, (y0 + y1) / 2), text, font=font, fill=_with_alpha(text_color, 170))
        return

    mean_lat = math.fsum(point.latitude for point in points if point.latitude is not None) / len(points)
    longitude_scale = max(0.05, abs(math.cos(math.radians(mean_lat))))
    coords = [
        (point.longitude * longitude_scale, point.latitude)
        for point in points
        if point.latitude is not None and point.longitude is not None
    ]
    min_x = min(x for x, _ in coords)
    max_x = max(x for x, _ in coords)
    min_y = min(y for _, y in coords)
    max_y = max(y for _, y in coords)
    pad = max(round(35 * scale), min(x1 - x0, y1 - y0) // 9)
    available_w = max(1, x1 - x0 - pad * 2)
    available_h = max(1, y1 - y0 - pad * 2)
    span_x = max(max_x - min_x, 0.00001)
    span_y = max(max_y - min_y, 0.00001)
    factor = min(available_w / span_x, available_h / span_y)
    offset_x = (x0 + x1 - (max_x + min_x) * factor) / 2
    offset_y = (y0 + y1 + (max_y + min_y) * factor) / 2
    pixels = [(offset_x + x * factor, offset_y - y * factor) for x, y in coords]
    draw.line(pixels, fill=track_color, width=max(4, round(8 * scale)), joint="curve")
    start_x, start_y = pixels[0]
    end_x, end_y = pixels[-1]
    point_radius = max(8, round(13 * scale))
    for x, y, fill in (
        (start_x, start_y, (107, 235, 176)),
        (end_x, end_y, (255, 255, 255)),
    ):
        draw.ellipse(
            (x - point_radius, y - point_radius, x + point_radius, y + point_radius),
            fill=fill,
            outline=(7, 19, 29),
            width=max(2, round(4 * scale)),
        )


def _draw_power_chart(
    image: Image.Image,
    box: tuple[int, int, int, int],
    points: list[tuple[float, float]],
    line_color: tuple[int, int, int],
    text_color: tuple[int, int, int],
    fonts: _Fonts,
    scale: float,
    *,
    empty_label: str,
) -> None:
    _draw_panel(image, box, radius=round(20 * scale), fill=(4, 15, 27, 205))
    draw = ImageDraw.Draw(image)
    x0, y0, x1, y1 = box
    margin = round(24 * scale)
    draw.text((x0 + margin, y0 + margin), "功率曲线", font=fonts.get(round(22 * scale), bold=True), fill=text_color)
    chart = (x0 + margin, y0 + round(58 * scale), x1 - margin, y1 - margin)
    cx0, cy0, cx1, cy1 = chart
    if len(points) < 2 or cx1 <= cx0 or cy1 <= cy0:
        font = fonts.get(round(19 * scale))
        draw.text((cx0, (cy0 + cy1) / 2), empty_label, font=font, fill=_with_alpha(text_color, 175))
        return
    min_t = min(time_s for time_s, _ in points)
    max_t = max(time_s for time_s, _ in points)
    min_p = min(power for _, power in points)
    max_p = max(power for _, power in points)
    if max_t <= min_t:
        max_t = min_t + 1
    if max_p <= min_p:
        min_p = max(0, min_p - 1)
        max_p += 1
    pad_y = max(4, round(9 * scale))
    span_p = max_p - min_p
    chart_points = [
        (
            cx0 + (time_s - min_t) / (max_t - min_t) * (cx1 - cx0),
            cy1 - pad_y - (power - min_p) / span_p * max(1, cy1 - cy0 - pad_y * 2),
        )
        for time_s, power in points
    ]
    draw.line(chart_points, fill=line_color, width=max(3, round(5 * scale)), joint="curve")
    draw.line((cx0, cy1, cx1, cy1), fill=_with_alpha(text_color, 75), width=max(1, round(2 * scale)))


def _power_chart_box(
    route_box: tuple[int, int, int, int],
    scale: float,
) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = route_box
    width = x1 - x0
    height = y1 - y0
    box_width = min(round(width * 0.46), round(390 * scale))
    box_height = min(round(height * 0.42), round(260 * scale))
    right_margin = round(20 * scale)
    bottom_margin = round(20 * scale)
    return (x1 - box_width - right_margin, y1 - box_height - bottom_margin, x1 - right_margin, y1 - bottom_margin)


def _draw_stats(
    image: Image.Image,
    box: tuple[int, int, int, int],
    metrics: list[_Metric],
    text_color: tuple[int, int, int],
    accent: tuple[int, int, int],
    fonts: _Fonts,
    scale: float,
    *,
    compact: bool,
    full_info: bool,
) -> None:
    _draw_panel(image, box, radius=round(28 * scale), fill=(4, 15, 27, 176))
    draw = ImageDraw.Draw(image)
    x0, y0, x1, y1 = box
    padding = max(round(24 * scale), (x1 - x0) // 28)
    inner_x0, inner_x1 = x0 + padding, x1 - padding
    inner_y0, inner_y1 = y0 + padding, y1 - padding
    if not metrics:
        return
    primary = metrics[:2]
    secondary = metrics[2:]
    if compact:
        _draw_compact_stats(
            draw,
            (inner_x0, inner_y0, inner_x1, inner_y1),
            metrics,
            text_color,
            accent,
            fonts,
            scale,
        )
        return

    panel_height = inner_y1 - inner_y0
    primary_height = round(panel_height * (0.44 if secondary else 0.90))
    primary_gap = round(12 * scale)
    cell_width = (inner_x1 - inner_x0 - primary_gap) // max(1, len(primary))
    for index, metric in enumerate(primary):
        cell = (
            inner_x0 + index * (cell_width + primary_gap),
            inner_y0,
            inner_x0 + index * (cell_width + primary_gap) + cell_width,
            inner_y0 + primary_height,
        )
        _draw_metric_cell(draw, cell, metric, text_color, accent, fonts, scale, primary=True)
    if secondary:
        columns = 3 if full_info and len(secondary) > 4 else 2
        rows = math.ceil(len(secondary) / columns)
        gap_x = round(10 * scale)
        gap_y = round(7 * scale)
        secondary_y0 = inner_y0 + primary_height + round(9 * scale)
        available_h = inner_y1 - secondary_y0
        cell_w = (inner_x1 - inner_x0 - gap_x * (columns - 1)) // columns
        cell_h = (available_h - gap_y * (rows - 1)) // rows
        for index, metric in enumerate(secondary):
            row, col = divmod(index, columns)
            cell = (
                inner_x0 + col * (cell_w + gap_x),
                secondary_y0 + row * (cell_h + gap_y),
                inner_x0 + col * (cell_w + gap_x) + cell_w,
                secondary_y0 + row * (cell_h + gap_y) + cell_h,
            )
            _draw_metric_cell(draw, cell, metric, text_color, accent, fonts, scale, primary=False)


def _draw_compact_stats(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    metrics: list[_Metric],
    text_color: tuple[int, int, int],
    accent: tuple[int, int, int],
    fonts: _Fonts,
    scale: float,
) -> None:
    x0, y0, x1, y1 = box
    gap = round(11 * scale)
    cell_h = (y1 - y0 - gap * (len(metrics) - 1)) // max(1, len(metrics))
    for index, metric in enumerate(metrics):
        cell = (x0, y0 + index * (cell_h + gap), x1, y0 + index * (cell_h + gap) + cell_h)
        _draw_metric_cell(draw, cell, metric, text_color, accent, fonts, scale, primary=index < 2, stacked=True)


def _draw_metric_cell(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    metric: _Metric,
    text_color: tuple[int, int, int],
    accent: tuple[int, int, int],
    fonts: _Fonts,
    scale: float,
    *,
    primary: bool,
    stacked: bool = False,
) -> None:
    x0, y0, x1, y1 = box
    width = max(1, x1 - x0)
    height = max(1, y1 - y0)
    if not primary:
        line_y = y0 + round(3 * scale)
        draw.rounded_rectangle(
            (x0, line_y, x0 + round(5 * scale), y1 - round(3 * scale)),
            radius=round(3 * scale),
            fill=accent,
        )
        label_x = x0 + round(15 * scale)
        label_y = y0 + round(5 * scale)
        value_y = y0 + round(30 * scale)
        label_size = round(18 * scale)
        value_size = round(28 * scale)
    elif stacked:
        label_x = x0
        label_y = y0
        value_y = y0 + round(28 * scale)
        label_size = round(17 * scale)
        value_size = round(27 * scale)
    else:
        label_x = x0
        label_y = y0 + round(5 * scale)
        value_y = y0 + round(height * 0.38)
        label_size = round(21 * scale)
        value_size = round(52 * scale if metric.unit != "h" else 40 * scale)
    draw.text((label_x, label_y), metric.label, font=fonts.get(label_size), fill=_with_alpha(text_color, 195))
    unit_text = f"{' ' if not metric.unit.startswith('/') else ''}{metric.unit}" if metric.unit else ""
    value_text = f"{metric.value}{unit_text}"
    _draw_fitted_text(
        draw,
        (label_x, value_y),
        value_text,
        fonts,
        size=value_size,
        max_width=max(1, width - (label_x - x0) - round(8 * scale)),
        color=text_color,
        bold=True,
    )


def _draw_watermark(
    draw: ImageDraw.ImageDraw,
    width: int,
    height: int,
    margin: int,
    watermark: str | None,
    text_color: tuple[int, int, int],
    fonts: _Fonts,
    scale: float,
) -> None:
    text = watermark.strip() if watermark else ""
    if not text:
        return
    font = fonts.get(round(17 * scale), bold=True)
    bbox = draw.textbbox((0, 0), text, font=font)
    x = width - margin - (bbox[2] - bbox[0])
    y = height - round(34 * scale)
    draw.text((x, y), text, font=font, fill=_with_alpha(text_color, 140))


def _select_metrics(
    summary: dict[str, object],
    selected: str,
) -> list[_Metric]:
    distance = _number(summary.get("distance_m"))
    duration = _number(summary.get("timer_time_s")) or _number(summary.get("elapsed_time_s"))
    metrics = [
        _Metric("距离", f"{distance / 1000:.2f}" if distance is not None else "—", "km"),
        _Metric("运动时间", _format_duration(duration), ""),
    ]
    values: dict[str, _Metric | None] = {
        "ascent": _metric_number("累计爬升", summary.get("total_ascent_m"), "m", digits=0),
        "speed": _metric_number("平均速度", _speed_kph(summary), "km/h"),
        "pace": _pace_metric(summary),
        "power": _metric_number("平均功率", summary.get("average_power_w"), "W", digits=0),
    }
    metric = values[selected]
    if metric is None:
        labels = {
            "ascent": ("累计爬升", "m"),
            "speed": ("平均速度", "km/h"),
            "pace": ("平均配速", "/km"),
            "power": ("平均功率", "W"),
        }
        label, unit = labels[selected]
        metric = _Metric(label, "—", unit)
    metrics.append(metric)
    return metrics


def _metric_number(label: str, value: object, unit: str, *, digits: int = 1) -> _Metric | None:
    number = _number(value)
    if number is None:
        return None
    return _Metric(label, f"{number:.{digits}f}" if digits else f"{number:.0f}", unit)


def _speed_kph(summary: dict[str, object]) -> float | None:
    speed = _number(summary.get("average_speed_mps"))
    if speed is None:
        distance = _number(summary.get("distance_m"))
        duration = _number(summary.get("timer_time_s")) or _number(summary.get("elapsed_time_s"))
        if distance is None or duration is None or duration <= 0:
            return None
        speed = distance / duration
    return speed * 3.6 if speed > 0 else None


def _pace_metric(summary: dict[str, object]) -> _Metric | None:
    speed = _number(summary.get("average_speed_mps"))
    if speed is None:
        distance = _number(summary.get("distance_m"))
        duration = _number(summary.get("timer_time_s")) or _number(summary.get("elapsed_time_s"))
        if distance is None or duration is None or duration <= 0:
            return None
        speed = distance / duration
    if speed <= 0:
        return None
    swimming = str(summary.get("sport_type") or "").lower() in {"swimming", "swim"}
    unit_distance = 100.0 if swimming else 1000.0
    minutes, seconds = divmod(round(unit_distance / speed), 60)
    unit = "/100m" if swimming else "/km"
    return _Metric("平均配速", f"{minutes}'{seconds:02d}''", unit)


def _gps_points(points: Iterable[TrackPoint]) -> list[TrackPoint]:
    return [
        point
        for point in points
        if point.latitude is not None
        and point.longitude is not None
        and math.isfinite(point.latitude)
        and math.isfinite(point.longitude)
        and -90 <= point.latitude <= 90
        and -180 <= point.longitude <= 180
    ]


def _power_points(points: Iterable[TrackPoint]) -> list[tuple[float, float]]:
    curve = calculate_activity_power_curve(points)
    return [(float(duration), float(power)) for duration, power in curve.items()]


def _sport_label(sport: str | None) -> str:
    normalized = (sport or "").strip().lower()
    labels = {
        "cycling": "骑行",
        "ride": "骑行",
        "virtual_ride": "虚拟骑行",
        "indoor_cycling": "室内骑行",
        "running": "跑步",
        "run": "跑步",
        "trail_run": "越野跑",
        "swimming": "游泳",
        "walking": "步行",
        "hiking": "徒步",
        "generic": "运动",
    }
    return labels.get(normalized, sport or "")


def _draw_panel(
    image: Image.Image,
    box: tuple[int, int, int, int],
    *,
    radius: int,
    fill: tuple[int, int, int, int],
) -> None:
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rounded_rectangle(box, radius=max(1, radius), fill=fill)
    image.alpha_composite(overlay)
    overlay.close()


def _draw_fitted_text(
    draw: ImageDraw.ImageDraw,
    position: tuple[int, int],
    text: str,
    fonts: _Fonts,
    *,
    size: int,
    max_width: int,
    color: tuple[int, int, int],
    bold: bool = False,
) -> None:
    font = fonts.get(size, bold=bold)
    while size > 16 and draw.textbbox((0, 0), text, font=font)[2] > max_width:
        size = round(size * 0.9)
        font = fonts.get(size, bold=bold)
    draw.text(position, text, font=font, fill=color)


def _parse_color(value: str, label: str) -> tuple[int, int, int]:
    try:
        color = ImageColor.getrgb(value)
    except ValueError as exc:
        raise ValueError(f"Invalid {label}: {value}") from exc
    if len(color) != 3:
        raise ValueError(f"{label.capitalize()} must be an RGB color")
    return color


def _with_alpha(color: tuple[int, int, int], alpha: int) -> tuple[int, int, int, int]:
    return color[0], color[1], color[2], alpha


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "—"
    total = round(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"
