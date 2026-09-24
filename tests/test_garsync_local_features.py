from __future__ import annotations

import contextlib
import io
import json
import tempfile
import threading
import unittest
import urllib.request
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from http.server import ThreadingHTTPServer

from sport_sync_bridge.activity_analysis import build_ai_analysis_prompt, summarize_activity
from sport_sync_bridge.activity_library import LocalActivityLibrary
from sport_sync_bridge.cli import main
from sport_sync_bridge.formats import read_activity_file
from sport_sync_bridge.health import import_health_csv, summarize_health, summarize_health_for_activity
from sport_sync_bridge.sources import LocalFileSource
from sport_sync_bridge.state import StateDB
from sport_sync_bridge.training import (
    export_training_plan_ics,
    export_workout_template,
    get_training_template,
    install_training_plan,
    list_training_templates,
    list_workout_templates,
)
from sport_sync_bridge.wifi_transfer import _make_handler
from tests.activity_fixtures import create_gpx


class GarSyncLocalFeatureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = StateDB(self.root / "state.db")
        self.library = LocalActivityLibrary(self.state, self.root / ".data")

    def tearDown(self) -> None:
        self.state.close()
        self.temporary.cleanup()

    def test_local_import_deduplicates_and_exposes_syncable_source(self) -> None:
        activity_path = create_gpx(self.root / "morning.gpx")
        original = activity_path.read_bytes()
        first = self.library.import_paths([activity_path])[0]
        second = self.library.import_paths([activity_path])[0]

        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(activity_path.read_bytes(), original)
        self.assertEqual(len(self.state.list_local_activities()), 1)
        source = LocalFileSource(SimpleNamespace(), self.state)
        found = source.list_activities(None, None, None)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].source, "local")
        self.assertEqual(source.download_fit(found[0], self.root).suffix, ".gpx")

    def test_zip_import_does_not_extract_paths_and_imports_supported_members(self) -> None:
        activity_path = create_gpx(self.root / "route.gpx")
        archive_path = self.root / "activities.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("../../outside/route.gpx", activity_path.read_bytes())
        results = self.library.import_paths([archive_path])

        self.assertEqual(len(results), 1)
        self.assertEqual(Path(self.state.get_local_activity(results[0].fingerprint)["file_path"]).parent, self.root / ".data" / "local_imports")
        self.assertFalse((self.root / "outside").exists())

    def test_json_and_track_csv_are_normalized_to_gpx(self) -> None:
        json_path = self.root / "activity.json"
        json_path.write_text(
            json.dumps(
                {
                    "name": "JSON ride",
                    "sport_type": "cycling",
                    "track_points": [
                        {"timestamp": "2026-01-02T03:04:00Z", "lat": 31.23, "lon": 121.47, "distance": 0},
                        {"timestamp": "2026-01-02T03:05:00Z", "lat": 31.231, "lon": 121.471, "distance": 100},
                    ],
                }
            ),
            encoding="utf-8",
        )
        csv_path = self.root / "track.csv"
        csv_path.write_text(
            "timestamp,latitude,longitude,elevation,heart_rate\n"
            "2026-01-02T03:04:00Z,31.23,121.47,10,150\n"
            "2026-01-02T03:05:00Z,31.231,121.471,11,151\n",
            encoding="utf-8",
        )

        json_result = self.library.import_paths([json_path])[0]
        csv_result = self.library.import_paths([csv_path])[0]

        self.assertEqual(json_result.file_format, "gpx")
        self.assertEqual(csv_result.file_format, "gpx")
        json_activity = read_activity_file(Path(self.state.get_local_activity(json_result.fingerprint)["file_path"]))
        csv_activity = read_activity_file(Path(self.state.get_local_activity(csv_result.fingerprint)["file_path"]))
        self.assertEqual(json_activity.name, "JSON ride")
        self.assertEqual(csv_activity.track_points[0].heart_rate_bpm, 150)

    def test_invalid_activity_is_not_added_to_library(self) -> None:
        malformed = self.root / "bad.gpx"
        malformed.write_text("<gpx", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Could not parse XML"):
            self.library.import_paths([malformed])
        self.assertEqual(self.state.list_local_activities(), [])

    def test_invalid_json_coordinates_remove_partial_import_files(self) -> None:
        activity_path = self.root / "invalid.json"
        activity_path.write_text(
            json.dumps(
                {
                    "track_points": [
                        {"timestamp": "2026-01-02T03:04:00Z", "lat": 91, "lon": 121},
                    ]
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "Invalid GPS coordinates"):
            self.library.import_paths([activity_path])
        self.assertEqual(list((self.root / ".data" / "local_imports").glob("*")), [])

    def test_activity_start_time_is_normalized_for_date_filters(self) -> None:
        activity_path = create_gpx(self.root / "offset.gpx")
        payload = activity_path.read_text(encoding="utf-8")
        payload = payload.replace("2026-01-02T03:04:00Z", "2026-01-02T00:04:00+09:00")
        payload = payload.replace("2026-01-02T03:05:00Z", "2026-01-02T00:05:00+09:00")
        activity_path.write_text(payload, encoding="utf-8")

        imported = self.library.import_paths([activity_path])[0]
        rows = self.state.list_local_activities(
            since="2026-01-01T00:00:00+00:00",
            until="2026-01-01T23:59:59.999999+00:00",
        )

        self.assertEqual(imported.start_time, "2026-01-01T15:04:00+00:00")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["start_time"], imported.start_time)

    def test_all_localized_plan_templates_schedule_and_export(self) -> None:
        for locale in ("en", "es", "fr", "it", "pt", "zh"):
            self.assertEqual(len(list_training_templates(locale=locale)), 7)
        template = get_training_template("8w_beginner_run", locale="zh")
        plan_id, items = install_training_plan(
            self.state,
            template,
            locale="zh",
            start_date=date(2026, 1, 5),
        )

        self.assertEqual(len(items), 56)
        self.assertEqual(len(self.state.list_training_plans()), 1)
        self.assertTrue(any(item["name"].startswith("5公里毕业跑") for item in items))
        output = export_training_plan_ics(self.state, plan_id, self.root / "plan.ics")
        calendar = output.read_text(encoding="utf-8")
        self.assertIn("BEGIN:VCALENDAR", calendar)
        self.assertIn("DTSTART;VALUE=DATE:20260228", calendar)
        self.assertNotIn("SUMMARY:休息日", calendar)

    def test_plan_requires_monday_start(self) -> None:
        template = get_training_template("8w_beginner_run", locale="zh")
        with self.assertRaisesRegex(ValueError, "Monday"):
            install_training_plan(
                self.state,
                template,
                locale="zh",
                start_date=date(2026, 1, 6),
            )

    def test_workout_fit_assets_can_be_listed_and_exported(self) -> None:
        templates = list_workout_templates()
        self.assertEqual(len(templates), 33)
        output = export_workout_template(templates[0].template_id, self.root / "workout.fit")
        self.assertEqual(output.read_bytes(), templates[0].fit_path.read_bytes())

    def test_health_csv_import_and_bmi_summary(self) -> None:
        health_csv = self.root / "health.csv"
        health_csv.write_text(
            "date,metric,value,unit\n"
            "2026-01-02,weight,70,kg\n"
            "2026-01-02,height,175,cm\n"
            "2026-01-02,steps,8000,count\n",
            encoding="utf-8",
        )
        self.assertEqual(import_health_csv(self.state, health_csv), 3)
        summary = summarize_health(self.state)
        self.assertEqual(summary["measurement_count"], 3)
        self.assertEqual(summary["latest"]["bmi"]["value"], 22.9)

    def test_health_csv_imports_garsync_indicators_and_statuses(self) -> None:
        health_csv = self.root / "health-indicators.csv"
        health_csv.write_text(
            "date,metric,value,unit\n"
            "2026-01-02,vo2_max_run,48,mL/kg/min\n"
            "2026-01-02,vo2_max_ride,45,mL/kg/min\n"
            "2026-01-02,sleep_score,85,score\n"
            "2026-01-02,lt_hr,165,bpm\n"
            "2026-01-02,lt_speed,14.1,km/h\n"
            "2026-01-02,calories,450,kcal\n"
            "2026-01-02,floors,12,count\n"
            "2026-01-02,respiration,14,brpm\n"
            "2026-01-02,hydration,1500,ml\n"
            "2026-01-02,recovery,12,h\n"
            "2026-01-02,hrv_status,Moderate,status\n"
            "2026-01-02,ready_to_train,High,status\n"
            "2026-01-02,fully_recovered,True,status\n",
            encoding="utf-8",
        )

        self.assertEqual(import_health_csv(self.state, health_csv), 13)
        summary = summarize_health(self.state)
        latest = summary["latest"]
        self.assertEqual(latest["vo2_max_run"]["value"], 48)
        self.assertEqual(latest["vo2_max_ride"]["unit"], "mL/kg/min")
        self.assertEqual(latest["sleep_score"]["value"], 85)
        self.assertEqual(latest["lactate_threshold_hr_bpm"]["value"], 165)
        self.assertEqual(latest["lactate_threshold_speed_kmh"]["value"], 14.1)
        self.assertEqual(latest["hydration_l"]["value"], 1.5)
        self.assertEqual(latest["recovery_hours"]["value"], 12)
        self.assertEqual(latest["hrv_status"]["value"], "Moderate")
        self.assertEqual(latest["ready_to_train_status"]["value"], "High")
        self.assertEqual(latest["fully_recovered"]["value"], "True")

        context = summarize_health_for_activity(
            self.state,
            "2026-01-02T03:00:00Z",
            "2026-01-02T04:00:00Z",
        )
        self.assertEqual(context["before_activity"]["hrv_status"]["value"], "Moderate")
        activity = read_activity_file(create_gpx(self.root / "health-context.gpx"))
        prompt = build_ai_analysis_prompt(summarize_activity(activity), [], health_summary=context)
        self.assertIn("hrv_status: Moderate status", prompt)

    def test_activity_health_context_respects_activity_and_utc_day_boundaries(self) -> None:
        observations = [
            ("2025-12-01T08:00:00+00:00", "weight_kg", 70, "kg"),
            ("2026-01-02T02:50:00+00:00", "hrv_ms", 42, "ms"),
            ("2026-01-02T03:00:00+00:00", "spo2_percent", 98, "%"),
            ("2026-01-02T03:30:00+00:00", "stress_score", 70, "score"),
            ("2026-01-02T04:00:00+00:00", "hrv_ms", 99, "ms"),
            ("2026-01-02T04:30:00+00:00", "hrv_ms", 45, "ms"),
            ("2026-01-02T23:59:59+00:00", "body_battery", 63, "score"),
            ("2026-01-03T00:00:00+00:00", "body_battery", 2, "score"),
        ]
        for index, (observed_at, metric, value, unit) in enumerate(observations):
            self.state.upsert_health_observation(
                observed_at=observed_at,
                metric=metric,
                value=value,
                unit=unit,
                source_label="test",
                fingerprint=f"activity-health-{index}",
            )

        context = summarize_health_for_activity(
            self.state,
            "2026-01-02T11:00:00+08:00",
            "2026-01-02T12:00:00+08:00",
        )

        self.assertEqual(context["before_activity"]["weight_kg"]["value"], 70)
        self.assertEqual(context["before_activity"]["hrv_ms"]["value"], 42)
        self.assertNotIn("spo2_percent", context["before_activity"])
        self.assertNotIn("stress_score", context["before_activity"])
        self.assertEqual(context["after_activity"]["hrv_ms"]["value"], 45)
        self.assertEqual(context["after_activity"]["body_battery"]["value"], 63)
        self.assertNotIn("stress_score", context["after_activity"])
        self.assertEqual(
            summarize_health_for_activity(self.state, None, None),
            {"before_activity": {}, "after_activity": {}},
        )

    def test_ai_prompt_uses_summary_data_without_track_coordinates(self) -> None:
        activity = read_activity_file(create_gpx(self.root / "ride.gpx"))
        summary = summarize_activity(activity)
        prompt = build_ai_analysis_prompt(summary, self.state.list_local_activities(), "恢复训练怎么安排？")
        self.assertIn("恢复训练怎么安排", prompt)
        self.assertNotIn("31.23", prompt)
        self.assertNotIn("121.47", prompt)
        self.assertIn("未知", prompt)
        self.assertIn("必须使用语言代码 zh-CN", prompt)
        self.assertIn("医学诊断", prompt)

    def test_ai_analysis_cli_uses_track_speed_for_garsync_intensity(self) -> None:
        start = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
        speeds = [6.0] * 11 + [11.0] * 8 + [12.0] * 12
        points = "".join(
            "<trkpt lat=\"31.23\" lon=\"121.47\">"
            f"<time>{(start + timedelta(seconds=index)).isoformat().replace('+00:00', 'Z')}</time>"
            f"<extensions><ssb:speed>{speed}</ssb:speed></extensions></trkpt>"
            for index, speed in enumerate(speeds)
        )
        activity_path = self.root / "rowing.gpx"
        activity_path.write_text(
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
            "<gpx xmlns=\"http://www.topografix.com/GPX/1/1\" "
            "xmlns:ssb=\"https://sport-sync-bridge.example/xmlschemas/extensions/v1\" "
            "version=\"1.1\" creator=\"test\"><trk><name>Intervals</name>"
            f"<type>rowing</type><trkseg>{points}</trkseg></trk></gpx>",
            encoding="utf-8",
        )
        imported = self.library.import_paths([activity_path])[0]
        self.state.upsert_local_activity(
            fingerprint=imported.fingerprint,
            name="Intervals",
            sport_type="rowing",
            start_time=start.isoformat(),
            file_path=str(activity_path),
            source_label="test",
            file_format="gpx",
            summary={
                "timer_time_s": 30,
                "end_time": (start + timedelta(seconds=30)).isoformat(),
                "time_in_zone_messages": [],
            },
        )
        self.state.upsert_health_observation(
            observed_at="2026-01-02T03:00:00+00:00",
            metric="lactate_threshold_speed_kmh",
            value=36,
            unit="km/h",
            source_label="test",
            fingerprint="threshold-speed-before-activity",
        )
        config = SimpleNamespace(
            data_dir=self.root / ".data",
            db_path=self.state.path,
            log_level="INFO",
            log_path=self.root / "sync.log",
        )
        output = io.StringIO()
        with (
            patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
            patch("sport_sync_bridge.cli.configure_logging"),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(main(["ai-analysis", imported.fingerprint, "--prompt-only"]), 0)

        self.assertIn("FIT 分区训练强度参考：GarSync分类=VO2max", output.getvalue())

    def test_ai_prompt_options_cover_reversed_focus_and_detail_modes(self) -> None:
        activity = read_activity_file(create_gpx(self.root / "ai-options.gpx"))
        summary = summarize_activity(activity)
        focus_text = {
            "performance": "重点分析速度、功率、心率效率",
            "health": "重点关注训练负荷的影响和长期健康价值",
            "recovery": "重点评估恢复状态和过度训练风险",
        }
        detail_text = {
            "brief": "使用纯文本",
            "normal": "训练分析写 2-3 段",
            "detailed": "训练分析覆盖 3-5 个维度",
        }

        for focus, expected in focus_text.items():
            with self.subTest(focus=focus):
                prompt = build_ai_analysis_prompt(summary, [], focus=focus)
                self.assertIn(expected, prompt)
        for detail, expected in detail_text.items():
            with self.subTest(detail=detail):
                prompt = build_ai_analysis_prompt(summary, [], detail=detail)
                self.assertIn(expected, prompt)

        prompt = build_ai_analysis_prompt(summary, [], language="en-US", focus="health", detail="brief")
        self.assertIn("必须使用语言代码 en-US", prompt)
        self.assertIn("不作医学判断", prompt)
        with self.assertRaisesRegex(ValueError, "language code"):
            build_ai_analysis_prompt(summary, [], language="respond in English")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["ai-analysis", "activity-id", "--language", "respond in English"])

    def test_cli_ai_analysis_saves_result_and_history_without_resending(self) -> None:
        activity_path = create_gpx(self.root / "ai-ride.gpx")
        imported = self.library.import_paths([activity_path])[0]
        for observed_at, metric, value, unit, fingerprint in (
            ("2026-01-02T02:45:00+00:00", "sleep_hours", 7.2, "h", "cli-health-before"),
            ("2026-01-02T03:06:00+00:00", "body_battery", 59, "score", "cli-health-after"),
            ("2026-01-03T00:00:00+00:00", "steps", 98765, "count", "cli-health-future"),
        ):
            self.state.upsert_health_observation(
                observed_at=observed_at,
                metric=metric,
                value=value,
                unit=unit,
                source_label="test",
                fingerprint=fingerprint,
            )
        config = SimpleNamespace(
            data_dir=self.root / ".data",
            db_path=self.state.path,
            log_level="INFO",
            log_path=self.root / "sync.log",
            ai_api_base_url="https://ai.example.invalid/v1",
            ai_model="test-model",
            ai_api_key=None,
        )
        output = io.StringIO()
        analysis_results = iter(("本次训练节奏稳定。", "第二次分析已保存。"))

        def fake_analysis_request(**kwargs: object) -> str:
            result = next(analysis_results)
            on_delta = kwargs.get("on_delta")
            if callable(on_delta):
                on_delta(result)
            return result

        with (
            patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
            patch("sport_sync_bridge.cli.configure_logging"),
            patch("sport_sync_bridge.cli.SyncEngine", side_effect=AssertionError("sync engine is not needed")),
            patch(
                "sport_sync_bridge.activity_analysis.request_ai_analysis",
                side_effect=fake_analysis_request,
            ) as request,
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(
                main(
                    [
                        "ai-analysis",
                        imported.fingerprint,
                        "--prompt-only",
                        "--language",
                        "en-US",
                        "--focus",
                        "recovery",
                        "--detail",
                        "detailed",
                    ]
                ),
                0,
            )
            self.assertIn("必须使用语言代码 en-US", output.getvalue())
            self.assertIn("重点评估恢复状态", output.getvalue())
            self.assertIn("训练分析覆盖 3-5 个维度", output.getvalue())
            self.assertIn("sleep_hours: 7.2 h (2026-01-02T02:45:00+00:00)", output.getvalue())
            self.assertIn("body_battery: 59.0 score (2026-01-02T03:06:00+00:00)", output.getvalue())
            self.assertNotIn("98765", output.getvalue())
            self.assertEqual(self.state.list_ai_analysis_results(imported.fingerprint), [])
            self.assertEqual(main(["ai-analysis", imported.fingerprint]), 0)
            self.assertEqual(main(["ai-analysis", imported.fingerprint]), 0)
            self.assertEqual(main(["ai-analysis", imported.fingerprint, "--history"]), 0)

        saved = self.state.list_ai_analysis_results(imported.fingerprint)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(len(saved), 2)
        self.assertEqual(saved[0]["source_id"], "local")
        self.assertEqual(saved[0]["model_name"], "test-model")
        self.assertEqual({row["content"] for row in saved}, {"本次训练节奏稳定。", "第二次分析已保存。"})
        self.assertIn("本次训练节奏稳定。", output.getvalue())
        self.assertIn("第二次分析已保存。", output.getvalue())

    def test_wifi_upload_page_accepts_activity_multipart(self) -> None:
        sample = create_gpx(self.root / "wifi.gpx").read_bytes()
        server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self.library, 1024 * 1024))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            with urllib.request.urlopen(base_url + "/") as response:
                self.assertIn("运动文件传输", response.read().decode("utf-8"))
            boundary = "bridge-test-boundary"
            body = (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"wifi.gpx\"\r\n"
                "Content-Type: application/gpx+xml\r\n\r\n"
            ).encode("ascii") + sample + f"\r\n--{boundary}--\r\n".encode("ascii")
            request = urllib.request.Request(
                base_url + "/upload",
                data=body,
                headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "Content-Length": str(len(body)),
                },
                method="POST",
            )
            with urllib.request.urlopen(request) as response:
                result = json.loads(response.read())
            self.assertEqual(result["imported"], 1)
            self.assertEqual(len(self.state.list_local_activities()), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_wifi_upload_enforces_configured_per_file_limit(self) -> None:
        sample = create_gpx(self.root / "large.gpx").read_bytes()
        server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self.library, 64))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            boundary = "bridge-size-boundary"
            body = (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"large.gpx\"\r\n"
                "Content-Type: application/gpx+xml\r\n\r\n"
            ).encode("ascii") + sample + f"\r\n--{boundary}--\r\n".encode("ascii")
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/upload",
                data=body,
                headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "Content-Length": str(len(body)),
                },
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request)
            self.assertEqual(raised.exception.code, 413)
            raised.exception.close()
            self.assertEqual(self.state.list_local_activities(), [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_cli_local_import_and_list_run_without_account_engine(self) -> None:
        activity_path = create_gpx(self.root / "cli.gpx")
        config = SimpleNamespace(
            data_dir=self.root / ".data",
            db_path=self.root / ".data" / "state.db",
            log_level="INFO",
            log_path=self.root / "sync.log",
        )
        output = io.StringIO()
        with (
            patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
            patch("sport_sync_bridge.cli.configure_logging"),
            patch("sport_sync_bridge.cli.SyncEngine", side_effect=AssertionError("sync engine is not needed")),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(main(["library", "import", str(activity_path)]), 0)
            self.assertEqual(main(["library", "list", "--json"]), 0)
        self.assertIn('"format": "gpx"', output.getvalue())


if __name__ == "__main__":
    unittest.main()
