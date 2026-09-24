from __future__ import annotations

import json
import math
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests


API_BASE_URL = "https://api.unicgames.com/webapi/lbs"
WEATHER_TOKEN_ENV = "GARSYNC_WEATHER_TOKEN"
CACHE_TTL_SECONDS = 15 * 60
REQUEST_TIMEOUT_SECONDS = 15


class WeatherError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class WeatherNow:
    observed_at: str | None
    temperature_c: str | None
    feels_like_c: str | None
    icon_code: str | None
    condition: str | None
    wind_direction: str | None
    wind_scale: str | None
    humidity_percent: str | None

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> WeatherNow:
        return cls(
            observed_at=_optional_text(value.get("obsTime")),
            temperature_c=_optional_text(value.get("temp")),
            feels_like_c=_optional_text(value.get("feelsLike")),
            icon_code=_optional_text(value.get("icon")),
            condition=_optional_text(value.get("text")),
            wind_direction=_optional_text(value.get("windDir")),
            wind_scale=_optional_text(value.get("windScale")),
            humidity_percent=_optional_text(value.get("humidity")),
        )


@dataclass(frozen=True, slots=True)
class AirQualityNow:
    published_at: str | None
    aqi: str | None
    level: str | None
    category: str | None
    primary_pollutant: str | None

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> AirQualityNow:
        return cls(
            published_at=_optional_text(value.get("pubTime")),
            aqi=_optional_text(value.get("aqi")),
            level=_optional_text(value.get("level")),
            category=_optional_text(value.get("category")),
            primary_pollutant=_optional_text(value.get("primary")),
        )


@dataclass(frozen=True, slots=True)
class WeatherInfo:
    latitude: float
    longitude: float
    language: str
    fetched_at: str
    location_name: str
    weather: WeatherNow
    air: AirQualityNow | None

    def to_json(self) -> dict[str, Any]:
        return {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "language": self.language,
            "fetchTime": self.fetched_at,
            "locationName": self.location_name,
            "weather": asdict(self.weather),
            "air": asdict(self.air) if self.air is not None else None,
        }

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> WeatherInfo:
        weather = value.get("weather")
        air = value.get("air")
        if not isinstance(weather, dict) or (air is not None and not isinstance(air, dict)):
            raise WeatherError("Weather cache has invalid weather or air quality data")
        try:
            return cls(
                latitude=float(value["latitude"]),
                longitude=float(value["longitude"]),
                language=str(value["language"]),
                fetched_at=str(value["fetchTime"]),
                location_name=str(value["locationName"]),
                weather=WeatherNow.from_json(_weather_fields_from_cache(weather)),
                air=AirQualityNow.from_json(_air_fields_from_cache(air)) if isinstance(air, dict) else None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WeatherError("Weather cache has an invalid structure") from exc


def fetch_weather(
    latitude: float,
    longitude: float,
    token: str,
    language: str = "zh",
    *,
    request_get: Callable[..., Any] | None = None,
) -> WeatherInfo:
    latitude, longitude = _validate_coordinates(latitude, longitude)
    if not token.strip():
        raise WeatherError(f"Set {WEATHER_TOKEN_ENV} in the local .env file before requesting weather")

    get = request_get or requests.get
    params = {
        "x": str(longitude),
        "y": str(latitude),
        "token": token.strip(),
        "lang": language,
    }
    endpoints = {
        "weather": "v7_weather_now.php",
        "air": "v7_air_now.php",
        "location": "v2_city_lookup.php",
    }

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            name: pool.submit(_request_json, get, endpoint, params, name)
            for name, endpoint in endpoints.items()
        }
        weather_payload = futures["weather"].result()
        air_payload = futures["air"].result()
        location_payload = futures["location"].result()

    weather_now = weather_payload.get("now")
    air_now = air_payload.get("now") if air_payload is not None else None
    if not isinstance(weather_now, dict):
        raise WeatherError("Weather provider returned no current weather record")
    if air_payload is not None and not isinstance(air_now, dict):
        raise WeatherError("Weather provider returned an invalid current air quality record")
    locations = location_payload.get("location")
    if not isinstance(locations, list) or not locations or not isinstance(locations[0], dict):
        raise WeatherError("Weather provider returned no location name")

    return WeatherInfo(
        latitude=latitude,
        longitude=longitude,
        language=language,
        fetched_at=_utc_now().isoformat(),
        location_name=_optional_text(locations[0].get("name")) or "Unknown location",
        weather=WeatherNow.from_json(weather_now),
        air=AirQualityNow.from_json(air_now) if isinstance(air_now, dict) else None,
    )


