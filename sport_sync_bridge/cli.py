from __future__ import annotations

import argparse
import asyncio
import csv
import getpass
import json
import os
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .activity_analysis import (
    build_ai_analysis_prompt,
    format_activity_report,
    summarize_rows,
    validate_ai_language_code,
    write_activity_report_pdf,
    write_ai_analysis_markdown,
    write_ai_analysis_pdf,
)
from .ai_workout import (
    build_ai_single_workout_prompts,
    make_workout_output_path,
    normalize_ai_workout,
    write_ai_workout_fit,
)
from .activity_charts import write_activity_charts
from .activity_map import write_route_map
from .activity_poster import POSTER_LAYOUTS, POSTER_METRICS, POSTER_RATIOS, write_activity_poster
from .activity_library import LocalActivityLibrary
from .activity_merge import merge_fit_files
from .ai_preferences import (
    AI_ANALYSIS_DETAILS,
    AI_ANALYSIS_FOCI,
    load_ai_analysis_preferences,
    reset_ai_analysis_preferences,
    save_ai_analysis_preferences,
)
from .ai_profile import (
    AI_ATHLETE_PROFILE_FIELDS,
    AI_ATHLETE_PROFILE_GENDERS,
    AI_ATHLETE_PROFILE_INTEGER_FIELDS,
    load_ai_athlete_profile,
    reset_ai_athlete_profile,
    save_ai_athlete_profile,
)
from .ble_sensors import (
    BLE_TYPES,
    BleDeviceRegistry,
    BleError,
    scan_ble_devices,
    read_battery_level,
    stream_heart_rate,
    stream_sensor_data,
    validate_sensor_recording_options,
)
from .ble_bigrun_ecg import (
    BIGRUN_ECG_MODES,
    BIGRUN_ECG_SAMPLE_RATE_HZ,
    decode_bigrun_ecg_payload,
    set_bigrun_ecg_work_mode,
    stream_bigrun_ecg,
    validate_bigrun_ecg_options,
)
from .ble_permission_guide import format_ble_permission_guide
from .ble_trainer import (
    RideRunResult,
    run_trainer_course,
    set_trainer_resistance_mode,
    set_trainer_target_power,
)
from .virtual_ride import (
    RideCourse,
    RideCourseError,
    demo_ride_course,
    intensity_multiplier_from_percent,
    load_ride_course,
    write_ride_activity_fit,
)
from .config import AppConfig
from .ecg_signal import EcgSignalNormalizer, analyze_bigrun_ecg_signal
from .engine import SyncEngine
from .google_health import (
    GOOGLE_HEALTH_FITBIT_DATASETS,
    GoogleHealthClient,
    validate_google_health_date_range,
)
from .intervals_icu import IntervalsIcuSource
from .mywhoosh_source import MyWhooshSource
from .mywhoosh_workouts import remote_workout_summary, upload_mywhoosh_workout
from .health_sources import (
    GARMIN_HEALTH_DETAIL_ENDPOINTS,
    fetch_garmin_health_details,
    fetch_garmin_training_readiness,
    fetch_garmin_user_summaries,
    validate_training_readiness_date_range,
    validate_garmin_health_detail_date_range,
    validate_garmin_user_summary_date_range,
    fetch_intervals_icu_wellness,
)
from .targets import GarminTarget
from .fit_tools import (
    normalize_fit_coordinates,
    repair_fit_track_continuity,
    smooth_fit_gps_track,
)
from .formats import SUPPORTED_FORMATS, TrackPoint, convert_activity_file, read_activity_file
from .force_vector_analysis import (
    FORCE_VECTOR_FOCUSES,
    build_force_vector_analysis_prompt,
    load_force_vector_snapshot,
)
from .health import (
    import_garmin_health_details,
    import_garmin_user_summaries,
    import_google_health_daily_summary,
    import_google_health_data_points,
    import_intervals_icu_wellness,
    format_health_summary_text,
    import_health_csv,
    list_garmin_health_details,
    list_garmin_user_summaries,
    summarize_health,
    summarize_health_for_activity,
)
from .training_readiness import (
    format_training_readiness_text,
    import_training_readiness_json,
    import_training_readiness_records,
    summarize_training_readiness,
)
from .period_summary import calculate_period_summary, format_period_summary
from .running_dynamics import (
    analyze_running_dynamics_file,
    format_running_dynamics,
)
from .samba import import_samba_activity, list_samba_directory
from .share_links import build_friend_invite_url, build_group_invite_url
from .social_feed import SocialFeedClient, SocialFeedError
from .social_friends import NakamaFriendsClient, NakamaFriendsError
from .social_publish import build_activity_publish_data
from .state import StateDB
from .training_balance import calculate_training_balance, format_training_balance
from .swim_css import (
    calculate_swim_css,
    calculate_swim_rest_seconds,
    format_swim_css,
    format_swim_rest,
    parse_swim_time,
)
from .vdot import analyze_running_activities, format_vdot_report
from .training import (
    export_training_plan_ics,
    export_workout_template,
    format_training_plan_progress,
    get_training_template,
    get_workout_template,
    install_training_plan,
    link_training_activity,
    list_training_schedule,
    list_training_templates,
    list_workout_templates,
    summarize_training_plan_progress,
    unlink_training_activity,
)
from .utils import configure_logging, ensure_directory, pack_directory_to_base64_zip, parse_datetime, safe_filename
from .weather import WeatherError, format_weather_report, get_weather
from .wifi_transfer import serve_transfer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import, analyze, convert, and sync sports activity files."
    )
    subparsers = parser.add_subparsers(dest="command", required=False)

    sync_parser = subparsers.add_parser("sync", help="Run a sync pass")
    sync_parser.add_argument(
        "--source",
        action="append",
        choices=[
            "igpsport",
            "onelap",
            "intervals_icu",
            "local",
            "garmin",
            "strava",
            "concept2",
            "hammerhead",
            "polar",
            "wahoo",
            "fitbit",
            "withings",
            "coros",
            "smashrun",
            "ridewithgps",
            "nolio",
            "suunto",
            "cycling_analytics",
            "mywhoosh",
        ],
        help="Repeatable source",
    )
    sync_parser.add_argument(
        "--target",
        action="append",
        choices=[
            "garmin",
            "strava",
            "wahoo",
            "hammerhead",
            "intervals_icu",
            "ridewithgps",
            "nolio",
            "suunto",
            "cycling_analytics",
        ],
        help="Repeatable target",
    )
    sync_parser.add_argument("--from", dest="date_from", help="Start date, e.g. 2026-01-01")
    sync_parser.add_argument("--to", dest="date_to", help="End date, e.g. 2026-03-01")
    sync_parser.add_argument("--limit", type=int, help="Limit activities per source")
    sync_parser.add_argument(
        "--format",
        dest="target_formats",
        action="append",
        type=_parse_target_format,
        metavar="TARGET=FORMAT",
        help="Upload format for a target (repeatable; fit, gpx, or tcx)",
    )
    sync_parser.add_argument("--dry-run", action="store_true", help="List candidates without uploading")
    sync_parser.add_argument("--loop", action="store_true", help="Run forever with a polling interval")
    sync_parser.add_argument("--interval", type=int, help="Loop interval in seconds")

    convert_parser = subparsers.add_parser("convert", help="Convert a local FIT, GPX, or TCX activity file")
    convert_parser.add_argument("input", type=Path, help="Input activity file")
    convert_parser.add_argument("--to", required=True, choices=sorted(SUPPORTED_FORMATS), help="Output format")
    convert_parser.add_argument("--output", required=True, type=Path, help="Output file path")
    convert_parser.add_argument(
        "--source",
        choices=["igpsport", "onelap"],
        help="Coordinate fallback for unmatched FIT device rules",
    )

    library_parser = subparsers.add_parser("library", help="Import and manage local activity files")
    library_actions = library_parser.add_subparsers(dest="library_action", required=True)
    library_repair_fit = library_actions.add_parser(
        "repair-fit-continuity",
        help="Remove FIT track records with backward timestamps or gaps over 48 hours",
    )
    library_repair_fit.add_argument("input", type=Path, help="Input FIT file")
    library_repair_fit.add_argument("--output", required=True, type=Path, help="Repaired FIT output path")
    library_smooth_gps = library_actions.add_parser(
        "smooth-gps",
        help="Smooth a FIT GPS track with an adaptive Kalman filter",
    )
    library_smooth_gps.add_argument("input", type=Path, help="Input FIT file")
    library_smooth_gps.add_argument("--output", required=True, type=Path, help="Smoothed FIT output path")
    library_smooth_gps.add_argument(
        "--accuracy-m",
        type=float,
        help="Fallback GPS accuracy in metres for records without gps_accuracy",
    )
    library_smooth_gps.add_argument(
        "--q",
        type=float,
        default=2.5,
        help="Kalman process noise Q (default: 2.5)",
    )
    library_smooth_gps.add_argument(
        "--no-adaptive-q",
        action="store_true",
        help="Disable speed- and turn-based Q adjustment",
    )
    library_import = library_actions.add_parser("import", help="Import FIT, GPX, TCX, JSON, CSV, or ZIP files")
    library_import.add_argument("paths", nargs="+", type=Path, help="Files or directories to import")
    library_import.add_argument("--recursive", action="store_true", help="Scan directories recursively")
    library_import.add_argument(
        "--password-env",
        default="ACTIVITY_ARCHIVE_PASSWORD",
        help="Environment variable for encrypted ZIP passwords (default: ACTIVITY_ARCHIVE_PASSWORD)",
    )
    library_preview = library_actions.add_parser(
        "preview", help="Inspect supported activity files without importing them"
    )
    library_preview.add_argument("paths", nargs="+", type=Path, help="Files or directories to preview")
    library_preview.add_argument("--recursive", action="store_true", help="Scan directories recursively")
    library_preview.add_argument(
        "--password-env",
        default="ACTIVITY_ARCHIVE_PASSWORD",
        help="Environment variable for encrypted ZIP passwords",
    )
    library_list = library_actions.add_parser("list", help="List imported activities")
    library_list.add_argument("--from", dest="date_from", help="Filter start date")
    library_list.add_argument("--to", dest="date_to", help="Filter end date")
    library_list.add_argument("--sport", help="Filter sport type")
    library_list.add_argument("--limit", type=int, help="Maximum number of rows")
    library_list.add_argument("--json", action="store_true", help="Print JSON lines")
    library_show = library_actions.add_parser("show", help="Show a local activity summary")
    library_show.add_argument("activity_id", help="Activity fingerprint or its unique prefix")
    library_actions.add_parser("stats", help="Summarize local activity volume by sport and week")
    library_period = library_actions.add_parser("period", help="Summarize a local training period")
    library_period.add_argument("--days", type=int, default=90, help="Trailing period length in days (default: 90)")
    library_period.add_argument(
        "--from",
        dest="date_from",
        type=_parse_period_datetime,
        help="First activity date to include",
    )
    library_period.add_argument(
        "--to",
        dest="date_to",
        type=_parse_period_datetime,
        help="Last activity date to include (inclusive)",
    )
    library_period.add_argument("--sport", help="Include only this sport type")
    library_period.add_argument("--resting-hr", type=float, default=60.0, help="Resting heart rate for HR-TSS (default: 60)")
    library_period.add_argument("--threshold-hr", type=float, help="Lactate threshold heart rate for HR-TSS")
    library_period.add_argument("--format", choices=["json", "txt"], default="json")
    library_balance = library_actions.add_parser("balance", help="Calculate local HR-TSS, CTL, ATL, and TSB")
    library_balance.add_argument("--resting-hr", type=float, default=60.0, help="Resting heart rate in bpm (default: 60)")
    library_balance.add_argument("--threshold-hr", type=float, help="Lactate threshold heart rate in bpm for HR-TSS")
    library_balance.add_argument("--from", dest="date_from", help="First date to show; earlier activities still seed CTL/ATL")
    library_balance.add_argument("--to", dest="date_to", help="Last date to show (inclusive)")
    library_balance.add_argument("--format", choices=["json", "csv", "txt"], default="json")
    library_vdot = library_actions.add_parser("vdot", help="Calculate local running VDOT and training paces")
    library_vdot.add_argument("--from", dest="date_from", help="First activity date to include")
    library_vdot.add_argument("--to", dest="date_to", help="Last activity date to include (inclusive)")
    library_vdot.add_argument("--format", choices=["json", "txt"], default="json")
    library_swim_css = library_actions.add_parser(
        "swim-css", help="Calculate swim CSS from 200 m and 400 m time trials"
    )
    library_swim_css.add_argument("--time-200m", required=True, help="200 m time as M:SS or seconds")
    library_swim_css.add_argument("--time-400m", required=True, help="400 m time as M:SS or seconds")
    library_swim_css.add_argument("--pool-length", choices=[25, 50], type=int, default=25)
    library_swim_css.add_argument("--format", choices=["json", "txt"], default="json")
    library_swim_rest = library_actions.add_parser(
        "swim-rest", help="Calculate the default swim interval rest from its distance"
    )
    library_swim_rest.add_argument("distance_m", type=int, help="Swim interval distance in meters")
    library_swim_rest.add_argument("--format", choices=["json", "txt"], default="json")
    library_running_dynamics = library_actions.add_parser(
        "running-dynamics", help="Analyze a local accelerometer and GPS event stream"
    )
    library_running_dynamics.add_argument("input", type=Path, help="JSON Lines sensor event file")
    library_running_dynamics.add_argument("--height-cm", type=float, default=175.0)
    library_running_dynamics.add_argument("--format", choices=["json", "txt"], default="json")
    library_report = library_actions.add_parser("report", help="Export local activity summaries")
    library_report.add_argument("--format", choices=["txt", "json", "csv", "html", "pdf"], default="txt")
    library_report.add_argument("--output", type=Path, help="Output path; omit to print text reports to stdout")
    library_poster = library_actions.add_parser("poster", help="Create a share poster for one local activity")
    library_poster.add_argument("activity_id", help="Activity fingerprint or its unique prefix")
    library_poster.add_argument("--output", type=Path, required=True, help="JPEG output path")
    library_poster.add_argument("--layout", choices=POSTER_LAYOUTS, default="classic")
    library_poster.add_argument("--ratio", choices=sorted(POSTER_RATIOS), default="portrait")
    library_poster.add_argument("--photo", type=Path, help="Optional background photo")
    library_poster.add_argument("--title", help="Override the activity title")
    library_poster.add_argument("--user", help="Display name shown below the title")
    track_visibility = library_poster.add_mutually_exclusive_group()
    track_visibility.add_argument("--show-track", dest="show_track", action="store_true", help="Show the GPS track")
    track_visibility.add_argument("--no-track", dest="show_track", action="store_false", help="Hide the GPS track")
    title_visibility = library_poster.add_mutually_exclusive_group()
    title_visibility.add_argument("--show-title", dest="show_title", action="store_true", help="Show the activity title")
    title_visibility.add_argument("--no-title", dest="show_title", action="store_false", help="Hide the activity title")
    power_visibility = library_poster.add_mutually_exclusive_group()
    power_visibility.add_argument("--power-curve", dest="power_curve", action="store_true", help="Show the duration power curve")
    power_visibility.add_argument("--no-power-curve", dest="power_curve", action="store_false", help="Hide the power curve")
    library_poster.add_argument("--metric", dest="poster_metric", choices=POSTER_METRICS, help="Choose ascent, speed, pace, or average power")
    library_poster.add_argument("--text-color", default="#F5F7FA", help="Poster text color in #RRGGBB form")
    library_poster.add_argument("--track-color", default="#51E2B7", help="Track color in #RRGGBB form")
    library_poster.add_argument("--accent-color", help="Accent color in #RRGGBB form")
    library_poster.add_argument("--font", type=Path, help="Font file for poster text")
    watermark = library_poster.add_mutually_exclusive_group()
    watermark.add_argument("--watermark", metavar="TEXT", help="Set watermark text")
    watermark.add_argument("--no-watermark", dest="watermark", action="store_const", const=None)
    library_poster.set_defaults(show_track=None, show_title=None, power_curve=None, watermark="SPORT SYNC BRIDGE")
    library_route = library_actions.add_parser("route", help="Export one activity track")
    library_route.add_argument("activity_id", help="Activity fingerprint or its unique prefix")
    library_route.add_argument("--to", choices=sorted(SUPPORTED_FORMATS), required=True)
    library_route.add_argument("--output", type=Path, required=True)
    library_map = library_actions.add_parser("map", help="Create an interactive HTML map for one activity")
    library_map.add_argument("activity_id", help="Activity fingerprint or its unique prefix")
    library_map.add_argument("--output", type=Path, required=True, help="HTML output path")
    library_chart = library_actions.add_parser("chart", help="Export time-series charts for one activity")
    library_chart.add_argument("activity_id", help="Activity fingerprint or its unique prefix")
    library_chart.add_argument("--output", type=Path, required=True, help="HTML output path")
    library_merge = library_actions.add_parser("merge", help="Merge FIT activities into one FIT file")
    library_merge.add_argument("paths", nargs="+", type=Path, help="FIT files in the desired activity order")
    library_merge.add_argument("--output", type=Path, required=True, help="Merged FIT output path")
    library_merge.add_argument("--name", help="Merged activity label for the command result")
    library_samba = library_actions.add_parser("samba", help="Browse and import activities from an SMB share")
    samba_actions = library_samba.add_subparsers(dest="samba_action", required=True)
    samba_list = samba_actions.add_parser("list", help="List one SMB share directory")
    samba_import = samba_actions.add_parser("import", help="Import one SMB activity file or ZIP archive")
    for samba_command in (samba_list, samba_import):
        samba_command.add_argument("url", help="SMB URL such as smb://server/share/path")
        samba_command.add_argument("--username", help="SMB username; omit for the default or guest session")
        samba_command.add_argument(
            "--password-env",
            default="SAMBA_PASSWORD",
            help="Environment variable containing the SMB password (default: SAMBA_PASSWORD)",
        )
        samba_command.add_argument("--timeout", type=float, default=30.0, help="SMB connection timeout in seconds")
        samba_command.add_argument(
            "--legacy-smb",
            action="store_true",
            help="Use the explicit PySMB backend with SMB1 and NetBIOS compatibility",
        )
        samba_command.add_argument(
            "--server-name",
            help="Remote NetBIOS name for legacy SMB (defaults to the first hostname label)",
        )
    samba_import.add_argument(
        "--archive-password-env",
        default="ACTIVITY_ARCHIVE_PASSWORD",
        help="Environment variable containing an encrypted ZIP password",
    )

    ble_parser = subparsers.add_parser("ble", help="Scan and manage BLE sports sensors")
    ble_actions = ble_parser.add_subparsers(dest="ble_action", required=True)
    ble_guide = ble_actions.add_parser("guide", help="Show BLE permission and sensor setup guidance")
    ble_guide.add_argument("--locale", choices=["en", "zh"], default="zh")
    ble_scan = ble_actions.add_parser("scan", help="Scan for nearby BLE sensors")
    ble_scan.add_argument("--timeout", type=float, default=8.0, help="Scan duration in seconds")
    ble_scan.add_argument("--save", action="store_true", help="Save discovered sensors locally")
    ble_devices = ble_actions.add_parser("devices", help="List saved BLE sensors")
    ble_battery = ble_actions.add_parser("battery", help="Read a saved sensor battery level")
    ble_battery.add_argument("address", help="BLE address or platform identifier")
    ble_battery.add_argument("--timeout", type=float, default=15.0, help="Connection timeout in seconds")
    ble_heart_rate = ble_actions.add_parser("heart-rate", help="Record heart rate notifications")
    ble_heart_rate.add_argument("address", help="BLE address or platform identifier")
    ble_heart_rate.add_argument("--duration", type=float, default=60.0, help="Recording duration in seconds")
    ble_heart_rate.add_argument("--timeout", type=float, default=15.0, help="Connection timeout in seconds")
    ble_heart_rate.add_argument("--output", type=Path, help="Optional CSV output path")
    ble_record = ble_actions.add_parser("record", help="Record standard BLE sports sensor measurements")
    ble_record.add_argument("address", help="BLE address or platform identifier")
    ble_record.add_argument("--duration", type=float, default=60.0, help="Recording duration in seconds")
    ble_record.add_argument("--timeout", type=float, default=15.0, help="Connection timeout in seconds")
    ble_record.add_argument(
        "--wheel-circumference-m",
        type=float,
        help="Wheel circumference in meters for deriving wheel-sensor speed and distance",
    )
    ble_record.add_argument("--output", type=Path, help="Optional JSON Lines output path")
    ble_bigrun_ecg = ble_actions.add_parser(
        "bigrun-ecg",
        help="Record raw BigRun ECG sensor notifications",
    )
    ble_bigrun_ecg.add_argument("address", help="BigRun BLE device address or platform identifier")
    ble_bigrun_ecg.add_argument("--duration", type=float, default=60.0, help="Recording duration in seconds")
    ble_bigrun_ecg.add_argument("--timeout", type=float, default=15.0, help="Connection timeout in seconds")
    ble_bigrun_ecg.add_argument("--output", type=Path, help="Optional JSON Lines output path")
    ble_bigrun_ecg_decode = ble_actions.add_parser(
        "bigrun-ecg-decode",
        help="Decode a raw BigRun ECG JSON Lines capture",
    )
    ble_bigrun_ecg_decode.add_argument("input", type=Path, help="Raw JSON Lines capture from bigrun-ecg")
    ble_bigrun_ecg_decode.add_argument("--output", type=Path, required=True, help="Decoded JSON output path")
    ble_bigrun_ecg_decode.add_argument(
        "--normalize",
        action="store_true",
        help="Add the APK-style -5..5 normalized waveform to the decoded JSON",
    )
    ble_bigrun_ecg_analyze = ble_actions.add_parser(
        "bigrun-ecg-analyze",
        help="Calculate non-clinical metrics and recovered pattern flags from BigRun ECG samples",
    )
    ble_bigrun_ecg_analyze.add_argument(
        "input",
        type=Path,
        help="Decoded JSON output from bigrun-ecg-decode",
    )
    ble_bigrun_ecg_analyze.add_argument(
        "--sample-rate",
        type=float,
        help="Override the sample rate stored in the decoded JSON",
    )
    ble_bigrun_ecg_analyze.add_argument("--output", type=Path, help="Optional JSON report path")
    ble_bigrun_ecg_mode = ble_actions.add_parser(
        "bigrun-ecg-mode",
        help="Set a BigRun ECG sensor work mode",
    )
    ble_bigrun_ecg_mode.add_argument("address", help="BigRun BLE device address or platform identifier")
    ble_bigrun_ecg_mode.add_argument("mode", choices=BIGRUN_ECG_MODES)
    ble_bigrun_ecg_mode.add_argument("--timeout", type=float, default=15.0, help="Connection timeout in seconds")
    ble_rename = ble_actions.add_parser("rename", help="Rename a saved BLE sensor")
    ble_rename.add_argument("address", help="Saved BLE device address")
    ble_rename.add_argument("name", help="Local display name")
    ble_remove = ble_actions.add_parser("remove", help="Remove a saved BLE sensor")
    ble_remove.add_argument("address", help="Saved BLE device address")
    ble_prefer = ble_actions.add_parser("prefer", help="Set a preferred device for a sensor type")
    ble_prefer.add_argument("address", help="Saved BLE device address")
    ble_prefer.add_argument("--type", required=True, choices=sorted(BLE_TYPES))
    ble_trainer = ble_actions.add_parser("trainer", help="Control an FTMS smart trainer")
    trainer_actions = ble_trainer.add_subparsers(dest="trainer_action", required=True)
    trainer_power = trainer_actions.add_parser("set-power", help="Set the trainer's target power")
    trainer_power.add_argument("address", help="FTMS trainer BLE address")
    trainer_power.add_argument("--watts", type=int, required=True, help="Target power in watts")
    trainer_power.add_argument("--timeout", type=float, default=15.0, help="Connection timeout in seconds")
    trainer_resistance = trainer_actions.add_parser(
        "set-resistance",
        help="Set the FTMS target resistance level to 0.0",
    )
    trainer_resistance.add_argument("address", help="FTMS trainer BLE address")
    trainer_resistance.add_argument("--timeout", type=float, default=15.0, help="Connection timeout in seconds")
    trainer_preview = trainer_actions.add_parser("preview", help="Preview a virtual ride course without BLE")
    trainer_preview.add_argument("course_file", type=Path, nargs="?", help="Course JSON or AI workout .fit.meta file")
    trainer_preview.add_argument("--ftp", type=float, help="FTP in watts for %%FTP and fallback power targets")
    trainer_preview.add_argument(
        "--intensity-percent",
        type=float,
        default=100.0,
        help="Starting power multiplier as a percentage",
    )
    trainer_ride = trainer_actions.add_parser(
        "ride",
        help="Run an FTMS power course; interactive terminals accept +, -, and s controls",
        description="Run a FIT-backed FTMS ride. Interactive keys: + and - change intensity by 5%; s skips; p pauses.",
    )
    trainer_ride.add_argument("address", help="FTMS trainer BLE address")
    trainer_ride.add_argument("course_file", type=Path, nargs="?", help="Course JSON or AI workout .fit.meta file")
    trainer_ride.add_argument("--ftp", type=float, help="FTP in watts for %%FTP and fallback power targets")
    trainer_ride.add_argument(
        "--intensity-percent",
        type=float,
        default=100.0,
        help="Starting power multiplier as a percentage",
    )
    trainer_ride.add_argument("--timeout", type=float, default=15.0, help="Connection timeout in seconds")
    trainer_ride.add_argument("--output", type=Path, help="Output FIT activity path")

    plans_parser = subparsers.add_parser("plans", help="Use bundled training plan templates")
    plans_actions = plans_parser.add_subparsers(dest="plans_action", required=True)
    plans_templates = plans_actions.add_parser("list", help="List plan templates")
    plans_templates.add_argument("--locale", default="zh", choices=["en", "es", "fr", "it", "pt", "zh"])
    plans_templates.add_argument("--sport", help="Filter by sport type")
    plans_show = plans_actions.add_parser("show", help="Show a plan template")
    plans_show.add_argument("template_id")
    plans_show.add_argument("--locale", default="zh", choices=["en", "es", "fr", "it", "pt", "zh"])
    plans_install = plans_actions.add_parser("install", help="Create a dated local training schedule")
    plans_install.add_argument("template_id")
    plans_install.add_argument("--locale", default="zh", choices=["en", "es", "fr", "it", "pt", "zh"])
    plans_install.add_argument("--start-date", required=True, help="Plan week 1 Monday, YYYY-MM-DD")
    plans_installed = plans_actions.add_parser("installed", help="List local plan schedules")
    plans_schedule = plans_actions.add_parser("schedule", help="Show an installed plan schedule")
    plans_schedule.add_argument("plan_id")
    plans_link = plans_actions.add_parser("link-activity", help="Link a local activity to a plan item")
    plans_link.add_argument("plan_id")
    plans_link.add_argument("item_id")
    plans_link.add_argument("activity_id")
    plans_unlink = plans_actions.add_parser("unlink-activity", help="Clear a plan item's local activity link")
    plans_unlink.add_argument("plan_id")
    plans_unlink.add_argument("item_id")
    plans_progress = plans_actions.add_parser("progress", help="Compare linked activities with plan targets")
    plans_progress.add_argument("plan_id")
    plans_progress.add_argument("--format", choices=["json", "txt"], default="txt")
    plans_export = plans_actions.add_parser("export", help="Export an installed plan as iCalendar")
    plans_export.add_argument("plan_id")
    plans_export.add_argument("--output", type=Path, required=True)

    workouts_parser = subparsers.add_parser("workouts", help="Browse, generate, and manage workout templates")
    workouts_actions = workouts_parser.add_subparsers(dest="workouts_action", required=True)
    workouts_list = workouts_actions.add_parser("list", help="List FIT workout templates")
    workouts_list.add_argument("--sport", help="Filter by sport type")
    workouts_show = workouts_actions.add_parser("show", help="Show a workout template")
    workouts_show.add_argument("workout_id")
    workouts_export = workouts_actions.add_parser("export", help="Copy a workout FIT template")
    workouts_export.add_argument("workout_id")
    workouts_export.add_argument("--output", type=Path, required=True)
    workouts_generate = workouts_actions.add_parser(
        "generate", help="Generate one structured workout with the configured AI"
    )
    workouts_generate.add_argument("--sport", required=True, choices=["running", "cycling", "swimming"])
    workouts_generate.add_argument("--task", required=True, help="Workout request or session description")
    workouts_generate.add_argument(
        "--target-mode",
        choices=["pace", "heart-rate", "power", "mixed"],
        default="mixed",
        help="Preferred target type (default: mixed)",
    )
    workouts_generate.add_argument("--target-duration", help="Optional target duration, such as 45min")
    workouts_generate.add_argument("--target-distance", help="Optional target distance, such as 10km")
    workouts_generate.add_argument("--target-pace", help="Optional target pace, such as 4:30/km")
    workouts_generate.add_argument("--target-heart-rate", help="Optional target heart-rate zone or bpm")
    workouts_generate.add_argument("--target-tss", type=float, help="Optional target training stress score")
    workouts_generate.add_argument("--athlete-context", help="Optional athlete profile or training context")
    workouts_generate.add_argument(
        "--feedback", action="append", default=[], help="Feedback to apply; may be supplied more than once"
    )
    workouts_generate.add_argument(
        "--language", default="zh-CN", type=validate_ai_language_code, help="Workout language (default: zh-CN)"
    )
    workouts_generate.add_argument("--output", type=Path, help="FIT output path (default: data/generated_workouts)")
    workouts_generate.add_argument(
        "--prompt-only", action="store_true", help="Print the prompts without contacting the AI service"
    )
    workouts_mywhoosh = workouts_actions.add_parser(
        "mywhoosh", help="List, upload, or delete MyWhoosh cycling workouts"
    )
    workouts_mywhoosh_actions = workouts_mywhoosh.add_subparsers(
        dest="mywhoosh_workout_action", required=True
    )
    workouts_mywhoosh_actions.add_parser("list", help="List workouts in the MyWhoosh account")
    workouts_mywhoosh_upload = workouts_mywhoosh_actions.add_parser(
        "upload", help="Upload a local cycling workout template"
    )
    workouts_mywhoosh_upload.add_argument("template_id")
    workouts_mywhoosh_delete = workouts_mywhoosh_actions.add_parser(
        "delete", help="Delete a MyWhoosh workout by its remote workout ID"
    )
    workouts_mywhoosh_delete.add_argument("workout_id")

    health_parser = subparsers.add_parser("health", help="Import and summarize local health measurements")
    health_actions = health_parser.add_subparsers(dest="health_action", required=True)
    health_import = health_actions.add_parser("import", help="Import a UTF-8 health CSV")
    health_import.add_argument("input", type=Path)
    health_readiness_import = health_actions.add_parser(
        "import-readiness", help="Import GarSync training readiness JSON records"
    )
    health_readiness_import.add_argument("input", type=Path)
    health_readiness_fetch = health_actions.add_parser(
        "fetch-readiness", help="Fetch Garmin training readiness for an inclusive date range"
    )
    health_readiness_fetch.add_argument("--start-date", required=True, help="Start date, YYYY-MM-DD")
    health_readiness_fetch.add_argument("--end-date", required=True, help="End date, YYYY-MM-DD")
    health_garmin_summary_fetch = health_actions.add_parser(
        "fetch-garmin-summary",
        help="Fetch Garmin daily health summaries for an inclusive date range",
    )
    health_garmin_summary_fetch.add_argument(
        "--start-date", required=True, help="Start date, YYYY-MM-DD"
    )
    health_garmin_summary_fetch.add_argument(
        "--end-date", required=True, help="End date, YYYY-MM-DD"
    )
    health_garmin_detail_fetch = health_actions.add_parser(
        "fetch-garmin-details",
        help="Fetch detailed Garmin sleep and wellness records for an inclusive date range",
    )
    health_garmin_detail_fetch.add_argument(
        "--dataset",
        action="append",
        choices=tuple(GARMIN_HEALTH_DETAIL_ENDPOINTS),
        required=True,
        help="Repeat for each supported Garmin health dataset",
    )
    health_garmin_detail_fetch.add_argument(
        "--start-date", required=True, help="Start date, YYYY-MM-DD"
    )
    health_garmin_detail_fetch.add_argument(
        "--end-date", required=True, help="End date, YYYY-MM-DD"
    )
    health_garmin_summaries = health_actions.add_parser(
        "summaries", help="Show imported Garmin daily summaries as JSON"
    )
    health_garmin_summaries.add_argument(
        "--start-date", required=True, help="Start date, YYYY-MM-DD"
    )
    health_garmin_summaries.add_argument(
        "--end-date", required=True, help="End date, YYYY-MM-DD"
    )
    health_garmin_details = health_actions.add_parser(
        "details", help="Show stored Garmin health detail payloads as JSON"
    )
    health_garmin_details.add_argument(
        "--dataset", choices=tuple(GARMIN_HEALTH_DETAIL_ENDPOINTS)
    )
    health_garmin_details.add_argument(
        "--start-date", required=True, help="Start date, YYYY-MM-DD"
    )
    health_garmin_details.add_argument(
        "--end-date", required=True, help="End date, YYYY-MM-DD"
    )
    health_intervals_wellness = health_actions.add_parser(
        "fetch-intervals-wellness",
        help="Fetch Intervals.icu wellness for an inclusive date range",
    )
    health_intervals_wellness.add_argument("--start-date", required=True, help="Start date, YYYY-MM-DD")
    health_intervals_wellness.add_argument("--end-date", required=True, help="End date, YYYY-MM-DD")
    health_fitbit_fetch = health_actions.add_parser(
        "fetch-fitbit",
        help="Fetch Fitbit sleep, weight, steps, or heart-rate data from Google Health",
    )
    health_fitbit_fetch.add_argument(
        "--dataset",
        action="append",
        choices=GOOGLE_HEALTH_FITBIT_DATASETS,
        required=True,
        help="Repeat for each Google Health dataset, or select daily-summary",
    )
    health_fitbit_fetch.add_argument("--start-date", required=True, help="Start date, YYYY-MM-DD")
    health_fitbit_fetch.add_argument("--end-date", required=True, help="End date, YYYY-MM-DD")
    health_summary = health_actions.add_parser("summary", help="Show latest health measurements")
    health_summary.add_argument("--format", choices=["json", "text"], default="json")
    health_readiness = health_actions.add_parser("readiness", help="Show imported training readiness history")
    health_readiness.add_argument("--format", choices=["json", "text"], default="text")
    health_readiness.add_argument("--limit", type=int, default=30)

    weather_parser = subparsers.add_parser("weather", help="Show current weather and outdoor exercise advice")
    weather_parser.add_argument("--lat", required=True, type=float, help="Latitude in decimal degrees")
    weather_parser.add_argument("--lon", required=True, type=float, help="Longitude in decimal degrees")
    weather_parser.add_argument("--lang", default="zh", choices=["zh", "en"], help="Weather language")
    weather_parser.add_argument("--format", choices=["text", "json"], default="text")
    weather_parser.add_argument("--refresh", action="store_true", help="Ignore the 15-minute local cache")

    ai_parser = subparsers.add_parser("ai-analysis", help="Analyze an imported activity with a configured chat API")
    ai_parser.add_argument("activity_id", help="Activity fingerprint or its unique prefix")
    ai_parser.add_argument("--question", help="Optional analysis question")
    ai_parser.add_argument(
        "--language",
        default="zh-CN",
        type=validate_ai_language_code,
        help="Response language code (default: zh-CN)",
    )
    ai_parser.add_argument(
        "--focus",
        choices=list(AI_ANALYSIS_FOCI),
        help="Analysis focus (default: saved preference or performance)",
    )
    ai_parser.add_argument(
        "--detail",
        choices=list(AI_ANALYSIS_DETAILS),
        help="Response detail level (default: saved preference or normal)",
    )
    ai_parser.add_argument(
        "--force-vector-json",
        type=Path,
        help="Analyze a local Force Vector snapshot with the cycling AI coach prompt",
    )
    ai_parser.add_argument(
        "--force-vector-focus",
        choices=list(FORCE_VECTOR_FOCUSES),
        default="comprehensive",
        help="Force Vector focus when --force-vector-json is used",
    )
    ai_parser.add_argument("--prompt-only", action="store_true", help="Print the analysis prompt without sending data")
    ai_parser.add_argument("--history", action="store_true", help="Show saved analyses without contacting the AI service")

    ai_settings_parser = subparsers.add_parser(
        "ai-settings", help="Show or change saved AI analysis preferences"
    )
    ai_settings_actions = ai_settings_parser.add_subparsers(dest="ai_settings_action", required=True)
    ai_settings_actions.add_parser("show", help="Show saved AI analysis preferences")
    ai_settings_set = ai_settings_actions.add_parser("set", help="Save AI analysis preferences")
    ai_settings_set.add_argument("--focus", choices=list(AI_ANALYSIS_FOCI))
    ai_settings_set.add_argument("--detail", choices=list(AI_ANALYSIS_DETAILS))
    ai_settings_actions.add_parser("reset", help="Reset preferences to performance and normal")

    ai_profile_parser = subparsers.add_parser(
        "ai-profile", help="Show or change the local AI athlete profile"
    )
    ai_profile_actions = ai_profile_parser.add_subparsers(
        dest="ai_profile_action", required=True
    )
    ai_profile_actions.add_parser("show", help="Show saved AI athlete profile")
    ai_profile_set = ai_profile_actions.add_parser("set", help="Save AI athlete profile fields")
    for field in AI_ATHLETE_PROFILE_FIELDS:
        option = f"--{field.replace('_', '-')}"
        if field == "gender":
            ai_profile_set.add_argument(option, choices=list(AI_ATHLETE_PROFILE_GENDERS))
        else:
            value_type = int if field in AI_ATHLETE_PROFILE_INTEGER_FIELDS else float
            ai_profile_set.add_argument(option, type=value_type)
    ai_profile_set.add_argument(
        "--clear",
        dest="clear_fields",
        action="append",
        choices=list(AI_ATHLETE_PROFILE_FIELDS),
        help="Clear a saved field; may be repeated",
    )
    ai_profile_actions.add_parser("reset", help="Clear all saved athlete profile fields")

    ai_export_parser = subparsers.add_parser(
        "ai-report-export", help="Export a saved AI analysis as Markdown or PDF"
    )
    ai_export_parser.add_argument("activity_id", help="Activity fingerprint or its unique prefix")
    ai_export_parser.add_argument(
        "--result-id",
        help="Saved analysis ID or unique prefix (defaults to the most recent analysis)",
    )
    ai_export_parser.add_argument("--format", required=True, choices=["markdown", "pdf"])
    ai_export_parser.add_argument("--output", type=Path, help="Output file path")

    receive_parser = subparsers.add_parser("receive", help="Start a local Wi-Fi file import page")
    receive_parser.add_argument("--host", default="127.0.0.1", help="Bind address; use 0.0.0.0 for LAN access")
    receive_parser.add_argument("--port", type=int, default=8765)
    receive_parser.add_argument("--max-upload-mb", type=int, default=64)

    status_parser = subparsers.add_parser("status", help="Show local SQLite status")
    status_parser.add_argument("--json", action="store_true", help="Reserved for future use")

    check_parser = subparsers.add_parser("check", help="Verify configured source/target logins")
    check_parser.add_argument(
        "--source",
        action="append",
        choices=[
            "igpsport",
            "onelap",
            "intervals_icu",
            "local",
            "garmin",
            "strava",
            "concept2",
            "hammerhead",
            "polar",
            "wahoo",
            "fitbit",
            "withings",
            "coros",
            "smashrun",
            "ridewithgps",
            "nolio",
            "suunto",
            "cycling_analytics",
            "mywhoosh",
        ],
        help="Repeatable source",
    )
    check_parser.add_argument(
        "--target",
        action="append",
        choices=[
            "garmin",
            "strava",
            "wahoo",
            "hammerhead",
            "intervals_icu",
            "ridewithgps",
            "nolio",
            "suunto",
            "cycling_analytics",
        ],
        help="Repeatable target",
    )

    garmin_export_parser = subparsers.add_parser(
        "garmin-session-export",
        help="Export .data/.garmin_session as base64 zip for GARMIN_SESSION_B64 secret",
    )
    garmin_export_parser.add_argument(
        "--output",
        default="garmin_session.b64",
        help="Output file path for the base64 text",
    )

    auth_url_parser = subparsers.add_parser("strava-auth-url", help="Print the Strava OAuth authorize URL")
    auth_url_parser.add_argument("--force", action="store_true", help="Force Strava consent screen")

    exchange_parser = subparsers.add_parser("strava-exchange", help="Exchange Strava OAuth code for tokens")
    exchange_parser.add_argument("--code", required=True, help="OAuth code returned by Strava")

    wahoo_auth_url_parser = subparsers.add_parser(
        "wahoo-auth-url", help="Print the Wahoo OAuth authorize URL"
    )
    wahoo_exchange_parser = subparsers.add_parser(
        "wahoo-exchange", help="Exchange a Wahoo OAuth code for tokens"
    )
    wahoo_exchange_parser.add_argument("--code", required=True, help="OAuth code returned by Wahoo")

    subparsers.add_parser(
        "ridewithgps-auth-url", help="Print the Ride with GPS OAuth authorization URL"
    )
    ridewithgps_exchange_parser = subparsers.add_parser(
        "ridewithgps-exchange", help="Exchange a Ride with GPS OAuth code and save the token"
    )
    ridewithgps_exchange_parser.add_argument(
        "--code", required=True, help="OAuth code returned by Ride with GPS"
    )
    subparsers.add_parser("nolio-auth-url", help="Print the Nolio OAuth authorization URL")
    nolio_exchange_parser = subparsers.add_parser(
        "nolio-exchange", help="Exchange a Nolio OAuth code and save tokens locally"
    )
    nolio_exchange_parser.add_argument("--code", required=True, help="OAuth code returned by Nolio")
    nolio_exchange_parser.add_argument("--state", required=True, help="OAuth state returned to the redirect URI")
    subparsers.add_parser("suunto-auth-url", help="Print the Suunto OAuth authorization URL")
    suunto_exchange_parser = subparsers.add_parser(
        "suunto-exchange", help="Exchange a Suunto OAuth code and save tokens locally"
    )
    suunto_exchange_parser.add_argument("--code", required=True, help="OAuth code returned by Suunto")
    suunto_exchange_parser.add_argument("--state", required=True, help="OAuth state returned to the redirect URI")
    suunto_routes_parser = subparsers.add_parser(
        "suunto-routes", help="List routes from the Suunto account"
    )
    suunto_routes_parser.add_argument("--limit", type=int, help="Maximum routes to print")
    suunto_route_export_parser = subparsers.add_parser(
        "suunto-route-export", help="Export a Suunto route as GPX"
    )
    suunto_route_export_parser.add_argument("--route-id", required=True, help="Suunto route ID")
    suunto_route_export_parser.add_argument("--output", required=True, type=Path, help="Output GPX path")
    suunto_route_import_parser = subparsers.add_parser(
        "suunto-route-import", help="Import a GPX route to Suunto"
    )
    suunto_route_import_parser.add_argument("input", type=Path, help="Input GPX file")
    suunto_route_import_parser.add_argument(
        "--activities",
        default="1",
        help="Comma-separated Suunto activity IDs associated with the route (default: running)",
    )
    ridewithgps_delete_parser = subparsers.add_parser(
        "ridewithgps-delete", help="Permanently delete a Ride with GPS trip"
    )
    ridewithgps_delete_parser.add_argument("--trip-id", required=True, help="Ride with GPS trip ID")

    cycling_analytics_delete_parser = subparsers.add_parser(
        "cycling-analytics-delete", help="Permanently delete a Cycling Analytics ride"
    )
    cycling_analytics_delete_parser.add_argument("--ride-id", required=True, help="Cycling Analytics ride ID")
    cycling_analytics_delete_parser.add_argument(
        "--yes", action="store_true", help="Skip the typed-ID confirmation prompt"
    )

    google_health_auth_url_parser = subparsers.add_parser(
        "google-health-auth-url", help="Print the Fitbit Google Health OAuth authorization URL"
    )
    google_health_exchange_parser = subparsers.add_parser(
        "google-health-exchange", help="Exchange a Google Health OAuth code for local credentials"
    )
    google_health_exchange_parser.add_argument(
        "--code", required=True, help="OAuth code from the registered redirect URL"
    )
    google_health_exchange_parser.add_argument(
        "--state", required=True, help="OAuth state from the registered redirect URL"
    )

    subparsers.add_parser("withings-auth-url", help="Print the Withings OAuth authorization URL")
    withings_exchange_parser = subparsers.add_parser(
        "withings-exchange", help="Exchange a Withings OAuth code for local tokens"
    )
    withings_exchange_parser.add_argument(
        "--code", required=True, help="OAuth code from the registered redirect URL"
    )
    withings_exchange_parser.add_argument(
        "--state", required=True, help="OAuth state from the registered redirect URL"
    )

    subparsers.add_parser(
        "coros-auth",
        help="Authorize this CLI with COROS MCP and store the OAuth session locally",
    )
    subparsers.add_parser(
        "smashrun-auth",
        help="Validate a personal Smashrun token and store it in local SQLite",
    )

    concept2_auth_url_parser = subparsers.add_parser(
        "concept2-auth-url", help="Print the Concept2 OAuth authorization URL"
    )
    concept2_auth_url_parser.add_argument(
        "--write",
        action="store_true",
        help="Request result write access for the separate Concept2 delete command",
    )
    concept2_exchange_parser = subparsers.add_parser(
        "concept2-exchange", help="Exchange a Concept2 OAuth code for tokens"
    )
    concept2_exchange_parser.add_argument("--code", required=True, help="OAuth code returned by Concept2")
    concept2_exchange_parser.add_argument(
        "--write",
        action="store_true",
        help="Match an authorization URL created with --write",
    )

    concept2_delete_parser = subparsers.add_parser(
        "concept2-delete", help="Permanently delete a Concept2 Logbook result"
    )
    concept2_delete_parser.add_argument("--activity-id", required=True, help="Concept2 result ID")
    concept2_delete_parser.add_argument(
        "--yes", action="store_true", help="Skip the typed-ID confirmation prompt"
    )

    hammerhead_auth_url_parser = subparsers.add_parser(
        "hammerhead-auth-url", help="Print the Hammerhead OAuth authorization URL"
    )
    hammerhead_exchange_parser = subparsers.add_parser(
        "hammerhead-exchange", help="Exchange a Hammerhead OAuth code for tokens"
    )
    hammerhead_exchange_parser.add_argument("--code", required=True, help="OAuth code returned by Hammerhead")
    hammerhead_exchange_parser.add_argument(
        "--state", required=True, help="OAuth state returned to the registered redirect URI"
    )
    hammerhead_routes_parser = subparsers.add_parser(
        "hammerhead-routes", help="List Hammerhead routes accessible to this API client"
    )
    hammerhead_routes_parser.add_argument("--limit", type=int, help="Maximum routes to print")
    hammerhead_delete_route_parser = subparsers.add_parser(
        "hammerhead-delete-route", help="Delete a route created by this API client"
    )
    hammerhead_delete_route_parser.add_argument("--route-id", required=True, help="Hammerhead route ID")
    hammerhead_delete_route_parser.add_argument(
        "--yes", action="store_true", help="Skip the typed-ID confirmation prompt"
    )

    subparsers.add_parser("polar-auth-url", help="Print the Polar AccessLink OAuth URL")
    polar_exchange_parser = subparsers.add_parser(
        "polar-exchange", help="Exchange a Polar OAuth code and register this user"
    )
    polar_exchange_parser.add_argument("--code", required=True, help="OAuth code returned by Polar")
    polar_exchange_parser.add_argument("--state", required=True, help="OAuth state returned to the redirect URI")
    subparsers.add_parser("polar-register", help="Retry Polar AccessLink user registration")

    share_parser = subparsers.add_parser("share", help="Build GarSync friend and group invite links")
    share_actions = share_parser.add_subparsers(dest="share_action", required=True)
    friend_invite = share_actions.add_parser("friend-invite", help="Build a friend invite link")
    friend_invite.add_argument("user_id", help="GarSync user UUID")
    friend_invite.add_argument("--name", help="Optional display name included in the link")
    group_invite = share_actions.add_parser("group-invite", help="Build a group invite link")
    group_invite.add_argument("group_id", help="GarSync group UUID")
    group_invite.add_argument("--group-name", help="Optional group name included in the link")
    group_invite.add_argument("--name", help="Optional inviter name included in the link")

    social_parser = subparsers.add_parser(
        "social", help="Read and publish GarSync activity feeds and manage Nakama friends"
    )
    social_actions = social_parser.add_subparsers(dest="social_action", required=True)
    social_publish = social_actions.add_parser(
        "publish", help="Publish a local activity summary to the GarSync social feed"
    )
    social_publish.add_argument("input", type=Path, help="FIT, GPX, or TCX activity file")
    social_publish.add_argument("--activity-id", required=True, help="GarSync activity ID")
    social_publish.add_argument("--display-name", required=True, help="Name shown on the post")
    social_publish.add_argument("--title", help="Post title (defaults to the activity name)")
    social_publish.add_argument("--avatar-url", help="Optional profile avatar URL")
    social_publish.add_argument(
        "--location-name", help="Optional location label; no reverse-geocoding service is called"
    )
    social_publish.add_argument(
        "--base-url",
        help="Social API base URL (defaults to GARSYNC_SOCIAL_BASE_URL)",
    )
    social_publish.add_argument(
        "--token-env",
        default="GARSYNC_NAKAMA_AUTH_TOKEN",
        help="Environment variable containing the Nakama session token",
    )
    social_publish.add_argument(
        "--dry-run", action="store_true", help="Print the post JSON without sending it"
    )

    social_feed = social_actions.add_parser("feed", help="Fetch a user, nearby, newest, hot, or follow feed")
    social_feed.add_argument("kind", choices=["user", "nearby", "newest", "hot", "follow"])
    social_feed.add_argument(
        "--base-url",
        help="Social API base URL (defaults to GARSYNC_SOCIAL_BASE_URL)",
    )
    social_feed.add_argument(
        "--nakama-base-url",
        help="Nakama API URL for automatic follow-feed IDs (defaults to GARSYNC_NAKAMA_BASE_URL)",
    )
    social_feed.add_argument(
        "--token-env",
        default="GARSYNC_NAKAMA_AUTH_TOKEN",
        help="Environment variable containing the Nakama session token",
    )
    social_feed.add_argument("--page", type=int, default=0, help="Zero-based result page (default: 0)")
    social_feed.add_argument("--limit", type=int, default=40, help="Results per page (default: 40)")
    social_feed.add_argument("--user-id", help="Required for the user feed")
    social_feed.add_argument("--lat", type=float, help="Latitude for the nearby feed")
    social_feed.add_argument("--lng", type=float, help="Longitude for the nearby feed")
    social_feed.add_argument(
        "--following-id",
        action="append",
        help="Override automatic mutual-friend IDs for the follow feed; repeat for multiple IDs",
    )

    social_friends = social_actions.add_parser("friends", help="List or change Nakama friends")
    friend_actions = social_friends.add_subparsers(dest="friends_action", required=True)
    friends_list = friend_actions.add_parser("list", help="List friends and their raw server state")
    friends_list.add_argument(
        "--base-url",
        help="Nakama API base URL (defaults to GARSYNC_NAKAMA_BASE_URL)",
    )
    friends_list.add_argument(
        "--token-env",
        default="GARSYNC_NAKAMA_AUTH_TOKEN",
        help="Environment variable containing the Nakama session token",
    )
    friends_list.add_argument("--limit", type=int, default=2000, help="Maximum entries (default: 2000)")
    friends_list.add_argument("--cursor", help="Nakama pagination cursor")
    friends_list.add_argument(
        "--state",
        type=int,
        help="Optional numeric Nakama friendship state filter",
    )

    for action, help_text in (
        ("add", "Send a friend request by Nakama user ID"),
        ("remove", "Remove a friend by Nakama user ID"),
    ):
        friend_change = friend_actions.add_parser(action, help=help_text)
        friend_change.add_argument("user_id", help="Nakama user ID")
        friend_change.add_argument(
            "--base-url",
            help="Nakama API base URL (defaults to GARSYNC_NAKAMA_BASE_URL)",
        )
        friend_change.add_argument(
            "--token-env",
            default="GARSYNC_NAKAMA_AUTH_TOKEN",
            help="Environment variable containing the Nakama session token",
        )

    for action, help_text in (
        ("thumb", "Like a social activity"),
        ("unthumb", "Remove your like from a social activity"),
    ):
        thumb_action = social_actions.add_parser(action, help=help_text)
        thumb_action.add_argument("activity_id", help="GarSync social activity ID")
        thumb_action.add_argument(
            "--base-url",
            help="Social API base URL (defaults to GARSYNC_SOCIAL_BASE_URL)",
        )
        thumb_action.add_argument(
            "--token-env",
            default="GARSYNC_NAKAMA_AUTH_TOKEN",
            help="Environment variable containing the Nakama session token",
        )

    parser.set_defaults(command="sync")
    return parser


