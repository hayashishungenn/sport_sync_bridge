from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import requests

from sport_sync_bridge.cli import main
from sport_sync_bridge.weather import (
    AirQualityNow,
    WeatherError,
    WeatherInfo,
    WeatherNow,
    fetch_weather,
    format_weather_report,
    get_weather,
    outdoor_advice,
)


class FakeResponse:
    def __init__(self, payload: dict[str, object], status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError("request failed")

    def json(self) -> dict[str, object]:
        return self.payload


def response_for_url(url: str) -> FakeResponse:
    if url.endswith("v7_weather_now.php"):
        return FakeResponse(
            {
                "code": "200",
                "now": {
                    "obsTime": "2026-09-24T12:00+08:00",
                    "temp": "22",
                    "feelsLike": "23",
                    "icon": "100",
                    "text": "晴",
                    "windDir": "东风",
                    "windScale": "2",
                    "humidity": "60",
                },
            }
        )
    if url.endswith("v7_air_now.php"):
        return FakeResponse(
            {
                "code": "200",
                "now": {
                    "pubTime": "2026-09-24T12:00+08:00",
                    "aqi": "42",
                    "level": "1",
                    "category": "优",
                    "primary": "NA",
                },
            }
        )
    return FakeResponse({"code": "200", "location": [{"name": "香港"}]})


class WeatherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.temporary.name) / "weather_cache.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_fetches_three_aot_endpoints_and_parses_shared_fields(self) -> None:
        def fake_get(url: str, **kwargs: object) -> FakeResponse:
            self.assertEqual(kwargs["params"], {
                "x": "114.2",
                "y": "22.3",
                "token": "test-token",
                "lang": "zh",
            })
            return response_for_url(url)

        info = fetch_weather(22.3, 114.2, "test-token", request_get=fake_get)

        self.assertEqual(info.location_name, "香港")
        self.assertEqual(info.weather.temperature_c, "22")
        self.assertEqual(info.air.aqi, "42")
        self.assertEqual(info.weather.wind_scale, "2")

    def test_cache_is_reused_for_fifteen_minutes_then_refreshed(self) -> None:
        calls = 0

        def fake_get(url: str, **kwargs: object) -> FakeResponse:
            nonlocal calls
            calls += 1
            return response_for_url(url)

        first = get_weather(22.3, 114.2, "test-token", self.cache_path, request_get=fake_get)
        cached = get_weather(
            22.3,
            114.2,
            None,
            self.cache_path,
            now=datetime.now(timezone.utc) + timedelta(minutes=14),
            request_get=fake_get,
        )
        self.assertEqual(calls, 3)
        self.assertEqual(cached, first)

        get_weather(
            22.3,
            114.2,
            "test-token",
            self.cache_path,
            now=datetime.now(timezone.utc) + timedelta(minutes=16),
            request_get=fake_get,
        )
        self.assertEqual(calls, 6)

    def test_air_quality_and_weather_thresholds_drive_outdoor_advice(self) -> None:
        info = WeatherInfo(
            latitude=22.3,
            longitude=114.2,
            language="zh",
            fetched_at=datetime.now(timezone.utc).isoformat(),
            location_name="香港",
            weather=WeatherNow("now", "9", "9", "100", "晴", "东风", "2", "60"),
            air=AirQualityNow("now", "42", "1", "优", None),
        )
        self.assertEqual(outdoor_advice(info), "气温较低，注意保暖。")
        self.assertIn("香港", format_weather_report(info))
        self.assertEqual(outdoor_advice(replace(info, weather=replace(info.weather, temperature_c="10"))), "天气适宜，适合户外运动。")
        self.assertEqual(outdoor_advice(replace(info, weather=replace(info.weather, temperature_c="28"))), "气温与湿度过高，当心中暑。")
        self.assertEqual(outdoor_advice(replace(info, weather=replace(info.weather, icon_code="300"))), "目前天气不宜户外运动。")

        polluted = replace(info, air=AirQualityNow("now", "101", "3", "轻度污染", "PM2.5"))
        self.assertEqual(outdoor_advice(polluted), "空气质量过差，不建议进行户外运动。")

    def test_weather_cli_renders_report_and_json(self) -> None:
        output = io.StringIO()
        data_dir = Path(self.temporary.name) / "data"
        with patch.dict(os.environ, {"GARSYNC_WEATHER_TOKEN": "test-token", "SYNC_DATA_DIR": str(data_dir)}):
            with patch("sport_sync_bridge.cli.configure_logging"):
                with patch("sport_sync_bridge.weather.requests.get", side_effect=lambda url, **_: response_for_url(url)):
                    with contextlib.redirect_stdout(output):
                        result = main(["weather", "--lat", "22.3", "--lon", "114.2"])
        self.assertEqual(result, 0)
        self.assertIn("天气 · 香港", output.getvalue())
        self.assertIn("AQI 42", output.getvalue())
        self.assertTrue((data_dir / "weather_cache.json").is_file())

        data = json.loads(format_weather_report(
            get_weather(22.3, 114.2, None, data_dir / "weather_cache.json"),
            output_format="json",
        ))
        self.assertEqual(data["weather"]["temperature_c"], "22")
        self.assertIn("outdoorAdvice", data)

    def test_bad_provider_response_does_not_expose_token(self) -> None:
        def fake_get(url: str, **kwargs: object) -> FakeResponse:
            return FakeResponse({"code": "401", "message": "denied"})

        with self.assertRaises(WeatherError) as raised:
            fetch_weather(22.3, 114.2, "private-test-token", request_get=fake_get)
        self.assertNotIn("private-test-token", str(raised.exception))

    def test_unavailable_air_data_keeps_weather_and_can_be_cached(self) -> None:
        def fake_get(url: str, **kwargs: object) -> FakeResponse:
            if url.endswith("v7_air_now.php"):
                return FakeResponse({"error": {"status": 400, "title": "Data Not Available"}})
            return response_for_url(url)

        info = get_weather(22.3, 114.2, "test-token", self.cache_path, request_get=fake_get)

        self.assertIsNone(info.air)
        self.assertEqual(info.weather.condition, "晴")
        self.assertEqual(outdoor_advice(info), "天气适宜，适合户外运动。")
        self.assertIn("空气质量：暂无数据", format_weather_report(info))
        cached = get_weather(22.3, 114.2, None, self.cache_path)
        self.assertEqual(cached, info)

    def test_invalid_coordinates_are_rejected_before_network_access(self) -> None:
        with self.assertRaisesRegex(WeatherError, "Latitude"):
            fetch_weather(91, 114.2, "test-token", request_get=lambda *_args, **_kwargs: self.fail())


if __name__ == "__main__":
    unittest.main()