def get_weather(
    latitude: float,
    longitude: float,
    token: str | None,
    cache_path: Path,
    language: str = "zh",
    *,
    force_refresh: bool = False,
    request_get: Callable[..., Any] | None = None,
    now: datetime | None = None,
) -> WeatherInfo:
    latitude, longitude = _validate_coordinates(latitude, longitude)
    current_time = now or _utc_now()
    if current_time.tzinfo is None:
        raise ValueError("now must include a timezone")

    if not force_refresh:
        cached = _load_cache(cache_path)
        if cached is not None and _cache_matches(cached, latitude, longitude, language, current_time):
            return cached

    if not token or not token.strip():
        raise WeatherError(f"Set {WEATHER_TOKEN_ENV} in the local .env file before requesting weather")
    info = fetch_weather(
        latitude,
        longitude,
        token,
        language,
        request_get=request_get,
    )
    _write_cache(cache_path, info)
    return info


def outdoor_advice(info: WeatherInfo, language: str = "zh") -> str:
    messages = {
        "zh": {
            "air_bad": "空气质量过差，不建议进行户外运动。",
            "bad_weather": "目前天气不宜户外运动。",
            "too_cold": "气温较低，注意保暖。",
            "too_hot": "气温与湿度过高，当心中暑。",
            "suitable": "天气适宜，适合户外运动。",
            "unknown": "天气数据不足，暂无法判断户外条件。",
        },
        "en": {
            "air_bad": "Air quality is poor; outdoor exercise is not recommended.",
            "bad_weather": "Current weather is unsuitable for outdoor exercise.",
            "too_cold": "It is cold; dress warmly for outdoor exercise.",
            "too_hot": "Heat and humidity may raise the risk of heat illness.",
            "suitable": "Weather is suitable for outdoor exercise.",
            "unknown": "There is not enough weather data to assess outdoor conditions.",
        },
    }
    text = messages.get(language, messages["zh"])

    aqi = _number(info.air.aqi) if info.air is not None else None
    if aqi is not None and aqi > 100:
        return text["air_bad"]

    icon_code = _number(info.weather.icon_code)
    if icon_code is not None and 300 <= icon_code <= 515:
        return text["bad_weather"]

    temperature = _number(info.weather.temperature_c)
    if temperature is None:
        return text["unknown"]
    if temperature < 10:
        return text["too_cold"]
    if temperature >= 28:
        return text["too_hot"]
    return text["suitable"]


def format_weather_report(info: WeatherInfo, language: str = "zh", output_format: str = "text") -> str:
    advice = outdoor_advice(info, language)
    if output_format == "json":
        result = info.to_json()
        result["outdoorAdvice"] = advice
        return json.dumps(result, ensure_ascii=False, indent=2)
    if output_format != "text":
        raise ValueError(f"Unsupported weather output format: {output_format}")

    if language == "en":
        air_line = (
            f"Air quality: AQI {_display(info.air.aqi)} · {_display(info.air.category)}"
            if info.air is not None
            else "Air quality: unavailable"
        )
        lines = [
            f"Weather · {info.location_name}",
            f"Conditions: {_display(info.weather.condition)} (code {_display(info.weather.icon_code)})",
            f"Temperature: {_display(info.weather.temperature_c)} °C; feels like {_display(info.weather.feels_like_c)} °C",
            f"Wind: {_display(info.weather.wind_direction)} {_display(info.weather.wind_scale)}; humidity {_display(info.weather.humidity_percent)}%",
            air_line,
            f"Outdoor advice: {advice}",
            f"Observed: {_display(info.weather.observed_at)}",
        ]
    else:
        air_line = (
            f"空气质量：AQI {_display(info.air.aqi)} · {_display(info.air.category)}"
            if info.air is not None
            else "空气质量：暂无数据"
        )
        lines = [
            f"天气 · {info.location_name}",
            f"天气状况：{_display(info.weather.condition)}（代码 {_display(info.weather.icon_code)}）",
            f"气温：{_display(info.weather.temperature_c)}℃；体感 {_display(info.weather.feels_like_c)}℃",
            f"风：{_display(info.weather.wind_direction)} {_display(info.weather.wind_scale)}级；湿度 {_display(info.weather.humidity_percent)}%",
            air_line,
            f"户外建议：{advice}",
            f"观测时间：{_display(info.weather.observed_at)}",
        ]
    return "\n".join(lines)