def main(argv: list[str] | None = None) -> int:
    root_dir = Path(__file__).resolve().parent.parent
    parser = build_parser()
    args = parser.parse_args(argv)
    if (
        args.command == "library"
        and args.library_action == "report"
        and args.format == "pdf"
        and args.output is None
    ):
        parser.error("library report --format pdf requires --output")
    try:
        target_formats = _target_format_map(getattr(args, "target_formats", None))
    except ValueError as exc:
        parser.error(str(exc))

    if args.command == "share":
        return _run_share_command(args)

    config = AppConfig.load(root_dir)
    ensure_directory(config.data_dir)
    configure_logging(config.log_level, config.log_path)

    if args.command == "convert":
        return _convert_file(args, config)
    if args.command == "weather":
        return _run_weather(args, config)
    if args.command == "social":
        if args.social_action == "publish":
            return _run_social_publish_command(args)
        if args.social_action == "feed":
            return _run_social_feed_command(args)
        if args.social_action == "friends":
            return _run_social_friends_command(args)
        return _run_social_thumb_command(args)
    if args.command in {
        "library",
        "plans",
        "workouts",
        "health",
        "ai-analysis",
        "ai-settings",
        "ai-profile",
        "ai-report-export",
        "receive",
        "ble",
    }:
        return _run_local_command(args, config)

    engine = SyncEngine(config)
    try:
        if args.command == "status":
            stats = engine.status()
            print(f"activities={stats['activities']}")
            print(f"success={stats['success']}")
            print(f"duplicate={stats['duplicate']}")
            print(f"failed={stats['failed']}")
            return 0

        if args.command == "check":
            selected_sources = args.source or config.sources
            selected_targets = args.target or config.targets
            _validate_selection(selected_sources, engine.sources.keys(), "source")
            _validate_selection(selected_targets, engine.targets.keys(), "target")
            results = engine.check_connections(selected_sources, selected_targets)
            for kind, name, status in results:
                print(f"{kind}:{name}={status}")
            return 0

        if args.command == "garmin-session-export":
            session_dir = config.data_dir / ".garmin_session"
            encoded = pack_directory_to_base64_zip(session_dir)
            output_path = Path(args.output)
            output_path.write_text(encoded, encoding="utf-8")
            print(f"written={output_path}")
            return 0

        if args.command == "strava-auth-url":
            target = engine.get_strava_target()
            print(target.build_authorize_url(force_prompt=args.force))
            return 0

        if args.command == "strava-exchange":
            target = engine.get_strava_target()
            payload = target.exchange_code(args.code)
            print("Strava tokens saved to SQLite.")
            print(f"expires_at={payload.get('expires_at')}")
            print(f"scope={payload.get('scope')}")
            return 0

        if args.command == "wahoo-auth-url":
            target = engine.get_wahoo_target()
            print(target.build_authorize_url())
            return 0

        if args.command == "wahoo-exchange":
            target = engine.get_wahoo_target()
            payload = target.exchange_code(args.code)
            print("Wahoo tokens saved to SQLite.")
            expires_at = engine.state_db.get_value("wahoo_expires_at") or payload.get("expires_at")
            print(f"expires_at={expires_at}")
            print(f"scope={payload.get('scope')}")
            return 0

        if args.command == "ridewithgps-auth-url":
            print(engine.ridewithgps_client.build_authorize_url())
            return 0

        if args.command == "ridewithgps-exchange":
            result = engine.ridewithgps_client.exchange_code(args.code)
            print("Ride with GPS access token validated and saved to SQLite.")
            print(f"user_id={result.get('user_id')}")
            print(f"scope={result.get('scope')}")
            return 0

        if args.command == "nolio-auth-url":
            print(engine.nolio_client.build_authorize_url())
            return 0

        if args.command == "nolio-exchange":
            result = engine.nolio_client.exchange_code(args.code, args.state)
            print("Nolio tokens saved to SQLite.")
            print(f"expires_in={result.get('expires_in')}")
            print(f"scope={result.get('scope')}")
            return 0

        if args.command == "suunto-auth-url":
            print(engine.suunto_client.build_authorize_url())
            return 0

        if args.command == "suunto-exchange":
            result = engine.suunto_client.exchange_code(args.code, args.state)
            print("Suunto tokens saved to SQLite.")
            print(f"expires_in={result.get('expires_in')}")
            print(f"scope={result.get('scope')}")
            return 0

        if args.command == "suunto-routes":
            routes = engine.get_suunto_source().list_routes(limit=args.limit)
            print(json.dumps(routes, ensure_ascii=False, indent=2))
            return 0

        if args.command == "suunto-route-export":
            output_path = engine.get_suunto_source().download_route(args.route_id, args.output)
            print(f"exported={output_path}")
            return 0

        if args.command == "suunto-route-import":
            result = engine.get_suunto_target().import_route(args.input, activities=args.activities)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        if args.command == "ridewithgps-delete":
            expected = args.trip_id.strip()
            confirmation = input(
                f"Permanently delete Ride with GPS trip {expected}? Type the ID to confirm: "
            )
            if confirmation.strip() != expected:
                print("Ride with GPS trip deletion cancelled.")
                return 1
            engine.ridewithgps_client.delete_trip(expected)
            print(f"deleted=ridewithgps:{expected}")
            return 0

        if args.command == "cycling-analytics-delete":
            expected = args.ride_id.strip()
            if not args.yes:
                confirmation = input(
                    f"Permanently delete Cycling Analytics ride {expected}? Type the ID to confirm: "
                )
                if confirmation.strip() != expected:
                    print("Cycling Analytics ride deletion cancelled.")
                    return 1
            engine.get_cycling_analytics_target().delete_ride(expected)
            print(f"deleted=cycling_analytics:{expected}")
            return 0

        if args.command == "google-health-auth-url":
            print(engine.google_health_client.build_authorize_url())
            return 0

        if args.command == "google-health-exchange":
            result = engine.google_health_client.exchange_code(args.code, args.state)
            print("Google Health credentials saved to local SQLite for the Fitbit source.")
            print(f"expires_at={result.get('expires_at')}")
            return 0

        if args.command == "withings-auth-url":
            print(engine.withings_client.build_authorize_url())
            return 0

        if args.command == "withings-exchange":
            result = engine.withings_client.exchange_code(args.code, args.state)
            print("Withings tokens saved to local SQLite.")
            print(f"expires_at={result.get('expires_at')}")
            print(f"scope={result.get('scope')}")
            return 0

        if args.command == "coros-auth":
            engine.get_coros_source().authenticate()
            print("COROS MCP authorization is saved in local SQLite.")
            return 0

        if args.command == "smashrun-auth":
            token = getpass.getpass("Smashrun access token from the API Explorer: ").strip()
            if not token:
                raise ValueError("Smashrun access token must not be empty")
            engine.smashrun_source.save_access_token(token)
            print("Smashrun read access validated and saved in local SQLite.")
            return 0

        if args.command == "concept2-auth-url":
            source = engine.get_concept2_source()
            print(source.build_authorize_url(write=args.write))
            return 0

        if args.command == "concept2-exchange":
            source = engine.get_concept2_source()
            payload = source.exchange_code(args.code, write=args.write)
            print("Concept2 tokens saved to SQLite.")
            expires_at = engine.state_db.get_value("concept2_expires_at") or payload.get("expires_at")
            scope = engine.state_db.get_value("concept2_scope") or payload.get("scope")
            print(f"expires_at={expires_at}")
            print(f"scope={scope}")
            return 0

        if args.command == "concept2-delete":
            if not args.yes:
                expected = args.activity_id.strip()
                confirmation = input(
                    f"Permanently delete Concept2 result {expected}? Type the ID to confirm: "
                )
                if confirmation.strip() != expected:
                    print("Concept2 delete cancelled.")
                    return 1
            engine.get_concept2_source().delete_result(args.activity_id)
            print(f"deleted=concept2:{args.activity_id}")
            return 0

        if args.command == "hammerhead-auth-url":
            print(engine.hammerhead_client.build_authorize_url())
            return 0

        if args.command == "hammerhead-exchange":
            payload = engine.hammerhead_client.exchange_code(args.code, args.state)
            print("Hammerhead tokens saved to SQLite.")
            expires_at = engine.state_db.get_value("hammerhead_expires_at") or payload.get("expires_at")
            print(f"expires_at={expires_at}")
            return 0

        if args.command == "hammerhead-routes":
            routes = engine.get_hammerhead_source().list_routes(limit=args.limit)
            print(json.dumps(routes, ensure_ascii=False, indent=2))
            return 0

        if args.command == "hammerhead-delete-route":
            if not args.yes:
                expected = args.route_id.strip()
                confirmation = input(
                    f"Permanently delete Hammerhead route {expected}? Type the ID to confirm: "
                )
                if confirmation.strip() != expected:
                    print("Hammerhead route deletion cancelled.")
                    return 1
            engine.get_hammerhead_target().delete_route(args.route_id)
            print(f"deleted=hammerhead-route:{args.route_id}")
            return 0

        if args.command == "polar-auth-url":
            print(engine.polar_client.build_authorize_url())
            return 0

        if args.command == "polar-exchange":
            engine.polar_client.exchange_code(args.code, args.state)
            print("Polar AccessLink token saved to local SQLite; user registration completed.")
            return 0

        if args.command == "polar-register":
            engine.polar_client.register_user()
            print("Polar AccessLink user registration is ready.")
            return 0

        selected_sources = args.source or config.sources
        selected_targets = args.target or config.targets
        _validate_selection(selected_sources, engine.sources.keys(), "source")
        _validate_selection(selected_targets, engine.targets.keys(), "target")

        since = _parse_cli_datetime(args.date_from)
        until = _parse_cli_datetime(args.date_to, inclusive_end=True)

        if args.loop:
            engine.loop_forever(
                sources=selected_sources,
                targets=selected_targets,
                since=since,
                until=until,
                limit=args.limit,
                dry_run=args.dry_run,
                interval_seconds=args.interval,
                target_formats=target_formats,
            )
            return 0

        synced_count = engine.sync_once(
            sources=selected_sources,
            targets=selected_targets,
            since=since,
            until=until,
            limit=args.limit,
            dry_run=args.dry_run,
            target_formats=target_formats,
        )
        print(f"done={synced_count}")
        return 0
    finally:
        engine.close()


