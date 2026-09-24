from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from .activity_analysis import (
    build_ai_analysis_prompt,
    format_activity_report,
    validate_ai_language_code,
    write_activity_report_pdf,
    write_ai_analysis_markdown,
)
from .activity_poster import POSTER_LAYOUTS, POSTER_METRICS, POSTER_RATIOS, write_activity_poster
from .activity_library import LocalActivityLibrary
from .activity_merge import merge_fit_files
from .config import AppConfig
from .engine import SyncEngine
from .fit_tools import normalize_fit_coordinates
from .formats import SUPPORTED_FORMATS, convert_activity_file, read_activity_file
from .health import import_health_csv, summarize_health, summarize_health_for_activity
from .period_summary import calculate_period_summary, format_period_summary
from .state import StateDB
from .training_balance import calculate_training_balance, format_training_balance
from .vdot import analyze_running_activities, format_vdot_report
from .training import (
    export_training_plan_ics,
    export_workout_template,
    get_training_template,
    get_workout_template,
    install_training_plan,
    list_training_templates,
    list_workout_templates,
)
from .utils import configure_logging, ensure_directory, pack_directory_to_base64_zip, parse_datetime
from .wifi_transfer import serve_transfer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import, analyze, convert, and sync sports activity files."
    )
    subparsers = parser.add_subparsers(dest="command", required=False)

    sync_parser = subparsers.add_parser("sync", help="Run a sync pass")
    sync_parser.add_argument("--source", action="append", choices=["igpsport", "onelap", "local"], help="Repeatable source")
    sync_parser.add_argument("--target", action="append", choices=["garmin", "strava"], help="Repeatable target")
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
    library_import = library_actions.add_parser("import", help="Import FIT, GPX, TCX, JSON, CSV, or ZIP files")
    library_import.add_argument("paths", nargs="+", type=Path, help="Files or directories to import")
    library_import.add_argument("--recursive", action="store_true", help="Scan directories recursively")
    library_import.add_argument(
        "--password-env",
        default="ACTIVITY_ARCHIVE_PASSWORD",
        help="Environment variable for encrypted ZIP passwords (default: ACTIVITY_ARCHIVE_PASSWORD)",
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
    library_merge = library_actions.add_parser("merge", help="Merge FIT activities into one FIT file")
    library_merge.add_argument("paths", nargs="+", type=Path, help="FIT files in the desired activity order")
    library_merge.add_argument("--output", type=Path, required=True, help="Merged FIT output path")
    library_merge.add_argument("--name", help="Merged activity label for the command result")

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
    plans_export = plans_actions.add_parser("export", help="Export an installed plan as iCalendar")
    plans_export.add_argument("plan_id")
    plans_export.add_argument("--output", type=Path, required=True)

    workouts_parser = subparsers.add_parser("workouts", help="Browse and export bundled FIT workout templates")
    workouts_actions = workouts_parser.add_subparsers(dest="workouts_action", required=True)
    workouts_list = workouts_actions.add_parser("list", help="List FIT workout templates")
    workouts_list.add_argument("--sport", help="Filter by sport type")
    workouts_show = workouts_actions.add_parser("show", help="Show a workout template")
    workouts_show.add_argument("workout_id")
    workouts_export = workouts_actions.add_parser("export", help="Copy a workout FIT template")
    workouts_export.add_argument("workout_id")
    workouts_export.add_argument("--output", type=Path, required=True)

    health_parser = subparsers.add_parser("health", help="Import and summarize local health measurements")
    health_actions = health_parser.add_subparsers(dest="health_action", required=True)
    health_import = health_actions.add_parser("import", help="Import a UTF-8 health CSV")
    health_import.add_argument("input", type=Path)
    health_actions.add_parser("summary", help="Show latest health measurements")

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
        choices=["performance", "health", "recovery"],
        default="performance",
        help="Analysis focus (default: performance)",
    )
    ai_parser.add_argument(
        "--detail",
        choices=["brief", "normal", "detailed"],
        default="normal",
        help="Response detail level (default: normal)",
    )
    ai_parser.add_argument("--prompt-only", action="store_true", help="Print the analysis prompt without sending data")
    ai_parser.add_argument("--history", action="store_true", help="Show saved analyses without contacting the AI service")

    receive_parser = subparsers.add_parser("receive", help="Start a local Wi-Fi file import page")
    receive_parser.add_argument("--host", default="127.0.0.1", help="Bind address; use 0.0.0.0 for LAN access")
    receive_parser.add_argument("--port", type=int, default=8765)
    receive_parser.add_argument("--max-upload-mb", type=int, default=64)

    status_parser = subparsers.add_parser("status", help="Show local SQLite status")
    status_parser.add_argument("--json", action="store_true", help="Reserved for future use")

    check_parser = subparsers.add_parser("check", help="Verify configured source/target logins")
    check_parser.add_argument("--source", action="append", choices=["igpsport", "onelap", "local"], help="Repeatable source")
    check_parser.add_argument("--target", action="append", choices=["garmin", "strava"], help="Repeatable target")

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

    config = AppConfig.load(root_dir)
    ensure_directory(config.data_dir)
    configure_logging(config.log_level, config.log_path)

    if args.command == "convert":
        return _convert_file(args, config)
    if args.command in {"library", "plans", "workouts", "health", "ai-analysis", "receive"}:
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
    if not separator or target not in {"garmin", "strava"}:
        raise argparse.ArgumentTypeError("format must use TARGET=FORMAT with target garmin or strava")
    if activity_format not in SUPPORTED_FORMATS:
        supported = ", ".join(sorted(SUPPORTED_FORMATS))
        raise argparse.ArgumentTypeError(f"format must be one of: {supported}")
    return target, activity_format