def _request_json(
    request_get: Callable[..., Any],
    endpoint: str,
    params: dict[str, str],
    label: str,
) -> dict[str, Any] | None:
    try:
        response = request_get(
            f"{API_BASE_URL}/{endpoint}",
            params=params,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise WeatherError(f"{label} request failed ({type(exc).__name__})") from None
    except (TypeError, ValueError) as exc:
        raise WeatherError(f"{label} response was not valid JSON ({type(exc).__name__})") from None

    if not isinstance(payload, dict):
        raise WeatherError(f"{label} response must be a JSON object")
    if str(payload.get("code", "")) != "200":
        problem = payload.get("error")
        if isinstance(problem, dict):
            status = problem.get("status", "unknown status")
            title = problem.get("title", "provider error")
            if label == "air" and str(status) == "400" and str(title).casefold() == "data not available":
                return None
            raise WeatherError(f"{label} provider response failed ({status}: {title})")
        raise WeatherError(f"{label} provider response code was {payload.get('code', 'missing')}")
    return payload


def _load_cache(path: Path) -> WeatherInfo | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WeatherError(f"Cannot read weather cache at {path} ({type(exc).__name__})") from exc
    if not isinstance(value, dict):
        raise WeatherError(f"Weather cache at {path} must contain a JSON object")
    return WeatherInfo.from_json(value)


def _write_cache(path: Path, info: WeatherInfo) -> None:
    temporary_path = path.with_name(f".{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path.write_text(json.dumps(info.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary_path, path)
    except OSError as exc:
        raise WeatherError(f"Cannot write weather cache at {path} ({type(exc).__name__})") from exc


def _cache_matches(
    info: WeatherInfo,
    latitude: float,
    longitude: float,
    language: str,
    now: datetime,
) -> bool:
    if (
        round(info.latitude, 3) != round(latitude, 3)
        or round(info.longitude, 3) != round(longitude, 3)
        or info.language != language
    ):
        return False
    try:
        fetched_at = datetime.fromisoformat(info.fetched_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WeatherError("Weather cache has an invalid fetch time") from exc
    if fetched_at.tzinfo is None:
        return False
    age_seconds = (now.astimezone(timezone.utc) - fetched_at.astimezone(timezone.utc)).total_seconds()
    return 0 <= age_seconds < CACHE_TTL_SECONDS


def _validate_coordinates(latitude: float, longitude: float) -> tuple[float, float]:
    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except (TypeError, ValueError) as exc:
        raise WeatherError("Latitude and longitude must be numbers") from exc
    if not math.isfinite(latitude) or not -90 <= latitude <= 90:
        raise WeatherError("Latitude must be between -90 and 90")
    if not math.isfinite(longitude) or not -180 <= longitude <= 180:
        raise WeatherError("Longitude must be between -180 and 180")
    return latitude, longitude


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


def _number(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _display(value: str | None) -> str:
    return value if value not in (None, "") else "—"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _weather_fields_from_cache(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "obsTime": value.get("observed_at"),
        "temp": value.get("temperature_c"),
        "feelsLike": value.get("feels_like_c"),
        "icon": value.get("icon_code"),
        "text": value.get("condition"),
        "windDir": value.get("wind_direction"),
        "windScale": value.get("wind_scale"),
        "humidity": value.get("humidity_percent"),
    }


def _air_fields_from_cache(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "pubTime": value.get("published_at"),
        "aqi": value.get("aqi"),
        "level": value.get("level"),
        "category": value.get("category"),
        "primary": value.get("primary_pollutant"),
    }