def _parse_cli_datetime(value: str | None, inclusive_end: bool = False) -> datetime | None:
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    if inclusive_end and parsed.hour == 0 and parsed.minute == 0 and parsed.second == 0:
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed.astimezone(timezone.utc)


def _parse_period_datetime(value: str) -> str:
    parsed = parse_datetime(value)
    if parsed is None:
        raise argparse.ArgumentTypeError(f"invalid date/time: {value}")
    return parsed.astimezone(timezone.utc).isoformat()


def _validate_selection(selected: list[str], available: object, kind: str) -> None:
    available_set = set(available)
    if not available_set:
        raise SystemExit(f"No configured {kind}s found. Fill .env before running sync.")
    missing = [item for item in selected if item not in available_set]
    if missing:
        joined = ", ".join(missing)
        raise SystemExit(f"Configured {kind}(s) not available: {joined}")


def _parse_target_format(value: str) -> tuple[str, str]:
    target, separator, activity_format = value.partition("=")
    target = target.strip().lower()
    activity_format = activity_format.strip().lower()
    if not separator or target not in {
        "garmin",
        "strava",
        "wahoo",
        "hammerhead",
        "intervals_icu",
        "cycling_analytics",
        "nolio",
        "suunto",
    }:
        raise argparse.ArgumentTypeError(
            "format must use TARGET=FORMAT with target garmin, strava, wahoo, hammerhead, "
            "intervals_icu, cycling_analytics, nolio, or suunto"
        )
    if activity_format not in SUPPORTED_FORMATS:
        supported = ", ".join(sorted(SUPPORTED_FORMATS))
        raise argparse.ArgumentTypeError(f"format must be one of: {supported}")
    if target == "nolio" and activity_format not in {"fit", "tcx"}:
        raise argparse.ArgumentTypeError("Nolio uploads only support FIT and TCX")
    if target == "suunto" and activity_format != "fit":
        raise argparse.ArgumentTypeError("Suunto uploads only support FIT")
    return target, activity_format