def _target_format_map(values: list[tuple[str, str]] | None) -> dict[str, str]:
    formats: dict[str, str] = {}
    for target, activity_format in values or []:
        if target in formats:
            raise ValueError(f"Target format was specified more than once: {target}")
        formats[target] = activity_format
    return formats


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


def _run_local_command(args: argparse.Namespace, config: AppConfig) -> int:
    if args.command == "library":
        if args.library_action == "merge":
            result = merge_fit_files(args.paths, args.output, name=args.name)
            print(f"merged={result.input_count}")
            print(f"records={result.records_after}/{result.records_before}")
            print(f"decimated={'yes' if result.decimated else 'no'}")
            print(f"output={result.output_path}")
            for loss in result.losses:
                print(f"loss={loss}")
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

            if args.plans_action == "export":
                output_path = export_training_plan_ics(state, args.plan_id, args.output)
                print(f"output={output_path}")
                return 0
        finally:
            state.close()

    if args.command == "workouts":
        if args.workouts_action == "list":
            templates = list_workout_templates(args.sport)
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
            workout = get_workout_template(args.workout_id)
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
        output_path = export_workout_template(args.workout_id, args.output)
        print(f"output={output_path}")
        return 0

    if args.command == "health":
        state = StateDB(config.db_path)
        try:
            if args.health_action == "import":
                imported = import_health_csv(state, args.input)
                print(f"observations={imported}")
            else:
                print(json.dumps(summarize_health(state), ensure_ascii=False, indent=2))
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
                results = state.list_ai_analysis_results(row["fingerprint"])
                print(json.dumps([dict(result) for result in results], ensure_ascii=False, indent=2))
                return 0
            summary = json.loads(row["summary_json"])
            summary.update({"name": row["name"], "sport_type": row["sport_type"]})
            summary["start_time"] = row["start_time"] or summary.get("start_time")
            prompt = build_ai_analysis_prompt(
                summary,
                state.list_local_activities(),
                args.question,
                summarize_health_for_activity(state, summary.get("start_time"), summary.get("end_time")),
                language=args.language,
                focus=args.focus,
                detail=args.detail,
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


def _template_week_count(template: dict[str, object]) -> int:
    weeks: set[int] = set()
    week_templates = template.get("weekTemplates")
    if isinstance(week_templates, list):
        for week in week_templates:
            if isinstance(week, dict) and isinstance(week.get("applyToWeeks"), list):
                weeks.update(value for value in week["applyToWeeks"] if isinstance(value, int))
    return max(weeks, default=0)
