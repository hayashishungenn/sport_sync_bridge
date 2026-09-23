from __future__ import annotations

import argparse
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .config import AppConfig
from .engine import SyncEngine
from .fit_tools import normalize_fit_coordinates
from .formats import SUPPORTED_FORMATS, convert_activity_file
from .utils import configure_logging, ensure_directory, pack_directory_to_base64_zip, parse_datetime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sync FIT activities from iGPSPORT / OneLap to Garmin Connect global and Strava."
    )
    subparsers = parser.add_subparsers(dest="command", required=False)

    sync_parser = subparsers.add_parser("sync", help="Run a sync pass")
    sync_parser.add_argument("--source", action="append", choices=["igpsport", "onelap"], help="Repeatable source")
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

    status_parser = subparsers.add_parser("status", help="Show local SQLite status")
    status_parser.add_argument("--json", action="store_true", help="Reserved for future use")

    check_parser = subparsers.add_parser("check", help="Verify configured source/target logins")
    check_parser.add_argument("--source", action="append", choices=["igpsport", "onelap"], help="Repeatable source")
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
    try:
        target_formats = _target_format_map(getattr(args, "target_formats", None))
    except ValueError as exc:
        parser.error(str(exc))

    config = AppConfig.load(root_dir)
    ensure_directory(config.data_dir)
    configure_logging(config.log_level, config.log_path)

    if args.command == "convert":
        return _convert_file(args, config)

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
        return parsed.replace(hour=23, minute=59, second=59)
    return parsed.astimezone(timezone.utc)


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