def _iter_bigrun_ecg_capture(
    input_path: Path,
) -> Iterator[tuple[int, dict[str, object], tuple[int, ...] | None]]:
    with input_path.open("r", encoding="utf-8") as input_stream:
        for line_number, line in enumerate(input_stream, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on ECG capture line {line_number}") from exc
            if not isinstance(record, dict) or not isinstance(record.get("payload_hex"), str):
                raise ValueError(f"ECG capture line {line_number} has no payload_hex string")
            try:
                payload = bytes.fromhex(record["payload_hex"])
            except ValueError as exc:
                raise ValueError(f"Invalid payload_hex on ECG capture line {line_number}") from exc
            payload_length = record.get("payload_length")
            if payload_length is not None and (
                isinstance(payload_length, bool)
                or not isinstance(payload_length, int)
                or payload_length != len(payload)
            ):
                raise ValueError(f"ECG capture line {line_number} has a mismatched payload_length")
            yield line_number, record, decode_bigrun_ecg_payload(payload)


def _read_bigrun_ecg_decoded(input_path: Path) -> tuple[tuple[object, ...], object | None]:
    try:
        decoded = json.loads(input_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid decoded ECG JSON: {input_path}") from exc
    if not isinstance(decoded, dict) or not isinstance(decoded.get("frames"), list):
        raise ValueError("Decoded ECG JSON must contain a frames array")

    samples: list[object] = []
    for frame_number, frame in enumerate(decoded["frames"], start=1):
        if not isinstance(frame, dict) or not isinstance(frame.get("samples"), list):
            raise ValueError(f"Decoded ECG frame {frame_number} has no samples array")
        samples.extend(frame["samples"])
    return tuple(samples), decoded.get("sample_rate_hz")


def _target_format_map(values: list[tuple[str, str]] | None) -> dict[str, str]:
    formats: dict[str, str] = {}
    for target, activity_format in values or []:
        if target in formats:
            raise ValueError(f"Target format was specified more than once: {target}")
        formats[target] = activity_format
    return formats


def _run_weather(args: argparse.Namespace, config: AppConfig) -> int:
    try:
        info = get_weather(
            args.lat,
            args.lon,
            os.getenv("GARSYNC_WEATHER_TOKEN"),
            config.data_dir / "weather_cache.json",
            language=args.lang,
            force_refresh=args.refresh,
        )
    except WeatherError as exc:
        print(f"weather_error={exc}", file=sys.stderr)
        return 2
    print(format_weather_report(info, language=args.lang, output_format=args.format))
    return 0


def _convert_file(args: argparse.Namespace, config: AppConfig) -> int:
    input_path = args.input.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    source_format = input_path.suffix.lower().lstrip(".")
    if source_format not in SUPPORTED_FORMATS:
        supported = ", ".join(sorted(SUPPORTED_FORMATS))
        raise ValueError(f"Unsupported input file type: .{source_format}; supported formats: {supported}")
    if input_path == output_path:
        raise ValueError("Input and output paths must be different")
    if args.source and source_format != "fit":
        raise ValueError("--source applies only to FIT input files")

    normalized_path = input_path
    if source_format == "fit":
        fallback_mode = "none"
        if args.source == "igpsport":
            fallback_mode = config.igpsport_coord_mode
        elif args.source == "onelap":
            fallback_mode = config.onelap_coord_mode
        ensure_directory(config.converted_dir)
        with tempfile.TemporaryDirectory(prefix="convert-", dir=config.converted_dir) as temporary_dir:
            normalized_path, changed_pairs = normalize_fit_coordinates(
                input_path=input_path,
                output_path=Path(temporary_dir) / "normalized.fit",
                coordinate_mode=fallback_mode,
                coordinate_rules=config.coordinate_rules,
            )
            result = convert_activity_file(
                normalized_path,
                output_path,
                args.to,
            )
            if changed_pairs:
                print(f"coordinates_repaired={changed_pairs}")
    else:
        result = convert_activity_file(input_path, output_path, args.to)

    print(f"converted={result.source_format}->{result.target_format}")
    print(f"output={result.output_path}")
    for loss in result.losses:
        print(f"loss={loss}")
    return 0


def _run_garmin_readiness_fetch(args: argparse.Namespace, config: AppConfig) -> int:
    start_date, end_date = validate_training_readiness_date_range(args.start_date, args.end_date)
    target = GarminTarget(config)
    target.authenticate()
    records = fetch_garmin_training_readiness(
        target.client,
        start_date.isoformat(),
        end_date.isoformat(),
    )

    state = StateDB(config.db_path)
    try:
        added = import_training_readiness_records(
            state,
            records,
            source_label="Garmin Connect training readiness",
        )
    finally:
        state.close()
    print(f"records_fetched={len(records)}")
    print(f"records_added={added}")
    return 0


def _run_garmin_user_summary_fetch(args: argparse.Namespace, config: AppConfig) -> int:
    start_date, end_date = validate_garmin_user_summary_date_range(
        args.start_date, args.end_date
    )
    target = GarminTarget(config)
    target.authenticate()
    records = fetch_garmin_user_summaries(
        target.client,
        start_date.isoformat(),
        end_date.isoformat(),
    )

    state = StateDB(config.db_path)
    try:
        result = import_garmin_user_summaries(state, records)
    finally:
        state.close()
    print(f"summaries_fetched={len(records)}")
    print(f"summaries_stored={result['summaries_stored']}")
    print(f"observations_processed={result['observations_processed']}")
    return 0


def _run_garmin_health_detail_fetch(args: argparse.Namespace, config: AppConfig) -> int:
    start_date, end_date = validate_garmin_health_detail_date_range(
        args.start_date, args.end_date
    )
    target = GarminTarget(config)
    target.authenticate()
    records = fetch_garmin_health_details(
        target.client,
        args.dataset,
        start_date.isoformat(),
        end_date.isoformat(),
    )

    state = StateDB(config.db_path)
    try:
        result = import_garmin_health_details(state, records)
    finally:
        state.close()
    print(f"payloads_fetched={len(records)}")
    print(f"payloads_stored={result['snapshots_stored']}")
    print(f"observations_processed={result['observations_processed']}")
    return 0


def _run_intervals_icu_wellness_fetch(args: argparse.Namespace, config: AppConfig) -> int:
    records = fetch_intervals_icu_wellness(
        IntervalsIcuSource(config),
        args.start_date,
        args.end_date,
    )
    state = StateDB(config.db_path)
    try:
        imported = import_intervals_icu_wellness(state, records)
    finally:
        state.close()
    print(f"records_fetched={len(records)}")
    print(f"observations_processed={imported}")
    return 0


def _run_fitbit_health_fetch(args: argparse.Namespace, config: AppConfig) -> int:
    start_date, end_date = validate_google_health_date_range(
        args.start_date, args.end_date
    )
    state = StateDB(config.db_path)
    try:
        client = GoogleHealthClient(config, state)
        data_points: dict[str, list[dict[str, object]]] = {}
        daily_summary: dict[str, list[dict[str, object]]] | None = None
        for dataset in dict.fromkeys(args.dataset):
            if dataset == "daily-summary":
                daily_summary = client.list_fitbit_daily_summary(
                    start_date.isoformat(),
                    end_date.isoformat(),
                )
            else:
                data_points[dataset] = client.list_health_data_points(
                    dataset,
                    start_date.isoformat(),
                    end_date.isoformat(),
                )
        imported = 0
        if data_points:
            imported += import_google_health_data_points(state, data_points)
        if daily_summary is not None:
            imported += import_google_health_daily_summary(state, daily_summary)
    finally:
        state.close()
    fetched = sum(len(records) for records in data_points.values())
    if daily_summary is not None:
        fetched += sum(len(records) for records in daily_summary.values())
    print(f"data_points_fetched={fetched}")
    print(f"observations_processed={imported}")
    return 0


def _run_local_command(args: argparse.Namespace, config: AppConfig) -> int:
    if args.command == "ble":
        return _run_ble_command(args, config)

    if args.command == "library":
        if args.library_action == "samba":
            return _run_samba_command(args, config)

        if args.library_action == "running-dynamics":
            summary = analyze_running_dynamics_file(args.input, height_cm=args.height_cm)
            print(format_running_dynamics(summary, args.format), end="")
            return 0

        if args.library_action == "merge":
            result = merge_fit_files(args.paths, args.output, name=args.name)
            print(f"merged={result.input_count}")
            print(f"records={result.records_after}/{result.records_before}")
            print(f"decimated={'yes' if result.decimated else 'no'}")
            print(f"output={result.output_path}")
            for loss in result.losses:
                print(f"loss={loss}")
            return 0

        if args.library_action == "repair-fit-continuity":
            input_path = args.input.expanduser().resolve()
            repaired_path, removed_records = repair_fit_track_continuity(args.input, args.output)
            print(f"records_removed={removed_records}")
            print(f"already_repaired={'yes' if repaired_path == input_path else 'no'}")
            print(f"output={repaired_path}")
            return 0

        if args.library_action == "smooth-gps":
            input_path = args.input.expanduser().resolve()
            smoothed_path, changed_records = smooth_fit_gps_track(
                args.input,
                args.output,
                accuracy_m=args.accuracy_m,
                q_metres_per_second=args.q,
                adaptive_q=not args.no_adaptive_q,
            )
            print(f"records_smoothed={changed_records}")
            print(f"already_smoothed={'yes' if smoothed_path == input_path else 'no'}")
            print(f"output={smoothed_path}")
            return 0

        state = StateDB(config.db_path)
        library = LocalActivityLibrary(state, config.data_dir)
        try:
            if args.library_action == "import":
                password_value = os.getenv(args.password_env) if args.password_env else None
                results = library.import_paths(
                    args.paths,
                    recursive=args.recursive,
                    zip_password=password_value.encode("utf-8") if password_value else None,
                )
                for result in results:
                    print(
                        json.dumps(
                            {
                                "id": result.fingerprint[:12],
                                "name": result.name,
                                "sport_type": result.sport_type,
                                "start_time": result.start_time,
                                "format": result.file_format,
                                "duplicate": result.duplicate,
                                "source": result.source_label,
                            },
                            ensure_ascii=False,
                        )
                    )
                print(f"imported={len(results)}")
                return 0

            if args.library_action == "preview":
                password_value = os.getenv(args.password_env) if args.password_env else None
                previews = library.preview_paths(
                    args.paths,
                    recursive=args.recursive,
                    zip_password=password_value.encode("utf-8") if password_value else None,
                )
                for preview in previews:
                    print(
                        json.dumps(
                            {
                                "id": preview.fingerprint[:12],
                                "name": preview.name,
                                "sport_type": preview.sport_type,
                                "start_time": preview.start_time,
                                "format": preview.file_format,
                                "duplicate": preview.duplicate,
                                "source": preview.source_label,
                                "summary": preview.summary,
                            },
                            ensure_ascii=False,
                        )
                    )
                print(f"previewed={len(previews)}")
                return 0

            if args.library_action == "list":
                since = _parse_cli_datetime(args.date_from)
                until = _parse_cli_datetime(args.date_to, inclusive_end=True)
                rows = state.list_local_activities(
                    since=since.isoformat() if since else None,
                    until=until.isoformat() if until else None,
                    sport_type=args.sport,
                    limit=args.limit,
                )
                for row in rows:
                    if args.json:
                        print(
                            json.dumps(
                                {
                                    "id": str(row["fingerprint"])[:12],
                                    "name": row["name"],
                                    "sport_type": row["sport_type"],
                                    "start_time": row["start_time"],
                                    "format": row["file_format"],
                                    "distance_m": json.loads(row["summary_json"]).get("distance_m"),
                                },
                                ensure_ascii=False,
                            )
                        )
                    else:
                        summary = json.loads(row["summary_json"])
                        distance = summary.get("distance_m")
                        distance_text = f"{float(distance) / 1000:.2f} km" if distance is not None else "unknown distance"
                        print(
                            f"{str(row['fingerprint'])[:12]}  {row['start_time'] or 'unknown time'}  "
                            f"{row['sport_type'] or 'unknown'}  {distance_text}  {row['name']}  [{row['file_format']}]"
                        )
                print(f"activities={len(rows)}")
                return 0

            if args.library_action == "show":
                row = library.get_activity(args.activity_id)
                payload = json.loads(row["summary_json"])
                payload.update(
                    {
                        "id": str(row["fingerprint"]),
                        "name": row["name"],
                        "sport_type": row["sport_type"],
                        "start_time": row["start_time"],
                        "format": row["file_format"],
                        "source": row["source_label"],
                    }
                )
                print(json.dumps(payload, ensure_ascii=False, indent=2))
                return 0

            if args.library_action == "stats":
                from .activity_analysis import summarize_rows

                print(json.dumps(summarize_rows(state.list_local_activities()), ensure_ascii=False, indent=2))
                return 0

            if args.library_action == "period":
                date_from = _parse_cli_datetime(args.date_from)
                date_to = _parse_cli_datetime(args.date_to, inclusive_end=True)
                result = calculate_period_summary(
                    state.list_local_activities(),
                    date_from=date_from.date() if date_from else None,
                    date_to=date_to.date() if date_to else None,
                    days=args.days,
                    sport=args.sport,
                    resting_hr=args.resting_hr,
                    threshold_hr=args.threshold_hr,
                )
                print(format_period_summary(result, args.format), end="")
                return 0

            if args.library_action == "balance":
                date_from = _parse_cli_datetime(args.date_from)
                date_to = _parse_cli_datetime(args.date_to, inclusive_end=True)
                result = calculate_training_balance(
                    state.list_local_activities(),
                    resting_hr=args.resting_hr,
                    threshold_hr=args.threshold_hr,
                    date_from=date_from.date() if date_from else None,
                    date_to=date_to.date() if date_to else None,
                )
                print(format_training_balance(result, args.format), end="")
                return 0

            if args.library_action == "vdot":
                date_from = _parse_cli_datetime(args.date_from)
                date_to = _parse_cli_datetime(args.date_to, inclusive_end=True)
                result = analyze_running_activities(
                    state.list_local_activities(),
                    date_from=date_from.date() if date_from else None,
                    date_to=date_to.date() if date_to else None,
                )
                print(format_vdot_report(result, args.format), end="")
                return 0

            if args.library_action == "swim-css":
                result = calculate_swim_css(
                    parse_swim_time(args.time_200m, "200 m time"),
                    parse_swim_time(args.time_400m, "400 m time"),
                    pool_length_m=args.pool_length,
                )
                print(format_swim_css(result, args.format), end="")
                return 0

            if args.library_action == "swim-rest":
                rest_seconds = calculate_swim_rest_seconds(args.distance_m)
                print(format_swim_rest(args.distance_m, rest_seconds, args.format), end="")
                return 0

            if args.library_action == "report":
                rows = state.list_local_activities()
                if args.format == "pdf":
                    output_path = write_activity_report_pdf(rows, args.output)
                    print(f"written={output_path}")
                else:
                    report = format_activity_report(rows, args.format)
                    if args.output:
                        output_path = args.output.expanduser().resolve()
                        output_path.parent.mkdir(parents=True, exist_ok=True)
                        output_path.write_text(report, encoding="utf-8", newline="")
                        print(f"written={output_path}")
                    else:
                        print(report, end="")
                return 0

            if args.library_action == "poster":
                row = library.get_activity(args.activity_id)
                activity = read_activity_file(Path(row["file_path"]))
                result = write_activity_poster(
                    activity,
                    args.output,
                    layout=args.layout,
                    ratio=args.ratio,
                    photo_path=args.photo,
                    title=args.title,
                    user=args.user,
                    watermark=args.watermark,
                    show_track=args.show_track,
                    show_title=args.show_title,
                    show_power_curve=args.power_curve,
                    metric=args.poster_metric,
                    text_color=args.text_color,
                    track_color=args.track_color,
                    accent_color=args.accent_color,
                    font_path=args.font,
                )
                print(f"written={result.output_path}")
                print(f"size={result.width}x{result.height}")
                for warning in result.warnings:
                    print(f"warning={warning}")
                return 0

            if args.library_action == "route":
                row = library.get_activity(args.activity_id)
                result = convert_activity_file(Path(row["file_path"]), args.output, args.to)
                print(f"output={result.output_path}")
                for loss in result.losses:
                    print(f"loss={loss}")
                return 0

            if args.library_action == "map":
                row = library.get_activity(args.activity_id)
                activity = read_activity_file(Path(row["file_path"]))
                result = write_route_map(activity, args.output)
                print(f"output={result.output_path}")
                print(f"track_points={result.point_count}")
                return 0

            if args.library_action == "chart":
                row = library.get_activity(args.activity_id)
                activity = read_activity_file(Path(row["file_path"]))
                result = write_activity_charts(activity, args.output)
                print(f"output={result.output_path}")
                print(f"track_points={result.point_count}")
                print(f"charts={result.chart_count}")
                print(f"x_axis={result.x_axis}")
                return 0
        finally:
            state.close()

    if args.command == "plans":
        if args.plans_action == "list":
            templates = list_training_templates(locale=args.locale, sport_type=args.sport)
            for template in templates:
                plan = template.get("trainingPlan") if isinstance(template.get("trainingPlan"), dict) else {}
                print(
                    json.dumps(
                        {
                            "id": template.get("id") or template.get("_file_id"),
                            "name": plan.get("name") or template.get("name"),
                            "sport_type": plan.get("sportType"),
                            "locale": args.locale,
                            "weeks": _template_week_count(template),
                        },
                        ensure_ascii=False,
                    )
                )
            print(f"templates={len(templates)}")
            return 0

        if args.plans_action == "show":
            template = get_training_template(args.template_id, locale=args.locale)
            print(json.dumps(template, ensure_ascii=False, indent=2))
            return 0

        state = StateDB(config.db_path)
        try:
            if args.plans_action == "install":
                try:
                    start_date = date.fromisoformat(args.start_date)
                except ValueError as exc:
                    raise ValueError("--start-date must use YYYY-MM-DD") from exc
                template = get_training_template(args.template_id, locale=args.locale)
                plan_id, schedule = install_training_plan(
                    state,
                    template,
                    locale=args.locale,
                    start_date=start_date,
                )
                print(f"plan_id={plan_id}")
                print(f"scheduled_days={len(schedule)}")
                print(f"workout_days={sum(item['item_type'] == 'workout' for item in schedule)}")
                return 0

            if args.plans_action == "installed":
                plans = state.list_training_plans()
                for plan in plans:
                    print(
                        json.dumps(
                            {
                                "plan_id": plan["plan_id"],
                                "template_id": plan["template_id"],
                                "name": plan["name"],
                                "sport_type": plan["sport_type"],
                                "locale": plan["locale"],
                                "start_date": plan["start_date"],
                            },
                            ensure_ascii=False,
                        )
                    )
                print(f"plans={len(plans)}")
                return 0

            if args.plans_action == "schedule":
                schedule = list_training_schedule(state, args.plan_id)
                print(json.dumps(schedule, ensure_ascii=False, indent=2))
                return 0

            if args.plans_action == "link-activity":
                result = link_training_activity(
                    state,
                    args.plan_id,
                    args.item_id,
                    args.activity_id,
                )
                print(json.dumps(result, ensure_ascii=False))
                return 0

            if args.plans_action == "unlink-activity":
                result = unlink_training_activity(state, args.plan_id, args.item_id)
                print(json.dumps(result, ensure_ascii=False))
                return 0

            if args.plans_action == "progress":
                progress = summarize_training_plan_progress(state, args.plan_id)
                if args.format == "json":
                    print(json.dumps(progress, ensure_ascii=False, indent=2))
                else:
                    print(format_training_plan_progress(progress))
                return 0

            if args.plans_action == "export":
                output_path = export_training_plan_ics(state, args.plan_id, args.output)
                print(f"output={output_path}")
                return 0
        except ValueError as exc:
            print(f"plans_error={exc}", file=sys.stderr)
            return 2
        finally:
            state.close()

    if args.command == "workouts":
        if args.workouts_action == "generate":
            return _run_ai_workout_generation(args, config)
        if args.workouts_action == "mywhoosh":
            return _run_mywhoosh_workouts(args, config)
        if args.workouts_action == "list":
            templates = list_workout_templates(args.sport, generated_dir=config.data_dir / "generated_workouts")
            for workout in templates:
                print(
                    json.dumps(
                        {
                            "id": workout.template_id,
                            "name": workout.name,
                            "sport_type": workout.sport_type,
                            "duration_s": workout.estimated_duration_s,
                            "distance_m": workout.estimated_distance_m,
                            "steps": len(workout.steps),
                        },
                        ensure_ascii=False,
                    )
                )
            print(f"workouts={len(templates)}")
            return 0
        if args.workouts_action == "show":
            workout = get_workout_template(
                args.workout_id, generated_dir=config.data_dir / "generated_workouts"
            )
            print(
                json.dumps(
                    {
                        "id": workout.template_id,
                        "name": workout.name,
                        "sport_type": workout.sport_type,
                        "duration_s": workout.estimated_duration_s,
                        "distance_m": workout.estimated_distance_m,
                        "steps": workout.steps,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        output_path = export_workout_template(
            args.workout_id,
            args.output,
            generated_dir=config.data_dir / "generated_workouts",
        )
        print(f"output={output_path}")
        return 0

    if args.command == "health":
        if args.health_action == "fetch-readiness":
            return _run_garmin_readiness_fetch(args, config)
        if args.health_action == "fetch-garmin-summary":
            return _run_garmin_user_summary_fetch(args, config)
        if args.health_action == "fetch-garmin-details":
            return _run_garmin_health_detail_fetch(args, config)
        if args.health_action == "fetch-intervals-wellness":
            return _run_intervals_icu_wellness_fetch(args, config)
        if args.health_action == "fetch-fitbit":
            return _run_fitbit_health_fetch(args, config)
        state = StateDB(config.db_path)
        try:
            if args.health_action == "import":
                imported = import_health_csv(state, args.input)
                print(f"observations={imported}")
            elif args.health_action == "import-readiness":
                imported = import_training_readiness_json(state, args.input)
                print(f"records_added={imported}")
            elif args.health_action == "readiness":
                summary = summarize_training_readiness(state, limit=args.limit)
                if args.format == "text":
                    print(format_training_readiness_text(summary))
                else:
                    print(json.dumps(summary, ensure_ascii=False, indent=2))
            elif args.health_action == "summaries":
                summaries = list_garmin_user_summaries(
                    state, args.start_date, args.end_date
                )
                print(json.dumps(summaries, ensure_ascii=False, indent=2))
            elif args.health_action == "details":
                details = list_garmin_health_details(
                    state, args.start_date, args.end_date, args.dataset
                )
                print(json.dumps(details, ensure_ascii=False, indent=2))
            else:
                summary = summarize_health(state)
                if args.format == "text":
                    print(format_health_summary_text(summary))
                else:
                    print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0
        finally:
            state.close()

    if args.command == "ai-settings":
        state = StateDB(config.db_path)
        try:
            if args.ai_settings_action == "show":
                preferences = load_ai_analysis_preferences(state)
            elif args.ai_settings_action == "set":
                preferences = save_ai_analysis_preferences(
                    state,
                    focus=args.focus,
                    detail=args.detail,
                )
            else:
                preferences = reset_ai_analysis_preferences(state)
            print(json.dumps(preferences, ensure_ascii=False, indent=2))
            return 0
        finally:
            state.close()

    if args.command == "ai-profile":
        state = StateDB(config.db_path)
        try:
            if args.ai_profile_action == "show":
                profile = load_ai_athlete_profile(state)
            elif args.ai_profile_action == "set":
                values = {
                    field: getattr(args, field)
                    for field in AI_ATHLETE_PROFILE_FIELDS
                    if getattr(args, field) is not None
                }
                profile = save_ai_athlete_profile(
                    state,
                    values,
                    clear_fields=args.clear_fields or (),
                )
            else:
                profile = reset_ai_athlete_profile(state)
            print(json.dumps(profile, ensure_ascii=False, indent=2))
            return 0
        except ValueError as exc:
            print(f"ai_profile_error={exc}", file=sys.stderr)
            return 2
        finally:
            state.close()

    if args.command == "ai-report-export":
        state = StateDB(config.db_path)
        try:
            activity = state.get_local_activity(args.activity_id)
            if activity is None:
                raise ValueError(f"Local activity was not found: {args.activity_id}")
            analyses = state.list_ai_analysis_results(activity["fingerprint"])
            if not analyses:
                raise ValueError("No saved AI analyses are available for this activity")
            if args.result_id is None:
                analysis = analyses[0]
            else:
                prefix = args.result_id.strip().casefold()
                if not prefix:
                    raise ValueError("AI analysis result ID cannot be empty")
                matches = [
                    result
                    for result in analyses
                    if str(result["id"]).casefold().startswith(prefix)
                ]
                if not matches:
                    raise ValueError(f"Saved AI analysis was not found: {args.result_id}")
                if len(matches) > 1:
                    raise ValueError(f"AI analysis result ID prefix is ambiguous: {args.result_id}")
                analysis = matches[0]

            result_id = str(analysis["id"])
            activity_id = str(activity["fingerprint"])
            model_name = str(analysis["model_name"])
            content = str(analysis["content"])
            created_at = str(analysis["created_at"])
            if args.format == "markdown":
                output_path = write_ai_analysis_markdown(
                    config.data_dir / "ai_analysis",
                    activity_id=activity_id,
                    result_id=result_id,
                    model_name=model_name,
                    content=content,
                    created_at=created_at,
                    output_path=args.output,
                )
            else:
                output_path = args.output or (
                    config.data_dir
                    / "ai_analysis"
                    / safe_filename(activity_id)
                    / f"{safe_filename(result_id)}.pdf"
                )
                output_path = write_ai_analysis_pdf(
                    output_path,
                    activity_id=activity_id,
                    activity_name=str(activity["name"]),
                    result_id=result_id,
                    model_name=model_name,
                    created_at=created_at,
                    content=content,
                )
            print(f"written={output_path}")
            return 0
        finally:
            state.close()

    if args.command == "ai-analysis":
        state = StateDB(config.db_path)
        try:
            row = state.get_local_activity(args.activity_id)
            if row is None:
                raise ValueError(f"Local activity was not found: {args.activity_id}")
            if args.history:
                if args.force_vector_json is not None or args.force_vector_focus != "comprehensive":
                    raise ValueError("Force Vector options cannot be combined with --history")
                results = state.list_ai_analysis_results(row["fingerprint"])
                print(json.dumps([dict(result) for result in results], ensure_ascii=False, indent=2))
                return 0
            preferences = load_ai_analysis_preferences(state)
            focus = args.focus or preferences["focus"]
            detail = args.detail or preferences["detail"]
            summary = json.loads(row["summary_json"])
            summary.update({"name": row["name"], "sport_type": row["sport_type"]})
            summary["start_time"] = row["start_time"] or summary.get("start_time")
            if args.force_vector_json is not None:
                sport_type = str(summary.get("sport_type") or "").casefold()
                cycling_terms = ("cycl", "bike", "ride", "骑行")
                if sport_type and not any(token in sport_type for token in cycling_terms):
                    raise ValueError("Force Vector analysis requires a cycling activity")
                if args.focus is not None and args.focus != "performance":
                    raise ValueError("Use --force-vector-focus instead of --focus with Force Vector JSON")
                snapshot = load_force_vector_snapshot(args.force_vector_json)
                prompt = build_force_vector_analysis_prompt(
                    snapshot,
                    language=args.language,
                    detail=detail,
                    focus=args.force_vector_focus,
                    question=args.question,
                )
            else:
                if args.force_vector_focus != "comprehensive":
                    raise ValueError("--force-vector-focus requires --force-vector-json")
                health_context = summarize_health_for_activity(
                    state, summary.get("start_time"), summary.get("end_time")
                )
                speed_samples = None
                health_before = health_context.get("before_activity")
                threshold_speed = (
                    health_before.get("lactate_threshold_speed_kmh")
                    if isinstance(health_before, dict)
                    else None
                )
                if (
                    isinstance(threshold_speed, dict)
                    and str(threshold_speed.get("unit") or "").strip().casefold()
                    in {"km/h", "kmh", "kph"}
                    and isinstance(threshold_speed.get("value"), (int, float))
                    and not isinstance(threshold_speed.get("value"), bool)
                    and threshold_speed["value"] > 0
                    and isinstance(summary.get("timer_time_s"), (int, float))
                    and not isinstance(summary.get("timer_time_s"), bool)
                    and threshold_speed["value"] > summary["timer_time_s"]
                ):
                    source_path = Path(row["file_path"])
                    if source_path.is_file():
                        source_activity = read_activity_file(source_path)
                        speed_samples = [
                            (point.timestamp, point.speed_mps)
                            for point in source_activity.track_points
                        ]
                prompt = build_ai_analysis_prompt(
                    summary,
                    state.list_local_activities(),
                    args.question,
                    health_context,
                    language=args.language,
                    focus=focus,
                    detail=detail,
                    athlete_profile=load_ai_athlete_profile(state),
                    speed_samples=speed_samples,
                )
            if args.prompt_only:
                print(prompt)
                return 0
            if not config.ai_api_base_url or not config.ai_model:
                raise ValueError("Set AI_API_BASE_URL and AI_MODEL before requesting AI analysis")
            from .activity_analysis import request_ai_analysis

            streamed = False

            def show_delta(content: str) -> None:
                nonlocal streamed
                streamed = True
                print(content, end="", flush=True)

            result = request_ai_analysis(
                base_url=config.ai_api_base_url,
                model=config.ai_model,
                api_key=config.ai_api_key,
                prompt=prompt,
                on_delta=show_delta,
            )
            result_id = state.save_ai_analysis_result(
                activity_id=row["fingerprint"],
                model_name=config.ai_model,
                content=result,
            )
            markdown_path = write_ai_analysis_markdown(
                config.data_dir / "ai_analysis",
                activity_id=row["fingerprint"],
                result_id=result_id,
                model_name=config.ai_model,
                content=result,
            )
            if streamed:
                print()
            else:
                print(result)
            print(f"markdown_saved={markdown_path}")
            return 0
        finally:
            state.close()

    if args.command == "receive":
        if args.max_upload_mb <= 0 or not 0 <= args.port <= 65535:
            raise ValueError("--max-upload-mb must be positive and --port must be between 0 and 65535")
        state = StateDB(config.db_path)
        try:
            library = LocalActivityLibrary(state, config.data_dir)
            serve_transfer(args.host, args.port, library, args.max_upload_mb * 1024 * 1024)
            return 0
        finally:
            state.close()

    raise ValueError(f"Unsupported local command: {args.command}")


def _run_share_command(args: argparse.Namespace) -> int:
    try:
        if args.share_action == "friend-invite":
            invite_url = build_friend_invite_url(args.user_id, name=args.name)
        else:
            invite_url = build_group_invite_url(
                args.group_id,
                group_name=args.group_name,
                inviter_name=args.name,
            )
    except ValueError as exc:
        print(f"share_error={exc}", file=sys.stderr)
        return 2

    print(invite_url)
    return 0


def _run_social_publish_command(args: argparse.Namespace) -> int:
    try:
        activity = read_activity_file(args.input)
        if not activity.name:
            activity.name = args.input.stem
        publish_data = build_activity_publish_data(
            activity,
            activity_id=args.activity_id,
            display_name=args.display_name,
            title=args.title,
            avatar_url=args.avatar_url,
            location_name=args.location_name,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"social_error={exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(json.dumps(publish_data, ensure_ascii=False, indent=2, allow_nan=False))
        return 0

    base_url = args.base_url or os.environ.get("GARSYNC_SOCIAL_BASE_URL")
    if not base_url:
        print("social_error=set GARSYNC_SOCIAL_BASE_URL or pass --base-url", file=sys.stderr)
        return 2
    auth_token = os.environ.get(args.token_env)
    if not auth_token:
        print(
            "social_error=the configured Nakama session token environment variable is missing or empty",
            file=sys.stderr,
        )
        return 2

    try:
        result = SocialFeedClient(base_url, auth_token).publish(publish_data)
    except SocialFeedError as exc:
        print(f"social_error={exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _run_social_feed_command(args: argparse.Namespace) -> int:
    base_url = args.base_url or os.environ.get("GARSYNC_SOCIAL_BASE_URL")
    if not base_url:
        print("social_error=set GARSYNC_SOCIAL_BASE_URL or pass --base-url", file=sys.stderr)
        return 2
    auth_token = os.environ.get(args.token_env)
    if not auth_token:
        print("social_error=the configured Nakama session token environment variable is missing or empty", file=sys.stderr)
        return 2

    try:
        following_ids = args.following_id
        if args.kind == "follow" and following_ids is None:
            nakama_base_url = args.nakama_base_url or os.environ.get("GARSYNC_NAKAMA_BASE_URL")
            if not nakama_base_url:
                print(
                    "social_error=follow feed requires GARSYNC_NAKAMA_BASE_URL or --nakama-base-url",
                    file=sys.stderr,
                )
                return 2
            following_ids = NakamaFriendsClient(
                nakama_base_url,
                auth_token,
            ).list_mutual_friend_ids()

        client = SocialFeedClient(base_url, auth_token)
        payload = client.get_feed(
            args.kind,
            page=args.page,
            limit=args.limit,
            user_id=args.user_id,
            latitude=args.lat,
            longitude=args.lng,
            following_ids=following_ids,
        )
    except (SocialFeedError, NakamaFriendsError) as exc:
        print(f"social_error={exc}", file=sys.stderr)
        return 2

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _run_social_friends_command(args: argparse.Namespace) -> int:
    base_url = args.base_url or os.environ.get("GARSYNC_NAKAMA_BASE_URL")
    if not base_url:
        print("social_error=set GARSYNC_NAKAMA_BASE_URL or pass --base-url", file=sys.stderr)
        return 2
    auth_token = os.environ.get(args.token_env)
    if not auth_token:
        print(
            "social_error=the configured Nakama session token environment variable is missing or empty",
            file=sys.stderr,
        )
        return 2

    try:
        client = NakamaFriendsClient(base_url, auth_token)
        if args.friends_action == "list":
            payload = client.list_friends(
                limit=args.limit,
                cursor=args.cursor,
                state=args.state,
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        if args.friends_action == "add":
            client.add_friend(args.user_id)
        else:
            client.remove_friend(args.user_id)
    except NakamaFriendsError as exc:
        print(f"social_error={exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "operation": args.friends_action,
                "status": "ok",
                "userId": args.user_id.strip(),
            },
            ensure_ascii=False,
        )
    )
    return 0


def _run_social_thumb_command(args: argparse.Namespace) -> int:
    base_url = args.base_url or os.environ.get("GARSYNC_SOCIAL_BASE_URL")
    if not base_url:
        print("social_error=set GARSYNC_SOCIAL_BASE_URL or pass --base-url", file=sys.stderr)
        return 2
    auth_token = os.environ.get(args.token_env)
    if not auth_token:
        print(
            "social_error=the configured Nakama session token environment variable is missing or empty",
            file=sys.stderr,
        )
        return 2

    try:
        client = SocialFeedClient(base_url, auth_token)
        if args.social_action == "thumb":
            client.thumb(args.activity_id)
        else:
            client.unthumb(args.activity_id)
    except SocialFeedError as exc:
        print(f"social_error={exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "operation": args.social_action,
                "status": "ok",
                "activityId": args.activity_id.strip(),
            },
            ensure_ascii=False,
        )
    )
    return 0


def _run_ble_command(args: argparse.Namespace, config: AppConfig) -> int:
    if args.ble_action == "guide":
        print(format_ble_permission_guide(args.locale))
        return 0

    registry = BleDeviceRegistry(config.data_dir / "ble_devices.json")
    try:
        if args.ble_action == "scan":
            results = asyncio.run(scan_ble_devices(args.timeout))
            saved_addresses: set[str] = set()
            if args.save:
                for result in results:
                    registry.add_scan_result(result)
                    saved_addresses.add(result.address)
            for result in results:
                item = asdict(result)
                item["service_uuids"] = list(result.service_uuids)
                item["saved"] = result.address in saved_addresses
                print(json.dumps(item, ensure_ascii=False))
            print(f"devices={len(results)}")
            if args.save:
                print(f"saved={len(saved_addresses)}")
            return 0

        if args.ble_action == "devices":
            devices = registry.list_devices()
            for device in devices:
                print(json.dumps(device, ensure_ascii=False))
            print(f"devices={len(devices)}")
            return 0

        if args.ble_action == "battery":
            battery_level = asyncio.run(read_battery_level(args.address, args.timeout))
            registry.update_battery_level(args.address, battery_level)
            registry.update_last_connected(args.address)
            print(f"address={args.address}")
            print(f"battery_level={battery_level}")
            return 0

        if args.ble_action == "heart-rate":
            output_path = args.output.expanduser().resolve() if args.output else None
            output_stream = None
            writer = None
            if output_path is not None:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_stream = output_path.open("w", encoding="utf-8", newline="")
                writer = csv.DictWriter(
                    output_stream,
                    fieldnames=[
                        "timestamp",
                        "heart_rate_bpm",
                        "sensor_contact",
                        "energy_expended_kj",
                        "rr_intervals_ms",
                    ],
                )
                writer.writeheader()
            sample_count = 0

            async def _record() -> None:
                nonlocal sample_count
                async for sample in stream_heart_rate(args.address, args.duration, args.timeout):
                    row = asdict(sample)
                    row["rr_intervals_ms"] = list(sample.rr_intervals_ms)
                    if writer is not None and output_stream is not None:
                        writer.writerow(row)
                        output_stream.flush()
                    else:
                        print(json.dumps(row, ensure_ascii=False))
                    sample_count += 1

            try:
                asyncio.run(_record())
            finally:
                if output_stream is not None:
                    output_stream.close()
            registry.update_last_connected(args.address)
            print(f"samples={sample_count}")
            if output_path is not None:
                print(f"output={output_path}")
            return 0

        if args.ble_action == "bigrun-ecg":
            validate_bigrun_ecg_options(args.duration, args.timeout)
            output_path = args.output.expanduser().resolve() if args.output else None
            output_stream = None
            if output_path is not None:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_stream = output_path.open("w", encoding="utf-8", newline="")
            frame_count = 0

            async def _record_bigrun_ecg() -> None:
                nonlocal frame_count
                async for frame in stream_bigrun_ecg(
                    args.address,
                    args.duration,
                    args.timeout,
                ):
                    line = json.dumps(asdict(frame), ensure_ascii=False, sort_keys=True)
                    if output_stream is not None:
                        output_stream.write(line + "\n")
                        output_stream.flush()
                    else:
                        print(line)
                    frame_count += 1

            try:
                asyncio.run(_record_bigrun_ecg())
            finally:
                if output_stream is not None:
                    output_stream.close()
            registry.update_last_connected(args.address)
            print(f"frames={frame_count}")
            if output_path is not None:
                print(f"output={output_path}")
            return 0

        if args.ble_action == "bigrun-ecg-mode":
            asyncio.run(set_bigrun_ecg_work_mode(args.address, args.mode, args.timeout))
            registry.update_last_connected(args.address)
            print(f"mode={args.mode}")
            return 0

        if args.ble_action == "bigrun-ecg-analyze":
            input_path = args.input.expanduser().resolve()
            samples, stored_sample_rate = _read_bigrun_ecg_decoded(input_path)
            sample_rate = args.sample_rate if args.sample_rate is not None else stored_sample_rate
            if sample_rate is None:
                raise ValueError("Decoded ECG JSON has no sample_rate_hz; provide --sample-rate")
            metrics = analyze_bigrun_ecg_signal(samples, sample_rate)
            report = json.dumps(asdict(metrics), ensure_ascii=False, allow_nan=False, indent=2)
            if args.output is None:
                print(report)
                return 0

            output_path = args.output.expanduser().resolve()
            if input_path == output_path:
                raise ValueError("ECG analysis output must not overwrite decoded samples")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{output_path.name}.",
                suffix=".tmp",
                dir=output_path.parent,
            )
            temporary_path = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output_stream:
                    output_stream.write(report + "\n")
                    output_stream.flush()
                    os.fsync(output_stream.fileno())
                os.replace(temporary_path, output_path)
            finally:
                if temporary_path.exists():
                    temporary_path.unlink()
            print(f"r_peaks={len(metrics.r_peak_indices)} rr_intervals={len(metrics.rr_intervals_ms)}")
            if metrics.heart_rate_bpm is not None:
                print(f"heart_rate_bpm={metrics.heart_rate_bpm:.2f}")
            if metrics.heart_rate_threshold_status is not None:
                print(f"heart_rate_threshold_status={metrics.heart_rate_threshold_status}")
            print(f"pattern_labels={','.join(metrics.pattern_labels)}")
            print(f"output={output_path}")
            return 0

        if args.ble_action == "bigrun-ecg-decode":
            input_path = args.input.expanduser().resolve()
            output_path = args.output.expanduser().resolve()
            if input_path == output_path:
                raise ValueError("Decoded ECG output must not overwrite the raw capture")

            output_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{output_path.name}.",
                suffix=".tmp",
                dir=output_path.parent,
            )
            temporary_path = Path(temporary_name)
            sample_count = 0
            decoded_frame_count = 0
            ignored_frame_count = 0
            normalizer = EcgSignalNormalizer() if args.normalize else None
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output_stream:
                    output_stream.write(
                        f'{{"sample_rate_hz":{BIGRUN_ECG_SAMPLE_RATE_HZ},"frames":['
                    )
                    for line_number, record, frame_samples in _iter_bigrun_ecg_capture(input_path):
                        if frame_samples is None:
                            ignored_frame_count += 1
                            continue
                        if normalizer is not None:
                            normalizer.observe(frame_samples)
                        if decoded_frame_count:
                            output_stream.write(",")
                        frame = {"samples": frame_samples}
                        timestamp = record.get("timestamp")
                        if timestamp is not None:
                            if not isinstance(timestamp, str):
                                raise ValueError(
                                    f"ECG capture line {line_number} has an invalid timestamp"
                                )
                            frame["timestamp"] = timestamp
                        output_stream.write(json.dumps(frame, separators=(",", ":")))
                        decoded_frame_count += 1
                        sample_count += len(frame_samples)

                    output_stream.write(
                        f'],"sample_count":{sample_count},"decoded_frame_count":{decoded_frame_count},'
                        f'"ignored_frame_count":{ignored_frame_count}'
                    )
                    if normalizer is not None:
                        output_stream.write(',"normalized_samples":[')
                        normalized_count = 0
                        for _, _, frame_samples in _iter_bigrun_ecg_capture(input_path):
                            if frame_samples is None:
                                continue
                            for sample in frame_samples:
                                if normalized_count:
                                    output_stream.write(",")
                                output_stream.write(
                                    json.dumps(normalizer.normalize_value(sample), allow_nan=False)
                                )
                                normalized_count += 1
                        if normalized_count != sample_count:
                            raise ValueError("ECG capture changed while normalized samples were being written")
                        output_stream.write("]")
                    output_stream.write("}\n")
                    output_stream.flush()
                    os.fsync(output_stream.fileno())
                os.replace(temporary_path, output_path)
            finally:
                if temporary_path.exists():
                    temporary_path.unlink()

            print(f"frames={decoded_frame_count} samples={sample_count} sample_rate_hz={BIGRUN_ECG_SAMPLE_RATE_HZ}")
            if normalizer is not None:
                print(f"normalized_samples={sample_count}")
            print(f"output={output_path}")
            return 0

        if args.ble_action == "record":
            validate_sensor_recording_options(
                args.duration,
                args.timeout,
                args.wheel_circumference_m,
            )
            output_path = args.output.expanduser().resolve() if args.output else None
            output_stream = None
            if output_path is not None:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_stream = output_path.open("w", encoding="utf-8", newline="")
            sample_count = 0

            async def _record_sensor_data() -> None:
                nonlocal sample_count
                async for sample in stream_sensor_data(
                    args.address,
                    args.duration,
                    args.timeout,
                    wheel_circumference_m=args.wheel_circumference_m,
                ):
                    row = asdict(sample)
                    line = json.dumps(row, ensure_ascii=False, sort_keys=True)
                    if output_stream is not None:
                        output_stream.write(line + "\n")
                        output_stream.flush()
                    else:
                        print(line)
                    sample_count += 1

            try:
                asyncio.run(_record_sensor_data())
            finally:
                if output_stream is not None:
                    output_stream.close()
            registry.update_last_connected(args.address)
            print(f"samples={sample_count}")
            if output_path is not None:
                print(f"output={output_path}")
            return 0

        if args.ble_action == "trainer":
            if args.trainer_action == "set-power":
                asyncio.run(set_trainer_target_power(args.address, args.watts, args.timeout))
                print(f"address={args.address}")
                print(f"target_power_w={args.watts}")
                return 0
            if args.trainer_action == "set-resistance":
                asyncio.run(set_trainer_resistance_mode(args.address, args.timeout))
                print(f"address={args.address}")
                print("target_resistance_level=0.0")
                return 0

            course = (
                load_ride_course(args.course_file, ftp_watts=args.ftp)
                if args.course_file is not None
                else demo_ride_course()
            )
            intensity = intensity_multiplier_from_percent(args.intensity_percent)
            if args.trainer_action == "preview":
                _print_trainer_course_preview(course, intensity)
                return 0

            last_reported_segment: int | None = None
            last_reported_target: int | None = None

            def report_target(elapsed_s: float, segment_index: int, target_w: int) -> None:
                nonlocal last_reported_segment, last_reported_target
                if (
                    last_reported_segment == segment_index
                    and last_reported_target is not None
                    and abs(target_w - last_reported_target) < 5
                ):
                    return
                print(
                    f"elapsed_s={elapsed_s:g} segment={segment_index + 1}/{len(course.segments)} "
                    f"target_power_w={target_w}"
                )
                last_reported_segment = segment_index
                last_reported_target = target_w

            ride_started_at: datetime | None = None
            ride_started_monotonic: float | None = None
            timer_samples: list[TrackPoint] = []
            telemetry_samples: list[TrackPoint] = []

            def sample_timestamp() -> datetime:
                nonlocal ride_started_at, ride_started_monotonic
                if ride_started_at is None:
                    ride_started_at = datetime.now(timezone.utc)
                    ride_started_monotonic = time.monotonic()
                assert ride_started_monotonic is not None
                elapsed = max(0.0, time.monotonic() - ride_started_monotonic)
                return ride_started_at + timedelta(seconds=elapsed)

            def record_timer_sample(_elapsed_s: float) -> None:
                timer_samples.append(TrackPoint(timestamp=sample_timestamp()))

            def record_trainer_measurement(measurement: dict[str, object]) -> None:
                sample = TrackPoint(
                    timestamp=sample_timestamp(),
                    distance_m=measurement.get("distance_m"),
                    speed_mps=measurement.get("speed_mps"),
                    heart_rate_bpm=measurement.get("heart_rate_bpm"),
                    cadence_rpm=measurement.get("cadence_rpm"),
                    power_w=measurement.get("power_w"),
                )
                if any(
                    value is not None
                    for value in (
                        sample.distance_m,
                        sample.speed_mps,
                        sample.heart_rate_bpm,
                        sample.cadence_rpm,
                        sample.power_w,
                    )
                ):
                    telemetry_samples.append(sample)

            def report_pause(paused: bool) -> None:
                print(f"ride_paused={str(paused).lower()}")

            def report_erg_mode(erg_enabled: bool) -> None:
                mode = "erg" if erg_enabled else "resistance"
                print(f"trainer_mode={mode}")

            interactive = sys.stdin.isatty()
            if interactive:
                print(
                    "controls: + increase, - decrease intensity, s skip interval, p pause/resume, e toggle ERG/resistance",
                    file=sys.stderr,
                )

            ride_result = asyncio.run(
                run_trainer_course(
                    args.address,
                    course,
                    intensity=intensity,
                    timeout=args.timeout,
                    on_target=report_target,
                    on_tick=record_timer_sample,
                    on_measurement=record_trainer_measurement,
                    on_control=_read_trainer_control if interactive else None,
                    on_pause=report_pause,
                    on_erg_mode=report_erg_mode,
                )
            )
            if ride_started_at is None:
                raise RideCourseError("Ride finished before activity recording started")
            output_path = args.output or (
                config.data_dir
                / "virtual_rides"
                / f"ride-{ride_started_at:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}.fit"
            )
            written_path = write_ride_activity_fit(
                course.name,
                ride_started_at,
                ride_result.elapsed_time_s,
                telemetry_samples or timer_samples,
                output_path,
                timer_time_s=ride_result.timer_time_s,
            )
            registry.update_last_connected(args.address)
            print(f"address={args.address}")
            print(f"course={course.name!r}")
            print(
                f"ride_complete=true elapsed_s={ride_result.elapsed_time_s:g} "
                f"timer_s={ride_result.timer_time_s:g}"
            )
            print(f"fit_output={written_path}")
            return 0

        if args.ble_action == "rename":
            registry.rename_device(args.address, args.name)
            print(f"renamed={args.address}")
            return 0

        if args.ble_action == "remove":
            registry.remove_device(args.address)
            print(f"removed={args.address}")
            return 0

        registry.set_preferred_device(args.address, args.type)
        print(f"preferred_{args.type}={args.address}")
        return 0
    except (BleError, RideCourseError) as exc:
        print(f"ble_error={exc}", file=sys.stderr)
        return 2


def _print_trainer_course_preview(course: RideCourse, intensity: float) -> None:
    print(f"course={course.name!r}")
    print(f"duration_s={course.duration_s:g}")
    print(f"intensity_percent={intensity * 100:g}")
    for warning in course.warnings:
        print(f"course_warning={warning}", file=sys.stderr)
    for index, segment in enumerate(course.segments, 1):
        start_w = segment.target_power_at(0, intensity)
        end_w = segment.target_power_at(segment.duration_s, intensity)
        print(
            f"segment={index} start_s={segment.start_time_s:g} end_s={segment.end_time_s:g} "
            f"start_power_w={start_w} end_power_w={end_w} label={segment.label!r}"
        )


def _read_trainer_control() -> str | None:
    if os.name == "nt":
        import msvcrt

        if not msvcrt.kbhit():
            return None
        key = msvcrt.getwch()
        if key in {"\x00", "\xe0"}:
            msvcrt.getwch()
            return None
    else:
        import select

        ready, _, _ = select.select([sys.stdin], [], [], 0)
        if not ready:
            return None
        key = sys.stdin.readline().strip()

    return _trainer_control_from_key(key)


def _trainer_control_from_key(key: str) -> str | None:
    if not isinstance(key, str):
        return None
    return {
        "+": "increase",
        "-": "decrease",
        "s": "skip",
        "p": "pause",
        "e": "toggle_erg",
    }.get(key.casefold())


def _run_samba_command(args: argparse.Namespace, config: AppConfig) -> int:
    password_value = os.getenv(args.password_env) if args.password_env else None
    try:
        if args.samba_action == "list":
            entries = list_samba_directory(
                args.url,
                username=args.username,
                password=password_value,
                timeout=args.timeout,
                legacy_smb=args.legacy_smb,
                server_name=args.server_name,
            )
            for entry in entries:
                print(
                    json.dumps(
                        {
                            "name": entry.name,
                            "url": entry.url,
                            "type": "directory" if entry.is_directory else "file",
                            "size_bytes": entry.size_bytes,
                            "supported_activity": entry.supported_activity,
                        },
                        ensure_ascii=False,
                    )
                )
            print(f"entries={len(entries)}")
            return 0

        state = StateDB(config.db_path)
        try:
            archive_password_value = (
                os.getenv(args.archive_password_env) if args.archive_password_env else None
            )
            results = import_samba_activity(
                LocalActivityLibrary(state, config.data_dir),
                args.url,
                username=args.username,
                password=password_value,
                archive_password=archive_password_value.encode("utf-8") if archive_password_value else None,
                timeout=args.timeout,
                legacy_smb=args.legacy_smb,
                server_name=args.server_name,
            )
        finally:
            state.close()
        for result in results:
            print(
                json.dumps(
                    {
                        "id": result.fingerprint[:12],
                        "name": result.name,
                        "sport_type": result.sport_type,
                        "start_time": result.start_time,
                        "format": result.file_format,
                        "duplicate": result.duplicate,
                        "source": result.source_label,
                    },
                    ensure_ascii=False,
                )
            )
        print(f"imported={len(results)}")
        return 0
    except ValueError as exc:
        print(f"samba_error={exc}", file=sys.stderr)
        return 2


def _run_ai_workout_generation(args: argparse.Namespace, config: AppConfig) -> int:
    system_prompt, user_prompt = build_ai_single_workout_prompts(
        sport=args.sport,
        task=args.task,
        target_mode=args.target_mode.replace("-", "_"),
        target_duration=args.target_duration,
        target_distance=args.target_distance,
        target_pace=args.target_pace,
        target_heart_rate=args.target_heart_rate,
        target_tss=args.target_tss,
        athlete_context=args.athlete_context,
        feedback=args.feedback,
        language=args.language,
    )
    if args.prompt_only:
        print(f"SYSTEM PROMPT\n{system_prompt}\n\nUSER PROMPT\n{user_prompt}")
        return 0
    if not config.ai_api_base_url or not config.ai_model:
        raise ValueError("Set AI_API_BASE_URL and AI_MODEL before requesting an AI workout")

    from .activity_analysis import request_ai_analysis

    response = request_ai_analysis(
        base_url=config.ai_api_base_url,
        model=config.ai_model,
        api_key=config.ai_api_key,
        prompt=user_prompt,
        timeout_seconds=180,
        system_prompt=system_prompt,
        temperature=0.4,
    )
    workout = normalize_ai_workout(response, sport=args.sport)
    output_path = args.output.expanduser().resolve() if args.output is not None else make_workout_output_path(
        workout, config.data_dir / "generated_workouts"
    )
    written = write_ai_workout_fit(workout, output_path)
    print(
        json.dumps(
            {
                "workout_id": workout["workoutId"],
                "name": workout["name"],
                "sport": workout["sportType"],
                "fit_file": str(written),
                "metadata_file": f"{written}.meta",
                "estimated_duration_s": workout["estimatedDuration"],
                "estimated_distance_m": workout["estimatedDistance"],
            },
            ensure_ascii=False,
        )
    )
    return 0


def _run_mywhoosh_workouts(args: argparse.Namespace, config: AppConfig) -> int:
    state = StateDB(config.db_path)
    try:
        source = MyWhooshSource(config, state)
        if args.mywhoosh_workout_action == "list":
            workouts = source.list_workouts()
            for workout in workouts:
                print(json.dumps(remote_workout_summary(workout), ensure_ascii=False))
            print(f"workouts={len(workouts)}")
            return 0
        if args.mywhoosh_workout_action == "delete":
            status_code = source.delete_workout(args.workout_id)
            print(json.dumps({"status": "deleted", "workout_id": args.workout_id, "http_status": status_code}))
            return 0

        template = get_workout_template(
            args.template_id,
            generated_dir=config.data_dir / "generated_workouts",
        )
        result = upload_mywhoosh_workout(source, template)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"mywhoosh_workout_error={exc}", file=sys.stderr)
        return 2
    finally:
        state.close()


def _template_week_count(template: dict[str, object]) -> int:
    weeks: set[int] = set()
    week_templates = template.get("weekTemplates")
    if isinstance(week_templates, list):
        for week in week_templates:
            if isinstance(week, dict) and isinstance(week.get("applyToWeeks"), list):
                weeks.update(value for value in week["applyToWeeks"] if isinstance(value, int))
    return max(weeks, default=0)
